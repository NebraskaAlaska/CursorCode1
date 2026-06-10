"""Streamlit interface for the flyash-phreeqc-ml project.

A thin GUI on top of the existing Phase 1 / Phase 2 code — it does **not**
reimplement any pipeline logic. A run-management sidebar drives a guided
five-tab workflow (Start, Data, Match PHREEQC, Run + Results, Audit / Help).
Each tab reuses the package functions; this file adds no chemistry or ML.
It lets you:

* see project + run status at a glance,
* enter measured / literature / demo data into per-run save files,
* map measured samples to PHREEQC rows and run the existing scripts,
* read an honest measured-vs-PHREEQC summary, and browse PHREEQC outputs.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

# Make the package importable when Streamlit runs this file directly.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from flyash_phreeqc_ml import calculations  # noqa: E402
from flyash_phreeqc_ml import config  # noqa: E402
from flyash_phreeqc_ml import dissolution_workbook  # noqa: E402
from flyash_phreeqc_ml import import_mapping  # noqa: E402
from flyash_phreeqc_ml import mapping_table  # noqa: E402
from flyash_phreeqc_ml import profiles  # noqa: E402
from flyash_phreeqc_ml import replicates  # noqa: E402
from flyash_phreeqc_ml import run_manager  # noqa: E402
from flyash_phreeqc_ml import scenarios  # noqa: E402
from flyash_phreeqc_ml.experiments import validate_experimental_df  # noqa: E402
from flyash_phreeqc_ml.parsers import (  # noqa: E402
    has_measured_data,
    load_experimental_release,
)
from flyash_phreeqc_ml.compare import comparison_inclusion  # noqa: E402
from flyash_phreeqc_ml.compare import inclusion as compare_inclusion  # noqa: E402
from flyash_phreeqc_ml.viz import compare_plots  # noqa: E402
from flyash_phreeqc_ml.viz import measured_overview  # noqa: E402

# Active model profile — its display name drives generic UI strings ("needs new
# {MODEL_NAME} simulation"). PHREEQC for this project; swappable via ModelProfile.
MODEL_NAME = profiles.PHREEQC_PROFILE.name

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
# CO2 labels come straight from config so the form, validator, and plan agree.
_CO2_OPTIONS = [""] + list(config.CO2_CONDITION_ALLOWED)
_YESNO_OPTIONS = ["", "yes", "no"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _run_script(relative_path: str, *args: str) -> subprocess.CompletedProcess:
    """Run a project script with the current interpreter, capturing output.

    Extra ``*args`` are passed through on the command line (e.g. ``--run NAME``).
    """
    return subprocess.run(
        [sys.executable, relative_path, *args],
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
# Experiment-run sidebar + workspace (the "save files" layer)
# --------------------------------------------------------------------------- #
def _run_type_warning(run_type: str) -> None:
    """Render the run-type warning with severity matching its meaning."""
    msg = run_manager.warning_for(run_type)
    if run_type == "lab_experiment":
        st.info(f"🧪 {msg}")
    elif run_type == "literature_benchmark":
        st.warning(f"📚 {msg}")
    elif run_type == "synthetic_demo":
        st.error(f"🧩 {msg}")
    else:  # plastic_composite
        st.warning(f"♻️ {msg}")


def _render_run_sidebar() -> str | None:
    """Sidebar 'Experiment runs' section: select or create a run.

    Returns the selected run's safe-name (or None). The selection persists across
    reruns via st.session_state['selected_run'].
    """
    st.sidebar.header("Experiment runs")
    runs = run_manager.list_runs()

    # --- select existing -------------------------------------------------- #
    current = st.session_state.get("selected_run")
    options = ["— none —"] + runs
    index = options.index(current) if current in runs else 0
    chosen = st.sidebar.selectbox("Open a run", options, index=index)
    st.session_state["selected_run"] = None if chosen == "— none —" else chosen

    # --- create new ------------------------------------------------------- #
    with st.sidebar.expander("➕ Create new run", expanded=not runs):
        new_name = st.text_input("Run name", key="new_run_name",
                                 placeholder="2026-06-03 pH-only lab data")
        new_type = st.selectbox("Run type", run_manager.RUN_TYPES, key="new_run_type")
        st.caption(run_manager.warning_for(new_type))
        new_desc = st.text_area("Description", key="new_run_desc", height=70)
        new_notes = st.text_input("Notes (optional)", key="new_run_notes")
        if st.button("Create run", use_container_width=True):
            raw = (new_name or "").strip()
            if not raw:
                st.error("Run name is required.")
            else:
                try:
                    safe = run_manager.safe_run_name(raw)
                    if run_manager.run_exists(safe):
                        st.error(f"A run named '{safe}' already exists — open it instead.")
                    else:
                        run_manager.create_run(
                            raw, new_type, description=new_desc, notes=new_notes
                        )
                        st.session_state["selected_run"] = safe
                        st.success(f"Created run '{safe}'.")
                        st.rerun()
                except run_manager.RunManagerError as exc:
                    st.error(str(exc))

    # --- show current ----------------------------------------------------- #
    selected = st.session_state.get("selected_run")
    st.sidebar.divider()
    if not selected:
        st.sidebar.caption("No run selected. Create or open one above.")
        return None

    cfg = run_manager.load_run_config(selected)
    st.sidebar.markdown(f"**Current run:** `{selected}`")
    st.sidebar.markdown(f"**Type:** `{cfg.get('run_type')}`")
    st.sidebar.markdown(f"**Source:** `{cfg.get('data_source')}`")
    st.sidebar.caption(f"📁 {run_manager.run_dir(selected).relative_to(_PROJECT_ROOT)}")
    if cfg.get("description"):
        st.sidebar.caption(f"📝 {cfg['description']}")
    st.sidebar.caption(f"⚠️ {run_manager.warning_for(cfg.get('run_type'))}")
    st.sidebar.info("➡️ Open the **Run + Results** tab to execute this run.")
    return selected


def _import_raw_frame(run_name: str, up) -> tuple[pd.DataFrame | None, str, str]:
    """Read an uploaded CSV/Excel into a raw frame (handles sheet selection).

    Returns ``(raw_df_or_None, kind, sheet_name)``. Renders the file-type / sheet
    widgets and any read error inline. ``raw_df`` is None when the file can't be
    read yet (bad type, unreadable, or empty).
    """
    try:
        kind = import_mapping.file_kind(up.name)
    except import_mapping.ImportMappingError as exc:
        st.error(str(exc))
        return None, "", ""

    data = up.getvalue()
    sheet_name = ""
    if kind == "excel":
        try:
            sheets = import_mapping.list_excel_sheets(io.BytesIO(data))
        except import_mapping.ImportMappingError as exc:
            st.error(str(exc))
            return None, kind, ""
        sheet_name = st.selectbox(
            "Select sheet", sheets, key=f"lab_import_sheet_{run_name}",
            help="Excel workbooks can hold several sheets — pick the one to import.",
        )

    try:
        raw = import_mapping.read_tabular(io.BytesIO(data), kind=kind, sheet=sheet_name or None)
    except import_mapping.ImportMappingError as exc:
        st.error(str(exc))
        return None, kind, sheet_name
    except Exception as exc:  # pragma: no cover - UI guard
        st.error(f"Could not read file: {exc}")
        return None, kind, sheet_name

    if raw.empty:
        st.warning("The selected file / sheet has no rows.")
        return None, kind, sheet_name
    return raw, kind, sheet_name


def _import_render_report(report: dict) -> None:
    """Render the pre-save validation summary (Feature 7) inline."""
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows to save", report["n_rows"])
    c2.metric("Rows missing required", report["rows_missing_required"])
    c3.metric("Blank sample IDs", report["blank_sample_ids"])
    c4.metric("Rows with no values", report["rows_no_measured_values"])

    if report["missing_required_columns"]:
        st.error("Missing required column(s): "
                 + ", ".join(f"`{c}`" for c in report["missing_required_columns"]))
    if report["rows_missing_required"]:
        st.warning(f"⚠️ {report['rows_missing_required']} row(s) are missing one or more "
                   "required metadata fields (sample_id, date, fly_ash_type, NaOH_M, "
                   "time, temperature, L/S, CO2, initial/final pH).")
    if report["ph_out_of_range"]:
        st.warning("⚠️ pH outside 0–14: "
                   + "; ".join(f"row {p['row']} {p['column']}={p['value']:g}"
                               for p in report["ph_out_of_range"][:8]))
    if report["duplicate_sample_ids"]:
        st.warning("⚠️ Duplicate sample_id(s): "
                   + ", ".join(f"`{s}`" for s in report["duplicate_sample_ids"][:10]))
    if report["rows_no_measured_values"]:
        st.warning(f"⚠️ {report['rows_no_measured_values']} row(s) have no measured values "
                   "(no pH and no chemistry).")
    if report["converted_columns"]:
        st.info("Converted to mM: "
                + ", ".join(f"`{c}` (from {u})" for c, u in report["converted_columns"].items()))
    if report["classifications"]:
        st.caption("Row classification — "
                   + ", ".join(f"{k}: {v}" for k, v in report["classifications"].items()))


def _lab_data_import(run_name: str) -> None:
    """Data-tab import for a lab run — pick the import mode, then dispatch.

    Two modes share this run's ``experimental_release.csv``:
    a **generic** rectangular CSV/Excel importer, and a **special-case** parser for
    the Class C fly ash dissolution workbook (stacked ICP OES element blocks + a
    pH sheet). The generic importer is never removed.
    """
    st.markdown("**Upload experimental data file**")
    mode = st.radio(
        "Import mode",
        ["Generic table (CSV / Excel)", "Class C fly ash dissolution workbook"],
        key=f"lab_import_mode_{run_name}",
        help="Use the dissolution-workbook parser for the multi-block lab workbook "
             "(ICP OES element blocks + pH sheet); use the generic importer for a "
             "normal single-table CSV/Excel.",
    )
    if mode.startswith("Class C"):
        _dissolution_import(run_name)
    else:
        _generic_table_import(run_name)


def _generic_table_import(run_name: str) -> None:
    """Generic CSV/Excel import into a lab run's experimental_release.csv.

    Reads .csv/.xlsx/.xls, previews the raw file, lets the user pick the Excel
    sheet, suggests a column mapping onto the app schema, converts chemistry units
    to mM, records leachant/provenance, validates, and only saves after explicit
    confirmation. Acid (HCl) rows are never forced into NaOH_M. Only ever writes to
    this lab run's own ``data/experimental_release.csv``.
    """
    st.caption(
        "Accepts `.csv`, `.xlsx`, or `.xls`. Preview → pick sheet → map columns → "
        "check units → confirm before saving to this run's `experimental_release.csv` "
        "(lab data only, never literature or synthetic)."
    )
    up = st.file_uploader(
        "Upload experimental data file", type=["csv", "xlsx", "xls"],
        key=f"lab_import_up_{run_name}",
    )
    if up is None:
        return

    raw, kind, sheet_name = _import_raw_frame(run_name, up)
    if raw is None:
        return

    # Feature 2 — raw preview before saving.
    st.markdown("**1 · Raw preview (nothing is saved yet)**")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("File", up.name)
    m2.metric("Type", kind)
    m3.metric("Sheet", sheet_name or "—")
    m4.metric("Raw shape", f"{raw.shape[0]} × {raw.shape[1]}")
    st.dataframe(raw, use_container_width=True, height=220)
    st.caption("Detected columns: " + ", ".join(f"`{c}`" for c in map(str, raw.columns)))

    # Feature 3 — column mapping interface.
    st.markdown("**2 · Map uploaded columns → app schema**")
    st.caption("Suggestions are pre-filled from column names; adjust any of them.")
    suggestion = import_mapping.suggest_column_mapping(raw.columns)
    options = ["(leave blank)"] + [str(c) for c in raw.columns]
    mapping: dict[str, str | None] = {}
    mcols = st.columns(3)
    for i, target in enumerate(import_mapping.MAPPING_TARGETS):
        widget_col = mcols[i % 3]
        default = suggestion.get(target)
        idx = options.index(str(default)) if (default is not None and str(default) in options) else 0
        choice = widget_col.selectbox(target, options, index=idx, key=f"lab_map_{run_name}_{target}")
        mapping[target] = None if choice == "(leave blank)" else choice

    # Feature 4 — unit handling for mapped chemistry columns.
    st.markdown("**3 · Units for chemistry columns**")
    st.caption("mM = mg/L / atomic_mass. mg/L and ppm are equivalent; ppb = µg/L. "
               "Sc and total REE stay in ppb.")
    units: dict[str, str] = {}
    mapped_chem = [c for c in import_mapping.CHEM_VALUE_COLUMNS if mapping.get(c)]
    if mapped_chem:
        ucols = st.columns(3)
        for i, chem in enumerate(mapped_chem):
            units[chem] = ucols[i % 3].selectbox(
                f"{chem} unit", import_mapping.UNIT_OPTIONS, index=0,
                key=f"lab_unit_{run_name}_{chem}",
            )
    else:
        st.caption("No chemistry columns mapped — nothing to convert.")

    # Feature 5 — acid/base (leachant) support.
    st.markdown("**4 · Leachant (acid / base)**")
    default_leachant = st.selectbox(
        "Default leachant when not given in a column", ["NaOH", "HCl", "other"],
        key=f"lab_leachant_{run_name}",
    )
    st.caption(
        "A mapped `leachant` column overrides this per row. Rows that look like acid "
        "leaching get `NaOH_M` blanked, `leachant`/`acid_M` recorded, and a warning note."
    )

    # Feature 6/7 — build the transformed (schema-aligned) frame + validation.
    transformed = import_mapping.build_schema_frame(
        raw, mapping, units, filename=up.name, sheet_name=sheet_name,
        default_leachant=default_leachant,
    )
    st.markdown("**5 · Transformed preview & validation**")
    _import_render_report(import_mapping.summarize_import(transformed, units))
    st.dataframe(transformed, use_container_width=True, height=240)

    if "sample_id" in transformed.columns:
        sid = transformed["sample_id"].astype(str).str.upper()
        flagged = int(sid.str.contains("TEST|SYNTH|MOCK|DEMO", na=False, regex=True).sum())
        if flagged:
            st.warning(f"⚠️ {flagged} sample_id(s) look like placeholders (TEST/SYNTH/MOCK/DEMO). "
                       "Only save real measured lab data into a lab run.")

    # Feature 8 — save options (replace / append), gated on confirmation.
    st.markdown("**6 · Save**")
    confirmed = st.checkbox(
        "I reviewed the imported data and understand these values will be saved to this "
        "experiment run.", key=f"lab_import_confirm_{run_name}",
    )
    existing = run_manager.read_data_file(run_name)

    def _do_save(mode: str) -> None:
        dest = run_manager.save_lab_dataframe(run_name, transformed, mode=mode)
        saved = run_manager.read_data_file(run_name)
        _read_csv.clear()
        st.success(f"Saved {len(transformed)} imported row(s) ({mode}) → "
                   f"`{dest.relative_to(_PROJECT_ROOT)}`. Run now has {len(saved)} row(s).")
        st.dataframe(saved, use_container_width=True, height=240)

    if not existing.empty:
        st.info(f"This run already has {len(existing)} row(s). Choose how to save:")
        rc, ac = st.columns(2)
        if rc.button("Replace current run data", key=f"lab_import_replace_{run_name}",
                     disabled=not confirmed):
            _do_save("replace")
        if ac.button("Append to current run data", key=f"lab_import_append_{run_name}",
                     disabled=not confirmed):
            _do_save("append")
    else:
        if st.button("Save imported data to this run", key=f"lab_import_save_{run_name}",
                     disabled=not confirmed):
            _do_save("replace")


def _save_transformed(run_name: str, transformed: pd.DataFrame, key_prefix: str) -> None:
    """Confirm-gated replace/append save of a transformed frame (shared by importers)."""
    confirmed = st.checkbox(
        "I reviewed the imported data and understand these values will be saved to this "
        "experiment run.", key=f"{key_prefix}_confirm_{run_name}",
    )
    existing = run_manager.read_data_file(run_name)

    def _do_save(mode: str) -> None:
        dest = run_manager.save_lab_dataframe(run_name, transformed, mode=mode)
        saved = run_manager.read_data_file(run_name)
        _read_csv.clear()
        st.success(f"Saved {len(transformed)} imported row(s) ({mode}) → "
                   f"`{dest.relative_to(_PROJECT_ROOT)}`. Run now has {len(saved)} row(s).")
        st.dataframe(saved, use_container_width=True, height=240)

    if not existing.empty:
        st.info(f"This run already has {len(existing)} row(s). Choose how to save:")
        rc, ac = st.columns(2)
        if rc.button("Replace current run data", key=f"{key_prefix}_replace_{run_name}",
                     disabled=not confirmed):
            _do_save("replace")
        if ac.button("Append to current run data", key=f"{key_prefix}_append_{run_name}",
                     disabled=not confirmed):
            _do_save("append")
    else:
        if st.button("Save imported data to this run", key=f"{key_prefix}_save_{run_name}",
                     disabled=not confirmed):
            _do_save("replace")


def _dissolution_import(run_name: str) -> None:
    """Special-case importer for the Class C fly ash dissolution workbook.

    Parses the stacked ICP OES element blocks + pH sheet via
    :mod:`dissolution_workbook`, lets the user set shared metadata defaults and the
    NaOH/HCl scope, shows a normalised preview with parse counts and warnings, and
    saves (confirm-gated) into this lab run's ``experimental_release.csv``. The
    generic importer remains available via the mode selector above.
    """
    st.caption(
        "For the multi-block lab workbook: an **ICP OES** sheet (Calcium / Silicon / "
        "Aluminum blocks, columns NaOH-OA/PF/GS, mmol/l + mg/L) and a **pH** sheet "
        "(`0.5M NaOH-OA-10`-style rows). mmol/l is preferred; mg/L is converted to mM."
    )
    up = st.file_uploader(
        "Upload dissolution workbook (.xlsx / .xls)", type=["xlsx", "xls"],
        key=f"diss_up_{run_name}",
    )
    if up is None:
        return

    # Feature 6 — shared metadata the user sets once for every imported row.
    st.markdown("**1 · Default metadata for all imported rows**")
    st.caption("These are not in the workbook — set them once and they fill every row. "
               "Rows are not rejected just because these are blank.")
    d1, d2, d3 = st.columns(3)
    defaults = {
        "experiment_date": d1.text_input("experiment_date", key=f"diss_date_{run_name}"),
        "temperature_C": d2.text_input("temperature_C", key=f"diss_temp_{run_name}"),
        "liquid_solid_ratio": d3.text_input("liquid_solid_ratio", key=f"diss_ls_{run_name}"),
        "CO2_condition": d1.selectbox("CO2_condition", _CO2_OPTIONS, key=f"diss_co2_{run_name}"),
        "initial_pH": d2.text_input("initial_pH", key=f"diss_iph_{run_name}"),
        "fly_ash_type": d3.text_input("fly_ash_type", value=dissolution_workbook.FLY_ASH_DEFAULT,
                                      key=f"diss_fat_{run_name}"),
    }

    # Feature 10 — NaOH-only vs NaOH+HCl.
    st.markdown("**2 · Rows to import**")
    scope = st.radio(
        "Scope", ["Import only NaOH rows", "Import NaOH + HCl rows"],
        index=1, key=f"diss_scope_{run_name}",
    )
    include_hcl = scope.endswith("HCl rows")

    try:
        transformed, report = dissolution_workbook.normalize_dissolution_workbook(
            io.BytesIO(up.getvalue()), defaults=defaults, include_hcl=include_hcl,
            filename=up.name,
        )
    except dissolution_workbook.DissolutionWorkbookError as exc:
        st.error(str(exc))
        return
    except Exception as exc:  # pragma: no cover - UI guard
        st.error(f"Could not parse workbook: {exc}")
        return

    if transformed.empty:
        st.warning("No NaOH/HCl sample rows were parsed from the pH sheet — check the workbook "
                   "matches the expected structure.")
        return

    # Feature 8 — normalised preview + counts.
    st.markdown("**3 · Normalised preview (nothing is saved yet)**")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("NaOH rows", report["n_naoh"])
    c2.metric("HCl rows", report["n_hcl"])
    c3.metric("Rows with pH", report["n_with_ph"])
    c4.metric("Rows with Ca/Si/Al", report["n_with_chem"])
    c5.metric("Rows missing metadata", report["rows_missing_metadata"])

    # Feature 9 — warnings.
    for msg in report["warnings"]:
        st.warning("⚠️ " + msg)

    st.dataframe(transformed, use_container_width=True, height=300)

    # Feature 8 — debug view: detected ICP tables + joined chemistry.
    with st.expander("Debug — detected ICP mmol/l tables & joined chemistry", expanded=False):
        debug = report.get("icp_debug", {})
        if not debug:
            st.info("No ICP chemistry was detected. Check the ICP OES sheet has element block "
                    "labels (Calcium/Silicon/Aluminum), a `mg/L` and `mmol/l` header row, and "
                    "`NaOH-OA/PF/GS` columns.")
        else:
            st.caption("Each table is the value used per (time, condition) — mmol/l preferred, "
                       "mg/L converted to mM as a fallback.")
            for col, label in (("Ca_mM", "Calcium"), ("Si_mM", "Silicon"), ("Al_mM", "Aluminum")):
                if col in debug:
                    st.markdown(f"**{label} → `{col}`**")
                    st.dataframe(debug[col], use_container_width=True, height=180)
        st.markdown("**Joined normalised rows (chemistry columns)**")
        st.dataframe(
            transformed[["sample_id", "time_min", "extra__condition_code", "final_pH",
                         "Ca_mM", "Si_mM", "Al_mM"]],
            use_container_width=True, height=240,
        )

    # Save (confirm-gated).
    st.markdown("**4 · Save**")
    _save_transformed(run_name, transformed, key_prefix="diss")


def _lab_entry_form(run_name: str) -> None:
    """Measured-release entry form for a lab-type run (pH-only or full ICP)."""
    st.write(
        "Enter a measured-release row. **Leave any chemistry field blank if not "
        "measured** — pH-only rows are fine; add ICP numbers later."
    )
    with st.form(f"lab_entry_{run_name}", clear_on_submit=True):
        inputs: dict[str, str] = {}
        cols = st.columns(3)
        for i, column in enumerate(config.EXPERIMENTAL_RELEASE_COLUMNS):
            widget_col = cols[i % 3]
            numeric = column in config.EXPERIMENTAL_NUMERIC_COLUMNS
            label = f"{column} (number)" if numeric else column
            if column == "CO2_condition":
                inputs[column] = widget_col.selectbox(column, _CO2_OPTIONS, key=f"{run_name}_{column}")
            elif column == "precipitate_observed":
                inputs[column] = widget_col.selectbox(column, _YESNO_OPTIONS, key=f"{run_name}_{column}")
            else:
                inputs[column] = widget_col.text_input(label, value="", key=f"{run_name}_{column}")
        submitted = st.form_submit_button("Save row to this run")

    if submitted:
        errors: list[str] = []
        for column in config.EXPERIMENTAL_NUMERIC_COLUMNS:
            raw = (inputs.get(column) or "").strip()
            if raw and _is_not_number(raw):
                errors.append(f"'{column}' must be a number (got '{raw}').")
        if not (inputs.get("sample_id") or "").strip():
            errors.append("'sample_id' is required.")
        if errors:
            for e in errors:
                st.error(e)
        else:
            row = {c: (inputs.get(c) or "").strip() for c in config.EXPERIMENTAL_RELEASE_COLUMNS}
            path = run_manager.append_lab_row(run_name, row)
            st.success(f"Saved sample '{row['sample_id']}' to {path.relative_to(_PROJECT_ROOT)}.")
            _read_csv.clear()


def _literature_entry(run_name: str) -> None:
    """Manual row entry + CSV upload for a literature-benchmark run."""
    st.write(
        "**Literature benchmark data** — values reported by other papers, for "
        "comparison only. This is kept separate from our measured experiment and is "
        "never written to a lab run's `experimental_release.csv`."
    )
    up = st.file_uploader("Upload a literature CSV", type=["csv"], key=f"lit_up_{run_name}")
    if up is not None:
        try:
            df = pd.read_csv(up)
            path = run_manager.save_literature_dataframe(run_name, df)
            st.success(f"Saved {len(df)} row(s) to {path.relative_to(_PROJECT_ROOT)}.")
            _read_csv.clear()
        except Exception as exc:  # pragma: no cover - UI guard
            st.error(f"Could not read CSV: {exc}")

    with st.form(f"lit_entry_{run_name}", clear_on_submit=True):
        inputs: dict[str, str] = {}
        cols = st.columns(3)
        for i, column in enumerate(run_manager.LITERATURE_BENCHMARK_COLUMNS):
            widget_col = cols[i % 3]
            if column == "CO2_condition":
                inputs[column] = widget_col.selectbox(column, _CO2_OPTIONS, key=f"lit_{run_name}_{column}")
            else:
                inputs[column] = widget_col.text_input(column, value="", key=f"lit_{run_name}_{column}")
        submitted = st.form_submit_button("Add literature row")
    if submitted:
        if not (inputs.get("source_id") or "").strip():
            st.error("'source_id' is required for a literature row.")
        else:
            row = {c: (inputs.get(c) or "").strip() for c in run_manager.LITERATURE_BENCHMARK_COLUMNS}
            path = run_manager.append_literature_row(run_name, row)
            st.success(f"Added literature row '{row['source_id']}' to {path.relative_to(_PROJECT_ROOT)}.")
            _read_csv.clear()


def _demo_entry(run_name: str) -> None:
    """Add synthetic demo rows (every row tagged source_type=synthetic_demo)."""
    st.error(
        "🧩 This is **synthetic / demo data only** — for testing the code, not for "
        "scientific conclusions. Every row is tagged `source_type=synthetic_demo`."
    )
    with st.form(f"demo_entry_{run_name}", clear_on_submit=True):
        inputs: dict[str, str] = {}
        cols = st.columns(3)
        for i, column in enumerate(config.EXPERIMENTAL_RELEASE_COLUMNS):
            widget_col = cols[i % 3]
            if column == "CO2_condition":
                inputs[column] = widget_col.selectbox(column, _CO2_OPTIONS, key=f"demo_{run_name}_{column}")
            else:
                inputs[column] = widget_col.text_input(column, value="", key=f"demo_{run_name}_{column}")
        submitted = st.form_submit_button("Add demo row")
    if submitted:
        if not (inputs.get("sample_id") or "").strip():
            st.error("'sample_id' is required.")
        else:
            row = {c: (inputs.get(c) or "").strip() for c in config.EXPERIMENTAL_RELEASE_COLUMNS}
            path = run_manager.append_demo_row(run_name, row)
            st.success(f"Added demo row '{row['sample_id']}' to {path.relative_to(_PROJECT_ROOT)}.")
            _read_csv.clear()


def _is_not_number(raw: str) -> bool:
    try:
        float(raw)
        return False
    except ValueError:
        return True


def _run_lab_workflow(run_name: str) -> None:
    """Export a lab run to the pipeline, then run the relevant scripts in order.

    Stops at the first failing step (non-zero exit), showing the command, stdout,
    stderr and a pass/fail status for each. Only touches the pipeline's
    manual-entry file via the explicit export; other runs are unaffected.
    """
    # Step 1 — export this run's CSV into the pipeline's manual-entry location.
    st.markdown("**Step 1 — Export run data → pipeline**")
    try:
        dest = run_manager.export_lab_run_to_pipeline(run_name)
        st.success(f"Exported to `{dest.relative_to(_PROJECT_ROOT)}`.")
    except run_manager.RunManagerError as exc:
        st.error(f"Export failed: {exc}")
        st.error("⛔ Workflow stopped — no scripts were run.")
        return

    # Mapping — needed for measured-vs-PHREEQC residuals. Export it if present,
    # otherwise warn (the workflow still runs, just without residuals).
    if run_manager.has_mapping(run_name):
        map_dest = run_manager.export_mapping_to_pipeline(run_name)
        st.success(f"Sample→PHREEQC mapping exported to `{map_dest.relative_to(_PROJECT_ROOT)}`.")
    else:
        st.warning(
            "No measured-data → model mapping found. The workflow can still run, but "
            "measured-vs-model residuals will not be calculated. Add a mapping in "
            "the **Match PHREEQC** tab."
        )

    # Steps 2..N — run each script, halting on the first failure. The comparison
    # step gets ``--run`` so it also writes this run's own outputs + provenance
    # stamp under experiments/<run>/outputs/ (what the Results tab reads).
    steps = [
        ("Phase 1 pipeline", "scripts/run_phase1.py", ()),
        ("Validate experimental data", "scripts/07_validate_experimental_data.py", ()),
        ("Compare measured vs PHREEQC", "scripts/05_compare_experimental.py",
         ("--run", run_name)),
        ("Sustainability score", "scripts/08_sustainability_score.py", ()),
    ]
    for i, (label, script, args) in enumerate(steps, start=2):
        st.markdown(f"**Step {i} — {label}**")
        st.code(" ".join(["python", script, *args]), language="bash")
        with st.spinner(f"Running {label}…"):
            proc = _run_script(script, *args)
        _show_process_result(label, proc)
        if proc.returncode != 0:
            st.error(f"⛔ Workflow stopped at step {i} ({label}) — see stderr above.")
            return

    st.success("✅ Workflow complete — all steps succeeded.")
    st.info(
        "Outputs written to:\n"
        f"- `experiments/{run_manager.safe_run_name(run_name)}/outputs/` — this run's "
        "comparison + figures (provenance-stamped)\n"
        "- `data/processed/` — parsed tables, master dataset, comparison\n"
        "- `outputs/tables/` — validation report, sustainability score\n"
        "- `reports/figures/` — plots"
    )
    _read_csv.clear()  # processed CSVs changed; refresh the viewers below


def _render_mapping_quality(mapping: pd.DataFrame) -> None:
    """Small mapping-quality summary + collision warning for the Mapping tab."""
    summary = run_manager.summarize_mapping(mapping)
    if summary["n_samples"] == 0:
        return

    st.markdown("**Mapping quality**")
    m1, m2 = st.columns(2)
    m1.metric("Mapped samples", summary["n_samples"])
    m2.metric("Unique model results used", summary["n_unique_rows"])

    if summary["samples_per_row"]:
        per_row = pd.DataFrame(
            [{"model_result": k, "samples_mapped": v}
             for k, v in summary["samples_per_row"].items()]
        )
        st.caption("Samples per model result:")
        st.dataframe(per_row, use_container_width=True, height=170)

    if summary["has_collisions"]:
        st.warning(
            "Multiple samples are mapped to the same model result. Scatter plots may "
            "appear as vertical lines because the model prediction is identical for "
            "those samples.\n\n"
            "Your graph may form a vertical line because several samples share the "
            "same model prediction."
        )
    if summary["n_samples"] > summary["n_unique_rows"]:
        st.warning(
            "There are more samples than distinct model results, so the comparison may "
            "not represent distinct model conditions."
        )


@st.cache_data(show_spinner=False)
def _scenario_manifest(results_path_str: str, mtime: float) -> pd.DataFrame:
    """Build (and persist) the PHREEQC scenario manifest, cached on results mtime."""
    manifest = scenarios.build_scenario_manifest(pd.read_csv(results_path_str))
    dest = scenarios.scenario_manifest_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    manifest.to_csv(dest, index=False)  # data/processed/ is gitignored
    return manifest


# Columns the Scenario Explorer table shows (readable subset of the manifest).
_EXPLORER_COLUMNS = [
    "scenario_label", "source_file", "state", "solution_number",
    "predicted_pH", "predicted_Ca_mM", "predicted_Si_mM", "predicted_Al_mM",
    "predicted_Fe_mM", "liquid_solid_ratio", "CO2_condition", "metadata_quality",
]


def _render_scenario_explorer(run_name: str, manifest: pd.DataFrame) -> None:
    """Feature 2 — filterable, readable table of PHREEQC scenarios."""
    st.markdown("#### PHREEQC Scenario Explorer")
    st.caption(
        "Each PHREEQC result row described in plain terms, with metadata inferred "
        "from the source filename where safe (anything uncertain is `unknown`)."
    )
    if manifest.empty:
        st.info("No PHREEQC scenarios yet — run Phase 1 to generate results.")
        return

    f1, f2, f3, f4 = st.columns(4)
    state_choice = f1.selectbox(
        "state", ["batch only", "initial only", "all"], key=f"exp_state_{run_name}")
    co2_opts = ["all"] + sorted(manifest["CO2_condition"].dropna().astype(str).unique())
    co2_choice = f2.selectbox("CO2_condition", co2_opts, key=f"exp_co2_{run_name}")
    ls_vals = sorted(
        {f"{v:g}" for v in manifest["liquid_solid_ratio"].dropna().tolist()})
    ls_choice = f3.selectbox("liquid_solid_ratio", ["all"] + ls_vals, key=f"exp_ls_{run_name}")
    src_opts = ["all"] + sorted(manifest["source_file"].dropna().astype(str).unique())
    src_choice = f4.selectbox("source_file", src_opts, key=f"exp_src_{run_name}")

    view = manifest
    if state_choice == "batch only":
        view = view[view["state"].astype(str).str.lower() == "batch"]
    elif state_choice == "initial only":
        view = view[view["state"].astype(str).str.lower() == "initial"]
    if co2_choice != "all":
        view = view[view["CO2_condition"].astype(str) == co2_choice]
    if ls_choice != "all":
        view = view[view["liquid_solid_ratio"].map(lambda v: f"{v:g}") == ls_choice]
    if src_choice != "all":
        view = view[view["source_file"].astype(str) == src_choice]

    cols = [c for c in _EXPLORER_COLUMNS if c in view.columns]
    st.dataframe(view[cols], use_container_width=True, height=280)
    st.caption(f"{len(view)} of {len(manifest)} scenarios shown.")

    st.info(
        "**sol1, sol2, sol3 are PHREEQC solution numbers / repeated solution outputs.** They "
        "should not be interpreted as experimental time points unless the PHREEQC input "
        "explicitly defines them that way. **In this project, sol1/sol2/sol3 should be treated "
        "as replicate/batch outputs for the same broad scenario, not as 10/20/60 min time points.**"
    )
    descriptions = scenarios.load_solution_descriptions()
    with st.expander("What do sol1 / sol2 / sol3 represent? (from the PHREEQC input files)"):
        if descriptions.empty:
            st.caption(
                "No parsed PHREEQC input solutions found yet (run Phase 1). PHREEQC `.pqi` "
                "files label each `SOLUTION n`; those labels — not time or replicate — are "
                "all the app can attribute to a solution number."
            )
        else:
            st.caption(
                "Parsed from the `.pqi` input files (`SOLUTION n <label>`). A blank/generic "
                "label means the input did not describe that solution as a time point or replicate."
            )
            st.dataframe(descriptions, use_container_width=True, height=200)


_ASSISTANT_META_FIELDS = [
    "NaOH_M", "time_min", "liquid_solid_ratio", "CO2_condition",
    "temperature_C", "final_pH", "Ca_mM", "Si_mM", "Al_mM", "Fe_mM",
]


def _render_mapping_assistant(run_name: str, data: pd.DataFrame,
                              sample_ids: list[str], manifest: pd.DataFrame) -> None:
    """Features 3/4/5 — sample metadata, top-3 suggestions, approve, no-match warning."""
    st.markdown("#### Mapping Assistant")
    st.caption(
        "Pick a measured sample; the assistant scores PHREEQC scenarios with simple, "
        "transparent rules (no ML) and suggests the best matches."
    )
    sel_sample = st.selectbox("Experimental sample_id", sample_ids,
                              key=f"assist_sample_{run_name}")
    sample_row = data[data["sample_id"].astype(str).str.strip() == sel_sample]
    sample = sample_row.iloc[0].to_dict() if not sample_row.empty else {"sample_id": sel_sample}

    meta = {f: sample.get(f, "") for f in _ASSISTANT_META_FIELDS if f in sample}
    exp_code = scenarios.sample_condition_code(sample)
    if exp_code:
        meta["condition_code"] = exp_code
        meta["cover_condition"] = scenarios.cover_condition(exp_code) or ""
        meta["CO2_exposure_level"] = scenarios.co2_exposure_level(exp_code) or ""
    if meta:
        st.markdown("**Measured metadata (known):**")
        st.dataframe(pd.DataFrame([meta]), use_container_width=True, height=80)
        st.caption(
            "The model (currently PHREEQC) knows L/S, CO2, state, solution number and "
            "predicted pH/Ca/Si/Al — but often **not** fine-grained measured metadata such "
            "as exact time, condition code, or concentration, so an exact match can't always "
            "be confirmed (see notes below)."
        )

    if manifest.empty:
        st.info("No PHREEQC scenarios available to suggest yet — run Phase 1 first.")
        return

    suggestions = scenarios.suggest_mappings(sample, manifest, top_n=3)
    if not suggestions or suggestions[0]["confidence"] == "low":
        # Feature 5 — best match is weak.
        st.error(
            "No strong PHREEQC match exists for this sample. The comparison may be "
            "misleading. Consider generating a new PHREEQC simulation for this "
            "experimental condition."
        )

    for i, sug in enumerate(suggestions):
        conf = sug["confidence"]
        badge = {"high": "🟢 high", "medium": "🟡 medium", "low": "🔴 low"}.get(conf, conf)
        with st.container(border=True):
            st.markdown(f"**{sug['scenario_label']}** · score {sug['score']} · {badge} confidence")
            st.caption(f"`{sug['suggested_phreeqc_record_key']}`")
            if sug.get("base_confidence") and sug["base_confidence"] != conf:
                st.caption(
                    f"Capped from {sug['base_confidence']} → {conf}: PHREEQC does not specify "
                    f"{', '.join(sug.get('phreeqc_missing', [])) or 'some experimental metadata'}."
                )
            st.markdown(
                f"- **Reason:** {sug['reason']}\n"
                f"- **Matched:** {', '.join(sug['matched_fields']) or '—'}\n"
                f"- **Mismatched:** {', '.join(sug['mismatched_fields']) or '—'}\n"
                f"- **Missing PHREEQC metadata:** {', '.join(sug.get('phreeqc_missing', [])) or '—'}"
            )
            for note in sug.get("metadata_notes", []):
                st.warning("ℹ️ " + note)
            if st.button("Use this mapping", key=f"assist_use_{run_name}_{i}"):
                try:
                    run_manager.add_mapping(
                        run_name, sel_sample, sug["suggested_phreeqc_record_key"])
                    st.success(
                        f"Mapped `{sel_sample}` → `{sug['suggested_phreeqc_record_key']}`.")
                    _read_csv.clear()
                    st.rerun()
                except run_manager.RunManagerError as exc:
                    st.error(str(exc))


def _render_sim_needed(run_name: str, data: pd.DataFrame, mapping: pd.DataFrame,
                       manifest: pd.DataFrame, table: pd.DataFrame) -> None:
    """Conditions whose best candidate is *needs new simulation*.

    Driven from the **same** suggestion ``table`` shown above, so the count here
    always agrees with the table's status column.
    """
    st.markdown(f"#### Conditions needing new {MODEL_NAME} simulations")
    needs_new = mapping_table.needs_new_simulation(table)
    st.caption(
        "Conditions with no usable model/simulation candidate (status **needs new "
        f"simulation** in the suggestion table). Generate matching {MODEL_NAME} scenarios for "
        "these. Conditions that are *scenario-level only* or *unsafe* appear in the table "
        "above with that status, not here."
    )
    if needs_new.empty:
        st.success("Every measured condition has at least one candidate model scenario.")
    else:
        st.dataframe(needs_new[["condition_key", "n_replicates", "confidence", "reason"]],
                     use_container_width=True, height=240)

    # Keep the older per-sample view available but out of the way.
    with st.expander("Per-sample detail (no mapping / low confidence / collisions)"):
        needed = scenarios.samples_needing_simulation(data, mapping, manifest)
        if needed.empty:
            st.success("Every sample has a confident, non-colliding PHREEQC mapping.")
        else:
            st.dataframe(needed, use_container_width=True, height=240)


def _render_manual_mapping(run_name: str, phreeqc: pd.DataFrame,
                           sample_ids: list[str]) -> None:
    """Feature 7 — the original manual dropdown, kept as advanced mode."""
    st.warning(
        "Use manual mapping only if you understand what the PHREEQC row represents."
    )
    only_batch = st.checkbox(
        "Only show PHREEQC 'batch' rows (post-equilibration)",
        value=True, key=f"map_batch_{run_name}",
    )
    view = phreeqc
    if only_batch and "state" in phreeqc.columns:
        batch = phreeqc[phreeqc["state"] == "batch"]
        view = batch if not batch.empty else phreeqc

    label_cols = [
        c for c in ["record_key", "source_file", "simulation", "state",
                    "solution_number", "pH", "mol_Ca", "mol_Si", "mol_Al", "mol_Na"]
        if c in view.columns
    ]

    def _phreeqc_label(pos) -> str:
        row = view.loc[pos]
        return " | ".join(f"{c}={row[c]}" for c in label_cols)

    c1, c2 = st.columns(2)
    with c1:
        sel_sample = st.selectbox("sample_id", sample_ids, key=f"map_sample_{run_name}")
    with c2:
        sel_pos = st.selectbox(
            "PHREEQC result row", list(view.index),
            format_func=_phreeqc_label, key=f"map_pheq_{run_name}",
        )

    if st.button("Save mapping", key=f"map_save_{run_name}"):
        record_key = str(view.loc[sel_pos, "record_key"]).strip()
        try:
            run_manager.add_mapping(run_name, sel_sample, record_key)
            st.success(f"Mapped `{sel_sample}` → `{record_key}`.")
            _read_csv.clear()
            st.rerun()
        except run_manager.RunManagerError as exc:
            st.error(str(exc))


def _render_replicate_summary(data: pd.DataFrame) -> None:
    """Feature 2 — replicate count + mean/std per experimental condition."""
    st.markdown("#### Replicate summary (grouped by condition)")
    st.caption(
        "Rows are grouped by `condition_key` (leachant + molarity + OA/PF/GS + time + "
        "L/S + CO2). `replicate_id` is read from the sample_id (R1/R2/R3, rep…, batch…). "
        "std needs ≥2 replicates."
    )
    summary = replicates.replicate_summary(data)
    if summary.empty:
        st.info("No rows to summarise yet.")
        return
    st.dataframe(summary, use_container_width=True, height=220)
    singles = summary[summary["number_of_replicates"] < 2]
    if not singles.empty:
        st.warning(f"⚠️ {len(singles)} condition(s) have a single replicate — no standard "
                   "deviation can be estimated for them.")


def _scenario_record_key_picker(run_name: str, manifest: pd.DataFrame, key: str) -> str | None:
    """Selectbox over batch-first manifest scenarios -> a PHREEQC record_key."""
    if manifest.empty:
        st.info("No PHREEQC scenarios yet — run Phase 1 first.")
        return None
    view = manifest.copy()
    view["_batch"] = view["state"].astype(str).str.lower().eq("batch")
    view = view.sort_values("_batch", ascending=False)
    keys = view["phreeqc_record_key"].astype(str).tolist()
    labels = {
        r["phreeqc_record_key"]: f"{r.get('scenario_label', '')}  ·  {r['phreeqc_record_key']}"
        for _, r in view.iterrows()
    }
    return st.selectbox("Model / simulation result", keys,
                        format_func=lambda k: labels.get(k, k), key=key,
                        help="Currently PHREEQC scenario rows. Each label includes its "
                             "PHREEQC file/source and solution number.")


# Status → (emoji, Streamlit alert level) for the simple mapping-status line.
_MAPPING_STATUS_BADGE = {
    replicates.MAPPING_STATUS_EXACT: ("✅", "success"),
    replicates.MAPPING_STATUS_SCENARIO: ("🟡", "warning"),
    replicates.MAPPING_STATUS_UNSAFE: ("🔴", "error"),
    replicates.MAPPING_STATUS_NEEDS_NEW: ("⚠️", "warning"),
}


def _manifest_row(manifest: pd.DataFrame, record_key: str | None) -> dict | None:
    """The manifest row for a PHREEQC ``record_key`` as a dict (or ``None``)."""
    if not record_key or manifest.empty or "phreeqc_record_key" not in manifest.columns:
        return None
    hit = manifest[manifest["phreeqc_record_key"].astype(str) == str(record_key)]
    return hit.iloc[0].to_dict() if not hit.empty else None


def _condition_representative_sample(data: pd.DataFrame, condition_key: str) -> dict:
    """First measured row belonging to ``condition_key`` (for metadata/status)."""
    ann = replicates.annotate(data)
    sub = ann[ann[replicates.CONDITION_KEY_COLUMN].astype(str) == str(condition_key)]
    return sub.iloc[0].to_dict() if not sub.empty else {}


# Project-specific (fly ash) explanation — only shown when the dataset actually
# uses OA/PF/GS condition codes, so the app stays generic for other experiments.
def _condition_code_descriptions_md(codes) -> str:
    """Markdown describing the given condition codes, sourced from the config dict.

    ``config.CONDITION_CODE_DESCRIPTIONS`` is the single source of truth — the UI
    never hard-codes the wording or the not-confirmed-sealed caution.
    """
    lines = []
    for code in sorted(codes):
        info = config.CONDITION_CODE_DESCRIPTIONS.get(str(code).strip().upper())
        if not info:
            continue
        caution = f" — ⚠️ {info['caution']}" if info.get("caution") else ""
        lines.append(f"- **{code} = {info['label']}**: {info['description']}{caution}")
    return "\n".join(lines)


def _dataset_condition_codes(data: pd.DataFrame) -> set[str]:
    """OA/PF/GS codes actually present in this run's measured rows (may be empty)."""
    codes: set[str] = set()
    if data is None or data.empty:
        return codes
    for _, r in data.iterrows():
        code = scenarios.sample_condition_code(r.to_dict())
        if code:
            codes.add(code)
    return codes


