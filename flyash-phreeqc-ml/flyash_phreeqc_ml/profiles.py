"""Dataset + model **profiles** — the first generalization layer (additive only).

The pipeline was written for *Class C fly ash + PHREEQC*. To begin generalizing
beyond that pairing **without renaming the package or breaking the working pipeline**,
this module introduces two small, immutable description objects:

* :class:`DatasetProfile` — what a measured dataset looks like: its id/time/replicate
  columns, variable columns + units, the condition column and its code dictionary
  (e.g. the OA/PF/GS cup covers), the fields + numeric tolerances that matter for
  mapping, and how to parse a sample id.
* :class:`ModelProfile` — what the prediction model is: its display *name* (so UI
  strings like "needs new {model} simulation" are profile-driven), its prediction
  metadata fields, and its parser entry point.

The existing ``config.py`` constants remain the **single source of truth**; the
fly-ash / PHREEQC profiles here only *reference* them. Profiles are threaded through
the existing seams with a fly-ash default, so all current behaviour is unchanged —
passing a different profile is what makes the same code work for another dataset.
Nothing here does chemistry or ML.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config, units


@dataclass(frozen=True)
class DatasetProfile:
    """How one measured dataset is shaped (columns, codes, mapping fields, tolerances)."""

    name: str
    id_column: str = "sample_id"
    time_column: str | None = "time_min"
    replicate_column: str = "replicate_id"
    # Regex that pulls a replicate number out of a sample id (R1 / rep2 / batch3 …).
    replicate_pattern: str = r"(?:^|[-_ ])(?:R|REP|REPLICATE|BATCH)\s*[-_]?\s*(\d+)\b"
    # --- Batch semantics (additive; see replicates.REPLICATE_ROLE_DEFINITIONS) ---
    # A distinct *preparation/batch* of the same condition (not a time point, not a
    # repeat measurement). The batch id is read from ``batch_column`` if present, else
    # parsed from the sample id with ``batch_pattern`` (group 1). When ``group_by_batch``
    # is True the batch becomes part of ``condition_key`` (batches compared separately);
    # otherwise batches fold into the condition like true replicates.
    batch_column: str | None = None
    batch_pattern: str | None = None
    group_by_batch: bool = False
    # Optional regex for parsing the whole sample id (documentation / future use).
    sample_id_pattern: str | None = None
    # The column that carries the experimental condition code (cup cover for fly ash).
    condition_column: str = "CO2_condition"
    # code -> {"description": str, "caution": str}. The UI reads this dict.
    condition_codes: dict = field(default_factory=dict)
    # Measured variable columns and their units (display only).
    variable_columns: tuple = ()
    variable_units: dict = field(default_factory=dict)
    # Variables offered in the measured-data overview (subset that makes sense to plot).
    overview_variables: tuple = ()
    # Fields used to group replicates into a condition and to judge a mapping.
    important_fields: tuple = ()
    # Per-field numeric tolerance for "matches" decisions.
    tolerances: dict = field(default_factory=dict)
    # variable -> (measured_column, model_prediction_column) for the comparison.
    comparison_variable_spec: dict = field(default_factory=dict)
    # "fly_ash" keeps the bespoke leachant/CO2 condition_key; anything else uses the
    # generic important-fields key builder.
    grouping: str = "generic"
    # --- Residual-correction model feature encoding (additive; used by ml.residual_model) ---
    # Raw numeric metadata columns used as continuous features (missing values imputed).
    feature_numeric_fields: tuple = ()
    # Categorical feature names. Names ``leachant_family`` / ``condition_code`` are
    # *derived* (via scenarios); any other name is read straight from that column.
    feature_categorical_fields: tuple = ()
    # --- Import unit contract (additive; used by import_mapping) ---
    # Per convertible column, the source units the importer accepts. An undeclared unit
    # is refused (no guess). Empty -> the importer falls back to the units.py default set.
    accepted_units: dict = field(default_factory=dict)
    # --- Batch-reaction mass balance (additive; used by mass_balance) ---
    # Empty `mass_balance_elements` = the feature is OFF for this dataset (the default,
    # so FLY_ASH_PROFILE is unchanged). Declaring elements opts a dataset in; the column
    # names default to the config batch schema and the units are declared, not guessed.
    mass_balance_elements: tuple = ()                 # e.g. ("Ca", "Si", "Al", "Fe")
    material_mass_column: str = "material_mass_g"
    solid_mass_column: str = "solid_mass_g"           # optional in data; flagged if absent
    liquid_volume_column: str = "liquid_volume_mL"
    starting_content_unit: str = "wt%"                # unit of {el}_starting_content
    solid_residue_unit: str = "wt%"                   # unit of {el}_solid_residue
    liquid_conc_unit: str = "mM"                      # unit of the measured liquid {el}_mM
    # --- Gap attribution via PHREEQC (Prompt 24; additive) ---
    # Candidate precipitate phases (PHREEQC phase name -> element) added to
    # EQUILIBRIUM_PHASES and read back from the selected output.
    mass_balance_candidate_phases: dict = field(default_factory=dict)
    # CONFIRMED for the fly-ash filtration protocol = False: a PHREEQC-predicted
    # precipitate leaves with the filtrate and is NOT in the measured solid residue, so
    # it counts toward EXPLAINING the gap (attribution_to_gap = min(P, gap)). Set True
    # for a protocol where precipitates are retained in the assayed solid (then they are
    # already in n_solid and do not reduce the gap). See docs/mass_balance.md.
    precipitate_in_measured_solid: bool = False


@dataclass(frozen=True)
class ModelProfile:
    """How the prediction model is described (name + metadata + parser entry point).

    ``parser_entry_point`` is the dotted module path of the parser that turns this
    model's raw output into a frame :func:`scenarios.build_scenario_manifest` accepts.
    Carrying it on the profile is what lets the app stay model-agnostic: the manifest
    (and everything downstream) does not care which parser produced the predictions.
    """

    name: str
    # Metadata columns carried from the model side (shown in advanced views).
    prediction_metadata_fields: tuple = ()
    # Dotted path to the parser that turns raw model output into a tidy frame.
    parser_entry_point: str = ""
    # How the model's output reaches the app: a parsed file format (PHREEQC .pqo) or a
    # generic prediction CSV. The Data tab uses this to choose the import path.
    source_kind: str = "phreeqc"

    def load_parser(self):
        """Import and return the parser module named by ``parser_entry_point``."""
        if not self.parser_entry_point:
            raise ValueError(f"model profile {self.name!r} has no parser_entry_point")
        import importlib
        return importlib.import_module(self.parser_entry_point)


# --------------------------------------------------------------------------- #
# Fly-ash dataset profile — populated from config.py (the single source of truth)
# --------------------------------------------------------------------------- #
# Experiment-side cup-cover codes (OA/PF/GS), referenced from the config dict.
_FLY_ASH_CONDITION_CODES = {
    code: config.CONDITION_CODE_DESCRIPTIONS[code]
    for code in ("OA", "PF", "GS")
}

# Measured variables shown in the overview: pH + the ICP element columns.
_FLY_ASH_OVERVIEW_VARIABLES = (
    "final_pH", "Ca_mM", "Si_mM", "Al_mM", "Fe_mM",
    "Na_mM", "K_mM", "Sc_ppb", "total_REE_ppb",
)
_FLY_ASH_VARIABLE_UNITS = {
    "final_pH": "pH",
    "Ca_mM": "mM", "Si_mM": "mM", "Al_mM": "mM", "Fe_mM": "mM",
    "Na_mM": "mM", "K_mM": "mM", "Sc_ppb": "ppb", "total_REE_ppb": "ppb",
}
# Comparison variable -> (measured_column, model_prediction_column). The element
# residuals come straight from config.RESIDUAL_ELEMENTS; pH is added explicitly.
_FLY_ASH_COMPARISON_SPEC = {
    "final_pH": ("final_pH", "phreeqc_pH"),
    **{f"{el}_mM": (f"{el}_mM", f"phreeqc_{el}_mM") for el in config.RESIDUAL_ELEMENTS},
}

FLY_ASH_PROFILE = DatasetProfile(
    name="Class C fly ash",
    id_column="sample_id",
    time_column="time_min",
    condition_column="CO2_condition",
    condition_codes=_FLY_ASH_CONDITION_CODES,
    variable_columns=tuple(config.EXPERIMENTAL_NUMERIC_COLUMNS),
    variable_units=_FLY_ASH_VARIABLE_UNITS,
    overview_variables=_FLY_ASH_OVERVIEW_VARIABLES,
    # Fields the bespoke condition_key + mapping use (leachant/molarity/cover/time/L:S).
    important_fields=("leachant", "NaOH_M", "acid_M", "CO2_condition",
                      "time_min", "liquid_solid_ratio", "temperature_C"),
    tolerances={"liquid_solid_ratio": 1e-6, "temperature_C": 1.0},
    comparison_variable_spec=_FLY_ASH_COMPARISON_SPEC,
    grouping="fly_ash",
    # Residual-model features: molarity, L/S, time (numeric) + leachant family and the
    # OA/PF/GS cover code (one-hot). Time is imputed when an exact mapping omits it.
    feature_numeric_fields=("NaOH_M", "liquid_solid_ratio", "time_min"),
    feature_categorical_fields=("leachant_family", "condition_code"),
    # Each convertible ICP column accepts mg/L, ppm, ppb, or mM (the lab-import set).
    accepted_units={f"{el}_mM": units.LAB_CONCENTRATION_SOURCE_UNITS
                    for el in config.RESIDUAL_ELEMENTS + ["Na", "K"]},
)


# --------------------------------------------------------------------------- #
# PHREEQC model profile
# --------------------------------------------------------------------------- #
PHREEQC_PROFILE = ModelProfile(
    name="PHREEQC",
    prediction_metadata_fields=("source_file", "simulation", "state", "solution_number"),
    parser_entry_point="flyash_phreeqc_ml.parsers.pqo_parser",
    source_kind="phreeqc",
)


# A model-agnostic prediction source: any model can supply a CSV that meets the
# documented contract (see docs/model_prediction_format.md). The manifest builder
# consumes its parser's output exactly like PHREEQC's, proving generality.
GENERIC_CSV_PROFILE = ModelProfile(
    name="Generic model (CSV)",
    prediction_metadata_fields=("leachant", "NaOH_M", "time_min",
                                "liquid_solid_ratio", "CO2_condition", "temperature_C"),
    parser_entry_point="flyash_phreeqc_ml.parsers.generic_prediction_parser",
    source_kind="generic_csv",
)


def default_dataset_profile() -> DatasetProfile:
    """The dataset profile assumed when a caller passes none (keeps current behaviour)."""
    return FLY_ASH_PROFILE


def default_model_profile() -> ModelProfile:
    """The model profile assumed when a caller passes none."""
    return PHREEQC_PROFILE
