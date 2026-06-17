"""Shared cross-tab state for the UI layer — constants, paths, and cached data
readers. No tab-specific rendering lives here; tab modules import from it.

Extracted from app.py by the UI modularization refactor — see
docs/refactor_plan.md. Behavior is unchanged (verbatim move)."""
from __future__ import annotations

from pathlib import Path
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from ui.formatters import has_numeric as _has_numeric  # noqa: E402
from flyash_phreeqc_ml import config  # noqa: E402
from flyash_phreeqc_ml import profiles  # noqa: E402
from flyash_phreeqc_ml import run_manager  # noqa: E402
from flyash_phreeqc_ml import scenarios  # noqa: E402

# Make the package importable when Streamlit runs this file directly.
_PROJECT_ROOT = Path(__file__).resolve().parent

def _rel(path: Path) -> Path:
    """Display a path relative to the project root, or as-is if it lives elsewhere.

    Presentation-only: runs are normally under the repo, but a deployment may point
    ``EXPERIMENT_RUNS_DIR`` elsewhere — a caption must never crash on that.
    """
    try:
        return Path(path).relative_to(_PROJECT_ROOT)
    except ValueError:
        return Path(path)

# Active model profile — its display name drives generic UI strings ("needs new
# {MODEL_NAME} simulation"). PHREEQC for this project; swappable via ModelProfile.
MODEL_NAME = profiles.PHREEQC_PROFILE.name

# The form appends here. Kept out of git (see .gitignore) so manually-entered
# measured data is never committed by accident.
MANUAL_ENTRY_FILENAME = "experimental_release_manual_entry.csv"

@st.cache_data(show_spinner=False)
def _read_csv(path_str: str, mtime: float) -> pd.DataFrame:
    """Read a CSV, cache-keyed on path + mtime so edits invalidate the cache."""
    return pd.read_csv(path_str)

@st.cache_data(show_spinner=False)
def _scenario_manifest(results_path_str: str, mtime: float) -> pd.DataFrame:
    """Build (and persist) the PHREEQC scenario manifest, cached on results mtime."""
    manifest = scenarios.build_scenario_manifest(pd.read_csv(results_path_str))
    dest = scenarios.scenario_manifest_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    manifest.to_csv(dest, index=False)  # data/processed/ is gitignored
    return manifest

_ICP_MEASURED_COLS = ["Ca_mM", "Si_mM", "Al_mM", "Fe_mM", "Na_mM", "K_mM", "Sc_ppb", "total_REE_ppb"]

def _looks_like_run_test(data: pd.DataFrame) -> bool:
    """True if any sample_id in a run's data frame looks like mock/test data."""
    if data.empty or "sample_id" not in data.columns:
        return False
    sids = data["sample_id"].astype(str).str.upper()
    return bool(sids.str.contains("TEST|SYNTH|DEMO|MOCK", na=False, regex=True).any())

def _run_comparison_path(run_name: str | None) -> Path | None:
    """The selected run's per-run comparison CSV path, or None.

    Returns None when no run is selected or the run is not lab-like (so a
    literature/synthetic run can never surface another run's comparison).
    """
    if not run_name:
        return None
    try:
        return run_manager.comparison_path(run_name)
    except run_manager.RunManagerError:  # non-lab run_type, or no config
        return None

# Comparison figures get specific captions; everything else is a PHREEQC-only plot.
_FIGURE_CAPTIONS = {
    "measured_vs_phreeqc.png": (
        "This plot compares measured values against PHREEQC predictions. Points on "
        "the dashed 1:1 line would indicate perfect agreement. Points far from the "
        "line indicate model/experiment mismatch or incorrect mapping. Proximity to "
        "the 1:1 line indicates agreement only if the mapping is scientifically valid."
    ),
    "residuals_by_sample.png": (
        "This plot shows measured − PHREEQC. Positive values mean the measured value "
        "is higher than the PHREEQC prediction. Near-zero residuals indicate agreement "
        "only if the mapping is scientifically valid."
    ),
}

