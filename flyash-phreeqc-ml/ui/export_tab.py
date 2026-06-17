"""Export tab — validation report, audit trail, previous simulation runs, user guide.

Extracted from app.py by the UI modularization refactor — see
docs/refactor_plan.md. Behavior is unchanged (verbatim move)."""
from __future__ import annotations

import io
import json
import zipfile
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
import app_ui  # noqa: E402  (presentation-only UI helper layer)
from flyash_phreeqc_ml import audit  # noqa: E402  (append-only audit log)
from flyash_phreeqc_ml import report  # noqa: E402  (one-click validation report)
from flyash_phreeqc_ml import run_manager  # noqa: E402
from flyash_phreeqc_ml.simulation import run_registry  # noqa: E402  (simulation run provenance)

from ui.common import _render_mapping_status_definitions, _render_next_step, _render_valid_now_section
from ui.state import _PRELIMINARY_CAVEAT, _PROJECT_ROOT, _rel

def _render_previous_simulation_runs() -> None:
    """Export-tab list of saved Simulate runs — kept separate from validation runs."""
    app_ui.section_header("Previous simulation runs",
                          "saved Simulate-tab PHREEQC executions — simulation outputs, "
                          "not measured-data validation")
    reg = run_registry.SimulationRunRegistry()
    runs = reg.list_runs()
    if not runs:
        st.caption("No saved simulation runs yet. Run a scenario or sweep in the **Simulate** "
                   "tab, then click **Save simulation run**.")
        return
    st.caption("These are **simulation runs** (model predictions under your assumptions), kept "
               "separate from measured-data validation runs.")
    st.dataframe(pd.DataFrame(runs), hide_index=True, use_container_width=True,
                 height=min(320, 60 + 30 * len(runs)))
    ids = [r["run_id"] for r in runs]
    chosen = st.selectbox("Download a run package", ids, key="exp_sim_run_choice")
    if chosen:
        try:
            st.download_button("Download run package (zip)", reg.export_zip(chosen),
                               file_name=f"{chosen}.zip", mime="application/zip",
                               key="exp_sim_run_zip")
        except Exception:                                     # noqa: BLE001
            pass

_OA_PF_GS_CAVEAT = (
    "OA, PF, and GS represent CO₂ exposure / cup-cover conditions: OA = open air, "
    "PF = plastic flap cover, GS = glass cover. OA is directly exposed to atmospheric CO₂; "
    "PF and GS are covered conditions that likely reduce CO₂ exchange. They are kept as "
    "distinct experimental conditions because cover material and CO₂ exposure can affect pH "
    "and carbonate formation. Do not treat PF or GS as fully sealed unless airtight sealing "
    "is confirmed."
)

def _render_export_report(selected_run: str | None) -> None:
    """"Export validation report" — build the self-contained bundle + offer a zip.

    Lab-like runs only. Builds ``experiments/<run>/outputs/validation_report_<ts>/``
    (report.html + CSVs + figures + MANIFEST.json), which itself logs an audit event,
    then offers the folder as a zip download.
    """
    if not selected_run:
        return
    try:
        rt = run_manager.load_run_config(selected_run).get("run_type")
    except run_manager.RunManagerError:
        return
    if rt not in run_manager.LAB_LIKE_RUN_TYPES:
        return

    app_ui.section_header("Results report (simulation + validation)",
                          "a self-contained bundle a reviewer can open without the app")
    st.caption(
        "Builds `experiments/<run>/outputs/validation_report_<ts>/` — a self-contained "
        "`report.html` (inline CSS + embedded figures), the supporting CSVs "
        "(measured / predictions / mapping / residuals / excluded / needed simulations), "
        "the audit log, and a `MANIFEST.json` of SHA-256 hashes. The report header always "
        "carries the validity status. (PDF export is future work.)"
    )
    if st.button("Export validation report", key=f"export_report_{selected_run}"):
        with st.spinner("Building validation report…"):
            try:
                out = report.build_report(selected_run)
            except Exception as exc:  # never let a build error crash the tab
                st.error(f"Report build failed: {exc}")
                return
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(out.iterdir()):
                    if p.is_file():
                        zf.write(p, arcname=f"{out.name}/{p.name}")
            st.session_state[f"report_zip_{selected_run}"] = (out.name, buf.getvalue())
            st.success(f"Built `{_rel(out)}`. Download below.")

    blob = st.session_state.get(f"report_zip_{selected_run}")
    if blob:
        name, data_bytes = blob
        st.download_button("⬇️ Download report (.zip)", data=data_bytes,
                           file_name=f"{name}.zip", mime="application/zip",
                           key=f"report_dl_{selected_run}")