def _render_condition_code_help(data: pd.DataFrame) -> None:
    """Show the cup-cover condition descriptions *only* if this dataset uses the codes."""
    codes = _dataset_condition_codes(data)
    if not codes:
        return
    with st.expander(f"What do the condition codes ({', '.join(sorted(codes))}) mean? (this dataset)"):
        st.markdown(_condition_code_descriptions_md(codes))
        st.caption("CO₂-exposure cup covers. Source: config.CONDITION_CODE_DESCRIPTIONS.")


def _render_mapping_status_line(sample: dict, scenario: dict | None) -> str:
    """Show the four-state mapping status as a compact, color-coded line."""
    status = replicates.mapping_status(sample, scenario)
    emoji, _ = _MAPPING_STATUS_BADGE.get(status, ("•", "info"))
    st.markdown(f"**Mapping status:** {emoji} {status}")
    st.caption(replicates.MAPPING_STATUS_DEFINITIONS.get(status, ""))
    return status


# Generic validation-metadata columns surfaced when present (project-agnostic).
_GENERIC_META_FIELDS = [
    "leachant", "acid_M", "NaOH_M", "time_min", "liquid_solid_ratio",
    "temperature_C", "CO2_condition",
]


def _is_present(v) -> bool:
    """True if a cell value is a real, non-blank entry."""
    return v not in (None, "") and str(v).strip().lower() != "nan"


