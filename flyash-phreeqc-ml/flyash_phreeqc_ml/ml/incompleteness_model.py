"""GP **model-incompleteness** estimator — where PHREEQC's mechanism is systematically short.

After Prompts 22–25 exist and batch data accumulates, this learns the part of the
measured closure gap that PHREEQC **could not attribute** (the *unexplained residual*,
Prompt 24/25) as a function of batch conditions. The output is framed strictly as a
**predicted systematic shortfall of the PHREEQC attribution under these conditions** —
never a measured element amount, never fed back into closure arithmetic.

It deliberately mirrors :mod:`residual_model` (the GP-residual-correction model) and
reuses its machinery rather than reinventing it:

* **Feature encoding** is :mod:`residual_model`'s exactly (profile
  ``feature_numeric_fields`` + ``feature_categorical_fields``, median-imputed numerics +
  one-hot categoricals) — imported, not re-implemented.
* **Gate.** Training *refuses* (a typed :class:`IncompletenessGateError` carrying the
  counts) unless the element has ≥:data:`GATE_MIN_ROWS` **well-determined** rows across
  ≥:data:`GATE_MIN_CONDITIONS` conditions. "Well-determined" = the closure gap's σ is
  small relative to the gap (:data:`GAP_SIGMA_REL_TOL`) **and** the recovery status is
  trustworthy (a complete closure) **and** the starting amount is a *measured* assay
  (never a literature stand-in — see the constraint below). The UI shows progress, not a
  premature train button.
* **Validation is leave-one-condition-out** (not random k-fold) against the
  :mod:`residual_stats`-style constant-bias baseline (the held-out-fold training mean).
  If the GP does not beat that baseline, the app says so and recommends the Prompt-13
  bias bands.
* **Noise guard.** If the unexplained residual's scatter is consistent with the stated
  measurement uncertainty (reduced χ² ≤ :data:`NOISE_CHI2_MAX`), training *refuses* with
  :class:`NoLearnablePatternError` — *"consistent with measurement noise; no learnable
  pattern"* — rather than fitting noise.

Constraints honoured here:

* Trains on **measured closure gaps + modeled attributions only** — rows whose starting
  assay is a confirmed *or* proposed literature stand-in are excluded (the gate filters
  on ``starting_provenance == "measured"``); a literature value can never become a
  training target.
* No predicted value ever enters a measured/derived closure column — predictions live in
  ``predicted_shortfall`` fields only.

Requires scikit-learn (lazy/optional, via :mod:`residual_model`). Trained models + cards
are gitignored run outputs.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .. import attribution, mass_balance, profiles, replicates
from . import residual_model as rm

# Reuse the residual_model machinery verbatim (encoding / GP / LOCO / card helpers).
from .residual_model import (  # noqa: F401  (re-exported for callers + tests)
    Z95,
    beats_baseline,
    _feature_frame,
    _fit_gp,
    _gp_predict,
    _library_versions,
    _loco,
    _matrix,
    _medians,
    _resolve_feature_fields,
    _training_hash,
    _training_run_names,
)

# --- Gate ---------------------------------------------------------------------
GATE_MIN_ROWS = 30          # mirrors residual_model.GATE_MIN_EXACT_PAIRS
GATE_MIN_CONDITIONS = 3
# A gap is "well-determined" when its σ is at most this fraction of |gap|. Rows whose
# σ is unknown or comparable to the gap (e.g. "closed" gaps within noise) are excluded.
GAP_SIGMA_REL_TOL = 0.5
# Reduced χ² of the target around its mean at/below which the scatter is consistent with
# measurement noise (no systematic pattern to learn).
NOISE_CHI2_MAX = 1.5

# Complete-closure attribution statuses (Prompt 24). "incomplete" closures never produce
# one of these with a finite unexplained residual, so requiring a finite target already
# drops them; this set keeps the intent explicit.
TRUSTWORTHY_STATUSES = (attribution.STATUS_CLOSED, attribution.STATUS_MODEL_EXPLAINED,
                        attribution.STATUS_PARTIAL, attribution.STATUS_UNEXPLAINED)

MEASURED_PROVENANCE = "measured"   # report.CLASS_MEASURED (kept local to avoid the import)

NON_CLAIM_LINE = (
    "Predicted systematic shortfall of the PHREEQC attribution under these conditions — "
    "an ML estimate of where the mechanistic model is incomplete, not a measured element "
    "amount. It never enters closure arithmetic, mapping, or validity status."
)


# --------------------------------------------------------------------------- #
# Recovery-dataset column convention (one row per measured sample; element-suffixed)
# --------------------------------------------------------------------------- #
def target_column(element: str) -> str:
    """The training target column: PHREEQC's *unexplained* closure gap for ``element`` (mmol)."""
    return f"unexplained_{element}"