_COMPARISON_FIGURES = set(_FIGURE_CAPTIONS)

# --------------------------------------------------------------------------- #
# Presentation-status wording (validation workflow, not an overclaimed model)
# --------------------------------------------------------------------------- #
_PRELIMINARY_CAVEAT = (
    "Current measured-vs-model comparison should be treated as preliminary / workflow "
    "check only unless mappings are exact."
)

_VALID_NOW = [
    "Real Excel workbook import",
    "pH extraction",
    "Ca/Si/Al extraction",
    "Data validation",
    "Formula audit",
    "PHREEQC parsing",
    "Preliminary workflow comparison",
]

_NOT_VALID_YET = [
    "Time-resolved PHREEQC validation",
    "HCl comparison (until HCl PHREEQC scenarios are generated)",
    "CO₂-resolved PHREEQC validation of OA vs PF/GS cover conditions",
    "ML training",
]

def _manifest_if_available() -> pd.DataFrame:
    # A non-PHREEQC model's predictions (generic CSV) take precedence when present —
    # the manifest (and everything downstream) is model-agnostic.
    mp = config.PROCESSED_DIR / config.MODEL_PREDICTIONS_CSV
    if mp.exists():
        return _scenario_manifest(str(mp), mp.stat().st_mtime)
    rp = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    if rp.exists():
        return _scenario_manifest(str(rp), rp.stat().st_mtime)
    return pd.DataFrame(columns=scenarios.MANIFEST_COLUMNS)

# --------------------------------------------------------------------------- #
# Tab renderers — each is a self-contained view; all reuse the helpers above.
# --------------------------------------------------------------------------- #
def _next_step_hint(selected_run: str | None) -> str:
    """One recommended next action for the selected run (the Start checklist logic).

    Shared by Start and surfaced as a "next step" hint at the top of every tab, so a
    user who didn't build the app always knows where to go next. References the new
    tab names (Import / Match / Compare / Export).
    """
    if not selected_run:
        return "Create or open a run in the **Experiment runs** sidebar (left)."
    try:
        cfg = run_manager.load_run_config(selected_run)
    except run_manager.RunManagerError:
        return "Create or open a run in the **Experiment runs** sidebar (left)."
    rt = cfg.get("run_type")
    data = run_manager.read_data_file(selected_run)
    lab_like = rt in run_manager.LAB_LIKE_RUN_TYPES
    has_map = run_manager.has_mapping(selected_run) if lab_like else False
    map_summary = (run_manager.summarize_mapping(run_manager.read_mapping(selected_run))
                   if lab_like else {"has_collisions": False})
    icp_present = lab_like and any(_has_numeric(data, c) for c in _ICP_MEASURED_COLS)
    is_mock = _looks_like_run_test(data)
    comp_exists = lab_like and run_manager.has_comparison(selected_run)

    if data.empty:
        return ("Describe an experiment in the **Simulate** tab, or import measured data "
                "in the **Import Data** tab to validate against predictions.")
    if is_mock:
        return "Mock/test data — for code checking only, not scientific conclusions."
    if rt == "literature_benchmark":
        return ("Review the literature table in the **Import Data** tab — literature data are "
                "kept separate from lab data and are not run through the pipeline.")
    if rt == "synthetic_demo":
        return "This is a synthetic/demo run — for testing only, not scientific output."
    if lab_like and not has_map:
        return "Map measured data to model results in the **Match** tab."
    if lab_like and map_summary["has_collisions"]:
        return ("Review mapping in the **Match** tab — several samples share one model "
                "result, so graphs may be misleading.")
    if not comp_exists:
        return "Run the workflow in the **Compare Results** tab to generate results."
    if lab_like and not icp_present:
        return "Only pH comparison is meaningful until ICP data are added."
    return "Read the comparison in the **Compare Results** tab, then build a report in **Export**."
