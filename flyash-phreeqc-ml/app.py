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
from flyash_phreeqc_ml import run_manager  # noqa: E402
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
# CO2 labels come straight from config so the form, validator, and plan agree.
_CO2_OPTIONS = [""] + list(config.CO2_CONDITION_ALLOWED)
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
    return selected


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
            "No sample-to-PHREEQC mapping found. The workflow can still run, but "
            "measured-vs-PHREEQC residuals will not be calculated. Add a mapping in "
            "the **Sample → PHREEQC mapping** part of the workspace below."
        )

    # Steps 2..N — run each script, halting on the first failure.
    steps = [
        ("Phase 1 pipeline", "scripts/run_phase1.py"),
        ("Validate experimental data", "scripts/07_validate_experimental_data.py"),
        ("Compare measured vs PHREEQC", "scripts/05_compare_experimental.py"),
        ("Sustainability score", "scripts/08_sustainability_score.py"),
    ]
    for i, (label, script) in enumerate(steps, start=2):
        st.markdown(f"**Step {i} — {label}**")
        st.code(f"python {script}", language="bash")
        with st.spinner(f"Running {label}…"):
            proc = _run_script(script)
        _show_process_result(label, proc)
        if proc.returncode != 0:
            st.error(f"⛔ Workflow stopped at step {i} ({label}) — see stderr above.")
            return

    st.success("✅ Workflow complete — all steps succeeded.")
    st.info(
        "Outputs written to:\n"
        "- `data/processed/` — parsed tables, master dataset, comparison\n"
        "- `outputs/tables/` — validation report, sustainability score\n"
        "- `reports/figures/` — plots"
    )
    _read_csv.clear()  # processed CSVs changed; refresh the viewers below


def _render_mapping_section(run_name: str) -> None:
    """Sample_id -> PHREEQC record_key mapping UI for a lab-like run.

    Saves to the run's own ``data/sample_phreeqc_map.csv`` and can export a copy
    to the pipeline location the comparison script reads.
    """
    st.markdown("---")
    st.subheader("Sample → PHREEQC mapping")
    st.caption(
        "Link each measured sample to the PHREEQC result row for the same chemistry. "
        "The comparison step needs this to compute pH residuals now (and Ca/Si/Al/Fe "
        "residuals later, once ICP data exist)."
    )

    data = run_manager.read_data_file(run_name)
    sample_ids: list[str] = []
    if "sample_id" in data.columns:
        for s in data["sample_id"].astype(str).map(str.strip).tolist():
            if s and s.lower() != "nan":
                sample_ids.append(s)
    sample_ids = list(dict.fromkeys(sample_ids))  # unique, order-preserving

    if not sample_ids:
        st.info("No `sample_id` rows in this run yet — enter data first.")
        return

    results_path = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    if not results_path.exists():
        st.info(
            "`data/processed/phreeqc_results.csv` not found — run Phase 1 first "
            "(Section 4 or the workflow button) to generate PHREEQC results."
        )
        return
    phreeqc = _read_csv(str(results_path), results_path.stat().st_mtime)
    if "record_key" not in phreeqc.columns:
        st.warning("`phreeqc_results.csv` has no `record_key` column — cannot map.")
        return

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

    mapping = run_manager.read_mapping(run_name)
    st.markdown(f"**Existing mappings** ({len(mapping)}):")
    st.dataframe(mapping, use_container_width=True, height=170)

    if not mapping.empty:
        with st.expander("🗑️ Delete mappings"):
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


def _render_results_summary() -> None:
    """Honest, presentation-friendly summary of the latest comparison run."""
    comp_path = config.PROCESSED_DIR / config.COMPARISON_CSV
    if not comp_path.exists():
        st.info(
            "No comparison results yet. Run the workflow (Section 2) for a lab run "
            "that has a sample→PHREEQC mapping to generate "
            "`data/processed/comparison_measured_vs_phreeqc.csv`."
        )
        return

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
        p2.metric("PHREEQC pH", f"{pred:.2f}" if pd.notna(pred) else "—")
        p3.metric("Residual pH (measured − PHREEQC)", f"{resid:+.2f}" if pd.notna(resid) else "—")

    # Comparison table preview — only columns that exist.
    present = [(src, disp) for src, disp in _COMPARISON_PREVIEW_SPEC if src in comp.columns]
    if present:
        preview = comp[[src for src, _ in present]].rename(columns=dict(present))
        st.markdown("**Comparison table** (measured vs PHREEQC, existing columns only):")
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

    # The lab comparison belongs to a *different* run — keep it collapsed and labelled.
    comp_path = config.PROCESSED_DIR / config.COMPARISON_CSV
    if comp_path.exists():
        with st.expander("Latest lab comparison from previous run", expanded=False):
            st.warning("This does not belong to the selected literature benchmark run.")
            _render_results_summary()


