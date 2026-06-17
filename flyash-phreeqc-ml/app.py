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

# UI tab modules (extracted from this file — see docs/refactor_plan.md).
from ui import (  # noqa: E402
    assistant_tab, simulate_tab, import_tab, validate_tab, match_tab,
    compare_tab, export_tab,
)
from ui.state import MODEL_NAME, PRODUCT_NAME, PRODUCT_SUBTITLE, _rel  # noqa: E402

from flyash_phreeqc_ml import run_manager  # noqa: E402
from flyash_phreeqc_ml.ai import config as ai_config  # noqa: E402  (AI settings/status authority)



















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
    st.sidebar.info("➡️ Use **Simulate** to plan, or **Compare Results** to validate against measured data.")
    return selected


def _render_ai_settings_panel() -> None:
    """Sidebar 'AI settings' panel — status + provider/model selection.

    Read-only with respect to the science: it shows whether the optional AI layer is
    enabled and lets the user pick the provider/model used for *suggestions*. It never
    affects mapping, residuals, validation status, or the comparison data, and it never
    shows or accepts the API key (the key comes only from the environment or Streamlit
    secrets — see :mod:`flyash_phreeqc_ml.ai.config`).
    """
    with st.sidebar.expander("🤖 AI settings", expanded=False):
        # The base model from env / secrets / default, ignoring any prior UI override —
        # so the picker defaults to it and a no-op selection never clobbers ANTHROPIC_MODEL.
        ai_config.clear_runtime_overrides()
        base_model = ai_config.resolve_config().model

        provider = st.selectbox(
            "Provider", list(ai_config.SUPPORTED_PROVIDERS), index=0,
            key="ai_provider_choice", help="Only Anthropic is supported today.")

        options = list(dict.fromkeys([base_model, *ai_config.SUGGESTED_MODELS]))
        picked = st.selectbox(
            "Model (suggested)", options, index=0, key="ai_model_pick",
            help="Used for AI suggestions only. Overrides ANTHROPIC_MODEL for this session.")
        custom = st.text_input(
            "…or enter a model id", key="ai_model_custom",
            help="Leave blank to use the selected model above.").strip()
        effective_model = custom or picked

        # Apply the choice for this process so the AI helpers + the status below use it.
        ai_config.set_runtime_overrides(provider=provider, model=effective_model)
        cfg = ai_config.resolve_config()

        st.markdown(f"**Status:** {'🟢 enabled' if cfg.enabled else '⚪ disabled'}")
        st.markdown(f"- Provider: `{cfg.provider}`")
        st.markdown(f"- Model: `{cfg.model}`")
        st.markdown(
            f"- API key detected: **{'yes' if cfg.key_present else 'no'}**"
            + (f" · {cfg.key_source}" if cfg.key_present else ""))
        st.markdown(f"- SDK available: **{'yes' if cfg.sdk_available else 'no'}**")
        st.caption(f"Role: {ai_config.AI_ROLE_LINE}.")
        if not cfg.enabled:
            st.caption(f"Disabled — {cfg.disabled_reason()}.")
        st.caption(
            "The API key is read only from the `ANTHROPIC_API_KEY` environment variable "
            "or a Streamlit secret — it is never entered or shown here.")
        st.warning(ai_config.AI_EXPERIMENTAL_WARNING)



























































































































































































































    # No lab-comparison read-out here: comparisons are per-run (stored under the lab
    # run's own outputs/), so a literature run never displays another run's results.








































































































































# --------------------------------------------------------------------------- #
# Page — wide layout, run-management sidebar, and a tabbed dashboard
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="Materials Research Assistant",
                   layout="wide", page_icon="🔬")
app_ui.inject_global_css()
app_ui.render_hero(
    PRODUCT_NAME,
    PRODUCT_SUBTITLE,
    eyebrow="Describe experiment → assistant asks what's missing → picks the right pathway → runs available simulations (after you confirm) → compares with measured data",
    chips=[
        (f"Executable engine: leaching / geochemistry via {MODEL_NAME}", "info"),
        ("Planning support: composites · thermal · cementitious · battery · corrosion", "neutral"),
        ("Modular — more engines can be added", "neutral"),
    ],
)
st.caption("💬 **Assistant** is the simple way in. The remaining tabs are **Advanced Mode** — "
           "full manual controls for each step (you don't need them to use the assistant).")

# Sidebar "save files" — selecting a run here drives every tab below.
SELECTED_RUN = _render_run_sidebar()

st.sidebar.divider()
DEV_MODE = st.sidebar.checkbox(
    "🛠️ Developer explanation mode", value=False, key="dev_mode",
    help="Show deeper chemistry/statistics explanations, mainly in the "
         "Validate tab.",
)

_render_ai_settings_panel()

(tab_assistant, tab_simulate, tab_import, tab_validate, tab_match, tab_compare,
 tab_export) = st.tabs([
    "Assistant", "Advanced Simulate", "Import Data", "Validate", "Match", "Compare", "Export",
])

with tab_assistant:
    assistant_tab.render(SELECTED_RUN, DEV_MODE)
with tab_simulate:
    app_ui.render_advanced_mode_note("Advanced Simulate")
    simulate_tab.render(SELECTED_RUN, DEV_MODE)
with tab_import:
    app_ui.render_advanced_mode_note("Import Data")
    import_tab.render(SELECTED_RUN)
with tab_validate:
    app_ui.render_advanced_mode_note("Validate")
    validate_tab.render(SELECTED_RUN, DEV_MODE)
with tab_match:
    app_ui.render_advanced_mode_note("Match")
    match_tab.render(SELECTED_RUN)
with tab_compare:
    app_ui.render_advanced_mode_note("Compare")
    compare_tab.render(SELECTED_RUN)
with tab_export:
    app_ui.render_advanced_mode_note("Export")
    export_tab.render(SELECTED_RUN)