def _render_help_tab() -> None:
    st.subheader("Design note — the validation module")
    st.info(
        "This describes the **Validation module** within the broader simulate → validate → "
        "learn platform. The validation module is a generic workflow: **measured data → "
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
        "experiment set) and read the three-mode overview + status.\n"
        "2. **Simulate** — describe an experiment in plain language → a structured scenario → "
        "a simulation plan/matrix → optionally run PHREEQC (gated, user-confirmed) and "
        "rank/refine the predictions. _Outputs are simulation predictions, not validated "
        "against measured data._\n"
        "3. **Import Data** — add measured rows (lab), upload/enter literature rows, or add "
        "synthetic demo rows, depending on run type, and review unit conversions.\n"
        "4. **Validate** — measured-data overview, data-quality validation, and "
        "calculation verification (the formula/unit/conversion audit).\n"
        "5. **Match** (lab runs) — link each `sample_id` to the model result row "
        "for the same chemistry.\n"
        "6. **Compare Results** — export to the pipeline, run Phase 1 → validate → compare → "
        "sustainability, then read the measured-vs-model comparison, residuals, and "
        "the validity line.\n"
        "7. **Export** — build a shareable results report, download the audit "
        "trail, and read this user guide."
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

    st.divider()
    with st.expander("Data safety — what is kept out of version control"):
        st.markdown(
            "- Real measured-release CSVs, uploaded Excel workbooks, generated outputs, "
            "processed CSVs, figures, `run_config.yaml`, condition/replicate mapping CSVs "
            "and per-run experiment files are **git-ignored by default** and are not "
            "committed unless explicitly force-added.\n"
            "- Each run's data lives in its own `experiments/<run>/` folder; only the "
            "folder's `README.md` is tracked.\n"
            "- Synthetic/demo rows are force-tagged `source_type=synthetic_demo` and are "
            "never treated as scientific output."
        )

    with st.expander("Future direction — an ML *correction* layer (not yet started)"):
        st.markdown(
            "The long-term aim is an ML layer that learns **where the model disagrees with "
            "experiment** — a correction on top of the chemistry, **not** a blind "
            "replacement for it. It stays dormant until real measured release data and "
            "exact mappings exist; today the app is a transparent, rule-based validation "
            "workflow with no learned weights."
        )

def _render_audit_trail(selected_run: str | None) -> None:
    """"Audit trail" — the run's append-only event log, filterable + downloadable.

    The JSONL file is itself the export; the table just makes it readable (newest
    first, filtered by event_type). The log records actions/ids/counts/hashes only —
    never measured values.
    """
    app_ui.section_header("Audit trail",
                          "append-only log of how this run's comparison was produced")
    if not selected_run:
        st.info("Select a run to see its audit trail.")
        return
    log = audit.read_audit(selected_run)
    if log.empty:
        st.caption("No audit events recorded for this run yet. Import data, accept "
                   "mappings, and run the workflow to populate the trail.")
        return

    present = list(dict.fromkeys(log["event_type"].tolist()))
    chosen = st.multiselect("Filter by event type", present, default=present,
                            key=f"audit_filter_{selected_run}")
    view = log[log["event_type"].isin(chosen)] if chosen else log
    # Newest first, with the payload rendered as compact JSON text.
    view = view.iloc[::-1].reset_index(drop=True)
    display = view.copy()
    display["payload"] = display["payload"].apply(
        lambda p: json.dumps(p, ensure_ascii=False, default=str))
    st.dataframe(display, use_container_width=True, height=320, hide_index=True)
    st.caption(f"{len(log)} event(s) total · showing {len(view)} after filter. "
               "The log is append-only — events are never edited or deleted.")

    raw = audit.audit_log_path(selected_run)
    if raw.exists():
        st.download_button(
            "⬇️ Download audit_log.jsonl",
            data=raw.read_bytes(),
            file_name=f"{selected_run}_audit_log.jsonl",
            mime="application/x-ndjson",
            key=f"audit_dl_{selected_run}",
        )

def _render_user_guide() -> None:
    """Render the markdown user guide (docs/user_guide/) so docs live in one place."""
    guide_dir = _PROJECT_ROOT / "docs" / "user_guide"
    order = ["getting_started.md", "input_formats.md", "mapping_guide.md",
             "interpreting_results.md", "data_safety.md", "faq.md"]
    files = [f for f in order if (guide_dir / f).exists()]
    files += sorted(p.name for p in guide_dir.glob("*.md") if p.name not in order) \
        if guide_dir.exists() else []
    if not files:
        st.info("User guide not found (expected markdown in `docs/user_guide/`).")
        return
    titles = {f: f.replace("_", " ").replace(".md", "").title() for f in files}
    choice = st.selectbox("Guide page", files, format_func=lambda f: titles[f],
                          key="user_guide_choice")
    st.markdown((guide_dir / choice).read_text(encoding="utf-8"))

def _render_export_tab(selected_run: str | None) -> None:
    """Export tab: validation report + downloads + audit trail + Help / user guide."""
    app_ui.render_page_header(
        "Export — share results and read the docs",
        "Build a self-contained results report (the model predictions used, replicate "
        "uncertainty, and validation against measured data), download the audit trail, and "
        "read the user guide — everything a reviewer needs without the app.",
        eyebrow="Export",
    )
    _render_next_step(selected_run)

    _render_export_report(selected_run)
    st.divider()
    _render_previous_simulation_runs()
    st.divider()
    _render_audit_trail(selected_run)

    st.divider()
    app_ui.section_header("Help & user guide", "for someone using the app for the first time")
    with st.expander("📖 User guide", expanded=False):
        _render_user_guide()
    with st.expander("Reference — design note, validation status & limitations", expanded=False):
        _render_help_tab()


# Tab entry point (app.py calls ui.export_tab.render).
render = _render_export_tab