def gap_column(element: str) -> str:
    return f"gap_{element}"


def gap_sigma_column(element: str) -> str:
    return f"gap_sigma_{element}"


def status_column(element: str) -> str:
    return f"recovery_status_{element}"


def provenance_column(element: str) -> str:
    return f"starting_provenance_{element}"


def element_unit(element: str) -> str:
    return "pH units" if element == "pH" else "mmol"


# --------------------------------------------------------------------------- #
# Build the per-row recovery dataset (measured closure + attribution + provenance)
# --------------------------------------------------------------------------- #
def build_recovery_dataset(data: pd.DataFrame, profile=None, *, selected_outputs=None,
                           sigmas_by_row=None, run_name=None) -> pd.DataFrame:
    """One row per measured sample with element-suffixed recovery terms + features.

    For each element the frame carries the unexplained residual (target), the measured
    closure gap + σ, the recovery status, and the starting-amount provenance, alongside
    the raw condition columns (the feature source) and ``condition_key``. ``selected_outputs``
    maps a ``condition_key`` to a parsed PHREEQC selected output (so attribution by phase is
    real); without it attribution is *unavailable* and the whole gap is unexplained — the
    honest offline state. Returns an empty frame when the profile declares no mass balance.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    if not mass_balance.is_enabled(profile) or data is None or data.empty:
        return pd.DataFrame()
    elements = list(getattr(profile, "mass_balance_elements", ()) or ())
    selected_outputs = selected_outputs or {}
    sigmas_by_row = sigmas_by_row or {}

    confirmed, overrides = [], {}
    if run_name:
        try:
            from ..ai import literature
            confirmed = literature.confirmed_records(run_name)
            overrides = literature.confirmed_assay_overrides(confirmed, profile)
        except Exception:
            confirmed, overrides = [], {}

    ann = replicates.annotate(data, profile)
    rows_out: list[dict] = []
    for _, r in ann.iterrows():
        base = r.to_dict()
        sid = str(base.get("sample_id", "")).strip()
        ck = str(base.get(replicates.CONDITION_KEY_COLUMN, ""))
        # Fill ONLY confirmed literature assays into blank starting-content cells.
        if confirmed:
            from ..ai import literature
            new_row, badges = literature.row_with_confirmed_assays(base, confirmed, profile)
        else:
            new_row, badges = base, {}
        sel = selected_outputs.get(ck)
        rec = dict(base)  # keep all original columns (feature source + condition_key)
        for el in elements:
            closure = mass_balance.closure(new_row, el, profile=profile,
                                           sigmas=sigmas_by_row.get(sid))
            attr = (attribution.attribute_gap(new_row, el, sel, profile=profile)
                    if sel is not None
                    else attribution.attribution_unavailable(new_row, el, profile=profile))
            scol = f"{el}_starting_content"
            if _present(base.get(scol)):
                prov = MEASURED_PROVENANCE
            elif scol in badges:
                prov = "literature-confirmed"
            else:
                prov = "missing"
            rec[target_column(el)] = attr.get("gap_unexplained")
            rec[gap_column(el)] = closure["gap"]
            rec[gap_sigma_column(el)] = closure["gap_sigma"]
            rec[status_column(el)] = attr["status"]
            rec[provenance_column(el)] = prov
        rows_out.append(rec)
    return pd.DataFrame(rows_out)


def _present(value) -> bool:
    if value in (None, ""):
        return False
    try:
        return not pd.isna(value)
    except (TypeError, ValueError):  # pragma: no cover
        return True


# --------------------------------------------------------------------------- #
# Eligible-row extraction (well-determined gap + trustworthy status + measured assay)
# --------------------------------------------------------------------------- #
def _extract_well_determined(recovery_df: pd.DataFrame, element: str, profile) -> pd.DataFrame:
    """Rows whose unexplained residual is a trainable, *measured* target for ``element``.

    Keeps rows where the target + gap + σ are finite, σ ≤ :data:`GAP_SIGMA_REL_TOL`·|gap|,
    the recovery status is a complete-closure status, and the starting amount is a
    *measured* assay (literature stand-ins excluded). Adds ``__residual`` (the target) and
    ``__gap_sigma``; guarantees a ``condition_key`` column (so :func:`_loco` can reuse it).
    """
    tcol = target_column(element)
    empty = pd.DataFrame()
    if recovery_df is None or recovery_df.empty or tcol not in recovery_df.columns:
        return empty
    df = recovery_df.copy()
    if replicates.CONDITION_KEY_COLUMN not in df.columns:
        df = replicates.annotate(df, profile)

    target = pd.to_numeric(df[tcol], errors="coerce")
    gap = pd.to_numeric(df.get(gap_column(element)), errors="coerce")
    sigma = pd.to_numeric(df.get(gap_sigma_column(element)), errors="coerce")
    mask = (target.notna() & gap.notna() & sigma.notna() & (sigma > 0)
            & (sigma <= GAP_SIGMA_REL_TOL * gap.abs()))

    scol = status_column(element)
    if scol in df.columns:
        mask &= df[scol].astype(str).isin(TRUSTWORTHY_STATUSES)
    pcol = provenance_column(element)
    if pcol in df.columns:                       # measured closure gaps only
        mask &= df[pcol].astype(str) == MEASURED_PROVENANCE

    ex = df[mask.values].copy()
    if ex.empty:
        return empty
    ex["__residual"] = target[mask.values].to_numpy(dtype=float)
    ex["__gap_sigma"] = sigma[mask.values].to_numpy(dtype=float)
    return ex.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IncompletenessGateStatus:
    """How close one element is to the training gate (well-determined rows + conditions)."""

    element: str
    n_rows: int
    n_conditions: int
    min_rows: int = GATE_MIN_ROWS
    min_conditions: int = GATE_MIN_CONDITIONS

    @property
    def meets(self) -> bool:
        return self.n_rows >= self.min_rows and self.n_conditions >= self.min_conditions

    def progress_message(self) -> str:
        return (f"{self.n_rows} of {self.min_rows} well-determined rows; "
                f"{self.n_conditions} of {self.min_conditions} conditions")


class IncompletenessGateError(Exception):
    """Raised when training is attempted below the data-sufficiency gate."""

    def __init__(self, gate: IncompletenessGateStatus):
        self.gate = gate
        super().__init__(
            f"model-incompleteness model for {gate.element!r} refused: "
            f"{gate.progress_message()} "
            f"(need ≥{gate.min_rows} well-determined rows across "
            f"≥{gate.min_conditions} conditions).")


class NoLearnablePatternError(Exception):
    """Raised when the unexplained residual is consistent with measurement noise.

    Carries the signal assessment so the app can report *"consistent with measurement
    noise; no learnable pattern"* rather than fitting noise.
    """

    def __init__(self, element: str, assessment: dict):
        self.element = element
        self.assessment = assessment
        super().__init__(
            f"model-incompleteness model for {element!r}: unexplained residual is "
            f"consistent with measurement noise (reduced χ² "
            f"{assessment.get('chi2_reduced'):.3g} ≤ {NOISE_CHI2_MAX}); no learnable "
            f"pattern — stay with the descriptive bias bands.")


def gate_status(recovery_df: pd.DataFrame, element: str, *, profile=None,
                min_rows: int = GATE_MIN_ROWS,
                min_conditions: int = GATE_MIN_CONDITIONS) -> IncompletenessGateStatus:
    """Count well-determined rows + distinct conditions for ``element`` against the gate."""
    profile = profile or profiles.FLY_ASH_PROFILE
    ex = _extract_well_determined(recovery_df, element, profile)
    n_rows = int(len(ex))
    n_conditions = int(ex[replicates.CONDITION_KEY_COLUMN].nunique()) if n_rows else 0
    return IncompletenessGateStatus(element, n_rows, n_conditions, min_rows, min_conditions)


# --------------------------------------------------------------------------- #
# Noise assessment (don't fit noise)
# --------------------------------------------------------------------------- #
def assess_signal(ex: pd.DataFrame) -> dict:
    """Reduced χ² of the target around its mean vs the stated per-row measurement σ.

    χ²_red ≤ :data:`NOISE_CHI2_MAX` → the scatter is consistent with measurement noise
    (no systematic pattern beyond what σ explains). > that → excess (learnable) variance.
    """
    n = int(len(ex))
    if n < 2 or "__gap_sigma" not in ex.columns:
        return {"chi2_reduced": None, "noise_dominated": False, "n": n,
                "threshold": NOISE_CHI2_MAX, "status": "unknown (no σ)"}
    target = ex["__residual"].to_numpy(dtype=float)
    sigma = ex["__gap_sigma"].to_numpy(dtype=float)
    valid = sigma > 0
    if valid.sum() < 2:
        return {"chi2_reduced": None, "noise_dominated": False, "n": n,
                "threshold": NOISE_CHI2_MAX, "status": "unknown (no σ)"}
    resid = target - float(target.mean())
    chi2_red = float(np.mean((resid[valid] / sigma[valid]) ** 2))
    return {"chi2_reduced": chi2_red, "noise_dominated": bool(chi2_red <= NOISE_CHI2_MAX),
            "n": n, "threshold": NOISE_CHI2_MAX, "status": "assessed"}


# --------------------------------------------------------------------------- #
# LOCO vs constant-bias baseline (reuse residual_model._loco verbatim)
# --------------------------------------------------------------------------- #
def loco_cross_validate(recovery_df: pd.DataFrame, element: str, *, profile=None) -> dict | None:
    """Leave-one-condition-out GP RMSE vs the constant-bias baseline for ``element``."""
    profile = profile or profiles.FLY_ASH_PROFILE
    numeric_cols, categorical_cols = _resolve_feature_fields(profile)
    ex = _extract_well_determined(recovery_df, element, profile)
    if ex.empty:
        return None
    return _loco(ex, numeric_cols, categorical_cols)


def use_model_recommended(loco: dict | None) -> bool:
    """Recommend the incompleteness model **only** when LOCO beats the constant bias.

    When False (no LOCO or it does not beat the baseline) the honest recommendation is to
    stay with the Prompt-13 descriptive bias bands.
    """
    return bool(loco) and bool(loco.get("beats_baseline"))


# --------------------------------------------------------------------------- #
# Model container + training
# --------------------------------------------------------------------------- #
@dataclass
class IncompletenessModel:
    """A fitted per-element shortfall GP + the metadata needed to encode/predict."""

    element: str
    estimator: object
    numeric_cols: list
    categorical_cols: list
    categories: dict
    numeric_medians: dict
    input_ranges: dict
    unit: str
    card: dict = field(default_factory=dict)


def train_element_model(recovery_df: pd.DataFrame, element: str, *, profile=None,
                        run_name=None, min_rows: int = GATE_MIN_ROWS,
                        min_conditions: int = GATE_MIN_CONDITIONS,
                        date: str | None = None) -> IncompletenessModel:
    """Fit the per-element shortfall GP — **raises below the gate, or if noise-dominated**.

    Raises :class:`IncompletenessGateError` (carrying the counts) below the sufficiency
    gate, or :class:`NoLearnablePatternError` when the unexplained residual is consistent
    with measurement noise. On success attaches a model card (gate values, signal
    assessment, LOCO-vs-baseline, training run names + hash, library versions, date).
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    ex = _extract_well_determined(recovery_df, element, profile)
    n_rows = int(len(ex))
    n_conditions = int(ex[replicates.CONDITION_KEY_COLUMN].nunique()) if n_rows else 0
    gate = IncompletenessGateStatus(element, n_rows, n_conditions, min_rows, min_conditions)
    if not gate.meets:
        raise IncompletenessGateError(gate)

    signal = assess_signal(ex)
    if signal.get("noise_dominated"):
        raise NoLearnablePatternError(element, signal)

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

    loco = _loco(ex, numeric_cols, categorical_cols)
    card = {
        "element": element,
        "kind": "model_incompleteness_gp",
        "target": f"unexplained PHREEQC attribution residual ({element}, mmol)",
        "feature_numeric": list(numeric_cols),
        "feature_categorical": list(categorical_cols),
        "categories": categories,
        "numeric_medians": medians,
        "input_ranges": {c: list(rng) for c, rng in input_ranges.items()},
        "gate": {
            "min_rows": min_rows, "min_conditions": min_conditions,
            "n_rows": n_rows, "n_conditions": n_conditions,
            "gap_sigma_rel_tol": GAP_SIGMA_REL_TOL,
        },
        "signal": signal,
        "loco": loco if loco is not None else {
            "status": "insufficient conditions for LOCO (need ≥2 evaluable)"},
        "training_run_names": _training_run_names(ex, run_name),
        "training_set_hash": _training_hash(ex, numeric_cols, categorical_cols),
        "n_samples": n_rows,
        "library_versions": _library_versions(),
        "date": date or _dt.date.today().isoformat(),
        "non_claim": NON_CLAIM_LINE,
    }
    return IncompletenessModel(
        element=element, estimator=est, numeric_cols=list(numeric_cols),
        categorical_cols=list(categorical_cols), categories=categories,
        numeric_medians=medians, input_ranges=input_ranges,
        unit=element_unit(element), card=card)


