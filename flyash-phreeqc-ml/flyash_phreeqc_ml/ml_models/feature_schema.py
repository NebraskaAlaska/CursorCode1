"""Input **feature** schema for the composite / mechanical models (dependency-free).

Declares the numeric and categorical features a model may use, their human labels, and a small
"core" set (at least one core feature must be supplied to make a prediction — refusing on a
near-empty input is part of the honesty contract). No scikit-learn import here.

The features mirror the training-row fields in :mod:`training_data`; the preprocessor in
:mod:`preprocessing` turns a frame of these into model inputs (median-imputed numerics, one-hot
categoricals that tolerate unseen values).
"""
from __future__ import annotations

# Numeric features (material oxides + mix proportions + plastic + curing) -------------------- #
NUMERIC_FEATURES = (
    # material / binder composition (oxide wt%)
    "SiO2_wt", "Al2O3_wt", "CaO_wt", "Fe2O3_wt", "MgO_wt", "Na2O_wt", "K2O_wt", "SO3_wt",
    # mix proportions
    "red_mud_percent", "cement_percent", "aggregate_percent",
    # plastic
    "plastic_particle_size_mm", "plastic_dosage_percent",
    # mixing / curing
    "water_binder_ratio", "activator_concentration_M", "curing_time_days", "curing_temperature_C",
)

# Categorical features --------------------------------------------------------------------- #
CATEGORICAL_FEATURES = (
    "fly_ash_class", "fly_ash_source", "plastic_type", "plastic_form", "plastic_replacement_basis",
    "activator_type", "curing_condition", "specimen_geometry", "test_standard",
)

ALL_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

#: At least one of these must be present to predict (refuse on an essentially empty input).
CORE_FEATURES = (
    "plastic_dosage_percent", "water_binder_ratio", "curing_time_days",
    "CaO_wt", "SiO2_wt", "plastic_type",
)

FEATURE_LABELS = {
    "SiO2_wt": "SiO₂ (wt%)", "Al2O3_wt": "Al₂O₃ (wt%)", "CaO_wt": "CaO (wt%)",
    "Fe2O3_wt": "Fe₂O₃ (wt%)", "MgO_wt": "MgO (wt%)", "Na2O_wt": "Na₂O (wt%)",
    "K2O_wt": "K₂O (wt%)", "SO3_wt": "SO₃ (wt%)",
    "red_mud_percent": "Red mud (%)", "cement_percent": "Cement (%)",
    "aggregate_percent": "Aggregate (%)",
    "plastic_particle_size_mm": "Plastic particle size (mm)",
    "plastic_dosage_percent": "Plastic dosage (%)",
    "water_binder_ratio": "Water / binder ratio",
    "activator_concentration_M": "Activator concentration (M)",
    "curing_time_days": "Curing time (days)", "curing_temperature_C": "Curing temperature (°C)",
    "fly_ash_class": "Fly ash class", "fly_ash_source": "Fly ash source",
    "plastic_type": "Plastic type", "plastic_form": "Plastic form",
    "plastic_replacement_basis": "Plastic replacement basis",
    "activator_type": "Activator type", "curing_condition": "Curing condition",
    "specimen_geometry": "Specimen geometry", "test_standard": "Test standard",
}


def numeric_features() -> list:
    return list(NUMERIC_FEATURES)


def categorical_features() -> list:
    return list(CATEGORICAL_FEATURES)


def feature_columns() -> list:
    return list(ALL_FEATURES)


def feature_label(name: str) -> str:
    return FEATURE_LABELS.get(name, name)


def is_numeric(name: str) -> bool:
    return name in NUMERIC_FEATURES


def is_categorical(name: str) -> bool:
    return name in CATEGORICAL_FEATURES
