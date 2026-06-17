"""Settings section — AI configuration, PHREEQC engine status, app preferences (presentation only).

Lets the user configure the **AI assistant** (provider/model — never the key) and see the
**PHREEQC** executable/database status, toggle developer explanations, and read the intended
AI-framework / future-engine architecture. It owns no chemistry and no result-path logic; it
only reads status and sets the AI provider/model runtime override (which persists for the
process) and the dev-mode session flag.

One of the seven top-level sections (Assistant · Workspace · Results · Data & Validation ·
Projects · Engine Library · Settings).
"""
from __future__ import annotations

import streamlit as st

import app_ui
from flyash_phreeqc_ml.ai import config as ai_config
from flyash_phreeqc_ml.simulation import phreeqc_executor


def _short_path(path) -> str:
    if not path:
        return ""
    s = str(path)
    return s if len(s) <= 42 else "…" + s[-40:]


def _render_ai_settings() -> None:
    """AI provider/model selector + status. Never shows the key. Sets a process-level runtime
    override (``ai_config.set_runtime_overrides``) that persists across reruns and every section."""
    app_ui.section_header("AI assistant",
                          "conversation / planning / explanation — never the chemistry")
    ai_config.clear_runtime_overrides()
    base_model = ai_config.resolve_config().model

    c1, c2 = st.columns(2)
    provider = c1.selectbox("Provider", list(ai_config.SUPPORTED_PROVIDERS), index=0,
                            key="ai_provider_choice", help="Only Anthropic is supported today.")
    options = list(dict.fromkeys([base_model, *ai_config.SUGGESTED_MODELS]))
    picked = c2.selectbox("Model (suggested)", options, index=0, key="ai_model_pick",
                          help="Used for AI suggestions only. Overrides ANTHROPIC_MODEL for this session.")
    custom = st.text_input("…or enter a model id", key="ai_model_custom",
                           help="Leave blank to use the selected model above.").strip()
    ai_config.set_runtime_overrides(provider=provider, model=(custom or picked))
    cfg = ai_config.resolve_config()

    app_ui.render_metric_cards([
        {"label": "AI status", "value": "Enabled" if cfg.enabled else "Disabled",
         "status": "success" if cfg.enabled else "neutral"},
        {"label": "Provider", "value": cfg.provider},
        {"label": "Model", "value": cfg.model},
        {"label": "API key", "value": "Detected" if cfg.key_present else "Not detected",
         "caption": cfg.key_source if cfg.key_present else "set ANTHROPIC_API_KEY",
         "status": "success" if cfg.key_present else "neutral"},
    ])
    st.caption(f"Role: {ai_config.AI_ROLE_LINE}.")
    st.caption(f"SDK available (`anthropic`): **{'yes' if cfg.sdk_available else 'no'}**.")
    st.caption("The API key is read only from the `ANTHROPIC_API_KEY` environment variable or a "
               "Streamlit secret — it is never entered or shown here.")
    if not cfg.enabled:
        st.caption(f"Disabled — {cfg.disabled_reason()}. The assistant still works with a "
                   "deterministic planner.")
    st.warning(ai_config.AI_EXPERIMENTAL_WARNING)


def _render_phreeqc_engine() -> None:
    app_ui.section_header("Geochemical engine — PHREEQC",
                          "the first executable engine (leaching / aqueous dissolution)")
    av = phreeqc_executor.check_availability()
    app_ui.render_metric_cards([
        {"label": "PHREEQC", "value": "Ready" if av.can_run else "Not configured",
         "status": "success" if av.can_run else "warning"},
        {"label": "Executable", "value": "Found" if av.executable_found else "Missing",
         "caption": _short_path(av.executable_path) or "set PHREEQC_EXE",
         "status": "success" if av.executable_found else "neutral"},
        {"label": "Database", "value": "Found" if av.database_found else "Missing",
         "caption": _short_path(av.database_path) or "set PHREEQC_DATABASE",
         "status": "success" if av.database_found else "neutral"},
    ])
    st.caption(av.message)
    st.caption("Configure by pointing the app at a PHREEQC CLI you supply: set `PHREEQC_EXE` and "
               "`PHREEQC_DATABASE` (CEMDATA18 is not redistributable — user-supplied). The "
               "assistant still **plans** and builds reviewable input without it.")


def _render_preferences() -> None:
    app_ui.section_header("Preferences")
    st.checkbox("🛠️ Developer explanation mode", value=False, key="dev_mode",
                help="Show deeper chemistry/statistics explanations (mainly in Data & Validation).")


def _render_future_architecture() -> None:
    app_ui.section_header("AI framework & future engines",
                          "designed for, not yet built — no LangGraph dependency added")
    st.markdown(
        "**Current:** a custom AI agent layer → tool/action registry → policy gate → deterministic "
        "backend tools, with **human confirmation before any execution**.\n\n"
        "**Future (compatible by design):**\n"
        "- **LangGraph-style stateful orchestrator** — the propose → policy-gate → confirm → "
        "deterministic-tool loop is already a state machine (`AgentState` = graph state, actions = "
        "nodes, the policy = the edge function), so it can be re-expressed as a graph without "
        "changing the safety model.\n"
        "- **Plugin engine registry** — engines register per domain (today only "
        "`leaching_geochemistry → PHREEQC`); see **Engine Library**.\n"
        "- **Literature / RAG agent**, **ML / surrogate agent**, **simulation-engine agents** "
        "(atomistic / mechanical-property / thermal), and a **validation / calibration agent**.\n\n"
        "No LangGraph dependency is added yet. See `docs/ai_architecture.md`.")


def _render_settings(selected_run: str | None) -> None:
    app_ui.render_page_header(
        "Settings",
        "Configure the AI assistant and the PHREEQC engine, set preferences, and read the "
        "intended AI-framework architecture.",
        eyebrow="AI · engine · preferences · architecture")
    _render_ai_settings()
    st.divider()
    _render_phreeqc_engine()
    st.divider()
    _render_preferences()
    st.divider()
    _render_future_architecture()


# The app dispatches to ``render``.
render = _render_settings
