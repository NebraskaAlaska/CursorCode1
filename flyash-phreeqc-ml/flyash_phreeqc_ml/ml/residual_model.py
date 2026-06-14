"""GP **residual-correction** model — hard-gated, raw-vs-corrected, never a replacement.

This is the learned successor to the descriptive bias bands (:mod:`residual_stats`).
For one element it fits a Gaussian-process regressor whose **target is the residual**
``measured − model`` and whose **features are condition metadata** (leachant family,
molarity, L/S, time, and the CO₂/cover code one-hot), encoded through the dataset
profile. A *corrected* prediction is then ``corrected = PHREEQC + predicted_residual``,
carried with the GP interval.

Three hard rules keep it honest:

1. **Data-sufficiency gate.** Training *refuses* (a typed
   :class:`ResidualModelGateError` carrying the counts) unless the element has at
   least :data:`GATE_MIN_EXACT_PAIRS` exact-mapped pairs spanning at least
   :data:`GATE_MIN_CONDITIONS` distinct ``condition_key`` values. The UI shows
   progress toward the gate instead of a train button until it is met. (Exactness is
   taken from the same inclusion status join as Prompt 13 — this never weakens that
   gate; it only adds a stricter one on top.)
2. **Leave-one-condition-out validation.** Generalising to an *unseen condition* is
   the failure mode that matters, so validation is LOCO, not random k-fold. The
   per-element LOCO RMSE is reported **against the Prompt-13 constant-bias baseline**;
   if the GP does not beat the constant bias, that is stated prominently and the
   recommendation is to stay with the bias bands.
3. **Corrected values are display-only.** ``corrected`` is shown solely as a
   clearly-labelled *"Corrected (experimental)"* overlay that always draws raw PHREEQC,
   the correction, and the interval **together** — never the corrected value alone.
   Corrected values never feed mapping status, validity status, or the comparison
   CSV's residual columns.

Requires scikit-learn (lazy/optional, like the surrogate). Trained models + cards are
gitignored run outputs.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import scipy
import sklearn
from sklearn.exceptions import ConvergenceWarning
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .. import profiles, replicates, scenarios
from . import residual_stats

# Data-sufficiency gate (the whole point: do not pretend to learn from too little).
GATE_MIN_EXACT_PAIRS = 30
GATE_MIN_CONDITIONS = 3

Z95 = 1.959963984540054  # 97.5th percentile of the standard normal

NON_CLAIM_LINE = (
    "Experimental residual correction. It is shown only as a raw-vs-corrected overlay "
    "and never replaces PHREEQC output, mapping status, validity status, or residual columns."
)

_ACID_TOKENS = ("hcl", "hno3", "h2so4", "acid")


# --------------------------------------------------------------------------- #
# Element column resolution
# --------------------------------------------------------------------------- #
def element_columns(element: str) -> dict:
    """Residual / model-prediction / measured column names + unit for one element."""
    if element == "pH":
        return {"residual": "residual_pH", "phreeqc": "phreeqc_pH",
                "measured": "final_pH", "unit": "pH units"}
    return {"residual": f"residual_{element}", "phreeqc": f"phreeqc_{element}_mM",
            "measured": f"{element}_mM", "unit": "mM"}


# --------------------------------------------------------------------------- #
# Feature encoding (profile-driven)
# --------------------------------------------------------------------------- #
def _resolve_feature_fields(profile) -> tuple[list[str], list[str]]:
    numeric = list(getattr(profile, "feature_numeric_fields", ()) or ())
    categorical = list(getattr(profile, "feature_categorical_fields", ()) or ())
    return numeric, categorical


def _leachant_family(value) -> str:
    s = str(value or "").strip().lower()
    if not s or s == "nan":
        return "unknown"
    if any(tok in s for tok in _ACID_TOKENS):
        return "acid"
    if "naoh" in s or "koh" in s or "base" in s or "alkali" in s:
        return "base"
    return s


def _categorical_value(row: dict, name: str) -> str:
    """Derive a categorical feature value (some names are computed via scenarios)."""
    if name == "leachant_family":
        return _leachant_family(row.get("leachant"))
    if name == "condition_code":
        return scenarios.sample_condition_code(row) or "unknown"
    v = row.get(name)
    if v is None:
        return "unknown"
    s = str(v).strip()
    return s if s and s.lower() != "nan" else "unknown"


def _feature_frame(rows: pd.DataFrame, numeric_cols, categorical_cols) -> pd.DataFrame:
    """Raw (un-encoded) feature frame: numeric columns coerced, categoricals derived."""
    recs = rows.to_dict("records")
    data: dict[str, list] = {}
    for c in numeric_cols:
        data[c] = [pd.to_numeric(pd.Series([r.get(c)]), errors="coerce").iloc[0] for r in recs]
    for c in categorical_cols:
        data[c] = [_categorical_value(r, c) for r in recs]
    return pd.DataFrame(data, index=range(len(recs)))


def _medians(feat: pd.DataFrame, numeric_cols) -> dict:
    out = {}
    for c in numeric_cols:
        col = pd.to_numeric(feat[c], errors="coerce")
        out[c] = float(col.median()) if col.notna().any() else 0.0
    return out


def _matrix(feat: pd.DataFrame, numeric_cols, categorical_cols, categories, medians) -> np.ndarray:
    """Encode a raw feature frame: numeric (median-imputed) + one-hot categoricals."""
    parts = []
    if numeric_cols:
        num = pd.DataFrame({c: pd.to_numeric(feat[c], errors="coerce") for c in numeric_cols})
        for c in numeric_cols:
            num[c] = num[c].fillna(medians.get(c, 0.0))
        parts.append(num.to_numpy(dtype=float))
    for c in categorical_cols:
        cats = categories[c]
        oh = np.array([[1.0 if str(v) == cat else 0.0 for cat in cats] for v in feat[c]],
                      dtype=float)
        parts.append(oh.reshape(len(feat), len(cats)))
    return np.hstack(parts) if parts else np.zeros((len(feat), 0))


def _gp_optimizer():
    """The GP hyperparameter optimizer, or ``None`` to skip it (fast/deterministic mode).

    Fast mode (env ``FLYASH_GP_FAST=1``) fixes the kernel hyperparameters at their
    initial values, skipping the expensive L-BFGS-B optimization — used by tests that
    exercise the LOCO / corrected-overlay logic but **not** GP fit quality. Production
    keeps the default optimizer.
    """
    return (None if os.environ.get("FLYASH_GP_FAST", "").lower() in ("1", "true", "yes")
            else "fmin_l_bfgs_b")


def _make_gp() -> Pipeline:
    kernel = (ConstantKernel(1.0, (1e-3, 1e3))
              * Matern(length_scale=1.0, length_scale_bounds=(1e-2, 1e3), nu=2.5)
              + WhiteKernel(noise_level=1.0, noise_level_bounds=(1e-8, 1e2)))
    gp = GaussianProcessRegressor(kernel=kernel, normalize_y=True, alpha=1e-10,
                                  optimizer=_gp_optimizer(),
                                  n_restarts_optimizer=0, random_state=0)
    return Pipeline([("scale", StandardScaler()), ("gp", gp)])


def _fit_gp(Xfeat: np.ndarray, y: np.ndarray) -> Pipeline:
    est = _make_gp()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=ConvergenceWarning)
        est.fit(Xfeat, y)
    return est


def _gp_predict(est: Pipeline, Xfeat: np.ndarray):
    scaler, gp = est.named_steps["scale"], est.named_steps["gp"]
    mean, std = gp.predict(scaler.transform(Xfeat), return_std=True)
    return mean, std


# --------------------------------------------------------------------------- #
# Exact-pair extraction (shares the Prompt-13 exact filter — never weakened)
# --------------------------------------------------------------------------- #
def _extract_exact(comparison_df: pd.DataFrame, statuses, element: str, profile) -> pd.DataFrame:
    """Exact, non-synthetic rows carrying a finite residual for ``element``.

    Adds ``__residual`` and guarantees a ``condition_key`` column. Reuses
    :func:`residual_stats.exact_mask`, so the exactness/synthetic rules are identical
    to Prompt 13 — this layer only *adds* the sufficiency gate.
    """
    cols = element_columns(element)
    empty = pd.DataFrame()
    if (comparison_df is None or comparison_df.empty
            or "sample_id" not in comparison_df.columns
            or cols["residual"] not in comparison_df.columns):
        return empty
    df = comparison_df.copy()
    if replicates.CONDITION_KEY_COLUMN not in df.columns:
        df = replicates.annotate(df, profile)
    ex = df[residual_stats.exact_mask(df, statuses).values].copy()
    if ex.empty:
        return empty
    ex["__residual"] = pd.to_numeric(ex[cols["residual"]], errors="coerce")
    ex = ex.dropna(subset=["__residual"]).reset_index(drop=True)
    return ex


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ResidualGateStatus:
    """How close one element is to the training gate (counts + thresholds)."""

    element: str
    n_exact_pairs: int
    n_conditions: int
    min_pairs: int = GATE_MIN_EXACT_PAIRS
    min_conditions: int = GATE_MIN_CONDITIONS

    @property
    def meets(self) -> bool:
        return (self.n_exact_pairs >= self.min_pairs
                and self.n_conditions >= self.min_conditions)

    def progress_message(self) -> str:
        return (f"{self.n_exact_pairs} of {self.min_pairs} exact pairs; "
                f"{self.n_conditions} of {self.min_conditions} conditions")


class ResidualModelGateError(Exception):
    """Raised when training is attempted below the data-sufficiency gate."""

    def __init__(self, gate: ResidualGateStatus):
        self.gate = gate
        super().__init__(
            f"residual-correction model for {gate.element!r} refused: "
            f"{gate.progress_message()} "
            f"(need ≥{gate.min_pairs} exact pairs across ≥{gate.min_conditions} conditions)."
        )


def gate_status(comparison_df: pd.DataFrame, statuses, element: str, *, profile=None,
                min_pairs: int = GATE_MIN_EXACT_PAIRS,
                min_conditions: int = GATE_MIN_CONDITIONS) -> ResidualGateStatus:
    """Count exact pairs + distinct conditions for ``element`` against the gate."""
    profile = profile or profiles.FLY_ASH_PROFILE
    ex = _extract_exact(comparison_df, statuses, element, profile)
    n_pairs = int(len(ex))
    n_conditions = int(ex[replicates.CONDITION_KEY_COLUMN].nunique()) if n_pairs else 0
    return ResidualGateStatus(element, n_pairs, n_conditions, min_pairs, min_conditions)


# --------------------------------------------------------------------------- #
# LOCO cross-validation vs the constant-bias baseline
# --------------------------------------------------------------------------- #
def beats_baseline(model_rmse, baseline_rmse) -> bool:
    """True only when the model's LOCO RMSE is strictly below the constant-bias one."""
    if model_rmse is None or baseline_rmse is None:
        return False
    try:
        m, b = float(model_rmse), float(baseline_rmse)
    except (TypeError, ValueError):
        return False
    if np.isnan(m) or np.isnan(b):
        return False
    return m < b


