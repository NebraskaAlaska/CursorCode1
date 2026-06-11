"""PHREEQC surrogate: one model per output variable, with uncertainty + honest validation.

A surrogate is a fast statistical approximation of PHREEQC trained on a sampled
input→output dataset (see :mod:`sampling` + ``scripts/10``). For each output
variable we fit one model that also produces a **95% prediction interval**:

* Default — :class:`~sklearn.gaussian_process.GaussianProcessRegressor` on
  standardized inputs with a Matérn kernel + white-noise term (interval from the
  predictive std).
* Fallback — when the training set is large (``> config.SURROGATE_GP_MAX_SAMPLES``)
  or GP fitting fails, :class:`~sklearn.ensemble.HistGradientBoostingRegressor` with
  three quantile models (2.5 / 50 / 97.5%) for the interval.

Honesty is built in:

* every model records a **model card** (training-set hash, n, input ranges = the
  validity domain, k-fold CV RMSE/MAE, library versions, date);
* :func:`validate_surrogate` reports per-output **RMSE, MAE, and 95%-interval
  coverage** on a held-out split (plus k-fold CV);
* :func:`predict` flags any row whose inputs fall **outside the trained ranges** as
  ``domain="extrapolation"`` — a surrogate must not be trusted off its training box.

The surrogate is an *approximation of PHREEQC*, never a measurement and never a
PHREEQC run; nothing here is wired into comparison/residual/mapping paths.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import scipy
import sklearn
from sklearn.exceptions import ConvergenceWarning
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.model_selection import KFold, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .. import config

METHOD_GP = "gp"
METHOD_HGBR = "hgbr"
DOMAIN_INTERIOR = "interior"
DOMAIN_EXTRAPOLATION = "extrapolation"
Z95 = 1.959963984540054  # 97.5th percentile of the standard normal


# --------------------------------------------------------------------------- #
# Model container
# --------------------------------------------------------------------------- #
@dataclass
class SurrogateModel:
    """A fitted surrogate for one output variable (+ its validity domain + card)."""

    output: str
    method: str
    estimator: object                      # GP pipeline, or the median HGBR
    lower_estimator: object | None         # HGBR 2.5% quantile (None for GP)
    upper_estimator: object | None         # HGBR 97.5% quantile (None for GP)
    input_cols: list
    categorical_cols: list
    categories: dict                       # categorical col -> sorted category list
    input_ranges: dict                     # continuous col -> (min, max) = validity domain
    card: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Feature assembly
# --------------------------------------------------------------------------- #
def _continuous(input_cols, categorical_cols):
    return [c for c in input_cols if c not in categorical_cols]


def _feature_matrix(X: pd.DataFrame, input_cols, categorical_cols, categories) -> np.ndarray:
    cont = _continuous(input_cols, categorical_cols)
    parts = [X[cont].astype(float).to_numpy()] if cont else []
    for c in categorical_cols:
        cats = categories[c]
        oh = np.array([[1.0 if str(v) == cat else 0.0 for cat in cats] for v in X[c]],
                      dtype=float)
        parts.append(oh.reshape(len(X), len(cats)))
    return np.hstack(parts) if parts else np.zeros((len(X), 0))


def _assemble(dataset: pd.DataFrame, output: str, input_cols, categorical_cols):
    """Rows usable for ``output``: status ok (if present) and a finite y + finite inputs."""
    df = dataset
    if "status" in df.columns:
        df = df[df["status"].astype(str).str.lower() == "ok"]
    cont = _continuous(input_cols, categorical_cols)
    keep = df[output].apply(lambda v: pd.notna(pd.to_numeric(v, errors="coerce")))
    for c in cont:
        keep &= df[c].apply(lambda v: pd.notna(pd.to_numeric(v, errors="coerce")))
    df = df[keep]
    y = pd.to_numeric(df[output], errors="coerce").to_numpy(dtype=float)
    return df[input_cols].reset_index(drop=True), y


# --------------------------------------------------------------------------- #
# Fit / predict primitives
# --------------------------------------------------------------------------- #
def _make_gp(n_features: int) -> Pipeline:
    kernel = (ConstantKernel(1.0, (1e-3, 1e3))
              * Matern(length_scale=1.0, length_scale_bounds=(1e-2, 1e3), nu=2.5)
              + WhiteKernel(noise_level=1.0, noise_level_bounds=(1e-8, 1e2)))
    gp = GaussianProcessRegressor(kernel=kernel, normalize_y=True, alpha=1e-10,
                                  n_restarts_optimizer=0, random_state=0)
    return Pipeline([("scale", StandardScaler()), ("gp", gp)])


def _fit_method(Xfeat, y, method):
    if method == METHOD_GP:
        est = _make_gp(Xfeat.shape[1])
        # The kernel-hyperparameter optimiser can hit its iteration cap on noisy data;
        # that is benign (we still get a usable fit + calibrated std), so don't spam it.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=ConvergenceWarning)
            est.fit(Xfeat, y)
        return est, None, None
    median = HistGradientBoostingRegressor(loss="quantile", quantile=0.5, random_state=0)
    lower = HistGradientBoostingRegressor(loss="quantile", quantile=0.025, random_state=0)
    upper = HistGradientBoostingRegressor(loss="quantile", quantile=0.975, random_state=0)
    median.fit(Xfeat, y)
    lower.fit(Xfeat, y)
    upper.fit(Xfeat, y)
    return median, lower, upper


def _predict_method(method, est, lower, upper, Xfeat):
    """Return (mean, lo95, hi95) for a feature matrix."""
    if method == METHOD_GP:
        scaler, gp = est.named_steps["scale"], est.named_steps["gp"]
        mean, std = gp.predict(scaler.transform(Xfeat), return_std=True)
        return mean, mean - Z95 * std, mean + Z95 * std
    mean = est.predict(Xfeat)
    lo, hi = lower.predict(Xfeat), upper.predict(Xfeat)
    return mean, np.minimum(lo, hi), np.maximum(lo, hi)


def _choose_method(n_samples: int) -> str:
    return METHOD_HGBR if n_samples > config.SURROGATE_GP_MAX_SAMPLES else METHOD_GP


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def _rmse(y_true, y_pred) -> float:
    return float(np.sqrt(mean_squared_error(y_true, y_pred)))


def _coverage(y_true, lo, hi) -> float:
    inside = (y_true >= lo) & (y_true <= hi)
    return float(np.mean(inside)) if len(y_true) else float("nan")


def _kfold_metrics(Xfeat, y, method, n_folds, seed):
    n_folds = max(2, min(n_folds, len(y)))
    kf = KFold(n_splits=n_folds, shuffle=True, random_state=seed)
    rmses, maes = [], []
    for tr, te in kf.split(Xfeat):
        est, lo, hi = _fit_method(Xfeat[tr], y[tr], method)
        pred, _, _ = _predict_method(method, est, lo, hi, Xfeat[te])
        rmses.append(_rmse(y[te], pred))
        maes.append(float(mean_absolute_error(y[te], pred)))
    return {"rmse": float(np.mean(rmses)), "mae": float(np.mean(maes)), "folds": n_folds}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _dataset_hash(dataset, cols) -> str:
    present = [c for c in cols if c in dataset.columns]
    blob = dataset[present].to_csv(index=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _library_versions() -> dict:
    return {"scikit-learn": sklearn.__version__, "scipy": scipy.__version__,
            "numpy": np.__version__, "joblib": joblib.__version__}


def _resolve_cols(dataset, input_cols, output_cols, categorical_cols):
    space = config.SURROGATE_INPUT_SPACE
    if input_cols is None:
        input_cols = [c for c in space if c in dataset.columns]
    if categorical_cols is None:
        categorical_cols = [c for c in input_cols
                            if isinstance(space.get(c), list)]
    if output_cols is None:
        output_cols = [c for c in config.SURROGATE_OUTPUTS if c in dataset.columns]
    return list(input_cols), list(output_cols), list(categorical_cols)


def train_surrogate(dataset: pd.DataFrame, *, input_cols=None, output_cols=None,
                    categorical_cols=None, n_folds: int = 5, seed: int = 0,
                    date: str | None = None) -> dict:
    """Fit one :class:`SurrogateModel` per output and attach a model card to each.

    Returns ``{output: SurrogateModel}``. Each card carries the training-set hash,
    n_samples, input ranges (validity domain), k-fold CV RMSE/MAE, library versions,
    and date. Outputs with too few usable rows are skipped.
    """
    input_cols, output_cols, categorical_cols = _resolve_cols(
        dataset, input_cols, output_cols, categorical_cols)
    cont = _continuous(input_cols, categorical_cols)
    categories = {c: sorted({str(v) for v in dataset[c].dropna()})
                  for c in categorical_cols}
    date = date or _dt.date.today().isoformat()

    models: dict = {}
    for output in output_cols:
        X, y = _assemble(dataset, output, input_cols, categorical_cols)
        if len(y) < 4:
            continue
        Xfeat = _feature_matrix(X, input_cols, categorical_cols, categories)
        method = _choose_method(len(y))
        try:
            est, lower, upper = _fit_method(Xfeat, y, method)
        except Exception:  # GP can fail on degenerate data → fall back
            method = METHOD_HGBR
            est, lower, upper = _fit_method(Xfeat, y, method)
        input_ranges = {c: (float(X[c].astype(float).min()), float(X[c].astype(float).max()))
                        for c in cont}
        cv = _kfold_metrics(Xfeat, y, method, n_folds, seed)
        card = {
            "output": output,
            "method": method,
            "training_set_hash": _dataset_hash(dataset, input_cols + [output]),
            "n_samples": int(len(y)),
            "input_ranges": {**{c: list(rng) for c, rng in input_ranges.items()},
                             "categories": {c: categories[c] for c in categorical_cols}},
            "cv_metric": cv,
            "library_versions": _library_versions(),
            "date": date,
        }
        models[output] = SurrogateModel(
            output=output, method=method, estimator=est, lower_estimator=lower,
            upper_estimator=upper, input_cols=input_cols, categorical_cols=categorical_cols,
            categories=categories, input_ranges=input_ranges, card=card)
    return models


def _domain_flags(model: SurrogateModel, X: pd.DataFrame) -> list[str]:
    flags = []
    cont = _continuous(model.input_cols, model.categorical_cols)
    for _, row in X.iterrows():
        out = False
        for c in cont:
            lo, hi = model.input_ranges[c]
            v = pd.to_numeric(pd.Series([row[c]]), errors="coerce").iloc[0]
            if pd.isna(v) or v < lo or v > hi:
                out = True
                break
        if not out:
            for c in model.categorical_cols:
                if str(row[c]) not in model.categories[c]:
                    out = True
                    break
        flags.append(DOMAIN_EXTRAPOLATION if out else DOMAIN_INTERIOR)
    return flags


def predict(model: SurrogateModel, X: pd.DataFrame) -> pd.DataFrame:
    """Predict ``output`` for rows ``X``: mean, 95% interval, and a domain flag.

    Returns a frame with ``mean``, ``lower``, ``upper``, ``domain`` (rows outside the
    trained input ranges are flagged ``extrapolation`` — predict still returns a
    value, but it must be treated as untrustworthy).
    """
    Xfeat = _feature_matrix(X, model.input_cols, model.categorical_cols, model.categories)
    mean, lo, hi = _predict_method(model.method, model.estimator,
                                   model.lower_estimator, model.upper_estimator, Xfeat)
    return pd.DataFrame({
        "mean": mean, "lower": lo, "upper": hi,
        "domain": _domain_flags(model, X.reset_index(drop=True)),
    })


def validate_surrogate(dataset: pd.DataFrame, *, input_cols=None, output_cols=None,
                       categorical_cols=None, test_size: float = 0.2, n_folds: int = 5,
                       seed: int = 0) -> pd.DataFrame:
    """Honest per-output validation: held-out RMSE/MAE + 95% coverage, plus k-fold CV.

    Trains on a train split and evaluates on a disjoint held-out test split, so the
    numbers are not in-sample. Returns one row per output with ``method``, ``n_train``,
    ``n_test``, ``rmse``, ``mae``, ``coverage95`` (fraction of held-out truths inside
    the predicted interval), ``cv_rmse``, ``cv_mae``.
    """
    input_cols, output_cols, categorical_cols = _resolve_cols(
        dataset, input_cols, output_cols, categorical_cols)
    categories = {c: sorted({str(v) for v in dataset[c].dropna()})
                  for c in categorical_cols}

    rows = []
    for output in output_cols:
        X, y = _assemble(dataset, output, input_cols, categorical_cols)
        if len(y) < 6:
            continue
        Xfeat = _feature_matrix(X, input_cols, categorical_cols, categories)
        method = _choose_method(len(y))
        Xtr, Xte, ytr, yte = train_test_split(Xfeat, y, test_size=test_size,
                                              random_state=seed)
        try:
            est, lower, upper = _fit_method(Xtr, ytr, method)
        except Exception:
            method = METHOD_HGBR
            est, lower, upper = _fit_method(Xtr, ytr, method)
        pred, lo, hi = _predict_method(method, est, lower, upper, Xte)
        cv = _kfold_metrics(Xfeat, y, method, n_folds, seed)
        rows.append({
            "output": output, "method": method,
            "n_train": int(len(ytr)), "n_test": int(len(yte)),
            "rmse": _rmse(yte, pred), "mae": float(mean_absolute_error(yte, pred)),
            "coverage95": _coverage(yte, lo, hi),
            "cv_rmse": cv["rmse"], "cv_mae": cv["mae"],
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def save_surrogate(models: dict, directory) -> Path:
    """Persist each model (``<output>.joblib``) + its ``<output>.model_card.json``."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for output, model in models.items():
        safe = output.replace("/", "_")
        joblib.dump(model, directory / f"{safe}.joblib")
        (directory / f"{safe}.model_card.json").write_text(
            json.dumps(model.card, indent=2), encoding="utf-8")
    return directory


def load_surrogate(directory) -> dict:
    """Load all persisted surrogate models from a directory (``{output: model}``)."""
    directory = Path(directory)
    models: dict = {}
    for path in sorted(directory.glob("*.joblib")):
        model = joblib.load(path)
        models[model.output] = model
    return models