def _render_advanced_validation_metadata(sample: dict, scenario: dict | None) -> None:
    """Expander body — measured-side metadata (dynamic) + model-side PHREEQC metadata.

    Metadata is never discarded (it stays on every saved row); this view only
    *surfaces* whatever columns the current dataset actually has, so the app does
    not assume every experiment carries OA/PF/GS, CO₂ cover, or leachant fields.
    """
    # --- Measured-side metadata, shown dynamically ---
    derived: dict = {}
    exp_code = scenarios.sample_condition_code(sample)
    if exp_code:  # project-specific (fly ash) derived fields, only when codes exist
        derived["condition_code"] = exp_code
        derived["cover_condition"] = scenarios.cover_condition(exp_code) or "—"
        derived["CO2_exposure_level"] = scenarios.co2_exposure_level(exp_code) or "—"
    for f in _GENERIC_META_FIELDS:
        if _is_present(sample.get(f)):
            derived[f] = sample.get(f)
    # any other non-canonical metadata the importer preserved (extra__* columns)
    for k, v in sample.items():
        if str(k).startswith("extra__") and _is_present(v) and k not in (
                "extra__condition_code", "extra__cover_condition", "extra__CO2_exposure_level"):
            derived[k] = v

    if exp_code:
        st.caption(
            "Condition codes, cover type and CO₂ exposure are **auto-derived** from the "
            "sample/condition name for this dataset — no manual selection needed."
        )
    else:
        st.caption("Validation metadata available for this dataset (varies by experiment).")
    if derived:
        st.dataframe(pd.DataFrame([derived]), use_container_width=True, height=80)
    else:
        st.info("No extra validation metadata columns found for this record.")
    # Cup-cover description + not-confirmed-sealed caution, from config (single source).
    if exp_code and str(exp_code).upper() in config.CONDITION_CODE_DESCRIPTIONS:
        st.markdown(_condition_code_descriptions_md({exp_code}))

    if not scenario:
        st.info("Pick a model / simulation result above to see matched / missing / conflicting fields.")
        return

    # --- Model-side metadata (currently PHREEQC) ---
    st.markdown("**Model prediction source (PHREEQC)**")
    pheq_source = {
        "file/source": scenario.get("source_file", "—"),
        "solution number": scenario.get("solution_number", "—"),
        "state": scenario.get("state", "—"),
        "scenario": scenario.get("scenario_label", "—"),
    }
    st.dataframe(pd.DataFrame([pheq_source]), use_container_width=True, height=80)

    pheq_fields = ["liquid_solid_ratio", "CO2_condition", "time_min",
                   "condition_code", "NaOH_M", "temperature_C"]
    avail = {f: ("present" if _is_present(scenario.get(f)) else "not specified")
             for f in pheq_fields}
    st.markdown("**Model metadata availability**")
    st.dataframe(pd.DataFrame([avail]), use_container_width=True, height=80)

    score = scenarios.score_scenario(sample, scenario)
    st.markdown(
        f"- **Matched fields:** {', '.join(score['matched_fields']) or '—'}\n"
        f"- **Missing model fields:** {', '.join(score.get('phreeqc_missing', [])) or '—'}\n"
        f"- **Conflicting fields:** {', '.join(score['mismatched_fields']) or '—'}"
    )
    for note in score.get("metadata_notes", []):
        st.caption("ℹ️ " + note)