# Comparison figures get specific captions; everything else is a PHREEQC-only plot.
_FIGURE_CAPTIONS = {
    "measured_vs_phreeqc.png": (
        "This plot compares measured values against PHREEQC predictions. Points on "
        "the dashed 1:1 line would indicate perfect agreement. Points far from the "
        "line indicate model/experiment mismatch or incorrect mapping."
    ),
    "residuals_by_sample.png": (
        "This plot shows measured − PHREEQC. Positive values mean the measured value "
        "is higher than the PHREEQC prediction."
    ),
}
_COMPARISON_FIGURES = set(_FIGURE_CAPTIONS)


def _single_sample_comparison() -> bool:
    """True if the comparison has exactly one mapped sample (plots aren't a trend)."""
    comp_path = config.PROCESSED_DIR / config.COMPARISON_CSV
    if not comp_path.exists():
        return False
    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    if "phreeqc_record_key" not in comp.columns:
        return False
    mapped = comp["phreeqc_record_key"].apply(
        lambda v: not (pd.isna(v) or str(v).strip() == "")).sum()
    return int(mapped) == 1


def _render_figures() -> None:
    """Figure viewer: captioned comparison plots + a filtered PHREEQC-only viewer."""
    pngs = [p for d in _figure_dirs() if d.exists() for p in sorted(d.glob("*.png"))]
    if not pngs:
        st.warning("No figures yet — run Phase 1 (and the workflow once measured data exists).")
        return

    comparison = [p for p in pngs if p.name in _COMPARISON_FIGURES]
    phreeqc_only = [p for p in pngs if p.name not in _COMPARISON_FIGURES]

    if comparison:
        st.subheader("Measured vs PHREEQC")
        if _single_sample_comparison():
            st.warning(
                "This is a single-sample comparison, not a trend. It only checks one "
                "mapped condition."
            )
        for png in comparison:
            st.image(str(png), use_container_width=True)
            st.caption(_FIGURE_CAPTIONS.get(png.name, png.name))

    if phreeqc_only:
        st.subheader("PHREEQC model outputs")
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
# Page
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="flyash-phreeqc-ml", layout="wide")
st.title("flyash-phreeqc-ml — control panel")
st.caption(
    "A GUI over the existing Phase 1 / Phase 2 scripts. It does not change the "
    "chemistry or train any model."
)

# Sidebar "save files" — selecting a run here drives the workspace section below.
SELECTED_RUN = _render_run_sidebar()

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

# ---- 2. Run / Execute selected experiment --------------------------------- #
st.header("2. Run / Execute selected experiment")
st.write(
    "Enter data into a run (in the **Experiment run workspace** section below), then "
    "click here to run all the relevant scripts in order and see their output. This is "
    "a convenience wrapper — the individual step buttons still exist further down."
)
if not SELECTED_RUN:
    st.info(
        "Select or create a run in the **Experiment runs** sidebar (left) first, then "
        "this button will run the workflow for it."
    )
