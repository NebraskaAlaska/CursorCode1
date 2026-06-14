"""Generic model-prediction CSV parser — accept predictions that are not PHREEQC.

This is the proof that the app's comparison workflow is model-agnostic: any model can
supply predictions through a documented CSV contract (see
``docs/model_prediction_format.md``) and flow through the *same* scenario manifest,
suggestion engine, mapping statuses, inclusion logic, and plots as PHREEQC.

The contract (validated here, with specific errors):

* required columns ``record_key`` (the join key) and ``model_name``;
* at least one **prediction column** named per the dataset profile's variables —
  ``pred_pH`` for ``final_pH`` and ``pred_<variable>`` otherwise (e.g. ``pred_Ca_mM``);
* prediction units must be the profile's target units (mM for ``*_mM``, pH for pH).
  A ``units`` mapping (``{column: unit}``; from a header row or a sidecar) lets a file
  declare other units — they are converted through the **Prompt-16 registry**
  (:mod:`flyash_phreeqc_ml.units`) and the conversion id is tagged as provenance;
* optional metadata columns matching the profile's mapping fields (leachant,
  concentration, time, L/S, condition code, temperature).

:func:`parse_predictions` returns a **normalized predictions frame** carrying
``record_key`` + ``model_name`` + ``predicted_pH`` / ``predicted_<X>_mM`` (manifest
naming) + metadata. :func:`scenarios.build_scenario_manifest` consumes it directly, so
nothing downstream of the manifest needs to know which model produced the numbers.
"""
from __future__ import annotations

import pandas as pd

from .. import profiles, units

# Required, model-agnostic contract columns.
RECORD_KEY_COLUMN = "record_key"
MODEL_NAME_COLUMN = "model_name"
REQUIRED_COLUMNS = (RECORD_KEY_COLUMN, MODEL_NAME_COLUMN)

# Optional metadata columns the parser passes through when present (they match the
# profile's mapping fields, so the suggestion engine can score on them).
METADATA_COLUMNS = (
    "leachant", "NaOH_M", "acid_M", "time_min", "liquid_solid_ratio",
    "CO2_condition", "condition_code", "temperature_C",
)

# Provenance companion suffixes on a converted prediction column.
CONVERSION_ID_SUFFIX = "__conversion_id"
ORIG_UNIT_SUFFIX = "__orig_unit"


# --------------------------------------------------------------------------- #
# Typed contract errors (specific, never silent)
# --------------------------------------------------------------------------- #
class PredictionContractError(Exception):
    """Base error for a model-prediction CSV that violates the contract."""


class MissingRequiredColumn(PredictionContractError):
    """A required column (``record_key`` / ``model_name``) is absent."""


class NoPredictionColumns(PredictionContractError):
    """No ``pred_*`` prediction column for any of the profile's variables is present."""


class DuplicateRecordKey(PredictionContractError):
    """``record_key`` must be unique (it is the join key)."""


class BlankRecordKey(PredictionContractError):
    """``record_key`` must be non-blank on every row."""


class InvalidPredictionValue(PredictionContractError):
    """A prediction cell is present but not numeric."""


# --------------------------------------------------------------------------- #
# Column naming (derived from the dataset profile's variables)
# --------------------------------------------------------------------------- #
def prediction_column(variable: str) -> str:
    """Contract prediction column for a comparison variable (``pred_pH`` / ``pred_X``)."""
    return "pred_pH" if variable == "final_pH" else f"pred_{variable}"


def predicted_column(variable: str) -> str:
    """Normalized (manifest) prediction column (``predicted_pH`` / ``predicted_X``)."""
    return "predicted_pH" if variable == "final_pH" else f"predicted_{variable}"


def expected_prediction_columns(profile=None) -> dict[str, str]:
    """``{variable: pred_column}`` the contract allows for this dataset profile."""
    profile = profile or profiles.FLY_ASH_PROFILE
    return {v: prediction_column(v) for v in profile.comparison_variable_spec}