def _loco(ex: pd.DataFrame, numeric_cols, categorical_cols) -> dict | None:
    """Leave-one-condition-out: GP RMSE vs constant-bias (training-mean) RMSE.

    For each held-out ``condition_key`` the GP and the constant bias are both fit on
    the *other* conditions and scored on the held-out one — so both are judged on
    generalisation to an unseen condition. Returns None if fewer than two conditions
    can be evaluated.
    """
    conditions = sorted(ex[replicates.CONDITION_KEY_COLUMN].astype(str).unique())
    if len(conditions) < 2:
        return None
    model_sq: list[float] = []
    base_sq: list[float] = []
    folds = 0
    for held in conditions:
        ck = ex[replicates.CONDITION_KEY_COLUMN].astype(str)
        train = ex[ck != held]
        test = ex[ck == held]
        if len(train) < 2 or test.empty:
            continue
        base_pred = float(train["__residual"].mean())  # Prompt-13 constant bias
        feat_tr = _feature_frame(train, numeric_cols, categorical_cols)
        feat_te = _feature_frame(test, numeric_cols, categorical_cols)
        medians = _medians(feat_tr, numeric_cols)
        categories = {c: sorted({str(v) for v in feat_tr[c]}) for c in categorical_cols}
        Xtr = _matrix(feat_tr, numeric_cols, categorical_cols, categories, medians)
        Xte = _matrix(feat_te, numeric_cols, categorical_cols, categories, medians)
        ytr = train["__residual"].to_numpy(dtype=float)
        try:
            est = _fit_gp(Xtr, ytr)
            pred, _ = _gp_predict(est, Xte)
        except Exception:  # pragma: no cover - degenerate fold
            continue
        actual = test["__residual"].to_numpy(dtype=float)
        model_sq.extend(((actual - pred) ** 2).tolist())
        base_sq.extend(((actual - base_pred) ** 2).tolist())
        folds += 1
    if folds < 2 or not model_sq:
        return None
    model_rmse = float(np.sqrt(np.mean(model_sq)))
    base_rmse = float(np.sqrt(np.mean(base_sq)))
    return {
        "model_loco_rmse": model_rmse,
        "baseline_loco_rmse": base_rmse,
        "beats_baseline": beats_baseline(model_rmse, base_rmse),
        "n_folds": folds,
        "n_evaluated": len(model_sq),
    }


