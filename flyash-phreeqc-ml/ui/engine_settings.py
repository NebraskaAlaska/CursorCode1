"""Engine Settings section — engines, AI configuration, and the future engine roadmap.

A presentation-only settings page: it shows which **simulation engines** are available
(PHREEQC for leaching/geochemistry) vs planning-only vs future, lets the user configure the
**AI assistant** (provider/model — never the key), and shows the **PHREEQC** executable/database
status. It owns no chemistry and no result-path logic; it only reads status and sets the
AI provider/model runtime override (which persists for the process).

This is one of the four top-level sections of the Materials Research Assistant
(Research Assistant · Projects / Runs · Data & Validation · Engine Settings).
"""
from __future__ import annotations

import streamlit as st

import app_ui
from flyash_phreeqc_ml.agent import domains
from flyash_phreeqc_ml.ai import config as ai_config
from flyash_phreeqc_ml.simulation import phreeqc_executor


def _render_ai_settings() -> None:
    """AI provider/model selector + status (moved from the sidebar). Never shows the key.

    Sets a process-level runtime override (``ai_config.set_runtime_overrides``) that persists
    across reruns and every section, so a model picked here applies in the Research Assistant.
    """
    app_ui.section_header("AI assistant", "conversation / planning / explanation — never the chemistry")
    # Default the picker to the env/secret/default model, ignoring any prior UI override.
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
    effective_model = custom or picked
    ai_config.set_runtime_overrides(provider=provider, model=effective_model)
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


def _short_path(path) -> str:
    """A compact tail of a long path (display only)."""
    if not path:
        return ""
    s = str(path)
    return s if len(s) <= 42 else "…" + s[-40:]


def _render_future_architecture() -> None:
    app_ui.section_header("Future engine architecture",
                          "how more engines plug in — designed for, not yet built")
    st.markdown(
        "The assistant is built as a **plugin engine registry**: each domain maps to an engine "
        "(today, only `leaching_geochemistry → PHREEQC`). New engines slot in behind the same "
        "policy gate + confirmation flow without changing the conversation:\n\n"
        "- **LangGraph-style stateful orchestrator** — the current propose → policy-gate → confirm "
        "→ deterministic-tool loop is already a state machine (`AgentState` + a discrete action "
        "vocabulary), so it can be re-expressed as a graph without changing the safety model.\n"
        "- **Literature / RAG agent** — sourced benchmarks (the quarantined literature layer is the seam).\n"
        "- **ML / surrogate agent** — fast approximations of a simulator (the experimental surrogate is the seam).\n"
        "- **Simulation-engine agents** — atomistic / mechanical-property / thermal engines, registered per domain.\n"
        "- **Validation agent** — measured-vs-model comparison (the existing result path).\n\n"
        "No LangGraph dependency is added yet — the architecture is kept *compatible* with it. "
        "See `docs/ai_architecture.md`.")


def _render_engine_settings(selected_run: str | None) -> None:
    app_ui.render_page_header(
        "Engine Settings",
        "Which engines run today, which are planning-only, and how the AI assistant is configured.",
        eyebrow="Engines · AI · roadmap")

    app_ui.section_header("Engines & capabilities")
    app_ui.render_engine_cards(domains.engine_status())
    st.caption("Simulation outputs are model estimates — **not validation**. Validation needs "
               "measured data (Data & Validation).")

    st.divider()
    _render_phreeqc_engine()
    st.divider()
    _render_ai_settings()
    st.divider()
    _render_future_architecture()


# The app dispatches to ``render``.
render = _render_engine_settings
