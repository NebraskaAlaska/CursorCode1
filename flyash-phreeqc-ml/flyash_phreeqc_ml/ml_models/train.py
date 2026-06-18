"""Train a composite / mechanical surrogate model (scikit-learn, lazily imported).

The model is a ``Pipeline(preprocessor → estimator)`` where the estimator is a random forest,
gradient boosting, or a Ridge baseline. Honesty rules enforced here:

* **Data-sufficiency gate.** Real training refuses (typed :class:`InsufficientTrainingDataError`)
  below :data:`MIN_REAL_TRAINING_ROWS` approved rows. The UI shows the count instead of a train
  button until the gate is met. ``demo=True`` bypasses the gate for *synthetic* demo data only.
* **Out-of-sample metrics.** A held-out split (larger sets) or k-fold cross-validation (smaller
  sets) gives MAE / RMSE / R² that are not in-sample; the final model is then fit on all rows.
* **Never "validated".** ``validation_status`` is ``demo`` or ``experimental`` — never
  ``validated`` (that requires measured-experiment agreement this engine does not assert).
* **Graceful without scikit-learn.** :func:`train_model` raises a clear
  :class:`SklearnNotAvailableError` with an install message instead of crashing on import.
"""
from __future__ import annotations

import datetime as _dt

import numpy as np

from . import model_card, model_schema, preprocessing, training_data

#: Below this many eligible rows, a *real* model is refused (demo bypasses).
MIN_REAL_TRAINING_ROWS = 10
#: At/above this, use a held-out split; between the gate and this, use k-fold CV.
SPLIT_MIN_ROWS = 24
#: R² is only reported when at least this many out-of-sample points exist.
R2_MIN_POINTS = 5

DEFAULT_RF_TREES = 300


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class MLModelError(Exception):
    """Base error for the ml_models training/prediction layer."""


class SklearnNotAvailableError(MLModelError):
    """scikit-learn is not installed (training/prediction needs it)."""


class UnsupportedTargetError(MLModelError):
    """The requested target is not a supported prediction output."""


class NoTargetValuesError(MLModelError):
    """No rows carry a numeric value for the requested target."""


class NoUsableFeaturesError(MLModelError):
    """No feature column carries any observed value — nothing to learn from."""


class InsufficientTrainingDataError(MLModelError):
    """Fewer than :data:`MIN_REAL_TRAINING_ROWS` eligible rows for a real model."""

    def __init__(self, n: int, minimum: int, target: str):
        self.n = int(n)
        self.minimum = int(minimum)
        self.target = target
        super().__init__(
            f"Not enough approved data to train a reliable model: {self.n} of {self.minimum} "
            f"rows for {model_schema.target_label(target)}.")

    def message(self) -> str:
        return ("Not enough approved data to train a reliable model "
                f"({self.n} of {self.minimum} rows). Approve more evidence/lab rows, or train a "
                "clearly-labelled demo model for workflow testing.")


# --------------------------------------------------------------------------- #
# scikit-learn availability
# --------------------------------------------------------------------------- #
def sklearn_available() -> bool:
    try:
        import sklearn
        return sklearn is not None
    except ImportError:
        return False


def require_sklearn() -> None:
    if not sklearn_available():
        raise SklearnNotAvailableError(
            "scikit-learn is required to train prediction models. Install it with "
            "`pip install scikit-learn` (and scipy). The rest of the app works without it.")


def _build_estimator(model_type: str, seed: int):
    """Construct the (unfitted) estimator (scikit-learn imported lazily)."""
    if model_type == model_schema.MODEL_RANDOM_FOREST:
        from sklearn.ensemble import RandomForestRegressor
        return RandomForestRegressor(n_estimators=DEFAULT_RF_TREES, random_state=seed)
    if model_type == model_schema.MODEL_GRADIENT_BOOSTING:
        from sklearn.ensemble import GradientBoostingRegressor
        return GradientBoostingRegressor(random_state=seed)
    if model_type == model_schema.MODEL_RIDGE:
        from sklearn.linear_model import Ridge
        return Ridge(alpha=1.0, random_state=seed)
    raise MLModelError(f"unsupported model_type {model_type!r}")


def _make_pipeline(numeric_cols, categorical_cols, model_type, seed):
    from sklearn.pipeline import Pipeline
    pre = preprocessing.build_preprocessor(
        numeric_cols, categorical_cols,
        scale_numeric=(model_type == model_schema.MODEL_RIDGE))
    est = _build_estimator(model_type, seed)
    return Pipeline([("pre", pre), ("est", est)])


def _rmse(y_true, y_pred) -> float:
    from sklearn.metrics import mean_squared_error
    return float(np.sqrt(mean_squared_error(y_true, y_pred)))


def _evaluate(x_df, y, numeric_cols, categorical_cols, model_type, seed):
    """Out-of-sample metrics + residual sigma (held-out split or k-fold CV)."""
    from sklearn.metrics import mean_absolute_error, r2_score
    from sklearn.model_selection import KFold, cross_val_predict, train_test_split

    n = len(y)
    if n >= SPLIT_MIN_ROWS:
        method = "holdout"
        idx = np.arange(n)
        tr, te = train_test_split(idx, test_size=0.25, random_state=seed)
        pipe = _make_pipeline(numeric_cols, categorical_cols, model_type, seed)
        pipe.fit(x_df.iloc[tr], y[tr])
        y_pred = pipe.predict(x_df.iloc[te])
        y_eval = y[te]
        n_val = int(len(te))
    else:
        method = "cross_val"
        k = max(2, min(5, n))
        pipe = _make_pipeline(numeric_cols, categorical_cols, model_type, seed)
        cv = KFold(n_splits=k, shuffle=True, random_state=seed)
        y_pred = cross_val_predict(pipe, x_df, y, cv=cv)
        y_eval = y
        n_val = int(n)

    residuals = np.asarray(y_eval, float) - np.asarray(y_pred, float)
    mae = float(mean_absolute_error(y_eval, y_pred))
    rmse = _rmse(y_eval, y_pred)
    sigma = float(np.std(residuals, ddof=1)) if residuals.size >= 2 else 0.0
    r2 = None
    if n_val >= R2_MIN_POINTS and float(np.var(y_eval)) > 1e-9:
        r2 = round(float(r2_score(y_eval, y_pred)), 4)

    metrics = {
        "method": method, "MAE": round(mae, 4), "RMSE": round(rmse, 4), "R2": r2,
        "n_train": int(n), "n_validation": n_val,
    }
    return metrics, sigma, n_val