def loco_cross_validate(comparison_df: pd.DataFrame, statuses, element: str, *,
                        profile=None) -> dict | None:
    """Public LOCO entry point: extract exact pairs, then run :func:`_loco`."""
    profile = profile or profiles.FLY_ASH_PROFILE
    numeric_cols, categorical_cols = _resolve_feature_fields(profile)
    ex = _extract_exact(comparison_df, statuses, element, profile)
    if ex.empty:
        return None
    return _loco(ex, numeric_cols, categorical_cols)


def use_correction_recommended(loco: dict | None) -> bool:
    """Recommend the correction overlay **only** when LOCO shows it beats the bias.

    When this is False (no LOCO, or the GP did not beat the constant-bias baseline),
    the honest recommendation is to stay with the Prompt-13 bias bands.
    """
    return bool(loco) and bool(loco.get("beats_baseline"))


# --------------------------------------------------------------------------- #
# Model container + training
# --------------------------------------------------------------------------- #
@dataclass
class ResidualCorrectionModel:
    """A fitted per-element residual GP + the metadata needed to encode/predict."""

    element: str
    estimator: object
    numeric_cols: list
    categorical_cols: list
    categories: dict
    numeric_medians: dict
    input_ranges: dict
    phreeqc_col: str
    unit: str
    card: dict = field(default_factory=dict)


