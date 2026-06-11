"""Central configuration: all filesystem paths and domain constants live here.

Keeping paths in one module means scripts and notebooks never hard-code directory
layout, and re-pointing the pipeline at a different dataset is a one-line change.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Directory layout
# --------------------------------------------------------------------------- #
# config.py lives at <repo>/flyash_phreeqc_ml/config.py, so the repo root is two
# levels up.
PACKAGE_DIR: Path = Path(__file__).resolve().parent
PROJECT_ROOT: Path = PACKAGE_DIR.parent

DATA_DIR: Path = PROJECT_ROOT / "data"
RAW_DIR: Path = DATA_DIR / "raw"
PROCESSED_DIR: Path = DATA_DIR / "processed"

REPORTS_DIR: Path = PROJECT_ROOT / "reports"
FIGURES_DIR: Path = REPORTS_DIR / "figures"

# Generated, re-creatable tabular outputs (validation reports, scores, plans).
# Kept separate from data/processed/ so it is obvious these are analysis tables,
# not pipeline datasets. Gitignored like the other generated artifacts.
OUTPUTS_DIR: Path = PROJECT_ROOT / "outputs"
TABLES_DIR: Path = OUTPUTS_DIR / "tables"

# Experiment-run "save files" (app-level run manager). Each run is a subfolder
# under here holding its own run_config.yaml, data/, and outputs/. This is an
# app-level save/open layer; it does NOT replace the data/raw/experimental_icp
# pipeline workflow. Run data is gitignored by default (see .gitignore).
EXPERIMENT_RUNS_DIR: Path = PROJECT_ROOT / "experiments"

# Raw sub-directories (names contain spaces, matching the delivered dataset).
PHREEQC_INPUT_DIR: Path = RAW_DIR / "PHREEQC inputs"
PHREEQC_OUTPUT_DIR: Path = RAW_DIR / "PHREEQC outputs"
ICP_DIR: Path = RAW_DIR / "experimental icp"  # the CFA+MK mix-design workbook

# Phase 2: measured experimental release data (filled from the lab/ICP results).
# Note the underscore — this is a new directory, distinct from the space-named
# "experimental icp" folder that holds the mix-design workbook.
EXPERIMENTAL_ICP_DIR: Path = RAW_DIR / "experimental_icp"

# --------------------------------------------------------------------------- #
# Processed output file names (written to PROCESSED_DIR)
# --------------------------------------------------------------------------- #
PHREEQC_INPUT_SOLUTIONS_CSV = "phreeqc_input_solutions.csv"
PHREEQC_EQUILIBRIUM_PHASES_CSV = "phreeqc_input_equilibrium_phases.csv"
PHREEQC_RESULTS_CSV = "phreeqc_results.csv"
PHREEQC_SCENARIO_MANIFEST_CSV = "phreeqc_scenario_manifest.csv"  # mapping-assistant view
PHREEQC_SI_CSV = "phreeqc_saturation_indices.csv"
PHREEQC_ASSEMBLAGE_CSV = "phreeqc_phase_assemblage.csv"
MASTER_DATASET_CSV = "master_dataset.csv"

# Phase 2 artifacts.
EXPERIMENTAL_TEMPLATE_CSV = "experimental_release_template.csv"  # blank header template
SAMPLE_PHREEQC_MAP_CSV = "sample_phreeqc_map.csv"               # sample_id -> record_key
EXPERIMENTAL_RELEASE_CSV = "experimental_release.csv"            # tidy, parsed measured data
COMPARISON_CSV = "comparison_measured_vs_phreeqc.csv"            # joined + residuals

# Experiment-planning / QA-QC artifacts.
# The generated run sheet is reusable for any experiment run (not one session).
EXPERIMENT_PLAN_CSV = "experiment_plan.csv"                      # generated run sheet
EXPERIMENTAL_VALIDATION_REPORT_CSV = "experimental_validation_report.csv"
SUSTAINABILITY_SCORE_CSV = "sustainability_score.csv"

# Files in EXPERIMENTAL_ICP_DIR that are NOT measured-release data and must be
# skipped when loading the directory. The generated experiment plan is a blank
# run sheet (different schema), so it is skipped too.
EXPERIMENTAL_NON_DATA_FILES = {
    EXPERIMENTAL_TEMPLATE_CSV,
    SAMPLE_PHREEQC_MAP_CSV,
    EXPERIMENT_PLAN_CSV,
}

# CO2-condition / cup-cover vocabulary.
#
# The experiment controls CO2 exposure with **cup covers**, encoded as condition
# codes (this is the experimental fact, not an assumption):
#   OA = open air         — directly exposed to atmospheric CO2
#   PF = plastic flap cover — covered cup, *likely* reduced CO2 exchange
#   GS = glass cover        — covered cup, *likely* reduced CO2 exchange
# PF and GS are NOT confirmed airtight: nothing in code/UI/plots/docs may call them
# "sealed". The model side (PHREEQC scenarios) uses atm_CO2 / low_CO2 / no_CO2.
CO2_CONDITION_ALLOWED = ["OA", "PF", "GS", "atm_CO2", "low_CO2", "no_CO2", "unknown"]

# Single source of truth for the human-readable condition descriptions + the
# not-confirmed-sealed caution. The UI reads this dict (it never hard-codes the
# wording), and only shows it for datasets that actually use these codes.
_PF_GS_CAUTION = "Not confirmed airtight — do not treat as sealed."
CONDITION_CODE_DESCRIPTIONS = {
    "OA": {"label": "open air",
           "description": "Open air — directly exposed to atmospheric CO2.",
           "caution": ""},
    "PF": {"label": "plastic flap cover",
           "description": "Plastic flap cover — covered cup, likely reduced CO2 exchange.",
           "caution": _PF_GS_CAUTION},
    "GS": {"label": "glass cover",
           "description": "Glass cover — covered cup, likely reduced CO2 exchange.",
           "caution": _PF_GS_CAUTION},
    "atm_CO2": {"label": "atmospheric CO2 (model)",
                "description": "PHREEQC scenario at atmospheric CO2.", "caution": ""},
    "low_CO2": {"label": "low CO2 (model)",
                "description": "PHREEQC scenario at reduced CO2.", "caution": ""},
    "no_CO2": {"label": "no CO2 ingress (model)",
               "description": "PHREEQC scenario with no CO2 ingress.", "caution": ""},
    "unknown": {"label": "unknown",
                "description": "CO2 condition not specified.", "caution": ""},
}

# --------------------------------------------------------------------------- #
# Domain constants
# --------------------------------------------------------------------------- #
# Elements we care about in the input/output composition tables.
KEY_ELEMENTS = ["Na", "Si", "Al", "Ca", "Fe", "C"]

# Mineral phases of primary interest for carbonation / leaching analysis.
# Used to pick a compact, consistent set of saturation-index columns.
KEY_PHASES = [
    "Cal",          # calcite
    "Arg",          # aragonite
    "Portlandite",  # Ca(OH)2
    "CO2(g)",       # dissolved/atmospheric CO2
    "Qtz",          # quartz (Si)
    "Amor-Sl",      # amorphous silica
    "Gbs",          # gibbsite (Al)
    "AlOHam",       # amorphous Al(OH)3
    "Kln",          # kaolinite
]


# --------------------------------------------------------------------------- #
# Phase 2: experimental release template schema
# --------------------------------------------------------------------------- #
# Canonical column order for the measured-experimental-release template/file.
# Editing this list is the single source of truth for the CSV schema, the parser,
# and the tests.
EXPERIMENTAL_RELEASE_COLUMNS = [
    "sample_id",
    "experiment_date",
    "fly_ash_type",
    "NaOH_M",
    "time_min",
    "temperature_C",
    "liquid_solid_ratio",
    "CO2_condition",
    "initial_pH",
    "final_pH",
    "conductivity_mS_cm",
    "Ca_mM",
    "Si_mM",
    "Al_mM",
    "Fe_mM",
    "Na_mM",
    "K_mM",
    "Sc_ppb",
    "total_REE_ppb",
    "filtration_notes",
    "precipitate_observed",
    "notes",
]

# Numeric columns within the template (everything else is text/categorical).
EXPERIMENTAL_NUMERIC_COLUMNS = [
    "NaOH_M",
    "time_min",
    "temperature_C",
    "liquid_solid_ratio",
    "initial_pH",
    "final_pH",
    "conductivity_mS_cm",
    "Ca_mM",
    "Si_mM",
    "Al_mM",
    "Fe_mM",
    "Na_mM",
    "K_mM",
    "Sc_ppb",
    "total_REE_ppb",
]

# --------------------------------------------------------------------------- #
# Phase 2: measured <-> PHREEQC residual definitions
# --------------------------------------------------------------------------- #
# PHREEQC reports element totals as molality (mol/kgw). For dilute solutions
# mol/kgw ~= mol/L, so multiplying by 1000 gives mM, matching the measured units.
PHREEQC_MOLALITY_TO_MM = 1000.0

# Each residual = measured - phreeqc, defined by (measured_col, phreeqc_source).
# For elements, phreeqc_source is the molality column converted to mM; for pH it is
# the PHREEQC pH directly.
RESIDUAL_ELEMENTS = ["Ca", "Si", "Al", "Fe"]  # measured_<X>_mM vs phreeqc mol_<X>


# --------------------------------------------------------------------------- #
# PHREEQC execution (Prompt 11 — the on-demand simulation runner)
# --------------------------------------------------------------------------- #
# The PHREEQC binary + database are **user-supplied and never committed**. The
# CEMDATA18 database the project uses is not redistributable. Both are read from
# the environment so a deployment configures them without code changes; when
# unset, the runner raises a typed PhreeqcNotConfiguredError with setup guidance.
PHREEQC_EXE_PATH: str = os.environ.get("PHREEQC_EXE", "phreeqc")  # CLI name / abs path
PHREEQC_DATABASE_PATH: str | None = os.environ.get("PHREEQC_DATABASE") or None
PHREEQC_RUN_TIMEOUT_S: float = float(os.environ.get("PHREEQC_TIMEOUT_S", "120"))

# Solution chemistry the template cannot know from a measured condition's metadata
# alone (fly-ash release is not measured up front). These are **ASSUMED** defaults
# — surfaced verbatim in the generated .pqi comments and the UI preview, never
# silently. Sourced from the L-S_5.pqi solution-1 composition (mol/L).
ASSUMED_STOCK_SOLUTION = {"Si": 0.00031, "Al": 0.00073, "Ca": 0.00233}
ASSUMED_TEMPERATURE_C = 25.0
ASSUMED_DENSITY = 1.0
ASSUMED_PH = 13.0  # used only when a condition has no measured pH

# CO₂ cup-cover semantics → PHREEQC EQUILIBRIUM_PHASES CO2(g) encoding, decoded
# from the real data/raw inputs+outputs:
#   atm  → CO2(g) at log pCO2 -3.37, large reservoir (full atmospheric equilibration)
#   low  → same target SI, tiny depletable reservoir 0.001 mol (reduced exchange)
#   none → no CO2(g) phase at all (sealed)
ATM_CO2_LOG_PCO2 = -3.37
# model scenario label -> (CO2(g) saturation index | None, reservoir mol | None)
CO2_SCENARIO_ENCODING = {
    "atm_CO2": (ATM_CO2_LOG_PCO2, 10.0),
    "low_CO2": (ATM_CO2_LOG_PCO2, 0.001),
    "no_CO2": (None, None),
}
# Cup-cover code (experiment side) -> the model scenario(s) to generate. PF/GS are
# covered but NOT confirmed sealed, so we generate BOTH a low- and a no-CO₂ variant.
COVER_TO_CO2_SCENARIOS = {
    "OA": ["atm_CO2"],
    "PF": ["low_CO2", "no_CO2"],
    "GS": ["low_CO2", "no_CO2"],
}
# Short {atm,low,none} aliases used by the surrogate sampler's CO₂ axis.
CO2_SCENARIO_ALIASES = {"atm": "atm_CO2", "low": "low_CO2", "none": "no_CO2"}

# Tag columns written into phreeqc_results.csv / the manifest for generated rows,
# so generated scenarios are distinguishable from hand-built ones everywhere.
GENERATED_FLAG_COLUMN = "generated"
GENERATED_SOURCE_COLUMN = "source_condition_key"
GENERATED_AT_COLUMN = "generated_at"
# Generated-scenario metadata carried alongside (lets the manifest use the exact
# condition metadata instead of filename inference, so OA can reach an exact map).
GENERATED_META_COLUMNS = {
    "NaOH_M": "gen_NaOH_M",
    "liquid_solid_ratio": "gen_liquid_solid_ratio",
    "CO2_condition": "gen_CO2_condition",
    "temperature_C": "gen_temperature_C",
    "time_min": "gen_time_min",
}

# --------------------------------------------------------------------------- #
# Surrogate input space (Prompt 12 — Latin-hypercube sampling of PHREEQC)
# --------------------------------------------------------------------------- #
# The validity domain the surrogate is trained over. Ranges are inclusive; the
# CO₂ axis is categorical over the model scenario set.
SURROGATE_INPUT_SPACE = {
    "NaOH_M": (0.1, 5.0),
    "liquid_solid_ratio": (2.0, 20.0),
    "temperature_C": (15.0, 60.0),
    "co2_scenario": ["atm", "low", "none"],   # sampled categorically
}
# Output variables the surrogate learns (parsed from each run's batch state).
SURROGATE_OUTPUTS = ["pH", "Ca_mM", "Si_mM", "Al_mM", "Fe_mM", "Na_mM", "K_mM",
                     "SI_Cal", "SI_Portlandite"]  # SI_<phase> from config.KEY_PHASES
# Above this sample count, prefer HistGradientBoosting (quantile) over a GP.
SURROGATE_GP_MAX_SAMPLES = 2000


def ensure_output_dirs() -> None:
    """Create the generated-artifact directories if they do not yet exist."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)


def ensure_tables_dir() -> None:
    """Create the generated-tables directory (outputs/tables) if needed."""
    TABLES_DIR.mkdir(parents=True, exist_ok=True)
