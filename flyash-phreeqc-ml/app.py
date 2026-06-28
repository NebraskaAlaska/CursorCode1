"""Streamlit interface for the flyash-phreeqc-ml project.

A thin GUI on top of the existing Phase 1 / Phase 2 code — it does **not**
reimplement any pipeline logic. It presents an AI-assisted geochemical simulation &
validation platform: a run-management sidebar drives a guided seven-tab workflow
**Start → Simulate → Import Data → Validate → Match → Compare Results → Export**.
**Simulate** is the forward-looking planning core (describe an experiment → structured
scenario → simulation plan; no model is executed yet); the measured-vs-model mapping +
comparison is the current strongest **validation module**. Each tab reuses the package
functions; this file adds no chemistry or ML on the result path. It lets you:

* see the three product modes (Simulate / Validate / Learn) + run status at a glance,
* plan a simulation from a plain-language description (Simulate, planning only),
* enter measured / literature / demo data into per-run save files,
* map measured samples to model rows and run the existing scripts,
* read an honest measured-vs-model summary, and browse model outputs.

Run with:  streamlit run app.py
"""

import sys
from pathlib import Path



_APP_DIR = str(Path(__file__).resolve().parent)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import streamlit as st  # noqa: E402

import app_ui  # noqa: E402  (presentation-only UI helper layer)

# UI section + workflow modules (see docs/refactor_plan.md). The Assistant is the main
# workspace; the technical workflows are grouped into the other sections.
from ui import (  # noqa: E402
    assistant_tab, simulate_tab, import_tab, validate_tab, match_tab,
    compare_tab, export_tab, results, engine_library, settings, evidence_library,
    prediction_models, digital_lab,
)
from ui.state import MODEL_NAME, PRODUCT_NAME, PRODUCT_SUBTITLE, _rel  # noqa: E402

from flyash_phreeqc_ml import run_manager  # noqa: E402



















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
                                 placeholder="2026-06-03 fly-ash leaching experiment")
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
    st.sidebar.caption(f"📁 {_rel(run_manager.run_dir(selected))}")
    if cfg.get("description"):
        st.sidebar.caption(f"📝 {cfg['description']}")
    st.sidebar.caption(f"⚠️ {run_manager.warning_for(cfg.get('run_type'))}")
    st.sidebar.info("➡️ Use the **Research Assistant** to plan + run, or **Data & Validation** "
                    "to compare against measured data.")
    return selected



























































































































































































































    # No lab-comparison read-out here: comparisons are per-run (stored under the lab
    # run's own outputs/), so a literature run never displays another run's results.








































































































































# --------------------------------------------------------------------------- #
# Page — assistant-first cockpit with a simple seven-section left-rail navigation.
# Assistant (main) · Workspace · Results · Data & Validation · Projects · Engine Library ·
# Settings. Every existing workflow is still reachable — the technical builder is Workspace
# (the full manual Simulate), the measured-vs-model workflow is Data & Validation; none removed.
# --------------------------------------------------------------------------- #
SEC_ASSISTANT = "Assistant"
SEC_WORKSPACE = "Workspace"
SEC_RESULTS = "Results"
SEC_DATA = "Data & Validation"
SEC_PROJECTS = "Projects"
SEC_EVIDENCE = "Evidence Library"
SEC_PREDICTION = "Prediction Models"
SEC_ENGINES = "Engine Library"
SEC_DIGITAL_LAB = "Digital Lab"
SEC_SETTINGS = "Settings"
SECTIONS = [SEC_ASSISTANT, SEC_WORKSPACE, SEC_RESULTS, SEC_DATA, SEC_PROJECTS, SEC_EVIDENCE,
            SEC_PREDICTION, SEC_ENGINES, SEC_DIGITAL_LAB, SEC_SETTINGS]

st.set_page_config(page_title="Materials Research Assistant",
                   layout="wide", page_icon="🔬")
app_ui.inject_global_css()
app_ui.render_hero(
    PRODUCT_NAME,
    PRODUCT_SUBTITLE,
    eyebrow="Broad materials research software · the assistant is the front door",
    chips=[
        (f"First executable engine: leaching / geochemistry via {MODEL_NAME}", "info"),
        ("Planning support: composites · thermal · cementitious · battery · corrosion", "neutral"),
        ("Class C fly ash is the first mature demo — not the whole product", "neutral"),
    ],
)

# Sidebar — run management, then the primary section navigation, then a dev-mode flag (set in
# Settings, read here so every section can pass it through).
SELECTED_RUN = _render_run_sidebar()
st.sidebar.divider()
st.sidebar.markdown("**Navigate**")
SECTION = st.sidebar.radio("Navigate", SECTIONS, key="nav_section",
                           label_visibility="collapsed")
DEV_MODE = bool(st.session_state.get("dev_mode", False))

if SECTION == SEC_ASSISTANT:
    assistant_tab.render(SELECTED_RUN, DEV_MODE)

elif SECTION == SEC_WORKSPACE:
    app_ui.render_page_header(
        "Workspace",
        "The structured experiment builder behind the assistant — material profile, release model, "
        "database & phases, input preview, then gated run / sweep / ranking / target matching / save. "
        "Advanced controls; the assistant is the simple way in.",
        eyebrow="Build · preview · run · sweep · save")
    simulate_tab.render(SELECTED_RUN, DEV_MODE)

elif SECTION == SEC_RESULTS:
    results.render(SELECTED_RUN)

elif SECTION == SEC_DATA:
    app_ui.render_page_header(
        "Data & Validation",
        "Measured data only — import, validate, map measured samples to model runs, and compare. "
        "Simulation predicts; validation compares with reality. The two are kept separate.",
        eyebrow="Import · Validate · Match · Compare")
    sub_import, sub_validate, sub_match, sub_compare = st.tabs(
        ["Import", "Validate", "Match", "Compare"])
    with sub_import:
        app_ui.render_advanced_mode_note("Import Data")
        import_tab.render(SELECTED_RUN)
    with sub_validate:
        app_ui.render_advanced_mode_note("Validate")
        validate_tab.render(SELECTED_RUN, DEV_MODE)
    with sub_match:
        app_ui.render_advanced_mode_note("Match")
        match_tab.render(SELECTED_RUN)
    with sub_compare:
        app_ui.render_advanced_mode_note("Compare")
        compare_tab.render(SELECTED_RUN)

elif SECTION == SEC_PROJECTS:
    app_ui.render_page_header(
        "Projects",
        "Saved simulation runs, planning projects, validation runs, and material profiles — with "
        "report export, audit trail, and the user guide.",
        eyebrow="Runs · reports · provenance")
    export_tab.render(SELECTED_RUN)

elif SECTION == SEC_EVIDENCE:
    evidence_library.render(SELECTED_RUN, DEV_MODE)

elif SECTION == SEC_PREDICTION:
    prediction_models.render(SELECTED_RUN, DEV_MODE)

elif SECTION == SEC_ENGINES:
    engine_library.render(SELECTED_RUN)

elif SECTION == SEC_DIGITAL_LAB:
    digital_lab.render(SELECTED_RUN, DEV_MODE)

elif SECTION == SEC_SETTINGS:
    settings.render(SELECTED_RUN)
