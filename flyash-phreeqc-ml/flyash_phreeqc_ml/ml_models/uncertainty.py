"""Prediction **uncertainty** — an honest, approximate interval (no scikit-learn import).

For a random forest, the spread across trees is a usable epistemic-uncertainty proxy; for the
other estimators we fall back to the model's cross-validation residual standard deviation. Either
way the interval is reported as **approximate**, and the prediction is always labelled an
experimental surrogate estimate — never a measurement.
"""
from __future__ import annotations

import numpy as np

from . import model_schema

Z95 = 1.959963984540054  # 97.5th percentile of the standard normal

METHOD_FOREST = "forest_spread"        # std across the forest's trees (≈ epistemic uncertainty)
METHOD_CV_RESIDUAL = "cv_residual"     # the model's held-out residual std (constant interval)
METHOD_NONE = "none"


def _split_pipeline(pipeline):
    """Return ``(preprocessor, estimator)`` from a fitted 2-step Pipeline, else ``(None, None)``."""
    steps = getattr(pipeline, "named_steps", None)
    if not steps:
        return None, None
    pre = steps.get("pre")
    est = steps.get("est")
    return pre, est


def _forest_sigma(pre, est, x_df) -> float | None:
    """Std of per-tree predictions for one row (random forest only)."""
    trees = getattr(est, "estimators_", None)
    if not trees:
        return None
    try:
        xt = pre.transform(x_df) if pre is not None else x_df
        preds = np.array([float(np.ravel(t.predict(xt))[0]) for t in trees])
    except Exception:                                            # noqa: BLE001 - robust fallback
        return None
    if preds.size < 2:
        return None
    return float(np.std(preds, ddof=1))


def predict_with_uncertainty(model: model_schema.TrainedModel, x_df):
    """Return ``(mean, sigma, lower, upper, method)`` for a single-row frame ``x_df``.

    ``mean`` comes from the fitted pipeline; ``sigma`` is the forest spread (random forest) or the
    CV residual std (otherwise). The interval is ``mean ± Z95·sigma`` (approximate, ~95%).
    """
    mean = float(np.ravel(model.pipeline.predict(x_df))[0])

    sigma = None
    method = METHOD_NONE
    if model.model_type == model_schema.MODEL_RANDOM_FOREST:
        pre, est = _split_pipeline(model.pipeline)
        sigma = _forest_sigma(pre, est, x_df)
        if sigma is not None:
            method = METHOD_FOREST
    if sigma is None:
        rs = float(getattr(model, "residual_sigma", 0.0) or 0.0)
        if rs > 0:
            sigma = rs
            method = METHOD_CV_RESIDUAL

    if sigma is None or sigma <= 0:
        return mean, None, None, None, METHOD_NONE
    return mean, sigma, mean - Z95 * sigma, mean + Z95 * sigma, method