else:
    _wf_cfg = run_manager.load_run_config(SELECTED_RUN)
    _wf_rt = _wf_cfg.get("run_type")
    st.caption(f"Selected run: `{SELECTED_RUN}` — **{_wf_rt}**")
    if st.button("▶️ Run selected experiment workflow", type="primary"):
        if _wf_rt in run_manager.LAB_LIKE_RUN_TYPES:
            _run_lab_workflow(SELECTED_RUN)
        elif _wf_rt == "literature_benchmark":
            st.warning(
                "📚 This is a **literature-benchmark** run. Literature data are kept "
                "separate from our measured lab data and are **not** run through the "
                "measured-vs-PHREEQC pipeline. Nothing was exported."
            )
            _lit = run_manager.read_data_file(SELECTED_RUN)
            if not _lit.empty:
                st.markdown("**Literature benchmark data:**")
                st.dataframe(_lit, use_container_width=True, height=240)
            else:
                st.info("No literature rows entered yet.")
        elif _wf_rt == "synthetic_demo":
            st.warning(
                "🧩 This is a **synthetic/demo** run. Synthetic data are only for testing "
                "the code — they are not real experimental data and are not run through "
                "the pipeline."
            )

# ---- 3. Results summary --------------------------------------------------- #
st.header("3. Results summary")

# What's shown depends on the selected run type, so a literature/synthetic run never
# displays the lab measured-vs-PHREEQC residual as if it were its own result.
_summary_rt = (
    run_manager.load_run_config(SELECTED_RUN).get("run_type") if SELECTED_RUN else None
)

if _summary_rt == "literature_benchmark":
    _render_literature_summary(SELECTED_RUN)
elif _summary_rt == "synthetic_demo":
    st.warning(
        "This is a synthetic/demo run. Synthetic demo data are for testing the code "
        "only — not scientific output. The lab-experiment comparison is not shown here."
    )
else:
    # lab_experiment / plastic_composite, or no run selected.
    if _summary_rt in run_manager.LAB_LIKE_RUN_TYPES:
        st.markdown("**Latest lab-experiment PHREEQC comparison.**")
    else:
        st.write(
            "Latest PHREEQC comparison from the lab pipeline. Select a run in the "
            "sidebar for run-specific context."
        )
    st.caption(
        "Reads `data/processed/comparison_measured_vs_phreeqc.csv` plus the validation "
        "and sustainability tables in `outputs/tables/`."
    )
    _render_results_summary()

    # Lab-data QA/QC tables — only alongside the lab summary, never under lit/demo.
    for _rlabel, _rname in [
        ("Validation report", config.EXPERIMENTAL_VALIDATION_REPORT_CSV),
        ("Sustainability score", config.SUSTAINABILITY_SCORE_CSV),
    ]:
        _rpath = config.TABLES_DIR / _rname
        if _rpath.exists():
            with st.expander(f"{_rlabel} — {_rname}"):
                st.dataframe(
                    _read_csv(str(_rpath), _rpath.stat().st_mtime),
                    use_container_width=True,
                )

# ---- 4. Run pipeline ------------------------------------------------------ #
st.header("4. Run pipeline")
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

# ---- 5. View processed data ----------------------------------------------- #
st.header("5. View processed data")
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

# ---- 6. Enter experimental data (legacy) ---------------------------------- #
st.header("6. Enter experimental data")
st.caption(
    "**Recommended workflow: use the Experiment Run Workspace (Section 9) instead.** "
    "This global form writes straight to one shared manual-entry file and predates the "
    "per-run save files; it is kept for backward compatibility."
)
with st.expander("Legacy/manual global data entry", expanded=False):
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
        existing = pd.read_csv(MANUAL_ENTRY_PATH)
        st.markdown("**Current manual-entry file:**")
        st.dataframe(existing, use_container_width=True)

# ---- 7. View figures ------------------------------------------------------ #
st.header("7. View figures")
_render_figures()

# ---- 8. Experiment planning ----------------------------------------------- #
st.header("8. Experiment planning")
st.write(
    "Experiment planning tools: generate an experiment run sheet, validate filled "
    "CSVs, and compute sustainability/selectivity proxies. These call the existing "
    "scripts unchanged (`scripts/06_…`, `07_…`, `08_…`) and train no model."
)
ep1, ep2, ep3 = st.columns(3)
with ep1:
    if st.button("Generate experiment plan", use_container_width=True):
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

st.caption(
    "The validation report and sustainability score tables are shown in the "
    "**Results summary** (Section 3) once generated."
)

# ---- 9. Experiment run workspace ------------------------------------------ #
st.header("9. Experiment run workspace")
if not SELECTED_RUN:
    st.info(
        "Select or create an experiment run in the **Experiment runs** sidebar "
        "(left) to enter data into its own save file. This is separate from the "
        "global manual-entry file in section 6."
    )
