"""Start tab — overview, product modes, workflow checklist, next-step.

Extracted from app.py by the UI modularization refactor — see
docs/refactor_plan.md. Behavior is unchanged (verbatim move)."""
from __future__ import annotations

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
import app_ui  # noqa: E402  (presentation-only UI helper layer)
from ui.formatters import has_numeric as _has_numeric  # noqa: E402
from flyash_phreeqc_ml import config  # noqa: E402
from flyash_phreeqc_ml import replicates  # noqa: E402
from flyash_phreeqc_ml import run_manager  # noqa: E402
from flyash_phreeqc_ml.experiments import validate_experimental_df  # noqa: E402
from flyash_phreeqc_ml.parsers import (  # noqa: E402
    has_measured_data,
    load_experimental_release,
)

from ui.common import _render_mapping_status_definitions, _render_valid_now_section
from ui.state import MODEL_NAME, _ICP_MEASURED_COLS, _PRELIMINARY_CAVEAT, _looks_like_run_test, _manifest_if_available, _next_step_hint, _read_csv, _rel, _run_comparison_path

# The platform's forward arc (describe → simulate → validate → learn), shown as a neutral
# orientation stepper on Start. "Run model" is future work (no execution from Simulate yet).
WORKFLOW_STEPS = ["Describe experiment", "Extract scenario", "Simulation plan",
                  "Run model (future)", "Validate vs measured", "Learn & improve"]

def _load_measured_safe() -> pd.DataFrame:
    try:
        # Non-strict so a partially-filled manual file still loads in the UI.
        return load_experimental_release(strict=False)
    except Exception as exc:  # pragma: no cover - defensive UI guard
        st.warning(f"Could not load experimental data: {exc}")
        return pd.DataFrame()

def _comparison_has_residuals(run_name: str | None) -> bool:
    """True if the run's comparison CSV exists and has at least one numeric residual."""
    comp_path = _run_comparison_path(run_name)
    if comp_path is None or not comp_path.exists():
        return False
    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    return any(_has_numeric(comp, f"residual_{el}")
               for el in ["pH", "Ca", "Si", "Al", "Fe"])

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

    _counts = status["counts"]
    n_exact = _counts.get(replicates.MAPPING_STATUS_EXACT, 0)
    n_scn = _counts.get(replicates.MAPPING_STATUS_SCENARIO, 0)
    n_unsafe = _counts.get(replicates.MAPPING_STATUS_UNSAFE, 0)
    n_needs = _counts.get(replicates.MAPPING_STATUS_NEEDS_NEW, 0)

    # The six headline cards (data + mapping coverage), status-coloured.
    app_ui.render_metric_cards([
        {"label": "Data loaded", "value": "Yes" if n_rows else "No",
         "caption": rt, "status": "good" if n_rows else "neutral"},
        {"label": "Measured rows", "value": n_rows, "status": "info" if n_rows else "neutral"},
        {"label": "Mapped samples", "value": msum["n_samples"] if lab_like else "n/a",
         "caption": f'{msum["n_unique_rows"]} unique model result(s)' if lab_like else "",
         "status": "good" if (lab_like and msum["n_samples"]) else "neutral"},
        {"label": "Exact mappings", "value": n_exact if lab_like else "n/a",
         "status": "exact" if (lab_like and n_exact) else "neutral"},
        {"label": "Unsafe mappings", "value": n_unsafe if lab_like else "n/a",
         "status": "unsafe" if (lab_like and n_unsafe) else "neutral"},
        {"label": "Needs new sim", "value": n_needs if lab_like else "n/a",
         "status": "needs new simulation" if (lab_like and n_needs) else "neutral"},
    ])
    # Secondary cards — measured coverage + validation.
    app_ui.render_metric_cards([
        {"label": "Rows with pH", "value": rows_with_ph,
         "status": "good" if rows_with_ph else "neutral"},
        {"label": "Rows with Ca/Si/Al", "value": rows_with_chem,
         "status": "good" if rows_with_chem else "neutral"},
        {"label": "Scenario-level", "value": n_scn if lab_like else "n/a",
         "status": "scenario-level" if (lab_like and n_scn) else "neutral"},
        {"label": "Validation errors", "value": n_err,
         "status": "error" if n_err else "good"},
        {"label": "Validation warnings", "value": n_warn,
         "status": "warning" if n_warn else "good"},
    ])

    overall = status["overall"]
    comp_label = "Available (preliminary)" if comp_exists else "Not run yet"
    comp_status = "preliminary" if comp_exists else "neutral"
    st.markdown(
        "**Overall mapping status:** "
        + app_ui.status_badge(overall, overall if lab_like else "neutral")
        + " &nbsp; **Comparison:** " + app_ui.status_badge(comp_label, comp_status),
        unsafe_allow_html=True,
    )

    # Recommended next *scientific* step.
    if not n_rows:
        nxt = ("Describe an experiment in the **Simulate** tab to plan a simulation, or "
               "import measured data in the **Import Data** tab to validate against predictions.")
    elif n_err:
        nxt = f"Fix {n_err} validation error(s) in the **Import Data** / **Validate** tabs before mapping."
    elif lab_like and msum["n_samples"] == 0:
        nxt = "Map conditions to model scenarios in the **Match** tab."
    elif status["counts"].get("unsafe", 0):
        nxt = ("Resolve unsafe mapping(s) (e.g. HCl mapped to a NaOH/CO2 scenario) — generate "
               "matching acid PHREEQC scenarios.")
    elif lab_like and not status["all_exact"]:
        nxt = ("Generate time- and condition-resolved PHREEQC scenarios so mappings can become "
               "exact; the current comparison is a workflow check, not final validation.")
    elif not comp_exists:
        nxt = "Run the workflow in the **Compare Results** tab to generate the comparison."
    else:
        nxt = "Review the preliminary comparison in the **Compare Results** tab."
    st.success(f"**Recommended next scientific step:** {nxt}")

    if not (lab_like and status["all_exact"]):
        st.warning("⚠️ " + _PRELIMINARY_CAVEAT)

    with st.expander("Mapping status definitions"):
        _render_mapping_status_definitions()
    with st.expander("What is valid now vs not fully valid yet"):
        _render_valid_now_section()