# Status → a badged display string for the table's status column / detail headers.
def _status_badge(status: str) -> str:
    emoji = _MAPPING_STATUS_BADGE.get(status, ("•", "info"))[0]
    return f"{emoji} {status}"


def _map_flash(run_name: str, level: str, msg: str) -> None:
    """Queue a message that should survive the post-accept rerun (shown atop the table)."""
    st.session_state.setdefault(f"map_flash_{run_name}", []).append((level, msg))


def _render_map_flash(run_name: str) -> None:
    for level, msg in st.session_state.pop(f"map_flash_{run_name}", []):
        getattr(st, level, st.info)(msg)


# Metadata fields compared side-by-side in the row-detail alignment table.
_ALIGNMENT_FIELDS = ["liquid_solid_ratio", "CO2_condition", "temperature_C",
                     "time_min", "NaOH_M"]


def _fmt_meta(value) -> str:
    return str(value) if _is_present(value) else "—"


def _alignment_value_table(sample: dict, scenario: dict) -> pd.DataFrame:
    """Measured vs model values for each comparison field (both sides, with values)."""
    rows = [{"field": f, "measured": _fmt_meta(sample.get(f)),
             "model (PHREEQC)": _fmt_meta(scenario.get(f))} for f in _ALIGNMENT_FIELDS]
    rows.append({
        "field": "condition_code",
        "measured": scenarios.sample_condition_code(sample) or "—",
        "model (PHREEQC)": _fmt_meta(scenario.get("condition_code")),
    })
    return pd.DataFrame(rows)


def _accept_condition_mappings(run_name: str, rows: list[dict], *,
                               override: bool = False) -> int:
    """Save the chosen suggestion rows as condition mappings, then apply to per-sample.

    Each row needs a ``condition_key`` + ``phreeqc_record_key``. ``override=True`` tags
    the saved mapping (used for the confirmed unsafe manual-override path). Replicates
    inherit the link via :func:`run_manager.apply_condition_mapping`.
    """
    accepted = 0
    for row in rows:
        ck = str(row.get("condition_key", "")).strip()
        key = str(row.get("phreeqc_record_key", "")).strip()
        if not ck or not key:
            continue
        status = row.get("mapping_status", "")
        note = (f"override of {status} mapping" if override
                else f"accepted from suggestion table ({status})")
        try:
            run_manager.add_condition_mapping(run_name, ck, key, notes=note, override=override)
            accepted += 1
        except run_manager.RunManagerError as exc:
            st.error(f"{ck}: {exc}")
    if accepted:
        try:
            run_manager.apply_condition_mapping(run_name)
        except run_manager.RunManagerError as exc:
            st.error(str(exc))
    return accepted


# Columns shown in the editable suggestion table (status is badged for display).
_SUGGESTION_DISPLAY_COLUMNS = [
    "accept", "condition_key", "n_replicates", "scenario_label",
    "phreeqc_record_key", "status", "score", "confidence", "already_mapped", "reason",
]


def _runner_up_delta(best_score: int, candidate: dict) -> str:
    """One-line 'lost N points: …' for a runner-up, generated from its trace."""
    lost = int(best_score) - int(candidate.get("score") or 0)
    # Prefer the explicit conflicts; fall back to the model-missing metadata.
    why = list(candidate.get("mismatched_fields") or [])
    if not why:
        why = [f"model lacks {m}" for m in candidate.get("phreeqc_missing", [])]
    detail = "; ".join(why) if why else "lower-scoring metadata match"
    if lost <= 0:
        return f"tied with best · {detail}"
    return f"lost {lost} point(s) vs best: {detail}"


def _render_condition_detail(run_name: str, data: pd.DataFrame, manifest: pd.DataFrame,
                             condition_key: str) -> None:
    """Row-level detail: structured alignment + runner-up candidates + score breakdown."""
    sample, candidates = mapping_table.condition_candidates(data, condition_key, manifest, top_n=3)
    # Cup-cover condition description (with the not-confirmed-sealed caution), from
    # config — shown only when this condition actually carries an OA/PF/GS code.
    _det_code = scenarios.sample_condition_code(sample)
    if _det_code and str(_det_code).upper() in config.CONDITION_CODE_DESCRIPTIONS:
        st.caption(_condition_code_descriptions_md({_det_code}))
    if not candidates:
        st.info("No model/simulation candidate exists for this condition — it needs a new simulation.")
        return
    best_score = candidates[0].get("score") or 0
    for i, c in enumerate(candidates):
        scenario = mapping_table._manifest_row(manifest, c["suggested_phreeqc_record_key"])
        status = replicates.mapping_status(sample, scenario)
        header = "Best candidate" if i == 0 else f"Runner-up {i}"
        with st.container(border=True):
            st.markdown(
                f"**{header}: {c.get('scenario_label', '') or '—'}**  ·  score "
                f"{c['score']}  ·  {c['confidence']} confidence  ·  {_status_badge(status)}"
            )
            st.caption(f"`{c['suggested_phreeqc_record_key']}`")
            # Runner-up one-line delta vs the best candidate (generated from the trace).
            if i > 0:
                st.caption("↳ " + _runner_up_delta(best_score, c))
            if c.get("confidence_explanation"):
                st.caption(f"Confidence: {c['confidence_explanation']}")

            st.caption("Field alignment (measured vs model, with values):")
            st.dataframe(_alignment_value_table(sample, scenario or {}),
                         use_container_width=True, hide_index=True, height=250)
            st.markdown(
                f"- **Matched:** {', '.join(c['matched_fields']) or '—'}\n"
                f"- **Missing model fields:** {', '.join(c.get('phreeqc_missing', [])) or '—'}\n"
                f"- **Conflicting (both values):** {', '.join(c['mismatched_fields']) or '—'}"
            )
            breakdown = c.get("score_breakdown", [])
            if breakdown:
                st.caption("Score breakdown (which rule added/subtracted points):")
                st.dataframe(pd.DataFrame(breakdown), use_container_width=True,
                             hide_index=True, height=min(40 + 35 * len(breakdown), 220))
            for note in c.get("metadata_notes", []):
                st.caption("ℹ️ " + note)


def _render_suggestion_table(run_name: str, data: pd.DataFrame, manifest: pd.DataFrame,
                             table: pd.DataFrame) -> None:
    """Automatic-first suggestion table (no button) + review detail + accept actions."""
    _render_map_flash(run_name)
    st.markdown("#### Suggested mappings")
    st.caption(
        "One row per measured condition, generated automatically with transparent "
        "rule-based scoring (no ML). Replicates of a condition are mapped together. "
        "Tick **accept** then **Accept selected**; **Accept all exact** bulk-accepts only "
        "exact matches. Unsafe rows cannot be accepted here — use Manual override below."
    )
    if table.empty:
        st.info("No measured data groups to map yet.")
        return

    EXACT = replicates.MAPPING_STATUS_EXACT
    UNSAFE = replicates.MAPPING_STATUS_UNSAFE
    NEEDS_NEW = replicates.MAPPING_STATUS_NEEDS_NEW

    disp = table.copy()
    # Pre-tick exact, not-yet-mapped rows; never pre-tick unsafe/needs-new.
    disp.insert(0, "accept", [
        (r["mapping_status"] == EXACT and not bool(r["already_mapped"]))
        for _, r in disp.iterrows()
    ])
    disp["status"] = disp["mapping_status"].map(_status_badge)
    edited = st.data_editor(
        disp[_SUGGESTION_DISPLAY_COLUMNS],
        column_config={
            "accept": st.column_config.CheckboxColumn("accept", help="Tick to accept this mapping"),
            "reason": st.column_config.TextColumn("reason", width="large"),
        },
        disabled=[c for c in _SUGGESTION_DISPLAY_COLUMNS if c != "accept"],
        hide_index=True, use_container_width=True, height=320,
        key=f"sug_editor_{run_name}",
    )

    # Re-attach the raw status to each row (the editor only edits `accept`).
    records = table.to_dict("records")
    for rec, acc in zip(records, edited["accept"].tolist()):
        rec["accept"] = bool(acc)

    a1, a2, a3 = st.columns(3)
    if a1.button("✅ Accept all exact suggestions", key=f"acc_exact_{run_name}"):
        rows = mapping_table.exact_suggestions(table).to_dict("records")
        n = _accept_condition_mappings(run_name, rows)
        _map_flash(run_name, "success", f"Accepted {n} exact mapping(s).")
        _read_csv.clear()
        st.rerun()
    if a2.button("✅ Accept selected", key=f"acc_sel_{run_name}"):
        selected = [r for r in records if r["accept"]]
        acceptable = [r for r in selected
                      if r["mapping_status"] in mapping_table.SELECTABLE_STATUSES
                      and str(r["phreeqc_record_key"]).strip()]
        unsafe_sel = [r for r in selected if r["mapping_status"] == UNSAFE]
        new_sel = [r for r in selected if r["mapping_status"] == NEEDS_NEW]
        n = _accept_condition_mappings(run_name, acceptable)
        _map_flash(run_name, "success", f"Accepted {n} selected mapping(s).")
        if any(r["mapping_status"] == replicates.MAPPING_STATUS_SCENARIO for r in acceptable):
            _map_flash(run_name, "info",
                       "Some accepted mappings are **scenario-level only** — comparison graphs "
                       "for them are a preliminary / workflow check, not final validation.")
        if unsafe_sel:
            _map_flash(run_name, "error",
                       f"{len(unsafe_sel)} **unsafe** row(s) were NOT accepted. Unsafe mappings "
                       "(e.g. acid leachant on a NaOH/CO₂ scenario) can only be saved via "
                       "**Manual override / advanced mapping**, where a confirmation is required "
                       "and the mapping is tagged `override=true`.")
        if new_sel:
            _map_flash(run_name, "warning",
                       f"{len(new_sel)} row(s) are **needs new simulation** — no candidate to accept.")
        _read_csv.clear()
        st.rerun()
    if a3.button("➡️ Export mapping to pipeline", key=f"exp_sug_{run_name}"):
        try:
            dest = run_manager.export_mapping_to_pipeline(run_name)
            _map_flash(run_name, "success",
                       f"Copied mapping to {dest.relative_to(_PROJECT_ROOT)} — step 05 reads it.")
            _read_csv.clear()
        except run_manager.RunManagerError as exc:
            _map_flash(run_name, "error", str(exc))
        st.rerun()

    st.markdown("**Inspect a condition** — alignment, runner-up candidates & score breakdown")
    sel = st.selectbox("Condition to inspect", table["condition_key"].astype(str).tolist(),
                       key=f"detail_sel_{run_name}")
    _render_condition_detail(run_name, data, manifest, sel)


def _render_saved_condition_mappings(run_name: str) -> None:
    """Review of accepted/saved condition mappings (+ advanced apply/delete)."""
    cond_map = run_manager.read_condition_mapping(run_name)
    st.markdown(f"**Accepted / saved mappings** ({len(cond_map)})")
    if cond_map.empty:
        st.caption("No mappings accepted yet — use auto-suggest above, or manual override below.")
        return
    st.dataframe(cond_map, use_container_width=True, height=150)
    _render_condition_mapping_advanced(run_name, cond_map)


def _render_condition_mapping(run_name: str, data: pd.DataFrame, manifest: pd.DataFrame) -> None:
    """Manual override view: condition + scenario + status + notes + save/apply."""
    summary = replicates.replicate_summary(data)
    if summary.empty:
        st.info("No conditions to map yet — enter data first.")
        return

    condition_keys = summary["condition_key"].tolist()
    c1, c2 = st.columns([2, 3])
    with c1:
        sel_condition = st.selectbox("Measured data group", condition_keys,
                                     key=f"cond_sel_{run_name}",
                                     help="A group of measured records sharing one experimental "
                                          "condition (its replicates are mapped together).")
    with c2:
        chosen_key = _scenario_record_key_picker(run_name, manifest, key=f"cond_scn_{run_name}")

    sample = _condition_representative_sample(data, sel_condition)
    scenario = _manifest_row(manifest, chosen_key)
    status = _render_mapping_status_line(sample, scenario)

    notes = st.text_input(
        "Notes (optional)", key=f"cond_notes_{run_name}",
        help="Free-text validation context. Stored with the mapping; never sent to "
             "the comparison step.",
    )

    # Unsafe mappings can ONLY be saved here, and only with explicit confirmation;
    # the saved mapping is tagged override=true in the condition-mapping CSV.
    is_unsafe = (status == replicates.MAPPING_STATUS_UNSAFE)
    override_confirm = False
    if is_unsafe:
        st.error(
            "This mapping is **unsafe** — a known metadata conflict (e.g. an acid leachant "
            "mapped to a NaOH/CO₂ scenario, or opposite CO₂ families). It cannot be accepted "
            "from the suggestion table; saving it here records `override=true`."
        )
        override_confirm = st.checkbox(
            "I understand this mapping is unsafe and want to override and save it anyway.",
            key=f"cond_override_{run_name}",
        )

    if st.button("Save mapping", key=f"cond_map_{run_name}", type="primary"):
        if not chosen_key:
            st.warning("Pick a model / simulation result first.")
        elif is_unsafe and not override_confirm:
            st.warning("Tick the override confirmation to save an unsafe mapping.")
        else:
            try:
                run_manager.add_condition_mapping(
                    run_name, sel_condition, chosen_key, notes=notes, override=is_unsafe)
                run_manager.apply_condition_mapping(run_name)
                n = len(run_manager.read_mapping(run_name))
                st.success(
                    f"Saved & applied — condition `{sel_condition}` → `{chosen_key}` "
                    f"({n} sample row(s) mapped)."
                    + (" Tagged `override=true`." if is_unsafe else "")
                )
                if status != replicates.MAPPING_STATUS_EXACT:
                    st.info(
                        "This mapping is not *exact*, so any comparison graph is a "
                        "**preliminary / workflow check only**."
                    )
                _read_csv.clear()
                st.rerun()
            except run_manager.RunManagerError as exc:
                st.error(str(exc))

    with st.expander("Advanced validation metadata"):
        _render_advanced_validation_metadata(sample, scenario)


def _render_condition_mapping_advanced(run_name: str, cond_map: pd.DataFrame) -> None:
    """Advanced apply-options (replicate→solution) + delete, kept out of the main view."""
    with st.expander("Advanced apply options & replicate→solution mapping"):
        st.warning(
            "⚠️ Only use replicate→solution mapping if sol1/sol2/sol3 represent **replicate "
            "batches**, not time points or unrelated solutions."
        )
        st.caption("Map each replicate id to a PHREEQC solution number (e.g. R1→1, R2→2), "
                   "then re-apply to point each replicate at its own solution.")
        rc1, rc2, rc3 = st.columns(3)
        rep_in = rc1.text_input("replicate_id (e.g. R1)", key=f"repsol_rid_{run_name}")
        sol_in = rc2.text_input("solution_number (e.g. 1)", key=f"repsol_sol_{run_name}")
        if rc3.button("Add R→sol", key=f"repsol_add_{run_name}"):
            try:
                run_manager.add_replicate_solution(run_name, rep_in, sol_in)
                _read_csv.clear()
                st.rerun()
            except run_manager.RunManagerError as exc:
                st.error(str(exc))
        rs_map = run_manager.read_replicate_solution_map(run_name)
        if not rs_map.empty:
            st.dataframe(rs_map, use_container_width=True, height=120)
        use_rep_sol = st.checkbox(
            "Re-apply using replicate→solution mapping (instead of one scenario for all replicates)",
            key=f"repsol_use_{run_name}",
        )
        if st.button("Re-apply condition mapping → per-sample map", key=f"cond_apply_{run_name}"):
            try:
                path = run_manager.apply_condition_mapping(run_name, use_replicate_solution=use_rep_sol)
                n = len(run_manager.read_mapping(run_name))
                st.success(f"Applied — {n} sample row(s) written to `{path.relative_to(_PROJECT_ROOT)}`.")
                _read_csv.clear()
                st.rerun()
            except run_manager.RunManagerError as exc:
                st.error(str(exc))

    with st.expander("Delete condition mappings"):
        to_del = st.multiselect(
            "Rows to delete", options=list(range(len(cond_map))),
            format_func=lambda i: f"{cond_map.iloc[i]['condition_key']} → "
                                  f"{cond_map.iloc[i]['phreeqc_record_key']}",
            key=f"cond_del_{run_name}",
        )
        if st.button("Delete selected condition mappings", key=f"cond_delbtn_{run_name}") and to_del:
            run_manager.delete_condition_mapping_rows(run_name, to_del)
            _read_csv.clear()
            st.rerun()


