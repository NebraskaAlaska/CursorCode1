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
from flyash_phreeqc_ml.ai import client as ai_client
from flyash_phreeqc_ml.ai import config as ai_config
from flyash_phreeqc_ml.simulation import phreeqc_executor
from ui import state


def _short_path(path) -> str:
    if not path:
        return ""
    s = str(path)
    return s if len(s) <= 42 else "…" + s[-40:]


def _render_ai_settings() -> None:
    """AI provider/model selector + status + the master **Enable live AI assistant** toggle.

    Never shows the key. Sets a process-level runtime override (``ai_config.set_runtime_overrides``)
    that persists across reruns; the toggle writes a session flag (``state.LIVE_AI_KEY``) the
    Assistant reads. Live AI runs only when both the configuration is *capable* (key + SDK) and the
    user turns the toggle on (``ai_config.live_ai_active``)."""
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

    # Capability (key + SDK) vs. the user's master switch. ``cfg.enabled`` is exactly
    # "key present AND SDK present"; the toggle is only operable when that holds.
    #
    # The choice is persisted in a PLAIN session key (``state.LIVE_AI_KEY``) that the Assistant
    # reads. It must NOT be the toggle's own widget key: a widget-keyed value is dropped by
    # Streamlit the moment the widget stops rendering (i.e. as soon as you leave Settings), which
    # would silently reset live AI on the next rerun. So the toggle uses its own widget key and
    # syncs into the plain key on change; the plain key survives navigation + prompt submission.
    st.session_state.setdefault(state.LIVE_AI_KEY, False)
    if state.LIVE_AI_WIDGET_KEY not in st.session_state:        # re-seed after widget GC
        st.session_state[state.LIVE_AI_WIDGET_KEY] = bool(st.session_state[state.LIVE_AI_KEY])
    toggle_on = bool(st.session_state[state.LIVE_AI_KEY])
    # The ONE shared live-AI status (same helper the Assistant reads → they can never disagree).
    ai_status = ai_config.live_ai_status(cfg, toggle_on)
    live_on = ai_status.active

    # Status card: API key · AI SDK · Live AI · provider/model (all key-free).
    app_ui.render_metric_cards([
        {"label": "API key", "value": "Detected" if cfg.key_present else "Missing",
         "caption": (cfg.key_source if cfg.key_present else f"set {ai_config.API_KEY_ENV}"),
         "status": "success" if cfg.key_present else "neutral"},
        {"label": "AI SDK", "value": "Available" if cfg.sdk_available else "Missing",
         "caption": ("anthropic" if cfg.sdk_available else "pip install anthropic"),
         "status": "success" if cfg.sdk_available else "neutral"},
        {"label": "Live AI", "value": "Enabled" if live_on else "Disabled",
         "status": "success" if live_on else "neutral"},
        {"label": "Model", "value": cfg.model, "caption": cfg.provider},
    ])

    # The master enable switch — operable only when a key + the SDK are present. ``on_change``
    # mirrors the widget into the persistent plain key so the choice survives navigation/reruns.
    def _sync_live_ai() -> None:
        st.session_state[state.LIVE_AI_KEY] = bool(
            st.session_state.get(state.LIVE_AI_WIDGET_KEY, False))

    st.toggle("Enable live AI assistant", key=state.LIVE_AI_WIDGET_KEY, on_change=_sync_live_ai,
              disabled=not cfg.enabled,
              help="When on, the assistant sends your conversation to the API to phrase and plan "
                   "the next step. It never runs PHREEQC, saves, or touches the science without "
                   "your explicit confirmation.")
    if not cfg.enabled:
        st.caption(f"⚪ Can't enable live AI yet — {cfg.disabled_reason()}. The assistant still "
                   "works fully with the deterministic planner (it asks, plans, previews, and "
                   "runs on your confirmation).")
    elif live_on:
        st.success("🟢 Live AI is **on** for the assistant — phrasing, planning, and explanation "
                   "only. It never runs PHREEQC or saves anything without your confirmation, and "
                   "never affects mapping / residuals / validation.")
    else:
        st.caption("⚪ Live AI is **off** — the assistant uses the deterministic planner. Turn the "
                   "toggle on to use AI phrasing/planning (this sends conversation data to the API "
                   "for the assistant only — data leaves this machine).")

    st.caption(f"Role: {ai_config.AI_ROLE_LINE}.")
    st.caption("The API key is read only from the `ANTHROPIC_API_KEY` environment variable or a "
               "Streamlit secret — it is never entered or shown here.")
    st.warning(ai_config.AI_EXPERIMENTAL_WARNING)

    _render_ai_diagnostics(cfg, toggle_on)


def _render_ai_diagnostics(cfg, toggle_on: bool) -> None:
    """Safe diagnostics + a one-click live smoke test (no key, no raw response ever shown).

    Helps debug 'live AI is unavailable' without exposing secrets: it reports key *presence* +
    *length* (never the key), SDK availability, the selected model, and runs a harmless one-line
    prompt through the **same** client the assistant uses, surfacing only a sanitized category."""
    with app_ui.advanced_expander("AI diagnostics (safe — no key shown)"):
        diag = ai_config.diagnostics(toggle_on=toggle_on)
        st.write(diag.to_safe_dict())
        st.caption("All fields are non-secret. `key_length` confirms a key is present (≈100 chars) "
                   "without revealing any of it.")
        st.markdown("**Live AI smoke test** — send a harmless one-sentence prompt through the same "
                    "client the assistant uses. It never logs the key or stores the response.")
        if st.button("Run live AI smoke test", key="ai_smoke_test", disabled=not cfg.enabled,
                     help=None if cfg.enabled else f"Needs a key + SDK — {cfg.disabled_reason()}."):
            with st.spinner("Sending a one-sentence test prompt…"):
                res = ai_client.smoke_test()
            if res.ok:
                st.success(f"✅ Live AI reachable (model `{res.model}`) — key, SDK, and network all "
                           "work. The assistant can use live AI.")
            else:
                st.error(f"❌ Live AI call failed — **{res.category}**: {res.message} "
                         f"(model `{res.model}`). The assistant falls back to the deterministic "
                         "planner; the toggle stays on.")


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
    st.caption(phreeqc_executor.availability_hint(av))
    st.caption("Configure by pointing the app at a PHREEQC CLI you supply: set `PHREEQC_EXE` and "
               "`PHREEQC_DATABASE` (CEMDATA18 is not redistributable — user-supplied). The "
               "assistant still **plans** and builds reviewable input without it.")
    st.caption("**Hosted deployment:** to run PHREEQC server-side so colleagues need only a "
               "browser, see `docs/deployment.md` — a Docker image with PHREEQC built in, where "
               "`PHREEQC_EXE` / `PHREEQC_DATABASE` / `ANTHROPIC_API_KEY` are server-side environment "
               "variables / secrets (never entered or shown in the browser).")


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
