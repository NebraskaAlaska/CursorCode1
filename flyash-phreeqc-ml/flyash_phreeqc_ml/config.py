"""Central configuration: all filesystem paths and domain constants live here.

Keeping paths in one module means scripts and notebooks never hard-code directory
layout, and re-pointing the pipeline at a different dataset is a one-line change.
"""
from __future__ import annotations

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

# Raw sub-directories (names contain spaces, matching the delivered dataset).
PHREEQC_INPUT_DIR: Path = RAW_DIR / "PHREEQC inputs"
PHREEQC_OUTPUT_DIR: Path = RAW_DIR / "PHREEQC outputs"
ICP_DIR: Path = RAW_DIR / "experimental icp"

# --------------------------------------------------------------------------- #
# Processed output file names (written to PROCESSED_DIR)
# --------------------------------------------------------------------------- #
PHREEQC_INPUT_SOLUTIONS_CSV = "phreeqc_input_solutions.csv"
PHREEQC_EQUILIBRIUM_PHASES_CSV = "phreeqc_input_equilibrium_phases.csv"
PHREEQC_RESULTS_CSV = "phreeqc_results.csv"
PHREEQC_SI_CSV = "phreeqc_saturation_indices.csv"
PHREEQC_ASSEMBLAGE_CSV = "phreeqc_phase_assemblage.csv"
MASTER_DATASET_CSV = "master_dataset.csv"

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


def ensure_output_dirs() -> None:
    """Create the generated-artifact directories if they do not yet exist."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