def _render_replicate_collision_warnings(data: pd.DataFrame, mapping: pd.DataFrame,
                                         manifest: pd.DataFrame) -> None:
    """Feature 7 — replicate-aware mapping safety warnings."""
    warns = replicates.collision_report(data, mapping, manifest)
    if not warns:
        st.success("No replicate-aware mapping problems. (Replicates of one condition sharing a "
                   "PHREEQC scenario is expected, not a collision.)")
        return
    # Keep the (often repeated) detail tidy for presentation.
    st.warning(f"⚠️ {len(warns)} replicate-aware mapping warning(s) — expand for detail.")
    with st.expander("Mapping warning detail"):
        seen: set[str] = set()
        for w in warns:
            if w["message"] in seen:
                continue
            seen.add(w["message"])
            st.markdown(f"- {w['message']}")


def _render_mapping_section(run_name: str) -> None:
    """Measured-data → model-prediction mapping UI for a lab-like run.

    Automatic-first: a single suggestion table is generated as soon as run data +
    model results exist (no button). Workflow order: **suggestion table → accept →
    existing mappings → conditions needing simulation → advanced tools**. Manual
    dropdown mapping, the scenario explorer and the per-sample assistant are kept
    under expanders. Saves to the run's own ``data/sample_phreeqc_map.csv``.
    """
    st.markdown("---")
    st.subheader("Match measured data to model predictions")
    st.caption(
        "Automatic-first: a **suggestion table** is built as soon as data + model results "
        "exist — review, accept, then graph. Transparent rule-based scoring (no ML); "
        "manual mapping, scenario explorer and per-sample assistant are under advanced."
    )

    data = run_manager.read_data_file(run_name)

    sample_ids: list[str] = []
    if "sample_id" in data.columns:
        for s in data["sample_id"].astype(str).map(str.strip).tolist():
            if s and s.lower() != "nan":
                sample_ids.append(s)
    sample_ids = list(dict.fromkeys(sample_ids))  # unique, order-preserving

    # Item 1 — no measured data: a clear message, and no stale suggestions/state.
    if not sample_ids:
        for k in (f"map_flash_{run_name}", f"sug_editor_{run_name}", f"detail_sel_{run_name}"):
            st.session_state.pop(k, None)
        st.info(
            "No measured data found for this run. Upload or import a dataset in the "
            "**Data** tab first."
        )
        return

    graph_note = (
        "Graphs only require measured values, model-predicted values, and a saved "
        "mapping between them. Extra metadata is retained for scientific validation "
        "and interpretation."
    )
    if _dataset_condition_codes(data):
        graph_note += (
            "\n\nFor this dataset, OA/PF/GS and CO₂ exposure are treated as "
            "validation metadata."
        )
    st.info(graph_note)

    results_path = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    if not results_path.exists():
        st.info(
            "`data/processed/phreeqc_results.csv` not found — run Phase 1 first "
            "(the Run + Results tab) to generate model (PHREEQC) results."
        )
        return
    phreeqc = _read_csv(str(results_path), results_path.stat().st_mtime)
    if "record_key" not in phreeqc.columns:
        st.warning("`phreeqc_results.csv` has no `record_key` column — cannot map.")
        return

    manifest = _scenario_manifest(str(results_path), results_path.stat().st_mtime)
    mapping = run_manager.read_mapping(run_name)
    cond_map = run_manager.read_condition_mapping(run_name)

    # Build the suggestion table ONCE; the table view and the "needs new simulation"
    # section are both driven from it, so their counts always agree.
    table = mapping_table.build_suggestion_table(data, manifest, cond_map)

    # ---- 1) Suggestion table (auto-generated) + accept actions ----
    _render_suggestion_table(run_name, data, manifest, table)

    # ---- 2) Existing/accepted mappings + overall status ----
    st.markdown("---")
    _render_saved_condition_mappings(run_name)
    overall = replicates.overall_mapping_status(data, mapping, manifest)
    if not mapping.empty:
        line = " · ".join(f"{k}: {v}" for k, v in overall.get("counts", {}).items())
        if overall.get("all_exact"):
            st.success(f"Overall mapping status: **{overall.get('overall')}** — {line}")
        else:
            st.warning(
                f"Overall mapping status: **{overall.get('overall')}** — {line}. "
                "Comparison graphs are a **preliminary / workflow check only** until "
                "mappings are exact."
            )

    # ---- 3) Conditions needing new simulations (same table source) ----
    st.markdown("---")
    _render_sim_needed(run_name, data, mapping, manifest, table)

    _render_condition_code_help(data)

    # ---- 4) Advanced tools (all functionality kept, just demoted) ----
    with st.expander("Manual override / advanced mapping"):
        st.caption(
            "Pick a measured data group and model result by hand if a suggestion is wrong. "
            "**Unsafe** mappings can only be saved here, with a confirmation that records "
            "`override=true`."
        )
        _render_condition_mapping(run_name, data, manifest)
        st.markdown("---")
        st.markdown("**Per-sample manual mapping**")
        _render_manual_mapping(run_name, phreeqc, sample_ids)

    with st.expander("Validation context: replicate summary & mapping warnings"):
        _render_replicate_summary(data)
        st.markdown("---")
        _render_mapping_quality(mapping)
        _render_replicate_collision_warnings(data, mapping, manifest)

    with st.expander("Explore PHREEQC scenarios"):
        _render_scenario_explorer(run_name, manifest)

    with st.expander("Per-sample assistant (advanced)"):
        _render_mapping_assistant(run_name, data, sample_ids, manifest)

    with st.expander("Per-sample mappings: view, delete & export to pipeline"):
        st.markdown(f"**Existing per-sample mappings** ({len(mapping)}):")
        st.dataframe(mapping, use_container_width=True, height=170)
        if not mapping.empty:
            def _map_label(i: int) -> str:
                r = mapping.iloc[i]
                return f"Row {i} — {r.get('sample_id', '')} → {r.get('phreeqc_record_key', '')}"
            to_del = st.multiselect(
                "Select mapping rows to delete", options=list(range(len(mapping))),
                format_func=_map_label, key=f"map_del_{run_name}",
            )
            confirm = st.checkbox(
                "I understand this will delete the selected mapping rows.",
                key=f"map_delc_{run_name}",
            )
            if st.button("Delete selected mappings", key=f"map_delbtn_{run_name}"):
                if not to_del:
                    st.warning("No mapping rows selected — nothing was deleted.")
                elif not confirm:
                    st.warning("Tick the confirmation checkbox before deleting.")
                else:
                    n = run_manager.delete_mapping_rows(run_name, to_del)
                    st.success(f"Deleted {n} mapping row(s).")
                    st.rerun()

            if st.button("➡️ Export mapping to pipeline", key=f"map_export_{run_name}"):
                try:
                    dest = run_manager.export_mapping_to_pipeline(run_name)
                    st.success(
                        f"Copied mapping to {dest.relative_to(_PROJECT_ROOT)} — step 05 "
                        "will use it to compute residuals."
                    )
                    _read_csv.clear()
                except run_manager.RunManagerError as exc:
                    st.error(str(exc))


# --------------------------------------------------------------------------- #
# Results summary + comparison preview (presentation-friendly, honest)
# --------------------------------------------------------------------------- #
# (measured_col, display_name) — measured columns are unprefixed in the
# comparison CSV; renamed here so the preview reads "measured_ vs phreeqc_".
_COMPARISON_PREVIEW_SPEC = [
    ("sample_id", "sample_id"),
    ("phreeqc_record_key", "phreeqc_record_key"),
    ("final_pH", "measured_final_pH"),
    ("phreeqc_pH", "phreeqc_pH"),
    ("residual_pH", "residual_pH"),
    ("Ca_mM", "measured_Ca_mM"), ("phreeqc_Ca_mM", "phreeqc_Ca_mM"), ("residual_Ca", "residual_Ca"),
    ("Si_mM", "measured_Si_mM"), ("phreeqc_Si_mM", "phreeqc_Si_mM"), ("residual_Si", "residual_Si"),
    ("Al_mM", "measured_Al_mM"), ("phreeqc_Al_mM", "phreeqc_Al_mM"), ("residual_Al", "residual_Al"),
    ("Fe_mM", "measured_Fe_mM"), ("phreeqc_Fe_mM", "phreeqc_Fe_mM"), ("residual_Fe", "residual_Fe"),
]

_ICP_MEASURED_COLS = ["Ca_mM", "Si_mM", "Al_mM", "Fe_mM", "Na_mM", "K_mM", "Sc_ppb", "total_REE_ppb"]


def _has_numeric(df: pd.DataFrame, col: str) -> bool:
    """True if the column exists and has at least one numeric (non-NaN) value."""
    return col in df.columns and bool(pd.to_numeric(df[col], errors="coerce").notna().any())


def _looks_like_test(comp: pd.DataFrame) -> bool:
    if "sample_id" not in comp.columns:
        return False
    sids = comp["sample_id"].astype(str)
    return bool(sids.str.upper().str.contains("TEST").any())


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


def _run_comparison_figures_dir(run_name: str | None) -> Path | None:
    """The selected run's per-run comparison figures directory, or None."""
    if not run_name:
        return None
    try:
        return run_manager.comparison_figures_dir(run_name)
    except run_manager.RunManagerError:
        return None


def _render_stale_results_warning(run_name: str) -> None:
    """Prominent banner when a run's stored comparison no longer matches its inputs."""
    if not run_manager.has_comparison(run_name):
        return
    current, reasons = run_manager.comparison_is_current(run_name)
    if current:
        return
    st.warning(
        "⚠️ **These results were generated from older data/mappings for this run — "
        "re-run the workflow.** What changed:\n\n"
        + "\n".join(f"- {r}" for r in reasons)
    )


def _comparison_has_residuals(run_name: str | None) -> bool:
    """True if the run's comparison CSV exists and has at least one numeric residual."""
    comp_path = _run_comparison_path(run_name)
    if comp_path is None or not comp_path.exists():
        return False
    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    return any(_has_numeric(comp, f"residual_{el}")
               for el in ["pH", "Ca", "Si", "Al", "Fe"])


def _render_results_summary(run_name: str | None) -> None:
    """Honest, presentation-friendly summary of this run's comparison."""
    if not run_name:
        st.info("Select a lab run in the **Experiment runs** sidebar (left) to see its results.")
        return
    comp_path = _run_comparison_path(run_name)
    if comp_path is None:
        st.info(
            "The selected run is not a lab run, so it has no measured-vs-model comparison."
        )
        return
    if not comp_path.exists():
        st.info(
            "No comparison results yet for this run. Run the workflow (above) for a lab run "
            "that has a measured-data → model mapping to generate this run's comparison."
        )
        return

    _render_stale_results_warning(run_name)

    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    n_rows = len(comp)
    if "phreeqc_record_key" in comp.columns:
        mapped = int(comp["phreeqc_record_key"].apply(
            lambda v: not (pd.isna(v) or str(v).strip() == "")).sum())
    else:
        mapped = 0
    ph_ok = _has_numeric(comp, "residual_pH")
    icp_resid_ok = any(_has_numeric(comp, f"residual_{el}") for el in ["Ca", "Si", "Al", "Fe"])
    icp_missing = not any(_has_numeric(comp, c) for c in _ICP_MEASURED_COLS)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Experimental rows", n_rows)
    m2.metric("Mapped samples", mapped)
    m3.metric("pH residuals", "yes" if ph_ok else "no")
    m4.metric("Ca/Si/Al/Fe residuals", "yes" if icp_resid_ok else "no")

    st.markdown(
        f"- **ICP chemistry:** {'missing (pH-only)' if icp_missing else 'present'}\n"
        f"- **Comparison CSV:** `{comp_path.relative_to(_PROJECT_ROOT)}`"
    )

    # Test/demo guard.
    if _looks_like_test(comp):
        st.error(
            "This appears to be a test/demo row (sample_id contains \"TEST\"). "
            "Do not interpret it as scientific evidence."
        )

    # pH-only mode.
    if ph_ok and not icp_resid_ok:
        st.warning(
            "Only pH residuals are available because ICP chemistry values are blank. "
            "Ca/Si/Al/Fe/REE validation requires ICP-OES/ICP-MS data."
        )

    # Single-sample honesty + pH residual cards.
    if mapped == 1 and ph_ok:
        st.warning(
            "This is a single-sample comparison, not a trend. It only checks one "
            "mapped condition."
        )
        row = comp[pd.to_numeric(comp["residual_pH"], errors="coerce").notna()].iloc[0]
        meas = pd.to_numeric(pd.Series([row.get("final_pH")]), errors="coerce").iloc[0]
        pred = pd.to_numeric(pd.Series([row.get("phreeqc_pH")]), errors="coerce").iloc[0]
        resid = pd.to_numeric(pd.Series([row.get("residual_pH")]), errors="coerce").iloc[0]
        p1, p2, p3 = st.columns(3)
        p1.metric("Measured pH", f"{meas:.2f}" if pd.notna(meas) else "—")
        p2.metric("Model pH", f"{pred:.2f}" if pd.notna(pred) else "—")
        p3.metric("Residual pH (measured − model)", f"{resid:+.2f}" if pd.notna(resid) else "—")

    # Comparison table preview — only columns that exist.
    present = [(src, disp) for src, disp in _COMPARISON_PREVIEW_SPEC if src in comp.columns]
    if present:
        preview = comp[[src for src, _ in present]].rename(columns=dict(present))
        st.markdown("**Comparison table** (measured vs model, existing columns only):")
        st.dataframe(preview, use_container_width=True, height=200)


# Literature-summary columns (only those present are shown). Both reported_final_pH
# (the literature schema name) and final_pH are listed so whichever exists is used.
_LIT_SUMMARY_COLS = [
    "source_id", "paper_title", "year", "fly_ash_class",
    "reported_final_pH", "final_pH",
    "reported_Ca_mM", "reported_Al_mM", "reported_Fe_mM",
    "comparability_to_our_experiment",
]


def _render_literature_summary(run_name: str) -> None:
    """Literature-benchmark read-out — never the lab measured-vs-PHREEQC residual."""
    st.info(
        "This is a literature benchmark run. Literature data are stored separately "
        "and are not treated as measured lab data."
    )
    lit = run_manager.read_data_file(run_name)
    st.metric("Literature rows", len(lit))

    present = [c for c in _LIT_SUMMARY_COLS if c in lit.columns]
    if lit.empty:
        st.info("No literature rows entered yet for this run.")
    elif present:
        st.markdown("**Literature benchmark summary** (existing columns only):")
        st.dataframe(lit[present], use_container_width=True, height=200)
    # No lab-comparison read-out here: comparisons are per-run (stored under the lab
    # run's own outputs/), so a literature run never displays another run's results.


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


def _single_sample_comparison(run_name: str | None) -> bool:
    """True if the run's comparison has exactly one mapped sample (not a trend)."""
    comp_path = _run_comparison_path(run_name)
    if comp_path is None or not comp_path.exists():
        return False
    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    if "phreeqc_record_key" not in comp.columns:
        return False
    mapped = comp["phreeqc_record_key"].apply(
        lambda v: not (pd.isna(v) or str(v).strip() == "")).sum()
    return int(mapped) == 1