def _element_for(variable: str) -> str | None:
    return variable[:-3] if variable.endswith("_mM") else None


def _target_unit(variable: str) -> str:
    return units.UNIT_MM if variable.endswith("_mM") else "pH"


# --------------------------------------------------------------------------- #
# Parse
# --------------------------------------------------------------------------- #
def _read(source) -> pd.DataFrame:
    if isinstance(source, pd.DataFrame):
        return source.copy()
    return pd.read_csv(source)


def parse_predictions(source, *, profile=None, units_map: dict | None = None) -> pd.DataFrame:
    """Validate a model-prediction CSV/frame against the contract; return normalized.

    ``source`` is a path / file-like / DataFrame. ``profile`` is the
    :class:`profiles.DatasetProfile` whose variables name the expected ``pred_*``
    columns. ``units_map`` (``{pred_column: unit}``) declares non-target units to be
    converted via the Prompt-16 registry; omit it to assume the target units.

    Returns a frame with ``record_key`` + ``model_name`` + ``predicted_*`` (target
    units) + per-prediction provenance companions + any passed-through metadata.
    Raises a specific :class:`PredictionContractError` subclass on a violation.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    units_map = units_map or {}
    df = _read(source)
    if df is None or df.empty:
        raise PredictionContractError("the prediction file is empty (no rows).")

    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            raise MissingRequiredColumn(
                f"required column {col!r} is missing; the contract requires "
                f"{', '.join(REQUIRED_COLUMNS)} plus at least one pred_* column.")

    pred_map = expected_prediction_columns(profile)        # variable -> pred_col
    present = {v: c for v, c in pred_map.items() if c in df.columns}
    if not present:
        raise NoPredictionColumns(
            f"no prediction column found; expected at least one of "
            f"{', '.join(sorted(pred_map.values()))}.")

    rk = df[RECORD_KEY_COLUMN].astype(str).str.strip()
    if ((rk == "") | (rk.str.lower() == "nan")).any():
        raise BlankRecordKey("record_key must be non-blank on every row.")
    if rk.duplicated().any():
        dups = sorted(rk[rk.duplicated()].unique())
        raise DuplicateRecordKey(f"record_key must be unique; duplicates: {dups}.")

    out = pd.DataFrame(index=range(len(df)))
    out[RECORD_KEY_COLUMN] = rk.values
    out[MODEL_NAME_COLUMN] = df[MODEL_NAME_COLUMN].astype(str).values

    for variable, col in present.items():
        target_name = predicted_column(variable)
        raw = df[col]
        numeric = pd.to_numeric(raw, errors="coerce")
        # A present-but-non-numeric cell is a contract violation, not a silent NaN.
        bad = numeric.isna() & raw.notna() & (raw.astype(str).str.strip() != "")
        if bad.any():
            i = int(bad.idxmax())
            raise InvalidPredictionValue(
                f"column {col!r} row {i}: value {raw.iloc[i]!r} is not numeric.")

        element = _element_for(variable)
        target_unit = _target_unit(variable)
        declared = units_map.get(col)
        if declared and declared != target_unit and element is not None:
            converted, meta = units.convert_series(numeric, declared, target_unit, element)
            out[target_name] = converted.values
            out[f"{target_name}{CONVERSION_ID_SUFFIX}"] = meta.conversion_id
            out[f"{target_name}{ORIG_UNIT_SUFFIX}"] = declared
        else:
            out[target_name] = numeric.values
            out[f"{target_name}{CONVERSION_ID_SUFFIX}"] = units.IDENTITY_ID
            out[f"{target_name}{ORIG_UNIT_SUFFIX}"] = declared or target_unit

    for col in METADATA_COLUMNS:
        if col in df.columns:
            out[col] = df[col].values
    if "scenario_label" in df.columns:
        out["scenario_label"] = df["scenario_label"].values

    return out.reset_index(drop=True)