def _library_versions() -> dict:
    return {"scikit-learn": sklearn.__version__, "scipy": scipy.__version__,
            "numpy": np.__version__, "joblib": joblib.__version__}


def _training_run_names(ex: pd.DataFrame, run_name) -> list[str]:
    if "run_name" in ex.columns:
        names = sorted({str(v).strip() for v in ex["run_name"] if str(v).strip()})
        if names:
            return names
    return [str(run_name) if run_name else "unknown"]


def _training_hash(ex: pd.DataFrame, numeric_cols, categorical_cols) -> str:
    keep = [c for c in (list(numeric_cols) + list(categorical_cols)
                        + [replicates.CONDITION_KEY_COLUMN, "__residual"]) if c in ex.columns]
    blob = ex[keep].to_csv(index=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def train_element_model(comparison_df: pd.DataFrame, statuses, element: str, *,
                        profile=None, run_name=None,
                        min_pairs: int = GATE_MIN_EXACT_PAIRS,
                        min_conditions: int = GATE_MIN_CONDITIONS,
                        date: str | None = None) -> ResidualCorrectionModel:
    """Fit the GP residual model for ``element`` — **raises below the gate**.

    Raises :class:`ResidualModelGateError` (carrying the counts) unless the element
    has ≥``min_pairs`` exact pairs across ≥``min_conditions`` conditions. On success,
    attaches a model card with the gate values, LOCO-vs-baseline results, training
    run names + hash, library versions and date.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    ex = _extract_exact(comparison_df, statuses, element, profile)
    n_pairs = int(len(ex))
    n_conditions = int(ex[replicates.CONDITION_KEY_COLUMN].nunique()) if n_pairs else 0
    gate = ResidualGateStatus(element, n_pairs, n_conditions, min_pairs, min_conditions)
    if not gate.meets:
        raise ResidualModelGateError(gate)

    numeric_cols, categorical_cols = _resolve_feature_fields(profile)
    feat = _feature_frame(ex, numeric_cols, categorical_cols)
    medians = _medians(feat, numeric_cols)
    categories = {c: sorted({str(v) for v in feat[c]}) for c in categorical_cols}
    Xfeat = _matrix(feat, numeric_cols, categorical_cols, categories, medians)
    y = ex["__residual"].to_numpy(dtype=float)
    est = _fit_gp(Xfeat, y)

    input_ranges = {}
    for c in numeric_cols:
        col = pd.to_numeric(feat[c], errors="coerce").fillna(medians.get(c, 0.0))
        input_ranges[c] = (float(col.min()), float(col.max()))

    cols = element_columns(element)
    loco = _loco(ex, numeric_cols, categorical_cols)
    card = {
        "element": element,
        "kind": "residual_correction_gp",
        "feature_numeric": list(numeric_cols),
        "feature_categorical": list(categorical_cols),
        "categories": categories,
        "numeric_medians": medians,
        "input_ranges": {c: list(rng) for c, rng in input_ranges.items()},
        "gate": {
            "min_exact_pairs": min_pairs, "min_conditions": min_conditions,
            "n_exact_pairs": n_pairs, "n_conditions": n_conditions,
        },
        "loco": loco if loco is not None else {
            "status": "insufficient conditions for LOCO (need ≥2 evaluable)"},
        "training_run_names": _training_run_names(ex, run_name),
        "training_set_hash": _training_hash(ex, numeric_cols, categorical_cols),
        "n_samples": n_pairs,
        "library_versions": _library_versions(),
        "date": date or _dt.date.today().isoformat(),
        "non_claim": NON_CLAIM_LINE,
    }
    return ResidualCorrectionModel(
        element=element, estimator=est, numeric_cols=list(numeric_cols),
        categorical_cols=list(categorical_cols), categories=categories,
        numeric_medians=medians, input_ranges=input_ranges,
        phreeqc_col=cols["phreeqc"], unit=cols["unit"], card=card)


# --------------------------------------------------------------------------- #
# Prediction + corrected overlay (display-only; never the corrected value alone)
# --------------------------------------------------------------------------- #
def predict_residual(model: ResidualCorrectionModel, rows: pd.DataFrame) -> pd.DataFrame:
    """Predicted residual (mean + 95% interval) for ``rows`` (need feature columns)."""
    feat = _feature_frame(rows, model.numeric_cols, model.categorical_cols)
    Xfeat = _matrix(feat, model.numeric_cols, model.categorical_cols,
                    model.categories, model.numeric_medians)
    mean, std = _gp_predict(model.estimator, Xfeat)
    return pd.DataFrame({
        "predicted_residual": mean,
        "residual_lower": mean - Z95 * std,
        "residual_upper": mean + Z95 * std,
    })


def corrected_overlay(model: ResidualCorrectionModel, rows: pd.DataFrame, *,
                      phreeqc_col: str | None = None) -> pd.DataFrame:
    """Raw-vs-corrected overlay: PHREEQC, predicted residual, corrected + interval.

    Always returns the raw PHREEQC value alongside the corrected one (and its
    interval), so the corrected number is never presented on its own. This is a
    display artifact only — it must not be written into the comparison CSV's
    residual columns or used for mapping/validity status.
    """
    pcol = phreeqc_col or model.phreeqc_col
    ph = (pd.to_numeric(rows[pcol], errors="coerce").to_numpy(dtype=float)
          if pcol in rows.columns else np.full(len(rows), np.nan))
    resid = predict_residual(model, rows)
    pr = resid["predicted_residual"].to_numpy(dtype=float)
    lo = resid["residual_lower"].to_numpy(dtype=float)
    hi = resid["residual_upper"].to_numpy(dtype=float)
    out = pd.DataFrame({
        "phreeqc": ph,
        "predicted_residual": pr,
        "corrected": ph + pr,
        "corrected_lower": ph + lo,
        "corrected_upper": ph + hi,
    })
    if "sample_id" in rows.columns:
        out.insert(0, "sample_id", rows["sample_id"].astype(str).values)
    return out


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def save_residual_model(model: ResidualCorrectionModel, directory) -> Path:
    """Persist ``<element>.joblib`` + ``<element>.model_card.json`` to a directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    safe = str(model.element).replace("/", "_")
    joblib.dump(model, directory / f"{safe}.joblib")
    (directory / f"{safe}.model_card.json").write_text(
        json.dumps(model.card, indent=2), encoding="utf-8")
    return directory


def load_residual_models(directory) -> dict:
    """Load all persisted residual-correction models (``{element: model}``)."""
    directory = Path(directory)
    models: dict = {}
    if not directory.exists():
        return models
    for path in sorted(directory.glob("*.joblib")):
        model = joblib.load(path)
        models[model.element] = model
    return models