def _render_comparison_figures(run_name: str | None) -> None:
    """Measured-vs-PHREEQC + residual plots for the selected run (per-run figures)."""
    fig_dir = _run_comparison_figures_dir(run_name)
    if fig_dir is None or not fig_dir.exists():
        return
    comparison = [p for p in sorted(fig_dir.glob("*.png")) if p.name in _COMPARISON_FIGURES]
    if not comparison:
        return
    st.subheader("Measured vs PHREEQC (residual figures)")
    # The single-sample caveat is folded into the validity line of the inclusion
    # section above, so it is not repeated here.
    for png in comparison:
        st.image(str(png), use_container_width=True)
        st.caption(_FIGURE_CAPTIONS.get(png.name, png.name))


def _render_phreeqc_only_figures() -> None:
    """PHREEQC model-output plots only (pH, element molality, saturation indices,
    …). Excludes the measured-vs-PHREEQC comparison/residual figures."""
    pngs = [p for d in _figure_dirs() if d.exists() for p in sorted(d.glob("*.png"))]
    phreeqc_only = [p for p in pngs if p.name not in _COMPARISON_FIGURES]
    if not phreeqc_only:
        st.warning("No PHREEQC figures yet — run Phase 1 to generate them.")
        return
    st.info(
        "These are **PHREEQC model outputs, not measured experimental data.** "
        "Crowded axis labels come from the many PHREEQC solution states plotted "
        "together — use the selector to view one figure at a time."
    )
    names = [p.name for p in phreeqc_only]
    choice = st.selectbox("Choose a PHREEQC figure", names, key="phreeqc_fig_choice")
    chosen = next(p for p in phreeqc_only if p.name == choice)
    st.image(str(chosen), use_container_width=True)
    st.caption(f"{choice} — PHREEQC model output, not a measurement.")


# --------------------------------------------------------------------------- #
# Shared script-runner button (so Run Workflow + Tools reuse one code path;
# distinct key prefixes keep Streamlit widget identities unique)
# --------------------------------------------------------------------------- #
def _script_button(label: str, script: str, result_label: str, key: str,
                   refresh_csv: bool = False) -> None:
    if st.button(label, use_container_width=True, key=key):
        with st.spinner(f"Running {result_label}…"):
            proc = _run_script(script)
        _show_process_result(result_label, proc)
        if refresh_csv:
            _read_csv.clear()


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
_OA_PF_GS_CAVEAT = (
    "OA, PF, and GS represent CO₂ exposure / cup-cover conditions: OA = open air, "
    "PF = plastic flap cover, GS = glass cover. OA is directly exposed to atmospheric CO₂; "
    "PF and GS are covered conditions that likely reduce CO₂ exchange. They are kept as "
    "distinct experimental conditions because cover material and CO₂ exposure can affect pH "
    "and carbonate formation. Do not treat PF or GS as fully sealed unless airtight sealing "
    "is confirmed."
)


def _manifest_if_available() -> pd.DataFrame:
    rp = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    if rp.exists():
        return _scenario_manifest(str(rp), rp.stat().st_mtime)
    return pd.DataFrame(columns=scenarios.MANIFEST_COLUMNS)


def _render_mapping_status_definitions() -> None:
    """Feature 2 — the four mapping statuses and what they mean."""
    rows = [{"status": k, "meaning": v}
            for k, v in replicates.MAPPING_STATUS_DEFINITIONS.items()]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=180, hide_index=True)


def _render_valid_now_section() -> None:
    """Feature 5 — what is scientifically valid now vs not fully valid yet."""
    a, b = st.columns(2)
    with a:
        st.markdown("**✅ Valid now**")
        st.markdown("\n".join(f"- {x}" for x in _VALID_NOW))
    with b:
        st.markdown("**🚧 Not fully valid yet**")
        st.markdown("\n".join(f"- {x}" for x in _NOT_VALID_YET))


def _rows_with_any_numeric(data: pd.DataFrame, cols: list[str]) -> int:
    present = [c for c in cols if c in data.columns]
    if not present or data.empty:
        return 0
    num = data[present].apply(pd.to_numeric, errors="coerce")
    return int(num.notna().any(axis=1).sum())


def _render_presentation_summary(selected_run: str | None) -> None:
    """Feature 1 — a single honest status panel near the top of the Start tab."""
    if not selected_run:
        st.info("Select or create a run in the **Experiment runs** sidebar (left) for a summary.")
        return
    cfg = run_manager.load_run_config(selected_run)
    rt = cfg.get("run_type")
    lab_like = rt in run_manager.LAB_LIKE_RUN_TYPES
    data = run_manager.read_data_file(selected_run)

    n_rows = len(data)
    rows_with_ph = _rows_with_any_numeric(data, ["final_pH"])
    rows_with_chem = _rows_with_any_numeric(data, ["Ca_mM", "Si_mM", "Al_mM"])

    issues = (validate_experimental_df(data, source=selected_run)
              if lab_like and not data.empty else [])
    n_err = sum(1 for i in issues if i.get("severity") == "error")
    n_warn = sum(1 for i in issues if i.get("severity") == "warning")

    mapping = run_manager.read_mapping(selected_run) if lab_like else pd.DataFrame()
    msum = run_manager.summarize_mapping(mapping)
    manifest = _manifest_if_available()
    status = (replicates.overall_mapping_status(data, mapping, manifest)
              if lab_like else {"overall": "n/a", "all_exact": False,
                                "counts": {}, "n_mapped": 0, "n_unmapped": 0})
    comp_exists = lab_like and run_manager.has_comparison(selected_run)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Dataset imported", "yes" if n_rows else "no")
    m2.metric("Experimental rows", n_rows)
    m3.metric("Rows with pH", rows_with_ph)
    m4.metric("Rows with Ca/Si/Al", rows_with_chem)
    n1, n2, n3, n4 = st.columns(4)
    n1.metric("Validation errors", n_err)
    n2.metric("Validation warnings", n_warn)
    n3.metric("Mapped samples", msum["n_samples"])
    n4.metric("Unique model results", msum["n_unique_rows"])

    overall = status["overall"]
    _counts = status["counts"]
    st.markdown(f"**Overall mapping status:** `{overall}`"
                + ("" if not lab_like else
                   f"  ·  exact: {_counts.get(replicates.MAPPING_STATUS_EXACT, 0)}, "
                   f"scenario-level: {_counts.get(replicates.MAPPING_STATUS_SCENARIO, 0)}, "
                   f"unsafe: {_counts.get(replicates.MAPPING_STATUS_UNSAFE, 0)}, "
                   f"needs new sim: {_counts.get(replicates.MAPPING_STATUS_NEEDS_NEW, 0)}"))
    st.markdown(f"**Comparison status:** {'available (preliminary)' if comp_exists else 'not run yet'}")

    # Recommended next *scientific* step.
    if not n_rows:
        nxt = "Import the dissolution workbook in the **Data** tab."
    elif n_err:
        nxt = f"Fix {n_err} validation error(s) in the **Data** tab before mapping."
    elif lab_like and msum["n_samples"] == 0:
        nxt = "Map conditions to model scenarios in the **Match PHREEQC** tab."
    elif status["counts"].get("unsafe", 0):
        nxt = ("Resolve unsafe mapping(s) (e.g. HCl mapped to a NaOH/CO2 scenario) — generate "
               "matching acid PHREEQC scenarios.")
    elif lab_like and not status["all_exact"]:
        nxt = ("Generate time- and condition-resolved PHREEQC scenarios so mappings can become "
               "exact; the current comparison is a workflow check, not final validation.")
    elif not comp_exists:
        nxt = "Run the workflow in the **Run + Results** tab to generate the comparison."
    else:
        nxt = "Review the preliminary comparison in the **Run + Results** tab."
    st.success(f"**Recommended next scientific step:** {nxt}")

    if not (lab_like and status["all_exact"]):
        st.warning("⚠️ " + _PRELIMINARY_CAVEAT)

    with st.expander("Mapping status definitions"):
        _render_mapping_status_definitions()
    with st.expander("What is valid now vs not fully valid yet"):
        _render_valid_now_section()


# --------------------------------------------------------------------------- #
# Tab renderers — each is a self-contained view; all reuse the helpers above.
# --------------------------------------------------------------------------- #
def _render_overview(selected_run: str | None) -> None:
    """Project status cards + selected-run summary + a recommended next step."""
    st.subheader("Presentation summary")
    _render_presentation_summary(selected_run)
    st.divider()

    master_path = config.PROCESSED_DIR / config.MASTER_DATASET_CSV
    template_path = config.EXPERIMENTAL_ICP_DIR / config.EXPERIMENTAL_TEMPLATE_CSV
    measured = _load_measured_safe()
    measured_exists = has_measured_data(measured)

    st.subheader("Project status")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("master_dataset.csv", "present" if master_path.exists() else "missing")
    n_rows = (
        len(_read_csv(str(master_path), master_path.stat().st_mtime))
        if master_path.exists() else 0
    )
    c2.metric("master rows", n_rows)
    c3.metric("release template", "present" if template_path.exists() else "missing")
    c4.metric("measured data", "yes" if measured_exists else "not yet")
    if not measured_exists:
        st.info(
            "No measured experimental release data found yet — only the blank template. "
            "Phase 2 comparison and any future ML stay dormant until real data is entered."
        )

    st.divider()
    st.subheader("Selected run")
    if not selected_run:
        st.info("No run selected. Create or open one in the **Experiment runs** sidebar (left).")
        return

    cfg = run_manager.load_run_config(selected_run)
    rt = cfg.get("run_type")
    data = run_manager.read_data_file(selected_run)
    lab_like = rt in run_manager.LAB_LIKE_RUN_TYPES
    has_map = run_manager.has_mapping(selected_run) if lab_like else False
    map_summary = (
        run_manager.summarize_mapping(run_manager.read_mapping(selected_run))
        if lab_like else {"n_samples": 0, "n_unique_rows": 0, "has_collisions": False}
    )
    icp_present = lab_like and any(_has_numeric(data, c) for c in _ICP_MEASURED_COLS)
    is_mock = _looks_like_run_test(data)
    comp_exists = lab_like and run_manager.has_comparison(selected_run)

    st.markdown(f"**`{selected_run}`**")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Run type", rt)
    s2.metric("Data rows", len(data))
    s3.metric("Mapped samples", map_summary["n_samples"] if lab_like else "n/a")
    s4.metric("Unique model results used",
              map_summary["n_unique_rows"] if lab_like else "n/a")
    st.caption(f"📁 {run_manager.run_dir(selected_run).relative_to(_PROJECT_ROOT)} · source `{cfg.get('data_source')}`")

    # Data quality status (one honest line).
    if data.empty:
        quality = "⚪ No data entered yet."
    elif is_mock:
        quality = "🔴 Mock/test data detected — for code checking only, not evidence."
    elif lab_like and not icp_present:
        quality = "🟡 pH-only — ICP chemistry (Ca/Si/Al/Fe/REE) not yet entered."
    elif lab_like and icp_present:
        quality = "🟢 Full ICP chemistry present."
    else:
        quality = f"🟢 {len(data)} row(s) entered."
    st.markdown(f"**Data quality:** {quality}")

    # What's missing.
    missing: list[str] = []
    if data.empty:
        missing.append("no data rows entered yet")
    if lab_like:
        if not icp_present:
            missing.append("ICP chemistry (Ca/Si/Al/Fe/REE) — pH-only so far")
        if not has_map:
            missing.append("sample → PHREEQC mapping (needed for residuals)")
    if missing:
        st.markdown("**Missing / not yet present:**")
        for m in missing:
            st.markdown(f"- {m}")

    # Recommended next action.
    if data.empty:
        nxt = "Upload or enter lab data in the **Data** tab."
    elif is_mock:
        nxt = "Use only for code checking, not scientific conclusions."
    elif rt == "literature_benchmark":
        nxt = ("Review the literature table in the **Data** tab. Literature data are "
               "kept separate from lab data and are not run through the pipeline.")
    elif rt == "synthetic_demo":
        nxt = "This is a synthetic/demo run — for testing only, not scientific output."
    elif lab_like and not has_map:
        nxt = "Map samples to model scenarios in the **Match PHREEQC** tab."
    elif lab_like and map_summary["has_collisions"]:
        nxt = ("Review mapping in the **Match PHREEQC** tab; several samples share one "
               "PHREEQC row, so graphs may be misleading.")
    elif not comp_exists:
        nxt = "Run the workflow in the **Run + Results** tab to generate results."
    elif lab_like and not icp_present:
        nxt = "Only pH comparison is meaningful until ICP data are added."
    else:
        nxt = "Read the comparison in the **Run + Results** tab."
    st.success(f"**Recommended next action:** {nxt}")

    # Workflow checklist.
    st.divider()
    st.subheader("Workflow checklist")
    data_uploaded = not data.empty
    data_checked = (config.TABLES_DIR / config.EXPERIMENTAL_VALIDATION_REPORT_CSV).exists()
    mapping_complete = lab_like and has_map
    workflow_run = comp_exists
    results_available = comp_exists and _comparison_has_residuals(selected_run)

    def _check(done: bool, label: str) -> str:
        return f"{'✅' if done else '⬜'} {label}"

    st.markdown(
        "\n".join([
            _check(data_uploaded, "Data uploaded"),
            _check(data_checked, "Data checked"),
            _check(mapping_complete, "Mapping complete"),
            _check(workflow_run, "Workflow run"),
            _check(results_available, "Results available"),
        ])
    )


def _render_run_data_and_edit(run_name: str, rt: str) -> None:
    """This run's data table + row deletion + CSV/pipeline export (no mapping)."""
    data = run_manager.read_data_file(run_name)
    st.markdown(f"**This run's data** ({len(data)} row(s)):")
    st.dataframe(data, use_container_width=True, height=300)

    # --- Delete / clean rows --- only affects THIS run's CSV.
    if not data.empty:
        with st.expander("🗑️ Delete rows", expanded=False):
            id_col = run_manager.id_column_for(run_name)

            def _row_label(i: int) -> str:
                if id_col in data.columns:
                    val = data.iloc[i][id_col]
                    shown = "" if pd.isna(val) else str(val).strip()
                    return f"Row {i} — {id_col}={shown or '(blank)'}"
                return f"Row {i}"

            to_delete = st.multiselect(
                "Select row numbers to delete",
                options=list(range(len(data))),
                format_func=_row_label,
                key=f"del_rows_{run_name}",
            )
            confirm = st.checkbox(
                "I understand this will delete the selected rows from this run's CSV.",
                key=f"del_confirm_{run_name}",
            )
            if st.button("Delete selected rows", key=f"del_btn_{run_name}"):
                if not to_delete:
                    st.warning("No rows selected — nothing was deleted.")
                elif not confirm:
                    st.warning("Tick the confirmation checkbox before deleting.")
                else:
                    n = run_manager.delete_data_rows(run_name, to_delete)
                    st.success(f"Deleted {n} row(s) from this run's CSV.")
                    _read_csv.clear()
                    st.rerun()

            st.divider()
            st.caption(f"Remove rows with a blank `{id_col}` or where every value is empty.")
            if st.button("Remove blank rows", key=f"del_blank_{run_name}"):
                n = run_manager.remove_blank_data_rows(run_name)
                if n:
                    st.success(f"Removed {n} blank row(s).")
                    _read_csv.clear()
                    st.rerun()
                else:
                    st.info("No blank rows found.")

    # Export this run's CSV.
    ec1, ec2 = st.columns(2)
    with ec1:
        if not data.empty:
            st.download_button(
                "⬇️ Export this run's CSV",
                data=data.to_csv(index=False).encode("utf-8"),
                file_name=f"{run_name}_{run_manager.spec_for(rt).data_filename}",
                mime="text/csv",
                use_container_width=True,
            )
    with ec2:
        if rt in run_manager.LAB_LIKE_RUN_TYPES:
            if st.button("➡️ Export to pipeline (manual-entry CSV)", use_container_width=True,
                         key=f"export_pipe_{run_name}"):
                try:
                    dest = run_manager.export_lab_run_to_pipeline(run_name)
                    st.success(
                        f"Copied to {dest.relative_to(_PROJECT_ROOT)} — the existing "
                        "scripts (05/07) will pick it up."
                    )
                    _read_csv.clear()
                except run_manager.RunManagerError as exc:
                    st.error(str(exc))