def _training_provenance(rows) -> dict:
    by_source: dict = {}
    citations = []
    for r in rows:
        by_source[r.source_type] = by_source.get(r.source_type, 0) + 1
        if r.source_type == training_data.SOURCE_LITERATURE:
            cite = r.citation or r.doi or r.source_id
            if cite:
                citations.append(str(cite))
    uniq = sorted(set(citations))
    return {"rows_by_source": by_source, "n_literature_citations": len(uniq),
            "example_citations": uniq[:8]}


def train_model(rows, *, target: str = model_schema.DEFAULT_TARGET,
                model_type: str = model_schema.DEFAULT_MODEL_TYPE, demo: bool = False,
                seed: int = 0, date: str | None = None, name: str | None = None,
                source_type: str | None = None) -> model_schema.TrainedModel:
    """Train and return a :class:`~model_schema.TrainedModel` for ``target`` (never auto-saved).

    ``rows`` are :class:`~training_data.TrainingRow` (already filtered to the eligible set by the
    caller). Raises a typed error on an unsupported target, missing scikit-learn, no target values,
    no usable features, or — for a non-demo model — too few rows.
    """
    if not model_schema.is_supported_target(target):
        raise UnsupportedTargetError(f"unsupported target {target!r}")
    if model_type not in model_schema.SUPPORTED_MODEL_TYPES:
        raise MLModelError(f"unsupported model_type {model_type!r}")
    require_sklearn()

    rows = list(rows or [])
    labeled = [r for r in rows if training_data.target_value(r, target) is not None]
    if not labeled:
        raise NoTargetValuesError(
            f"no rows carry a {model_schema.target_label(target)} value.")
    if not demo and len(labeled) < MIN_REAL_TRAINING_ROWS:
        raise InsufficientTrainingDataError(len(labeled), MIN_REAL_TRAINING_ROWS, target)

    x_df, y, numeric_cols, categorical_cols, n_dropped = preprocessing.build_xy(labeled, target)
    if not numeric_cols and not categorical_cols:
        raise NoUsableFeaturesError(
            "no usable input features — every feature column is empty in the training rows.")

    metrics, sigma, n_val = _evaluate(x_df, y, numeric_cols, categorical_cols, model_type, seed)

    # Final model: fit on ALL labelled rows (the metrics above are out-of-sample).
    pipeline = _make_pipeline(numeric_cols, categorical_cols, model_type, seed)
    pipeline.fit(x_df, y)

    src = source_type or training_data.infer_dataset_source_type(labeled)
    validation_status = (model_schema.VALIDATION_DEMO if demo
                         else model_schema.VALIDATION_EXPERIMENTAL)
    date = date or _dt.date.today().isoformat()
    feature_ranges = preprocessing.feature_ranges(x_df, numeric_cols)
    categories_seen = preprocessing.categories_seen(x_df, categorical_cols)
    metrics = {**metrics, "n_dropped_no_target": int(n_dropped),
               "feature_coverage": preprocessing.feature_coverage(x_df)}

    card = model_card.build_model_card(
        model_type=model_type, target=target, source_type=src,
        validation_status=validation_status, n_train=len(y), n_validation=n_val,
        numeric_features=numeric_cols, categorical_features=categorical_cols, metrics=metrics,
        feature_ranges=feature_ranges, categories_seen=categories_seen,
        training_provenance=_training_provenance(labeled), date=date,
        version=model_schema.MODEL_VERSION)

    name = name or _default_name(target, model_type, validation_status)
    return model_schema.TrainedModel(
        name=name, target=target, model_type=model_type,
        model_family=model_schema.MODEL_FAMILY_COMPOSITE, pipeline=pipeline,
        numeric_features=list(numeric_cols), categorical_features=list(categorical_cols),
        feature_ranges=feature_ranges, categories_seen=categories_seen, residual_sigma=sigma,
        metrics=metrics, card=card, source_type=src, validation_status=validation_status,
        n_train=len(y), n_validation=n_val, version=model_schema.MODEL_VERSION, created=date)


def _default_name(target, model_type, validation_status) -> str:
    tag = "demo" if validation_status == model_schema.VALIDATION_DEMO else "exp"
    return f"{target}__{model_type}__{tag}"


# --------------------------------------------------------------------------- #
# Convenience: train a clearly-labelled demo model from synthetic rows
# --------------------------------------------------------------------------- #
def train_demo_model(*, target: str = model_schema.DEFAULT_TARGET,
                     model_type: str = model_schema.DEFAULT_MODEL_TYPE,
                     n: int = training_data.DEMO_N, seed: int = 0,
                     date: str | None = None) -> model_schema.TrainedModel:
    """Train a DEMO model on synthetic rows (workflow testing only — never validated)."""
    rows = training_data.demo_rows(n=n, seed=seed)
    return train_model(rows, target=target, model_type=model_type, demo=True, seed=seed, date=date,
                       source_type=training_data.SOURCE_DEMO)