else:
    _cfg = run_manager.load_run_config(SELECTED_RUN)
    _rt = _cfg.get("run_type")
    st.subheader(f"Run `{SELECTED_RUN}` — {_rt}")
    _run_type_warning(_rt)

    if _rt in run_manager.LAB_LIKE_RUN_TYPES:
        _lab_entry_form(SELECTED_RUN)
    elif _rt == "literature_benchmark":
        _literature_entry(SELECTED_RUN)
    elif _rt == "synthetic_demo":
        _demo_entry(SELECTED_RUN)

    # Preview this run's data file. The left-hand index is the row number used by
    # the "Delete rows" controls below.
    _data = run_manager.read_data_file(SELECTED_RUN)
    st.markdown(f"**This run's data** ({len(_data)} row(s)):")
    st.dataframe(_data, use_container_width=True, height=260)

    # --- Delete / clean rows ---------------------------------------------- #
    # Only affects THIS run's CSV (experiments/<run>/data/…). Never touches other
    # runs or data/raw/experimental_icp (that needs the explicit export button).
    if not _data.empty:
        with st.expander("🗑️ Delete rows", expanded=False):
            _id_col = run_manager.id_column_for(SELECTED_RUN)

            def _row_label(i: int) -> str:
                if _id_col in _data.columns:
                    val = _data.iloc[i][_id_col]
                    shown = "" if pd.isna(val) else str(val).strip()
                    return f"Row {i} — {_id_col}={shown or '(blank)'}"
                return f"Row {i}"

            to_delete = st.multiselect(
                "Select row numbers to delete",
                options=list(range(len(_data))),
                format_func=_row_label,
                key=f"del_rows_{SELECTED_RUN}",
            )
            confirm = st.checkbox(
                "I understand this will delete the selected rows from this run's CSV.",
                key=f"del_confirm_{SELECTED_RUN}",
            )
            if st.button("Delete selected rows", key=f"del_btn_{SELECTED_RUN}"):
                if not to_delete:
                    st.warning("No rows selected — nothing was deleted.")
                elif not confirm:
                    st.warning("Tick the confirmation checkbox before deleting.")
                else:
                    n = run_manager.delete_data_rows(SELECTED_RUN, to_delete)
                    st.success(f"Deleted {n} row(s) from this run's CSV.")
                    _read_csv.clear()
                    st.rerun()

            st.divider()
            st.caption(
                "Remove rows with a blank "
                f"`{_id_col}` or where every value is empty."
            )
            if st.button("Remove blank rows", key=f"del_blank_{SELECTED_RUN}"):
                n = run_manager.remove_blank_data_rows(SELECTED_RUN)
                if n:
                    st.success(f"Removed {n} blank row(s).")
                    _read_csv.clear()
                    st.rerun()
                else:
                    st.info("No blank rows found.")

    # Export this run's CSV.
    ec1, ec2 = st.columns(2)
    with ec1:
        if not _data.empty:
            st.download_button(
                "⬇️ Export this run's CSV",
                data=_data.to_csv(index=False).encode("utf-8"),
                file_name=f"{SELECTED_RUN}_{run_manager.spec_for(_rt).data_filename}",
                mime="text/csv",
                use_container_width=True,
            )
    with ec2:
        # Lab-type runs can be pushed into the existing pipeline's manual-entry file.
        if _rt in run_manager.LAB_LIKE_RUN_TYPES:
            if st.button("➡️ Export to pipeline (manual-entry CSV)", use_container_width=True):
                try:
                    dest = run_manager.export_lab_run_to_pipeline(SELECTED_RUN)
                    st.success(
                        f"Copied to {dest.relative_to(_PROJECT_ROOT)} — the existing "
                        "scripts (05/07) will pick it up."
                    )
                    _read_csv.clear()
                except run_manager.RunManagerError as exc:
                    st.error(str(exc))

    # Sample -> PHREEQC mapping (lab-like runs only).
    if _rt in run_manager.LAB_LIKE_RUN_TYPES:
        _render_mapping_section(SELECTED_RUN)

# ---- 10. Safety and limitations ------------------------------------------- #
st.header("10. Safety and limitations")
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
