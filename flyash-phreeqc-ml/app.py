"""Streamlit interface for the flyash-phreeqc-ml project.

A thin GUI on top of the existing Phase 1 / Phase 2 code — it does **not**
reimplement any pipeline logic. It lets you:

* see project status at a glance,
* run the existing scripts (Phase 1 pipeline, Phase 2 comparison) and view their output,
* preview processed CSVs,
* enter measured experimental data into a form (appended to a CSV, never overwritten),
* view generated figures.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Make the package importable when Streamlit runs this file directly.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from flyash_phreeqc_ml import config  # noqa: E402
from flyash_phreeqc_ml.parsers import (  # noqa: E402
    has_measured_data,
    load_experimental_release,
)

# The form appends here. Kept out of git (see .gitignore) so manually-entered
# measured data is never committed by accident.
MANUAL_ENTRY_FILENAME = "experimental_release_manual_entry.csv"
MANUAL_ENTRY_PATH = config.EXPERIMENTAL_ICP_DIR / MANUAL_ENTRY_FILENAME

# Processed CSVs surfaced first in the data viewer.
PREFERRED_PROCESSED = [
    config.MASTER_DATASET_CSV,
    config.PHREEQC_RESULTS_CSV,
    config.PHREEQC_SI_CSV,
    config.PHREEQC_ASSEMBLAGE_CSV,
]

# Free-text columns get plain text inputs; a couple get friendly dropdowns.
_CO2_OPTIONS = ["", "none", "atmospheric", "elevated", "sealed"]
_YESNO_OPTIONS = ["", "yes", "no"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _run_script(relative_path: str) -> subprocess.CompletedProcess:
    """Run a project script with the current interpreter, capturing output."""
    return subprocess.run(
        [sys.executable, relative_path],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
    )


def _show_process_result(label: str, proc: subprocess.CompletedProcess) -> None:
    if proc.returncode == 0:
        st.success(f"{label} finished (exit code 0).")
    else:
        st.error(f"{label} failed (exit code {proc.returncode}).")
    if proc.stdout:
        st.text_area("stdout", proc.stdout, height=220)
    if proc.stderr:
        st.text_area("stderr", proc.stderr, height=160)


@st.cache_data(show_spinner=False)
def _read_csv(path_str: str, mtime: float) -> pd.DataFrame:
    """Read a CSV, cache-keyed on path + mtime so edits invalidate the cache."""
    return pd.read_csv(path_str)


def _load_measured_safe() -> pd.DataFrame:
    try:
        # Non-strict so a partially-filled manual file still loads in the UI.
        return load_experimental_release(strict=False)
    except Exception as exc:  # pragma: no cover - defensive UI guard
        st.warning(f"Could not load experimental data: {exc}")
        return pd.DataFrame()


def _figure_dirs() -> list[Path]:
    """Where plots may live. Pipeline writes to reports/figures; outputs/figures
    is checked too since the task referred to it."""
    return [config.FIGURES_DIR, _PROJECT_ROOT / "outputs" / "figures"]


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="flyash-phreeqc-ml", layout="wide")
st.title("flyash-phreeqc-ml — control panel")
st.caption(
    "A GUI over the existing Phase 1 / Phase 2 scripts. It does not change the "
    "chemistry or train any model."
)

# ---- 1. Project status ---------------------------------------------------- #
st.header("1. Project status")

master_path = config.PROCESSED_DIR / config.MASTER_DATASET_CSV
template_path = config.EXPERIMENTAL_ICP_DIR / config.EXPERIMENTAL_TEMPLATE_CSV
measured = _load_measured_safe()
measured_exists = has_measured_data(measured)

c1, c2, c3, c4 = st.columns(4)
with c1:
    st.metric("master_dataset.csv", "present" if master_path.exists() else "missing")
with c2:
    n_rows = len(_read_csv(str(master_path), master_path.stat().st_mtime)) if master_path.exists() else 0
    st.metric("master rows", n_rows)
with c3:
    st.metric("release template", "present" if template_path.exists() else "missing")
with c4:
    st.metric("measured data", "yes" if measured_exists else "not yet")

if not measured_exists:
    st.info(
        "No measured experimental release data found yet — only the blank template. "
        "Phase 2 comparison and any future ML stay dormant until real data is entered."
    )

# ---- 2. Run pipeline ------------------------------------------------------ #
st.header("2. Run pipeline")
st.write(
    "These buttons call the existing scripts unchanged "
    "(`scripts/run_phase1.py`, `scripts/05_compare_experimental.py`)."
)
rc1, rc2 = st.columns(2)
with rc1:
    if st.button("Run Phase 1 pipeline", use_container_width=True):
        with st.spinner("Running Phase 1 (parse → processed CSVs → master → plots)…"):
            proc = _run_script("scripts/run_phase1.py")
        _show_process_result("Phase 1", proc)
        _read_csv.clear()  # processed CSVs may have changed
with rc2:
    if st.button("Run Phase 2 comparison", use_container_width=True):
        with st.spinner("Running Phase 2 (measured vs PHREEQC)…"):
            proc = _run_script("scripts/05_compare_experimental.py")
        _show_process_result("Phase 2", proc)
        _read_csv.clear()

# ---- 3. View processed data ----------------------------------------------- #
st.header("3. View processed data")
if not config.PROCESSED_DIR.exists():
    st.warning("`data/processed/` does not exist yet — run Phase 1 first.")
else:
    csvs = sorted(p.name for p in config.PROCESSED_DIR.glob("*.csv"))
    if not csvs:
        st.warning("No processed CSVs yet — run Phase 1 first.")
    else:
        # Show preferred files first, then the rest.
        ordered = [c for c in PREFERRED_PROCESSED if c in csvs] + [
            c for c in csvs if c not in PREFERRED_PROCESSED
        ]
        choice = st.selectbox("Processed CSV", ordered)
        path = config.PROCESSED_DIR / choice
        df = _read_csv(str(path), path.stat().st_mtime)
        st.write(f"{df.shape[0]} rows × {df.shape[1]} columns")
        st.dataframe(df, use_container_width=True, height=380)

# ---- 4. Enter experimental data ------------------------------------------- #
st.header("4. Enter experimental data")
st.write(
    f"Submitting appends one row to `{MANUAL_ENTRY_PATH.relative_to(_PROJECT_ROOT)}` "
    "(existing rows are never overwritten). Leave a field blank if not measured."
)

with st.form("experimental_entry", clear_on_submit=True):
    inputs: dict[str, str] = {}
    cols = st.columns(3)
    for i, column in enumerate(config.EXPERIMENTAL_RELEASE_COLUMNS):
        widget_col = cols[i % 3]
        numeric = column in config.EXPERIMENTAL_NUMERIC_COLUMNS
        label = f"{column} (number)" if numeric else column
        if column == "CO2_condition":
            inputs[column] = widget_col.selectbox(column, _CO2_OPTIONS)
        elif column == "precipitate_observed":
            inputs[column] = widget_col.selectbox(column, _YESNO_OPTIONS)
        else:
            inputs[column] = widget_col.text_input(label, value="")
    submitted = st.form_submit_button("Save row")

if submitted:
    # Validate numeric columns; blanks are allowed (treated as not-measured).
    errors: list[str] = []
    for column in config.EXPERIMENTAL_NUMERIC_COLUMNS:
        raw = (inputs.get(column) or "").strip()
        if raw == "":
            continue
        try:
            float(raw)
        except ValueError:
            errors.append(f"'{column}' must be a number (got '{raw}').")

    if not inputs.get("sample_id", "").strip():
        errors.append("'sample_id' is required.")

    if errors:
        for e in errors:
            st.error(e)
    else:
        row = {col: (inputs.get(col) or "").strip() for col in config.EXPERIMENTAL_RELEASE_COLUMNS}
        new_df = pd.DataFrame([row], columns=config.EXPERIMENTAL_RELEASE_COLUMNS)
        MANUAL_ENTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        write_header = not MANUAL_ENTRY_PATH.exists()
        new_df.to_csv(MANUAL_ENTRY_PATH, mode="a", header=write_header, index=False)

        total = len(pd.read_csv(MANUAL_ENTRY_PATH))
        st.success(
            f"Saved sample '{row['sample_id']}'. "
            f"{MANUAL_ENTRY_PATH.name} now has {total} row(s)."
        )
        st.dataframe(new_df, use_container_width=True)
        _read_csv.clear()

if MANUAL_ENTRY_PATH.exists():
    with st.expander("Current manual-entry file"):
        existing = pd.read_csv(MANUAL_ENTRY_PATH)
        st.dataframe(existing, use_container_width=True)

# ---- 5. View figures ------------------------------------------------------ #
st.header("5. View figures")
pngs = [p for d in _figure_dirs() if d.exists() for p in sorted(d.glob("*.png"))]
if not pngs:
    st.warning("No figures yet — run Phase 1 (and Phase 2 once measured data exists).")
else:
    for png in pngs:
        st.image(str(png), caption=str(png.relative_to(_PROJECT_ROOT)), use_container_width=True)

# ---- 6. Experiment planning ----------------------------------------------- #
st.header("6. Experiment planning")
st.write(
    "Pre-data helpers for Monday: generate the run sheet, QA/QC a filled CSV, and "
    "compute sustainability proxies. These call the existing scripts unchanged "
    "(`scripts/06_…`, `07_…`, `08_…`) and train no model."
)
ep1, ep2, ep3 = st.columns(3)
with ep1:
    if st.button("Generate Monday experiment plan", use_container_width=True):
        with st.spinner("Generating experiment plan…"):
            proc = _run_script("scripts/06_generate_experiment_plan.py")
        _show_process_result("Experiment plan", proc)
with ep2:
    if st.button("Validate experimental CSVs", use_container_width=True):
        with st.spinner("Validating experimental data…"):
            proc = _run_script("scripts/07_validate_experimental_data.py")
        _show_process_result("Validation", proc)
with ep3:
    if st.button("Run sustainability score", use_container_width=True):
        with st.spinner("Scoring sustainability proxies…"):
            proc = _run_script("scripts/08_sustainability_score.py")
        _show_process_result("Sustainability score", proc)

# Surface the generated tables if they exist.
for _label, _name in [
    ("Validation report", config.EXPERIMENTAL_VALIDATION_REPORT_CSV),
    ("Sustainability score", config.SUSTAINABILITY_SCORE_CSV),
]:
    _path = config.TABLES_DIR / _name
    if _path.exists():
        with st.expander(f"{_label} — {_name}"):
            st.dataframe(
                _read_csv(str(_path), _path.stat().st_mtime),
                use_container_width=True,
            )

# ---- 7. Safety and limitations -------------------------------------------- #
st.header("7. Safety and limitations")
st.warning(
    "- **PHREEQC is equilibrium / speciation modelling.** Its outputs are "
    "thermodynamic predictions, not direct measurements, and assume the modelled "
    "system reached equilibrium.\n"
    "- **No ML is trained until real measured experimental release data exists.** "
    "The interface and Phase 2 comparison are scaffolding; predictions remain NaN "
    "until measured data and a sample→PHREEQC mapping are provided.\n"
    "- **Entering a value here does not make it scientifically valid.** Check units, "
    "detection limits, dilution factors, and experimental metadata before trusting "
    "any comparison or residual. Garbage in, garbage out."
)
