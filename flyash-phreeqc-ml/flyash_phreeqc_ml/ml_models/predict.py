"""Make an **experimental surrogate** prediction from a trained model (no scikit-learn import).

A prediction carries the value, an approximate interval, the model name/version, the training-row
count, the model source, applicability / out-of-domain / missing-feature warnings, and the
honest status (``demo`` / ``experimental`` — never ``validated``). It **refuses** when there is no
model, an unsupported target, or an essentially empty input (no core feature supplied).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import feature_schema, model_schema, preprocessing, uncertainty

NOT_VALIDATED_NOTE = ("This is an experimental surrogate estimate, not a validated prediction or a "
                      "measurement. Validate against measured experiments before relying on it.")
DEMO_NOTE = ("DEMO MODEL — trained on synthetic data for workflow testing only. This number is "
             "meaningless; it is not a real or validated prediction.")
EXPLORATORY_NOTE = ("EXPLORATORY MODEL — training admitted rows outside the approved default "
                    "eligibility gate. This is not an approved-data model.")
LEGACY_UNKNOWN_NOTE = ("LEGACY MODEL — the saved artifact did not record training-data "
                       "eligibility status. Treat its training provenance as unknown.")

REFUSE_NO_MODEL = "no_model"
REFUSE_UNSUPPORTED = "unsupported_target"
REFUSE_INCOMPLETE = "inputs_incomplete"
REFUSE_INVALID_MODEL_OUTPUT = "invalid_model_output"

INVALID_MODEL_OUTPUT_MESSAGE = (
    "The fitted model produced a non-finite prediction or uncertainty value. No ML prediction was "
    "produced; re-train or re-select the model and verify its model card and feature schema."
)


def _is_finite_number(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _numeric_outputs_are_valid(value, lower, upper, sigma) -> bool:
    """A mean is required; optional uncertainty values must be finite when present."""
    return (_is_finite_number(value)
            and all(item is None or _is_finite_number(item) for item in (lower, upper, sigma)))


def _serialization_safe(value):
    """Recursively replace non-finite floats so strict JSON callers never receive NaN/Infinity."""
    if isinstance(value, dict):
        return {key: _serialization_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialization_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass
class Prediction:
    """The result of a prediction attempt (a refusal is a valid, explained result)."""

    target: str
    value: float | None = None
    lower: float | None = None
    upper: float | None = None
    sigma: float | None = None
    interval_method: str = uncertainty.METHOD_NONE
    model_name: str = ""
    model_version: str = ""
    n_training_rows: int = 0
    source_of_model: str = ""
    status: str = ""                              # demo / experimental
    training_data_status: str = model_schema.TRAINING_DATA_LEGACY_UNKNOWN
    is_demo: bool = False
    warnings: list = field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None
    used_features: dict = field(default_factory=dict)
    missing_features: list = field(default_factory=list)
    out_of_domain: list = field(default_factory=list)
    unseen_categories: dict = field(default_factory=dict)
    refusal_code: str | None = None

    def headline(self) -> str:
        if self.refused:
            return "No prediction"
        unit = model_schema.target_unit(self.target)
        return f"{self.value:.2f} {unit}".strip()

    def interval_text(self) -> str | None:
        if self.refused or self.lower is None or self.upper is None:
            return None
        unit = model_schema.target_unit(self.target)
        return f"{self.lower:.2f} – {self.upper:.2f} {unit}".strip()

    def to_dict(self) -> dict:
        """Return a serialization-safe prediction envelope with no NaN or Infinity tokens."""
        invalid_success = not self.refused and not _numeric_outputs_are_valid(
            self.value, self.lower, self.upper, self.sigma)
        refused = self.refused or invalid_success
        refusal_code = (REFUSE_INVALID_MODEL_OUTPUT if invalid_success else self.refusal_code)
        refusal_reason = (INVALID_MODEL_OUTPUT_MESSAGE if invalid_success else self.refusal_reason)
        warnings = list(self.warnings)
        if invalid_success and INVALID_MODEL_OUTPUT_MESSAGE not in warnings:
            warnings.append(INVALID_MODEL_OUTPUT_MESSAGE)
        numeric = {
            "value": None if invalid_success else self.value,
            "lower": None if invalid_success else self.lower,
            "upper": None if invalid_success else self.upper,
            "sigma": None if invalid_success else self.sigma,
        }
        return _serialization_safe({
            "target": self.target,
            **numeric,
            "interval_method": self.interval_method,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "n_training_rows": self.n_training_rows,
            "source_of_model": self.source_of_model,
            "status": self.status,
            "training_data_status": self.training_data_status,
            "is_demo": self.is_demo,
            "warnings": warnings,
            "refused": refused,
            "refusal_code": refusal_code,
            "refusal_reason": refusal_reason,
            "used_features": self.used_features,
            "missing_features": self.missing_features,
            "out_of_domain": self.out_of_domain,
            "unseen_categories": self.unseen_categories,
        })


def _coerce_features(features: dict) -> dict:
    """Keep only known feature keys with a non-empty value."""
    out = {}
    for k in feature_schema.ALL_FEATURES:
        v = (features or {}).get(k)
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        out[k] = v
    return out


def _refuse(target, reason_code, message, **kw) -> Prediction:
    return Prediction(target=target, refused=True, refusal_code=reason_code, refusal_reason=message,
                      warnings=[message], **kw)


def _invalid_model_output(model, target: str, known: dict) -> Prediction:
    """Return a controlled refusal without exposing artifact internals or non-finite values."""
    return _refuse(
        target, REFUSE_INVALID_MODEL_OUTPUT, INVALID_MODEL_OUTPUT_MESSAGE,
        model_name=model.name, model_version=model.version,
        n_training_rows=model.n_train, source_of_model=model.source_type,
        status=model.validation_status, training_data_status=model.training_status,
        is_demo=model.is_demo, used_features=known)


def predict(model: model_schema.TrainedModel | None, features: dict) -> Prediction:
    """Predict ``model.target`` for the given ``features`` (never raises).

    Refuses (with an explained :class:`Prediction`) when: there is no trained model; the target is
    unsupported; or fewer than one *core* feature is supplied (inputs too incomplete). Otherwise it
    returns the value + interval + applicability / out-of-domain / missing-feature warnings.
    """
    if model is None:
        return _refuse("unknown", REFUSE_NO_MODEL,
                       "No trained model is selected — train or select a model first.")

    target = model.target
    if not model_schema.is_supported_target(target):
        return _refuse(target, REFUSE_UNSUPPORTED,
                       f"Target {target!r} is not supported by this engine.",
                       model_name=model.name, model_version=model.version,
                       n_training_rows=model.n_train, source_of_model=model.source_type,
                       status=model.validation_status,
                       training_data_status=model.training_status, is_demo=model.is_demo)

    known = _coerce_features(features)
    if not any(k in known for k in feature_schema.CORE_FEATURES):
        return _refuse(
            target, REFUSE_INCOMPLETE,
            "Inputs are too incomplete to predict — provide at least one core input "
            f"({', '.join(feature_schema.feature_label(c) for c in feature_schema.CORE_FEATURES)}).",
            model_name=model.name, model_version=model.version,
            n_training_rows=model.n_train, source_of_model=model.source_type,
            status=model.validation_status, training_data_status=model.training_status,
            is_demo=model.is_demo, used_features=known)

    # Build the single-row frame over the model's own feature set.
    x_df = preprocessing.single_row_frame(known, model.numeric_features, model.categorical_features)
    try:
        mean, sigma, lower, upper, method = uncertainty.predict_with_uncertainty(model, x_df)
    except uncertainty.InvalidModelOutputError:
        return _invalid_model_output(model, target, known)
    if not _numeric_outputs_are_valid(mean, lower, upper, sigma):
        return _invalid_model_output(model, target, known)

    warnings: list = []
    if model.is_demo:
        warnings.append(DEMO_NOTE)
    elif model.is_exploratory:
        warnings.append(EXPLORATORY_NOTE)
    elif model.is_legacy_unknown:
        warnings.append(LEGACY_UNKNOWN_NOTE)
    warnings.append(NOT_VALIDATED_NOTE)

    # Missing model features (warn — they were median/“unknown”-imputed).
    model_features = list(model.numeric_features) + list(model.categorical_features)
    missing = [feature_schema.feature_label(f) for f in model_features if f not in known]
    if missing:
        warnings.append("Some inputs were left blank and filled with training defaults: "
                        + ", ".join(missing[:8]) + (" …" if len(missing) > 8 else "") + ".")

    # Out-of-domain numeric inputs (outside the trained range).
    out_of_domain = []
    for col, rng in (model.feature_ranges or {}).items():
        if col in known:
            try:
                val = float(known[col])
            except (TypeError, ValueError):
                continue
            lo, hi = float(rng[0]), float(rng[1])
            if val < lo or val > hi:
                out_of_domain.append({"feature": col, "value": val, "range": [lo, hi]})
    if out_of_domain:
        labels = ", ".join(feature_schema.feature_label(o["feature"]) for o in out_of_domain)
        warnings.append(f"Out-of-domain input(s) outside the training range: {labels}. "
                        "The estimate is an extrapolation and may be unreliable.")

    # Unseen categorical values.
    unseen = {}
    for col, seen in (model.categories_seen or {}).items():
        if col in known and str(known[col]) not in {str(s) for s in seen}:
            unseen[col] = str(known[col])
    if unseen:
        warnings.append("Unseen category value(s) not in the training data: "
                        + ", ".join(f"{feature_schema.feature_label(k)}={v}"
                                    for k, v in unseen.items()) + ".")

    # Implausible output (flag, do not refuse).
    rng = model_schema.TARGET_PLAUSIBLE_RANGE.get(target)
    if rng and (mean < rng[0] or mean > rng[1]):
        warnings.append(f"The estimate falls outside the physically-plausible range for "
                        f"{model_schema.target_label(target)} — treat it with strong caution.")

    return Prediction(
        target=target, value=round(float(mean), 3),
        lower=(round(float(lower), 3) if lower is not None else None),
        upper=(round(float(upper), 3) if upper is not None else None),
        sigma=(round(float(sigma), 4) if sigma is not None else None),
        interval_method=method, model_name=model.name, model_version=model.version,
        n_training_rows=model.n_train, source_of_model=model.source_type,
        status=model.validation_status, is_demo=model.is_demo, warnings=warnings,
        training_data_status=model.training_status,
        used_features=known, missing_features=missing, out_of_domain=out_of_domain,
        unseen_categories=unseen)