# --------------------------------------------------------------------------- #
# Prediction (predicted shortfall; never a measured amount)
# --------------------------------------------------------------------------- #
def predict_shortfall(model: IncompletenessModel, rows: pd.DataFrame) -> pd.DataFrame:
    """Predicted systematic shortfall (mean + 95% interval) for ``rows``.

    The values are an ML estimate of the PHREEQC attribution's shortfall under each row's
    conditions — explicitly **not** a measured element amount. Columns:
    ``predicted_shortfall`` / ``shortfall_lower`` / ``shortfall_upper`` (+ ``sample_id`` /
    ``condition_key`` when present).
    """
    feat = _feature_frame(rows, model.numeric_cols, model.categorical_cols)
    Xfeat = _matrix(feat, model.numeric_cols, model.categorical_cols,
                    model.categories, model.numeric_medians)
    mean, std = _gp_predict(model.estimator, Xfeat)
    out = pd.DataFrame({
        "predicted_shortfall": mean,
        "shortfall_lower": mean - Z95 * std,
        "shortfall_upper": mean + Z95 * std,
    })
    for c in ("sample_id", replicates.CONDITION_KEY_COLUMN):
        if c in rows.columns:
            out.insert(0, c, rows[c].astype(str).values)
    return out


# --------------------------------------------------------------------------- #
# Use (a): active-learning hook — flag strongly under-attributed conditions
# --------------------------------------------------------------------------- #
def flag_underattributed_conditions(model: IncompletenessModel, rows: pd.DataFrame, *,
                                    threshold: float, use_lower_bound: bool = False
                                    ) -> pd.DataFrame:
    """Conditions the model predicts to be strongly under-attributed (candidates for
    new experiments / better phase lists). Sorted by predicted shortfall, descending.

    ``threshold`` is in the target unit (mmol). With ``use_lower_bound`` the *lower* 95%
    bound must clear the threshold (a conservative flag); otherwise the mean must.
    """
    pred = predict_shortfall(model, rows)
    decide = pred["shortfall_lower"] if use_lower_bound else pred["predicted_shortfall"]
    pred = pred.copy()
    pred["underattributed"] = (decide >= float(threshold)).to_numpy()
    pred["threshold"] = float(threshold)
    return pred.sort_values("predicted_shortfall", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Use (b): off-by-default overlay for the recovery report (clearly ml-predicted)
# --------------------------------------------------------------------------- #
def incompleteness_overlay(model: IncompletenessModel, rows: pd.DataFrame) -> pd.DataFrame:
    """An ml-predicted shortfall overlay for the recovery report (off by default in UI).

    Same numbers as :func:`predict_shortfall`, plus a constant ``estimate_kind`` column so
    every rendering reads as *ML-predicted, with uncertainty* — never a measured value.
    """
    out = predict_shortfall(model, rows)
    out["estimate_kind"] = "ml-predicted shortfall (experimental)"
    return out


# --------------------------------------------------------------------------- #
# Persistence (mirror residual_model)
# --------------------------------------------------------------------------- #
def save_incompleteness_model(model: IncompletenessModel, directory) -> Path:
    """Persist ``<element>.joblib`` + ``<element>.model_card.json`` to a directory."""
    import joblib
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    safe = str(model.element).replace("/", "_")
    joblib.dump(model, directory / f"{safe}.joblib")
    (directory / f"{safe}.model_card.json").write_text(
        json.dumps(model.card, indent=2), encoding="utf-8")
    return directory


def load_incompleteness_models(directory) -> dict:
    """Load all persisted incompleteness models (``{element: model}``)."""
    import joblib
    directory = Path(directory)
    models: dict = {}
    if not directory.exists():
        return models
    for path in sorted(directory.glob("*.joblib")):
        model = joblib.load(path)
        models[model.element] = model
    return models