def _render_modes_panel() -> None:
    """The three product modes — the platform's front-door explanation."""
    st.markdown("#### Three ways to use this platform")
    m1, m2, m3 = st.columns(3)
    m1.markdown(
        "**1 · Simulate**  \nDescribe an experiment and the variables you care about. AI "
        "extracts a structured scenario, flags missing info and assumptions, builds a "
        "simulation plan/matrix, and — on an explicit, user-confirmed step — runs PHREEQC "
        "and plots the predicted outputs.  \n_Outputs are simulation predictions, not "
        "validated against measured data._")
    m2.markdown(
        "**2 · Validate**  \nCompare measured data against model predictions using "
        "transparent mapping status and residuals, with an honest validity status.  \n"
        f"_Current strongest workflow: Class C fly ash + {MODEL_NAME}._")
    m3.markdown(
        "**3 · Learn & Improve**  \nUse literature, measured data, residuals, and "
        "(experimental) surrogate/ML tools to improve predictions and recommend "
        "experiments.  \n_Experimental, display-only — never on the result path._")
    st.caption(
        "AI extracts and organizes scenario information; **deterministic code and "
        "simulation engines generate the scientific outputs**, and AI output always "
        "requires your review. A simulation plan is not a validated prediction — exact "
        "mapping and measured data are required for any validation claim.")
    st.divider()

def _render_overview(selected_run: str | None) -> None:
    """Three-mode product panel + run status + a recommended next step."""
    app_ui.render_page_header(
        "Start — what this platform does",
        "An AI-assisted platform for geochemical / material-leaching simulation and "
        "validation. Describe an experiment to plan a simulation, and — where you have "
        "measured data — validate and correct the predictions against it.",
        eyebrow="Overview",
    )
    _render_modes_panel()
    app_ui.render_workflow_steps(WORKFLOW_STEPS, current=None)
    st.write("")

    app_ui.section_header("Validation module — status",
                          "measured-data, mapping & comparison status for the current run")
    _render_presentation_summary(selected_run)
    st.divider()

    master_path = config.PROCESSED_DIR / config.MASTER_DATASET_CSV
    template_path = config.EXPERIMENTAL_ICP_DIR / config.EXPERIMENTAL_TEMPLATE_CSV
    measured = _load_measured_safe()
    measured_exists = has_measured_data(measured)

    app_ui.section_header("Workspace status", "shared pipeline artifacts (validation module)")
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
            "No measured data yet — the **Validate** module stays dormant until measured "
            "data is added. You can still use the **Simulate** tab to plan a simulation "
            "before any measurement. (For the fly-ash workflow this is the Phase-2 release "
            "data; comparison and any future ML stay dormant until it is entered.)"
        )

    st.divider()
    app_ui.section_header("Selected run", "the active save file")
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
    st.caption(f"📁 {_rel(run_manager.run_dir(selected_run))} · source `{cfg.get('data_source')}`")

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
            missing.append("measured → model mapping (needed for residuals)")
    if missing:
        st.markdown("**Missing / not yet present:**")
        for m in missing:
            st.markdown(f"- {m}")

    # Recommended next action (shared logic, also surfaced on every tab).
    st.success(f"**Recommended next action:** {_next_step_hint(selected_run)}")

    # Workflow checklist — rendered as a progression stepper (first incomplete
    # step is highlighted; completed steps read as done).
    st.divider()
    app_ui.section_header("Workflow checklist", "progress for this run")
    data_uploaded = not data.empty
    data_checked = (config.TABLES_DIR / config.EXPERIMENTAL_VALIDATION_REPORT_CSV).exists()
    mapping_complete = lab_like and has_map
    workflow_run = comp_exists
    results_available = comp_exists and _comparison_has_residuals(selected_run)

    checks = [data_uploaded, data_checked, mapping_complete, workflow_run, results_available]
    labels = ["Data uploaded", "Data checked", "Mapping complete",
              "Workflow run", "Results available"]
    current = next((i for i, c in enumerate(checks) if not c), len(checks))
    app_ui.render_workflow_steps(labels, current=current)
    st.markdown(
        " &nbsp;·&nbsp; ".join(
            app_ui.status_badge(lbl, "good" if done else "neutral")
            for lbl, done in zip(labels, checks)
        ),
        unsafe_allow_html=True,
    )

def _render_start_tab(selected_run: str | None) -> None:
    """Start tab: the overview/status + a pointer to the in-app help."""
    _render_overview(selected_run)
    st.divider()
    st.caption("📖 New here? Try the **Simulate** tab to describe an experiment, or see the "
               "**Export** tab → *Help & user guide* for getting-started, input formats, the "
               "mapping guide, how to read results, and data safety.")


# Tab entry point (app.py calls ui.start_tab.render).
render = _render_start_tab