def _render_basic_validation_summary(run_name: str) -> None:
    """Quick error/warning count over this lab run's data (reuses the validator)."""
    data = run_manager.read_data_file(run_name)
    if data.empty:
        return
    issues = validate_experimental_df(data, source=run_name)
    real = [i for i in issues if i.get("severity") in ("error", "warning")]
    errors = [i for i in real if i["severity"] == "error"]
    warnings = [i for i in real if i["severity"] == "warning"]

    st.markdown("**Basic data validation**")
    v1, v2 = st.columns(2)
    v1.metric("Errors", len(errors))
    v2.metric("Warnings", len(warnings))
    if not real:
        st.success("No validation errors or warnings on the entered rows.")
    else:
        report = pd.DataFrame(real)[["severity", "check", "column", "message"]]
        st.dataframe(report, use_container_width=True, height=200)


def _render_data_entry_tab(selected_run: str | None) -> None:
    if not selected_run:
        st.info("Select or create a run in the **Experiment runs** sidebar (left) to enter data.")
        return
    cfg = run_manager.load_run_config(selected_run)
    rt = cfg.get("run_type")
    st.subheader(f"Run `{selected_run}` — {rt}")
    _run_type_warning(rt)

    if rt in run_manager.LAB_LIKE_RUN_TYPES:
        _lab_data_import(selected_run)
        st.divider()
        _lab_entry_form(selected_run)
    elif rt == "literature_benchmark":
        _literature_entry(selected_run)
    elif rt == "synthetic_demo":
        _demo_entry(selected_run)

    st.divider()
    _render_run_data_and_edit(selected_run, rt)

    if rt in run_manager.LAB_LIKE_RUN_TYPES:
        st.divider()
        _render_basic_validation_summary(selected_run)

    st.divider()
    with st.expander("Legacy global data entry — not recommended", expanded=False):
        st.caption(
            "This form predates per-run save files and writes to one shared "
            "pipeline file. Prefer the run-specific entry above."
        )
        _render_legacy_global_form()


def _render_mapping_tab(selected_run: str | None) -> None:
    if not selected_run:
        st.info(
            "Select or create a **lab_experiment** (or **plastic_composite**) run in the "
            "sidebar to add a sample → PHREEQC mapping."
        )
        return
    rt = run_manager.load_run_config(selected_run).get("run_type")
    if rt == "literature_benchmark":
        st.info(
            "Literature benchmark runs do not use sample-to-PHREEQC mapping as measured "
            "lab data."
        )
        return
    if rt not in run_manager.LAB_LIKE_RUN_TYPES:
        st.info(
            "Mapping is only available for **lab_experiment** or **plastic_composite** "
            "runs. The current run is a synthetic/demo run (testing only)."
        )
        return
    _render_mapping_section(selected_run)


def _render_run_workflow_tab(selected_run: str | None) -> None:
    st.write(
        "Run all the relevant scripts in order for the selected run and see their output. "
        "For a lab run this exports the run's data (and mapping) to the pipeline, then "
        "runs Phase 1 → validate → compare → sustainability, stopping at the first failure."
    )
    if not selected_run:
        st.info(
            "Select or create a run in the **Experiment runs** sidebar (left) first, then "
            "this button will run the workflow for it."
        )
    else:
        rt = run_manager.load_run_config(selected_run).get("run_type")
        st.caption(f"Selected run: `{selected_run}` — **{rt}**")
        if st.button("▶️ Run selected experiment workflow", type="primary", key="wf_run_btn"):
            if rt in run_manager.LAB_LIKE_RUN_TYPES:
                _run_lab_workflow(selected_run)
            elif rt == "literature_benchmark":
                st.warning(
                    "📚 This is a **literature-benchmark** run. Literature data are kept "
                    "separate from our measured lab data and are **not** run through the "
                    "measured-vs-PHREEQC pipeline. Nothing was exported."
                )
                _lit = run_manager.read_data_file(selected_run)
                if not _lit.empty:
                    st.markdown("**Literature benchmark data:**")
                    st.dataframe(_lit, use_container_width=True, height=300)
                else:
                    st.info("No literature rows entered yet.")
            elif rt == "synthetic_demo":
                st.warning(
                    "🧩 This is a **synthetic/demo** run. Synthetic data are only for "
                    "testing the code — they are not real experimental data and are not "
                    "run through the pipeline."
                )

    with st.expander("Advanced individual script controls", expanded=False):
        st.caption("Low-level: run a single script and view its raw output.")
        a1, a2 = st.columns(2)
        with a1:
            _script_button("Run Phase 1 pipeline", "scripts/run_phase1.py", "Phase 1",
                           "adv_phase1", refresh_csv=True)
        with a2:
            _script_button("Run Phase 2 comparison", "scripts/05_compare_experimental.py",
                           "Phase 2", "adv_phase2", refresh_csv=True)
        b1, b2 = st.columns(2)
        with b1:
            _script_button("Validate experimental CSVs",
                           "scripts/07_validate_experimental_data.py", "Validation", "adv_validate")
        with b2:
            _script_button("Run sustainability score", "scripts/08_sustainability_score.py",
                           "Sustainability score", "adv_sustain")


def _render_condition_results(selected_run: str | None) -> None:
    """Feature 6 — replicate-aware results: condition mean ± std vs PHREEQC."""
    if not selected_run:
        return
    data = run_manager.read_data_file(selected_run)
    if data.empty or "sample_id" not in data.columns:
        return

    st.markdown("#### Condition-level replicate comparison")
    results_path = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    if results_path.exists():
        manifest = _scenario_manifest(str(results_path), results_path.stat().st_mtime)
    else:
        manifest = pd.DataFrame(columns=scenarios.MANIFEST_COLUMNS)
    cond_map = run_manager.read_condition_mapping(selected_run)

    mode = st.radio(
        "Comparison mode", ["Replicate mean comparison", "Individual replicate comparison"],
        key=f"cmp_mode_{selected_run}",
        help="Default compares each condition's replicate mean ± std to PHREEQC; the other "
             "compares each replicate row to its mapped PHREEQC solution.",
    )

    if mode.startswith("Replicate mean"):
        comp = replicates.condition_mean_comparison(data, cond_map, manifest)
        if comp.empty:
            st.info("No conditions to compare yet.")
            return
        st.dataframe(comp, use_container_width=True, height=260)
        n_lt2 = int((comp["n_replicates"] < 2).sum())
        if n_lt2:
            st.warning(f"⚠️ {n_lt2} condition(s) have fewer than 2 replicates — no standard "
                       "deviation, so the mean is a single measurement.")
        metric = st.selectbox("Metric to plot", replicates.VALUE_COLUMNS,
                              key=f"cmp_metric_{selected_run}")
        _render_condition_errorbar(comp, metric)
        with st.expander("Advanced: individual replicate scatter"):
            ind = replicates.individual_replicate_comparison(
                data, run_manager.read_mapping(selected_run), manifest)
            st.dataframe(ind, use_container_width=True, height=240)
    else:
        ind = replicates.individual_replicate_comparison(
            data, run_manager.read_mapping(selected_run), manifest)
        if ind.empty:
            st.info("No replicate rows to compare yet.")
            return
        st.dataframe(ind, use_container_width=True, height=300)
        st.caption("Each replicate vs its mapped PHREEQC solution (residual = measured − PHREEQC).")
    st.markdown("---")


def _render_condition_errorbar(comp: pd.DataFrame, metric: str) -> None:
    """Measured mean ± std vs PHREEQC prediction, one point per condition."""
    label = replicates.RESIDUAL_LABEL[metric]
    sub = comp.dropna(subset=[f"mean_{metric}"]).reset_index(drop=True)
    if sub.empty:
        st.caption(f"No measured `{metric}` values to plot.")
        return
    import matplotlib.pyplot as plt  # lazy: only when a figure is drawn

    x = list(range(len(sub)))
    fig, ax = plt.subplots(figsize=(7, 3.6))
    ax.errorbar(x, sub[f"mean_{metric}"], yerr=sub[f"std_{metric}"].fillna(0.0),
                fmt="o", capsize=4, label="measured mean ± std")
    pheq = pd.to_numeric(sub[f"phreeqc_{label}"], errors="coerce")
    if pheq.notna().any():
        ax.scatter(x, pheq, marker="x", color="red", s=60, label="PHREEQC")
    ax.set_xticks(x)
    ax.set_xticklabels(sub["condition_key"], rotation=45, ha="right", fontsize=7)
    ax.set_ylabel(metric)
    ax.legend(fontsize=8)
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)


def _render_overview_plot(ov: dict, variable: str, overlay: bool) -> None:
    """Matplotlib plot for the measured-data overview (points colored by condition)."""
    import matplotlib.pyplot as plt  # lazy: only when a figure is drawn

    plot = ov["plot"]
    tcol = measured_overview.TIME_COLUMN
    conditions = sorted(plot["condition_key"].astype(str).unique())
    cmap = plt.get_cmap("tab10" if len(conditions) <= 10 else "tab20")
    color = {c: cmap(i % cmap.N) for i, c in enumerate(conditions)}
    stats = {str(r["condition_key"]): r for _, r in ov["group_stats"].iterrows()}

    fig, ax = plt.subplots(figsize=(7.5, 4))
    use_time = ov["has_time"] and tcol in plot.columns and plot[tcol].notna().any()

    if use_time:
        for c in conditions:
            sub = plot[plot["condition_key"].astype(str) == c]
            ax.scatter(pd.to_numeric(sub[tcol], errors="coerce"), sub["value"],
                       color=color[c], label=c, edgecolor="black", linewidth=0.3, zorder=3)
            if overlay and c in stats:
                xt = pd.to_numeric(sub[tcol], errors="coerce").mean()
                g = stats[c]
                ax.errorbar(xt, g["mean"], yerr=(0.0 if pd.isna(g["std"]) else g["std"]),
                            fmt="_", color=color[c], capsize=4, elinewidth=1.6, zorder=2)
        ax.set_xlabel(tcol)
    else:
        pos = {c: i for i, c in enumerate(conditions)}
        for c in conditions:
            sub = plot[plot["condition_key"].astype(str) == c]
            ax.scatter([pos[c]] * len(sub), sub["value"], color=color[c], label=c,
                       edgecolor="black", linewidth=0.3, zorder=3)
            if overlay and c in stats:
                g = stats[c]
                ax.errorbar(pos[c], g["mean"], yerr=(0.0 if pd.isna(g["std"]) else g["std"]),
                            fmt="_", color="black", capsize=5, elinewidth=1.6, zorder=2)
        ax.set_xticks(range(len(conditions)))
        ax.set_xticklabels(conditions, rotation=45, ha="right", fontsize=7)

    ax.set_ylabel(variable)
    ax.set_title(f"{variable} — measured data only")
    if len(conditions) <= 12:
        ax.legend(fontsize=7, title="condition", loc="best")
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)


def _render_measured_overview(selected_run: str) -> None:
    """First plot family — measured data only, from the run's own rows.

    Renders fully without a sample→PHREEQC mapping and without
    ``data/processed/phreeqc_results.csv`` (it reads nothing but the run's data).
    """
    st.markdown("#### Measured data overview")
    st.caption("Measured data only — no model comparison.")

    data = run_manager.read_data_file(selected_run)
    variables = measured_overview.available_variables(data)
    if not variables:
        st.info(
            "No numeric measured variables in this run yet — enter pH or ICP values in "
            "the **Data** tab to see the overview."
        )
        return

    c1, c2 = st.columns([3, 2])
    variable = c1.selectbox("Measured variable", variables, key=f"overview_var_{selected_run}")
    overlay = c2.checkbox("Overlay condition mean ± std", value=True,
                          key=f"overview_overlay_{selected_run}")

    ov = measured_overview.prepare_overview(data, variable)
    rep_counts = ov["replicate_counts"]
    rc_txt = ", ".join(f"{k}: {v}" for k, v in sorted(rep_counts.items())) if rep_counts else "—"
    st.markdown(
        f"- **Rows shown:** {ov['n_shown']}  ·  **Distinct conditions:** {ov['n_conditions']}  "
        f"·  **Rows excluded:** {ov['n_excluded']}"
    )
    st.caption(f"Replicate counts per condition — {rc_txt}")

    if ov["n_excluded"]:
        with st.expander(f"Excluded rows ({ov['n_excluded']}) — blank or non-numeric values"):
            st.dataframe(ov["excluded"], use_container_width=True, height=200)

    if ov["plot"].empty:
        st.info(f"No numeric `{variable}` values to plot.")
        return
    _render_overview_plot(ov, variable, overlay)


def _inclusion_variables(comp: pd.DataFrame) -> list[str]:
    """Variables with a measured column (numeric) + a model-prediction column."""
    out = []
    for v, (mcol, pcol) in compare_inclusion.VARIABLE_SPEC.items():
        if mcol in comp.columns and pcol in comp.columns and _has_numeric(comp, mcol):
            out.append(v)
    return out


# Counts panel: (dict key, label) — built entirely from the inclusion dict.
_INCLUSION_METRICS = [
    ("measured_rows_available", "Measured rows"),
    ("rows_with_mapping", "With mapping"),
    ("rows_prediction_available", "Prediction available"),
    ("rows_plotted", "Plotted"),
    ("unique_predictions_used", "Unique predictions"),
    ("unmapped_rows", "Unmapped"),
]


def _render_comparison_inclusion(selected_run: str) -> None:
    """Explicit inclusion view: counts, exclusion reasons, status-aware scatter, validity.

    All filtering comes from the single `compare.comparison_inclusion` function — this
    renderer only displays its output (the plots never re-derive the filter).
    """
    comp_path = _run_comparison_path(selected_run)
    if comp_path is None or not comp_path.exists():
        return  # the results summary already explains "no comparison yet"
    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    variables = _inclusion_variables(comp)
    if not variables:
        return  # nothing comparable; residual figures / summary cover the messaging

    st.markdown("#### Model comparison — inclusion")
    data = run_manager.read_data_file(selected_run)
    mapping = run_manager.read_mapping(selected_run)
    manifest = _manifest_if_available()

    c1, c2 = st.columns([3, 2])
    variable = c1.selectbox("Variable", variables, key=f"incl_var_{selected_run}")
    include_unsafe = c2.checkbox(
        "Advanced: include unsafe mappings (flagged red)", value=False,
        key=f"incl_unsafe_{selected_run}",
        help="Unsafe mappings are excluded from the comparison by default. Toggling this "
             "plots them flagged in red — they are known metadata conflicts, not valid "
             "model comparisons.",
    )

    inc = comparison_inclusion(data, mapping, comp, variable,
                               manifest=manifest, include_unsafe=include_unsafe)

    # Counts panel — straight from the inclusion dict (consistent with the table).
    cols = st.columns(len(_INCLUSION_METRICS))
    for col, (key, label) in zip(cols, _INCLUSION_METRICS):
        col.metric(label, inc[key])

    if include_unsafe and inc["n_unsafe_plotted"]:
        st.error(
            f"⚠️ {inc['n_unsafe_plotted']} unsafe mapping(s) are plotted (red) — known "
            "metadata conflicts, shown for inspection only, not valid model comparison."
        )

    with st.expander(f"Rows excluded from model comparison ({inc['n_total'] - inc['rows_plotted']})"):
        st.caption("Each excluded row has exactly one reason; plotted + excluded = all rows.")
        if inc["excluded"].empty:
            st.success("No rows excluded — every measured row is plotted.")
        else:
            st.dataframe(inc["excluded"], use_container_width=True, height=240)
            rc = pd.DataFrame([{"reason": k, "rows": v} for k, v in inc["reason_counts"].items()])
            st.dataframe(rc, use_container_width=True, hide_index=True, height=180)

    if not inc["plotted"].empty:
        st.pyplot(compare_plots.comparison_scatter_figure(inc["plotted"], variable))

    if inc["collapse_warning"]:
        st.warning(
            "🔁 " + inc["collapse_message"]
            + " See the **Match PHREEQC** tab → *Conditions needing new PHREEQC "
            "simulations* for the list."
        )

    # One overall validity line, chosen by the documented rules. Only `valid` implies
    # the model was validated.
    getattr(st, inc["validity_severity"], st.info)(f"**{inc['validity'].upper()}** — {inc['validity_message']}")


