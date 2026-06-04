"""Streamlit interface for the flyash-phreeqc-ml project.

A thin GUI on top of the existing Phase 1 / Phase 2 code — it does **not**
reimplement any pipeline logic. A run-management sidebar drives a tabbed
dashboard (Overview, Data Entry, Mapping, Run Workflow, Results, PHREEQC
Outputs, Literature Benchmark, Tools, Help / Safety). Each tab reuses the
package functions; this file adds no chemistry or ML. It lets you:

* see project + run status at a glance,
* enter measured / literature / demo data into per-run save files,
* map measured samples to PHREEQC rows and run the existing scripts,
* read an honest measured-vs-PHREEQC summary, and browse PHREEQC outputs.

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

from flyash_phreeqc_ml import calculations  # noqa: E402
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
    st.sidebar.caption(f"⚠️ {run_manager.warning_for(cfg.get('run_type'))}")
    st.sidebar.info("➡️ Open the **Run Workflow** tab to execute this run.")
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


def _render_comparison_figures() -> None:
    """Measured-vs-PHREEQC + residual plots. Belongs with the lab comparison
    (Results tab), not the PHREEQC-only model outputs."""
    pngs = [p for d in _figure_dirs() if d.exists() for p in sorted(d.glob("*.png"))]
    comparison = [p for p in pngs if p.name in _COMPARISON_FIGURES]
    if not comparison:
        return
    st.subheader("Measured vs PHREEQC")
    if _single_sample_comparison():
        st.warning(
            "This is a single-sample comparison, not a trend. It only checks one "
            "mapped condition."
        )
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
# Tab renderers — each is a self-contained view; all reuse the helpers above.
# --------------------------------------------------------------------------- #
def _render_overview(selected_run: str | None) -> None:
    """Project status cards + selected-run summary + a recommended next step."""
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

    st.markdown(f"**`{selected_run}`**")
    s1, s2, s3 = st.columns(3)
    s1.metric("Run type", rt)
    s2.metric("Data rows", len(data))
    s3.metric("Mapping", ("yes" if has_map else "no") if lab_like else "n/a")
    st.caption(f"📁 {run_manager.run_dir(selected_run).relative_to(_PROJECT_ROOT)} · source `{cfg.get('data_source')}`")

    # What's missing.
    missing: list[str] = []
    if data.empty:
        missing.append("no data rows entered yet")
    if lab_like:
        icp_present = any(_has_numeric(data, c) for c in _ICP_MEASURED_COLS)
        if not icp_present:
            missing.append("ICP chemistry (Ca/Si/Al/Fe/REE) — pH-only so far")
        if not has_map:
            missing.append("sample → PHREEQC mapping (needed for residuals)")
    if missing:
        st.markdown("**Missing / not yet present:**")
        for m in missing:
            st.markdown(f"- {m}")

    # Recommended next step.
    if data.empty:
        nxt = "Add measured rows in the **Data Entry** tab."
    elif rt == "literature_benchmark":
        nxt = ("Review the **Literature Benchmark** tab. Literature data are kept "
               "separate from lab data and are not run through the pipeline.")
    elif rt == "synthetic_demo":
        nxt = "This is a synthetic/demo run — for testing only, not scientific output."
    elif lab_like and not has_map:
        nxt = ("If this is a pH-only lab run, map the sample to a PHREEQC batch row in "
               "the **Mapping** tab, then run the workflow in the **Run Workflow** tab.")
    else:
        nxt = ("Run the workflow in the **Run Workflow** tab, then read the "
               "**Results** tab.")
    st.success(f"**Recommended next step:** {nxt}")


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


def _render_data_entry_tab(selected_run: str | None) -> None:
    if not selected_run:
        st.info("Select or create a run in the **Experiment runs** sidebar (left) to enter data.")
        return
    cfg = run_manager.load_run_config(selected_run)
    rt = cfg.get("run_type")
    st.subheader(f"Run `{selected_run}` — {rt}")
    _run_type_warning(rt)

    if rt in run_manager.LAB_LIKE_RUN_TYPES:
        _lab_entry_form(selected_run)
    elif rt == "literature_benchmark":
        _literature_entry(selected_run)
    elif rt == "synthetic_demo":
        _demo_entry(selected_run)

    st.divider()
    _render_run_data_and_edit(selected_run, rt)


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
        b1, b2, b3 = st.columns(3)
        with b1:
            _script_button("Generate experiment plan", "scripts/06_generate_experiment_plan.py",
                           "Experiment plan", "adv_plan")
        with b2:
            _script_button("Validate experimental CSVs",
                           "scripts/07_validate_experimental_data.py", "Validation", "adv_validate")
        with b3:
            _script_button("Run sustainability score", "scripts/08_sustainability_score.py",
                           "Sustainability score", "adv_sustain")


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
    if summary_rt in run_manager.LAB_LIKE_RUN_TYPES:
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
    _render_comparison_figures()

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


def _render_phreeqc_tab() -> None:
    st.subheader("Processed data")
    st.caption(
        "These tables and the figures below are **PHREEQC model predictions**, not "
        "measured experimental data."
    )
    _render_processed_viewer()
    st.divider()
    st.subheader("PHREEQC model-output figures")
    _render_phreeqc_only_figures()


def _render_literature_tab(selected_run: str | None) -> None:
    rt = (
        run_manager.load_run_config(selected_run).get("run_type") if selected_run else None
    )
    if rt != "literature_benchmark":
        st.info(
            "This tab is for **literature_benchmark** runs. Create or select one in the "
            "sidebar to review literature data."
        )
        return
    st.warning(
        "📚 Literature values are reported by other papers — they are **not** our lab "
        "data and are kept separate. They are never run through the measured-vs-PHREEQC "
        "pipeline."
    )
    lit = run_manager.read_data_file(selected_run)
    st.metric("Literature rows", len(lit))
    if lit.empty:
        st.info("No literature rows entered yet. Add them in the **Data Entry** tab.")
        return

    st.markdown("**Uploaded literature table:**")
    st.dataframe(lit, use_container_width=True, height=300)

    present = [c for c in _LIT_SUMMARY_COLS if c in lit.columns]
    if present:
        st.markdown("**Key columns summary:**")
        st.dataframe(lit[present], use_container_width=True, height=300)

    if "comparability_to_our_experiment" in lit.columns:
        st.markdown("**Data quality / comparability notes:**")
        cols = [c for c in ["source_id", "comparability_to_our_experiment"] if c in lit.columns]
        st.dataframe(lit[cols], use_container_width=True, height=200)


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


def _render_tools_tab() -> None:
    st.subheader("Experiment planning tools")
    st.write(
        "Generate an experiment run sheet, validate filled CSVs, and compute "
        "sustainability/selectivity proxies. These call the existing scripts unchanged "
        "(`scripts/06_…`, `07_…`, `08_…`) and train no model."
    )
    t1, t2, t3 = st.columns(3)
    with t1:
        _script_button("Generate experiment plan", "scripts/06_generate_experiment_plan.py",
                       "Experiment plan", "tools_plan")
    with t2:
        _script_button("Validate experimental CSVs",
                       "scripts/07_validate_experimental_data.py", "Validation", "tools_validate")
    with t3:
        _script_button("Run sustainability score", "scripts/08_sustainability_score.py",
                       "Sustainability score", "tools_sustain")

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

    st.divider()
    st.caption(
        "Recommended workflow: use the **Data Entry** / **Run Workflow** tabs and the "
        "per-run save files. The form below predates them and writes to one shared file."
    )
    with st.expander("Legacy manual global data entry — not recommended", expanded=False):
        _render_legacy_global_form()


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
    st.subheader("How this app works")
    st.markdown(
        "1. **Create or open a run** in the sidebar (a 'save file' for one experiment set).\n"
        "2. **Data Entry** — add measured rows (lab), upload/enter literature rows, or add "
        "synthetic demo rows, depending on run type.\n"
        "3. **Mapping** (lab runs) — link each `sample_id` to the PHREEQC result row for the "
        "same chemistry.\n"
        "4. **Run Workflow** — export to the pipeline and run Phase 1 → validate → compare → "
        "sustainability.\n"
        "5. **Results** — read the measured-vs-PHREEQC comparison, pH residuals, validation, "
        "and sustainability proxies.\n"
        "6. **PHREEQC Outputs** — browse processed tables and model figures."
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

(
    tab_overview, tab_entry, tab_map, tab_run, tab_results,
    tab_phreeqc, tab_lit, tab_tools, tab_calc, tab_help,
) = st.tabs([
    "Overview", "Data Entry", "Mapping", "Run Workflow", "Results",
    "PHREEQC Outputs", "Literature Benchmark", "Tools",
    "Calculation Verification", "Help / Safety",
])

with tab_overview:
    _render_overview(SELECTED_RUN)
with tab_entry:
    _render_data_entry_tab(SELECTED_RUN)
with tab_map:
    _render_mapping_tab(SELECTED_RUN)
with tab_run:
    _render_run_workflow_tab(SELECTED_RUN)
with tab_results:
    _render_results_tab(SELECTED_RUN)
with tab_phreeqc:
    _render_phreeqc_tab()
with tab_lit:
    _render_literature_tab(SELECTED_RUN)
with tab_tools:
    _render_tools_tab()
with tab_calc:
    _render_calc_verification_tab(DEV_MODE)
with tab_help:
    _render_help_tab()
