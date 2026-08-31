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
    prediction_models, product_shell, phase3_evidence, phase3_workflows,
)
from ui.state import MODEL_NAME  # noqa: E402

from flyash_phreeqc_ml import run_manager  # noqa: E402
from flyash_phreeqc_ml.security import identity as security_identity  # noqa: E402
























































































































































































































    # No lab-comparison read-out here: comparisons are per-run (stored under the lab
    # run's own outputs/), so a literature run never displays another run's results.








































































































































# --------------------------------------------------------------------------- #
# Phase 2 product shell.  The sidebar owns one nine-page navigation system and
# persistent durable context.  Legacy scientific workflows are embedded under
# those pages; they are not duplicated as a second top-level navigation.
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="WPI Virtual LAB", layout="wide", page_icon="▦")
try:
    IDENTITY = security_identity.resolve_streamlit_identity(st)
except security_identity.AuthenticationRequiredError:
    # Logout/expiry must not leave the previous user's AI conversation, upload,
    # provider choice, or scientific form state available to a later login in the
    # same browser session.
    if security_identity.deployment_mode().value == "hosted":
        st.session_state.clear()
    st.title("WPI Virtual LAB")
    st.info("This hosted scientific workspace requires an invited account.")
    if hasattr(st, "login") and st.button("Sign in", type="primary"):
        st.login()
    st.stop()
except security_identity.AuthorizationError:
    st.session_state.clear()
    st.title("WPI Virtual LAB")
    st.error("This authenticated account is not authorized for the hosted beta.")
    if hasattr(st, "logout") and st.button("Sign out"):
        st.logout()
    st.stop()
except security_identity.IdentityError:
    st.session_state.clear()
    st.title("WPI Virtual LAB")
    st.error("Hosted identity configuration is invalid. Access remains closed until an "
             "administrator fixes it.")
    st.stop()

# Authentication and authorization are resolved before any tenant-aware store
# exists. OIDC tokens/claims are not copied into session state or diagnostics.
if IDENTITY.is_hosted:
    security_identity.bind_session_state(st.session_state, IDENTITY)
security_identity.set_current_identity(IDENTITY)
app_ui.inject_global_css()
STORE = product_shell.get_store(IDENTITY)
PAGE, CONTEXT = product_shell.render_sidebar(STORE)
DEV_MODE = bool(st.session_state.get("dev_mode", False))
LEGACY_RUN = st.session_state.get("selected_run")

product_shell.render_product_header(STORE, CONTEXT)

if PAGE == "Home":
    product_shell.render_home(STORE, CONTEXT)

elif PAGE == "Projects":
    product_shell.render_projects(STORE, CONTEXT)
    st.markdown('<div class="vl-control-gap"></div>', unsafe_allow_html=True)
    if st.button("Open legacy runs and reports", key="projects_show_legacy"):
        st.session_state["projects_legacy_visible"] = not st.session_state.get(
            "projects_legacy_visible", False)
        st.rerun()
    if st.session_state.get("projects_legacy_visible", False):
        st.divider()
        app_ui.section_header("Legacy runs and reports")
        LEGACY_RUN = product_shell.render_legacy_run_manager()
        export_tab.render(LEGACY_RUN)

elif PAGE == "Material Workspace":
    app_ui.render_page_header(
        "Material Workspace",
        "Record the material, its composition source, and the scientific workflows that use it.")
    material_tab, assistant_view, phreeqc_view, import_view = st.tabs(
        ["Material record", "Assistant", "PHREEQC planning", "Import measured data"])
    with material_tab:
        product_shell.render_material_record(STORE, CONTEXT)
    with assistant_view:
        assistant_tab.render(LEGACY_RUN, DEV_MODE)
    with phreeqc_view:
        app_ui.render_warning_panel(
            "Authoritative Phase 1A path",
            "PHREEQC input is built, previewed, reviewed, and exactly confirmed here. The generic "
            "machine workspace cannot author or execute PHREEQC input.", level="warning")
        simulate_tab.render(LEGACY_RUN, DEV_MODE)
    with import_view:
        import_tab.render(LEGACY_RUN)

elif PAGE == "Machines":
    ACTIVE_MACHINE = product_shell.render_machine_gallery(STORE, CONTEXT)
    if ACTIVE_MACHINE:
        product_shell.render_machine_workspace(STORE, CONTEXT, ACTIVE_MACHINE)
        specialized_visible = st.session_state.get(
            f"machine_specialized_visible__{ACTIVE_MACHINE}", False)
        if specialized_visible and ACTIVE_MACHINE == "ml_surrogate_predictor":
            st.divider()
            if st.button("Close approved-model workflow", key="machine_specialized_close_ml"):
                st.session_state[f"machine_specialized_visible__{ACTIVE_MACHINE}"] = False
                st.rerun()
            prediction_models.render(LEGACY_RUN, DEV_MODE)
        product_shell.render_machine_picker(STORE, CONTEXT)

elif PAGE == "Results":
    product_shell.render_results_index(STORE, CONTEXT)
    with st.expander("Legacy live PHREEQC result view"):
        results.render(LEGACY_RUN)

elif PAGE == "Validation & Uncertainty":
    product_shell.render_validation_header(STORE, CONTEXT)
    st.markdown("### Operational durable ICP validation")
    st.caption(
        "Only a finalized Phase 3 ICP review with an exactly verified source can cross "
        "this processor-owned validation gate."
    )
    phase3_workflows.render_icp_validation_gate(STORE, CONTEXT)
    st.divider()
    st.markdown("### Legacy validation workflows")
    st.caption(
        "The Import, Validate, Match, and Compare tools below preserve the existing "
        "workflow. They do not finalize a Phase 3 ICP review or authorize durable ICP evidence."
    )
    validation_overview, sub_import, sub_validate, sub_match, sub_compare = st.tabs(
        ["Overview", "Import", "Validate", "Match", "Compare"])
    with validation_overview:
        product_shell.render_validation_overview(STORE, CONTEXT, LEGACY_RUN)
    with sub_import:
        import_tab.render(LEGACY_RUN)
    with sub_validate:
        validate_tab.render(LEGACY_RUN, DEV_MODE)
    with sub_match:
        match_tab.render(LEGACY_RUN)
    with sub_compare:
        compare_tab.render(LEGACY_RUN)

elif PAGE == "Evidence":
    product_shell.render_evidence_header(STORE, CONTEXT)
    evidence_overview, evidence_manual, evidence_workflow = st.tabs(
        ["Overview", "Manual evidence & review", "Legacy Evidence Library"])
    with evidence_overview:
        st.write("Add evidence manually, review its exact source location, or use the legacy search library.")
        st.caption("Literature evidence is not a measurement of your material.")
    with evidence_manual:
        phase3_evidence.render_editor(STORE, CONTEXT)
    with evidence_workflow:
        evidence_library.render(
            LEGACY_RUN, DEV_MODE, store=STORE, context=CONTEXT)

elif PAGE == "Run History":
    product_shell.render_history(STORE, CONTEXT)

elif PAGE == "Settings & Diagnostics":
    product_shell.render_diagnostics(STORE, CONTEXT)
    settings_overview, settings_tab, engines_tab = st.tabs(
        ["Overview", "Application settings", "Engine library"])
    with settings_overview:
        st.write("Open Application settings to configure tools, or Engine library to inspect capabilities.")
    with settings_tab:
        settings.render(LEGACY_RUN, identity=IDENTITY)
    with engines_tab:
        engine_library.render(LEGACY_RUN)