def _render_results_tab(selected_run: str | None) -> None:
    # What's shown depends on run type, so a literature/synthetic run never displays
    # the lab measured-vs-PHREEQC residual as if it were its own result.
    summary_rt = (
        run_manager.load_run_config(selected_run).get("run_type") if selected_run else None
    )
    if summary_rt == "literature_benchmark":
        _render_literature_summary(selected_run)
        return
    if summary_rt == "synthetic_demo":
        st.warning(
            "This is a synthetic/demo run. Synthetic demo data are for testing the code "
            "only — not scientific output. The lab-experiment comparison is not shown here."
        )
        return

    # lab_experiment / plastic_composite, or no run selected.
    if summary_rt in run_manager.LAB_LIKE_RUN_TYPES and selected_run:
        st.markdown(f"**`{selected_run}` — measured-vs-model comparison.**")
    elif not selected_run:
        st.info("No run selected. Select a lab run in the sidebar to see its comparison.")
        return
    else:
        st.write("This run type has no measured-vs-model comparison.")
    st.caption(
        "Reads this run's own outputs (`experiments/<run>/outputs/`), stamped with "
        "provenance, plus the validation and sustainability tables in `outputs/tables/`."
    )

    # First plot family — measured data only, before/without any model comparison.
    # Renders even when phreeqc_results.csv is missing and no mappings exist.
    if summary_rt in run_manager.LAB_LIKE_RUN_TYPES and selected_run:
        _render_measured_overview(selected_run)
        st.divider()

    if summary_rt in run_manager.LAB_LIKE_RUN_TYPES and selected_run:
        _render_stale_results_warning(selected_run)
        data = run_manager.read_data_file(selected_run)
        status = replicates.overall_mapping_status(
            data, run_manager.read_mapping(selected_run), _manifest_if_available())
        if not status["all_exact"]:
            st.warning(
                "⚠️ **Preliminary / workflow check only.** The graphs below are not final "
                "model validation, because one or more mappings are scenario-level, unsafe, "
                "or missing exact PHREEQC metadata. Plotting is still allowed for inspection."
            )
    if summary_rt in run_manager.LAB_LIKE_RUN_TYPES:
        _render_condition_results(selected_run)
    _render_results_summary(selected_run)
    # Explicit inclusion panel (counts + exclusions + status-aware scatter + validity),
    # above the residual figures.
    if summary_rt in run_manager.LAB_LIKE_RUN_TYPES and selected_run:
        _render_comparison_inclusion(selected_run)
    _render_comparison_figures(selected_run)
    st.info(
        "📈 **Interpreting the plots:** if measured values vary while PHREEQC "
        "predictions stay constant, this usually means the mapping is too coarse or "
        "more PHREEQC simulations are needed."
    )

    for label, name in [
        ("Validation report", config.EXPERIMENTAL_VALIDATION_REPORT_CSV),
        ("Sustainability score", config.SUSTAINABILITY_SCORE_CSV),
    ]:
        path = config.TABLES_DIR / name
        if path.exists():
            with st.expander(f"{label} — {name}"):
                st.dataframe(
                    _read_csv(str(path), path.stat().st_mtime),
                    use_container_width=True, height=300,
                )


def _render_run_and_results_tab(selected_run: str | None) -> None:
    """Combined workflow execution + results (the two used to be separate tabs)."""
    st.subheader("Run workflow")
    _render_run_workflow_tab(selected_run)
    st.divider()
    st.subheader("Results")
    _render_results_tab(selected_run)


def _render_processed_viewer() -> None:
    if not config.PROCESSED_DIR.exists():
        st.warning("`data/processed/` does not exist yet — run Phase 1 first.")
        return
    csvs = sorted(p.name for p in config.PROCESSED_DIR.glob("*.csv"))
    if not csvs:
        st.warning("No processed CSVs yet — run Phase 1 first.")
        return
    ordered = [c for c in PREFERRED_PROCESSED if c in csvs] + [
        c for c in csvs if c not in PREFERRED_PROCESSED
    ]
    choice = st.selectbox("Processed CSV", ordered, key="processed_csv_choice")
    path = config.PROCESSED_DIR / choice
    df = _read_csv(str(path), path.stat().st_mtime)
    st.write(f"{df.shape[0]} rows × {df.shape[1]} columns")
    st.dataframe(df, use_container_width=True, height=300)


def _render_legacy_global_form() -> None:
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
        existing = pd.read_csv(MANUAL_ENTRY_PATH)
        st.markdown("**Current manual-entry file:**")
        st.dataframe(existing, use_container_width=True, height=300)


# Audit status -> emoji for at-a-glance scanning.
_AUDIT_STATUS_EMOJI = {
    calculations.STATUS_PASS: "✅ pass",
    calculations.STATUS_WARNING: "⚠️ warning",
    calculations.STATUS_FAIL: "❌ fail",
    calculations.STATUS_NA: "— not available",
}


def _render_formula_registry(dev_mode: bool) -> None:
    """List every documented formula with equation, I/O, units, provenance."""
    for f in calculations.FORMULAS:
        tag = "🧮 app-calculated" if f.source == "app-calculated" else "📥 parsed from PHREEQC"
        with st.expander(f"{f.name}  ·  {tag}", expanded=False):
            st.latex(f.latex)
            st.markdown(
                f"- **Equation:** `{f.equation}`\n"
                f"- **Inputs:** {', '.join(f'`{c}`' for c in f.inputs)}\n"
                f"- **Output:** `{f.output}`\n"
                f"- **Units:** {f.units}\n"
                f"- **Provenance:** {f.source}\n\n"
                f"{f.explanation}"
            )
            if dev_mode and f.detail:
                st.info(f"🛠️ {f.detail}")


def _render_residual_audit() -> None:
    """Recompute residuals from the stored comparison CSV and report pass/fail."""
    comp_path = config.PROCESSED_DIR / config.COMPARISON_CSV
    if not comp_path.exists():
        st.info(
            "No comparison file yet — run a lab workflow with a sample→PHREEQC mapping to "
            f"generate `{config.COMPARISON_CSV}`, then this audit re-derives every residual."
        )
        return

    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    audit = calculations.audit_comparison(comp)
    if audit.empty:
        st.info("Comparison file has no residual columns to audit yet.")
        return

    counts = audit["status"].value_counts().to_dict()
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("✅ pass", counts.get(calculations.STATUS_PASS, 0))
    s2.metric("⚠️ warning", counts.get(calculations.STATUS_WARNING, 0))
    s3.metric("❌ fail", counts.get(calculations.STATUS_FAIL, 0))
    s4.metric("— not available", counts.get(calculations.STATUS_NA, 0))

    if counts.get(calculations.STATUS_FAIL, 0):
        st.error(
            "At least one stored residual does **not** match a fresh recomputation. "
            "Investigate the mapping / units before trusting the comparison."
        )
    else:
        st.success(
            "Every re-derivable residual matches the stored value within tolerance "
            f"(pass ≤ {calculations.PASS_TOL:g}, warning ≤ {calculations.WARN_TOL:g})."
        )

    display = audit.copy()
    display["status"] = display["status"].map(_AUDIT_STATUS_EMOJI).fillna(display["status"])
    st.dataframe(display, use_container_width=True, height=300)
    st.caption(
        "`input_1 − input_2` is recomputed and compared to the stored residual. "
        "'not available' means a required input (or the stored value) is blank."
    )


def _render_unit_calculator() -> None:
    st.markdown("**ICP unit conversion** — dilution correction then mg/L → mM.")
    c1, c2, c3 = st.columns(3)
    element = c1.selectbox("Element", list(calculations.ATOMIC_MASSES), key="calc_unit_el")
    reported = c2.number_input("Reported ICP (mg/L)", min_value=0.0, value=5.0,
                               step=1.0, key="calc_unit_mgl")
    dil = c3.number_input("Dilution factor", min_value=0.0, value=10.0,
                          step=1.0, key="calc_unit_dil")
    mass = calculations.ATOMIC_MASSES[element]
    corrected = calculations.apply_dilution(reported, dil)
    mM = calculations.mgl_to_mM(corrected, element) if mass else float("nan")
    st.latex(r"\mathrm{corrected} = \mathrm{reported} \times \mathrm{dilution};\quad "
             r"\mathrm{mM} = \dfrac{\mathrm{corrected}}{\mathrm{atomic\ mass}}")
    st.success(
        f"corrected = {reported:g} × {dil:g} = **{corrected:g} mg/L** · "
        f"{element}_mM = {corrected:g} / {mass:g} = **{mM:.4g} mM**"
    )


def _render_ls_calculator() -> None:
    st.markdown("**Liquid/solid ratio** = solution volume (mL) / fly-ash mass (g).")
    c1, c2 = st.columns(2)
    mass_g = c1.number_input("fly_ash_mass_g", min_value=0.0, value=20.0,
                             step=1.0, key="calc_ls_mass")
    vol_mL = c2.number_input("solution_volume_mL", min_value=0.0, value=100.0,
                             step=1.0, key="calc_ls_vol")
    st.latex(r"\mathrm{L/S} = \dfrac{\mathrm{solution\_volume\_mL}}{\mathrm{fly\_ash\_mass\_g}}")
    if mass_g > 0:
        ls = calculations.liquid_solid_ratio(vol_mL, mass_g)
        st.success(f"L/S = {vol_mL:g} / {mass_g:g} = **{ls:.4g} mL/g**")
    else:
        st.warning("Enter a fly-ash mass greater than 0 to compute L/S.")


def _render_calc_verification_tab(dev_mode: bool) -> None:
    st.subheader("Calculation verification / formula audit")
    st.info(
        "**PHREEQC is an equilibrium/speciation solver. This app does not rederive PHREEQC "
        "internally.** It parses PHREEQC output values and verifies that downstream "
        "calculations, mappings, unit conversions, and residuals are applied correctly."
    )

    st.markdown("### Formulas used")
    st.caption("Each formula, its inputs/outputs, units, and whether the app computes it or "
               "parses it from PHREEQC.")
    _render_formula_registry(dev_mode)

    st.divider()
    st.markdown("### Per-row residual audit")
    st.caption("Recomputes `measured − PHREEQC` from the stored comparison file and checks it "
               "against the stored residual.")
    _render_residual_audit()

    st.divider()
    st.markdown("### Calculators")
    cc1, cc2 = st.columns(2)
    with cc1:
        _render_unit_calculator()
    with cc2:
        _render_ls_calculator()

    if dev_mode:
        st.divider()
        st.markdown("### 🛠️ Developer explanations")
        st.markdown(
            "- **Why pH uses activity:** pH = −log₁₀(a_H⁺) is defined on hydrogen-ion "
            "*activity*. In high-ionic-strength alkali systems activity ≠ concentration, so "
            "an activity model (PHREEQC) is needed; a naive concentration-based pH would be wrong.\n"
            "- **Why the saturation index indicates precipitation/dissolution tendency:** "
            "SI = log₁₀(IAP/Ksp). IAP > Ksp (SI > 0) means the solution holds more dissolved "
            "ions than equilibrium allows, so the phase tends to precipitate; SI < 0 means it "
            "tends to dissolve. It is a *tendency*, not a rate.\n"
            "- **Why residuals alone do not prove model validity:** a small `measured − PHREEQC` "
            "residual can occur for the wrong reasons (compensating errors, a single tuned "
            "sample, or pH-only data). Agreement on one analyte/condition is not validation.\n"
            "- **Why ICP unit conversion must include the dilution factor:** ICP reports the "
            "*diluted* aliquot. Converting mg/L → mM without first multiplying by the dilution "
            "factor understates the true solution concentration by that factor."
        )


def _render_help_tab() -> None:
    st.subheader("Design note — a generic validation workflow")
    st.info(
        "The app is designed around a generic validation workflow: **measured data → "
        "model prediction → mapping → residuals → validation status**. PHREEQC and the "
        "fly ash metadata (OA/PF/GS, CO₂ cover) are the current project implementation, "
        "not a hard limit of the system — the same workflow applies to other experiments "
        "and other models."
    )
    st.divider()

    st.subheader("Validation status — what is valid now vs not yet")
    _render_valid_now_section()
    st.caption("ℹ️ " + _PRELIMINARY_CAVEAT)
    st.caption("ℹ️ **Project-specific:** " + _OA_PF_GS_CAVEAT)
    with st.expander("Mapping status definitions"):
        _render_mapping_status_definitions()
    st.divider()

    st.subheader("How this app works")
    st.markdown(
        "1. **Start** — create or open a run in the sidebar (a 'save file' for one "
        "experiment set) and check the status + workflow checklist.\n"
        "2. **Data** — add measured rows (lab), upload/enter literature rows, or add "
        "synthetic demo rows, depending on run type.\n"
        "3. **Match PHREEQC** (lab runs) — link each `sample_id` to the PHREEQC result row "
        "for the same chemistry.\n"
        "4. **Run + Results** — export to the pipeline, run Phase 1 → validate → compare → "
        "sustainability, then read the measured-vs-PHREEQC comparison, pH residuals, "
        "validation, and sustainability proxies.\n"
        "5. **Audit / Help** — formula audit, calculators, raw PHREEQC tables/figures, and "
        "this reference."
    )

    st.subheader("Run types")
    st.markdown(
        "- **lab_experiment** — our measured release data (pH-only or full ICP). The only "
        "type compared against PHREEQC as real data.\n"
        "- **literature_benchmark** — values reported by other papers, kept separate and "
        "never run through the pipeline as lab data.\n"
        "- **synthetic_demo** — fake data for testing the code only; never scientific output.\n"
        "- **plastic_composite** — lab-like run for plastic-composite experiments."
    )

    st.subheader("Sample → PHREEQC mapping")
    st.markdown(
        "PHREEQC output `.pqo` filenames and measured `sample_id`s differ, so the comparison "
        "needs an explicit link: each measured `sample_id` → one PHREEQC `record_key` "
        "(`<file>|sim<N>|<state>|sol<N>`). Comparisons default to the post-equilibration "
        "(`batch`) state. **Without a mapping, residuals stay NaN** — a deliberate, visible "
        "state rather than a wrong join."
    )

    st.subheader("Residuals")
    st.markdown(
        "`residual_X = measured − PHREEQC` (in mM for Ca/Si/Al/Fe; pH for pH). Positive means "
        "the measured value is higher than the PHREEQC prediction. Fe is often unpredicted by "
        "the CEMDATA18 runs, so `residual_Fe` may be entirely NaN — that means **unavailable**, "
        "not 'PHREEQC predicts zero Fe'."
    )

    st.subheader("Limitations & safety")
    st.warning(
        "- **PHREEQC is equilibrium / speciation modelling.** Its outputs are "
        "thermodynamic predictions, not direct measurements, and assume the modelled "
        "system reached equilibrium.\n"
        "- **pH-only data only validates pH.** Ca/Si/Al/Fe/REE validation requires "
        "ICP-OES / ICP-MS data.\n"
        "- **Literature data must stay separate from lab data** — it is benchmark context, "
        "not our measurements.\n"
        "- **No ML is trained until real measured experimental release data exists.** "
        "The interface and Phase 2 comparison are scaffolding; predictions remain NaN "
        "until measured data and a sample→PHREEQC mapping are provided.\n"
        "- **Entering a value here does not make it scientifically valid.** Check units, "
        "detection limits, dilution factors, and experimental metadata before trusting "
        "any comparison or residual. Garbage in, garbage out."
    )


def _render_audit_help_tab(dev_mode: bool) -> None:
    """Combined audit + reference tab: formula audit, calculators, raw PHREEQC
    outputs (in an expander), and the Help / Safety reference."""
    _render_calc_verification_tab(dev_mode)

    st.divider()
    st.subheader("PHREEQC raw outputs & model-only plots")
    st.caption(
        "These tables and figures are **PHREEQC model predictions**, not measured "
        "experimental data."
    )
    with st.expander("Processed PHREEQC tables", expanded=False):
        _render_processed_viewer()
    with st.expander("PHREEQC model-output figures", expanded=False):
        _render_phreeqc_only_figures()

    st.divider()
    _render_help_tab()


# --------------------------------------------------------------------------- #
# Page — wide layout, run-management sidebar, and a tabbed dashboard
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="flyash-phreeqc-ml", layout="wide")
st.title("flyash-phreeqc-ml — control panel")
st.caption(
    "A GUI over the existing Phase 1 / Phase 2 scripts. It does not change the "
    "chemistry or train any model."
)

# Sidebar "save files" — selecting a run here drives every tab below.
SELECTED_RUN = _render_run_sidebar()

st.sidebar.divider()
DEV_MODE = st.sidebar.checkbox(
    "🛠️ Developer explanation mode", value=False, key="dev_mode",
    help="Show deeper chemistry/statistics explanations, mainly in the "
         "Calculation Verification tab.",
)

tab_start, tab_data, tab_map, tab_run_results, tab_audit = st.tabs([
    "Start", "Data", "Match PHREEQC", "Run + Results", "Audit / Help",
])

with tab_start:
    _render_overview(SELECTED_RUN)
with tab_data:
    _render_data_entry_tab(SELECTED_RUN)
with tab_map:
    _render_mapping_tab(SELECTED_RUN)
with tab_run_results:
    _render_run_and_results_tab(SELECTED_RUN)
with tab_audit:
    _render_audit_help_tab(DEV_MODE)
