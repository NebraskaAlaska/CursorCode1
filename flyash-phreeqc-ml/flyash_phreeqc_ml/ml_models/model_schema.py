"""Prediction-model **targets** + the persisted :class:`TrainedModel` container.

This is the lightweight, dependency-free core of the ``ml_models`` package — it declares the
supported prediction targets (the mechanical / composite properties), the supported estimator
types, and the plain data container a trained model is saved/loaded as. It imports **no**
scikit-learn (the fitted ``pipeline`` lives on the object as an opaque attribute), so every other
module and the UI can import these constants without scikit-learn installed.

Honesty note: a model trained here is a **surrogate / experimental estimator**. ``validation_status``
is at most :data:`VALIDATION_EXPERIMENTAL` (cross-validation metrics on the training rows) — it is
**never** :data:`VALIDATION_VALIDATED`, because "validated" in this app means held-out agreement
with independently *measured* experiments, which is a higher bar this engine does not assert.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Targets (the first model family: polymer composite / mechanical testing)
# --------------------------------------------------------------------------- #
TARGET_COMPRESSIVE = "compressive_strength_MPa"
TARGET_FLEXURAL = "flexural_strength_MPa"
TARGET_DENSITY = "density_g_cm3"
TARGET_WATER_ABSORPTION = "water_absorption_percent"

#: The supported targets, in priority order (compressive strength is the main target).
SUPPORTED_TARGETS = (TARGET_COMPRESSIVE, TARGET_FLEXURAL, TARGET_DENSITY, TARGET_WATER_ABSORPTION)
DEFAULT_TARGET = TARGET_COMPRESSIVE

TARGET_LABELS = {
    TARGET_COMPRESSIVE: "Compressive strength",
    TARGET_FLEXURAL: "Flexural strength",
    TARGET_DENSITY: "Density",
    TARGET_WATER_ABSORPTION: "Water absorption",
}
TARGET_UNITS = {
    TARGET_COMPRESSIVE: "MPa",
    TARGET_FLEXURAL: "MPa",
    TARGET_DENSITY: "g/cm³",
    TARGET_WATER_ABSORPTION: "%",
}

#: Physically-plausible output ranges — a prediction outside these is *flagged* (not refused),
#: because it usually means the inputs are far outside what the model saw.
TARGET_PLAUSIBLE_RANGE = {
    TARGET_COMPRESSIVE: (0.0, 200.0),
    TARGET_FLEXURAL: (0.0, 60.0),
    TARGET_DENSITY: (0.3, 5.0),
    TARGET_WATER_ABSORPTION: (0.0, 100.0),
}

#: The model family these targets belong to (room for thermal / durability families later).
MODEL_FAMILY_COMPOSITE = "polymer_composite_mechanical"
MODEL_VERSION = "v1"

# --------------------------------------------------------------------------- #
# Estimator types (simple, interpretable, small-data friendly)
# --------------------------------------------------------------------------- #
MODEL_RANDOM_FOREST = "random_forest"
MODEL_GRADIENT_BOOSTING = "gradient_boosting"
MODEL_RIDGE = "ridge"
SUPPORTED_MODEL_TYPES = (MODEL_RANDOM_FOREST, MODEL_GRADIENT_BOOSTING, MODEL_RIDGE)
DEFAULT_MODEL_TYPE = MODEL_RANDOM_FOREST

MODEL_TYPE_LABELS = {
    MODEL_RANDOM_FOREST: "Random forest",
    MODEL_GRADIENT_BOOSTING: "Gradient boosting",
    MODEL_RIDGE: "Ridge regression (linear baseline)",
}

# --------------------------------------------------------------------------- #
# Validation status (never "validated" in v1 — see module docstring)
# --------------------------------------------------------------------------- #
VALIDATION_DEMO = "demo"                 # trained on synthetic demo rows — workflow testing only
VALIDATION_EXPERIMENTAL = "experimental"  # real data + CV metrics, NOT measured-validated
VALIDATION_VALIDATED = "validated"        # reserved; never set by this engine in v1

VALIDATION_LABELS = {
    VALIDATION_DEMO: "Demo (synthetic — not validated)",
    VALIDATION_EXPERIMENTAL: "Experimental surrogate (cross-validated, not measured-validated)",
    VALIDATION_VALIDATED: "Validated against measured experiments",
}


def is_supported_target(target) -> bool:
    return target in SUPPORTED_TARGETS


def target_label(target) -> str:
    return TARGET_LABELS.get(target, str(target))


def target_unit(target) -> str:
    return TARGET_UNITS.get(target, "")


def target_display(target) -> str:
    """e.g. ``"Compressive strength (MPa)"``."""
    unit = target_unit(target)
    return f"{target_label(target)} ({unit})" if unit else target_label(target)


# --------------------------------------------------------------------------- #
# Trained-model container (joblib-serialisable; holds the fitted sklearn pipeline)
# --------------------------------------------------------------------------- #
@dataclass
class TrainedModel:
    """A trained surrogate model + everything needed to predict, explain, and audit it.

    ``pipeline`` is a fitted scikit-learn ``Pipeline`` (preprocessor + estimator). It is typed
    ``object`` so this module never imports scikit-learn. Persisted via joblib by the registry.
    """

    name: str
    target: str
    model_type: str
    model_family: str
    pipeline: object                         # fitted sklearn Pipeline (opaque here)
    numeric_features: list = field(default_factory=list)
    categorical_features: list = field(default_factory=list)
    feature_ranges: dict = field(default_factory=dict)      # numeric col -> [min, max] (applicability)
    categories_seen: dict = field(default_factory=dict)     # categorical col -> [values seen]
    residual_sigma: float = 0.0              # CV / held-out residual std (interval fallback)
    metrics: dict = field(default_factory=dict)
    card: dict = field(default_factory=dict)
    source_type: str = "unknown"             # literature / lab / manual / synthetic_demo / mixed
    validation_status: str = VALIDATION_EXPERIMENTAL
    n_train: int = 0
    n_validation: int = 0
    version: str = MODEL_VERSION
    created: str = ""

    @property
    def is_demo(self) -> bool:
        return self.validation_status == VALIDATION_DEMO

    @property
    def is_validated(self) -> bool:
        """Always False in v1 — this engine produces surrogates, never measured-validated models."""
        return self.validation_status == VALIDATION_VALIDATED

    def display_label(self) -> str:
        tag = " · DEMO" if self.is_demo else ""
        return f"{target_display(self.target)} — {MODEL_TYPE_LABELS.get(self.model_type, self.model_type)}{tag}"
