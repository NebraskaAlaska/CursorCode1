"""Assistant tab — the Materials Research Assistant homepage (the simple way in).

A chat with the materials research assistant (the agent layer in ``flyash_phreeqc_ml.agent``).
Describe an experiment in plain language; the assistant asks for the missing details, picks the
right modelling pathway, runs available simulations **only after you confirm**, and helps
compare predictions with measured data. For domains without an executable engine yet it gives
**planning + data-template** support — it never pretends to simulate.

This module is **UI only**: every decision/calculation happens in the agent layer (which calls
the existing deterministic Simulate backend). It never runs PHREEQC itself, never writes input,
and never affects mapping/residuals/validation. AI is opt-in + consent-gated; with no key the
deterministic planner drives the same workflow. Technical detail is tucked under expanders.
"""
from __future__ import annotations

import streamlit as st

import app_ui
from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.agent import agent_orchestrator as orch
from flyash_phreeqc_ml.agent import agent_state, domains
from flyash_phreeqc_ml.ai import config as ai_config
from flyash_phreeqc_ml.materials import profile_schema as mp
from flyash_phreeqc_ml.simulation import phreeqc_executor, source_terms

from .common import _render_next_step

_RELEASE_NONE = "No release (assay stays comment-only)"
_RELEASE_GLOBAL = "Global % release (an assumption)"

# Short example chips that seed the conversation (section 2).
EXAMPLE_CHIPS = [
    "Estimate pH and calcium release from a Class C fly ash NaOH leaching experiment",
    "Plan a fly ash + waste plastic composite compressive-strength test",
    "Compare measured ICP data with PHREEQC predictions",
    "Create a data template for a new materials experiment",
]

# The assistant flow in plain language (section 6 — compact, not a technical diagram).
ASSISTANT_FLOW = [
    "Understand the experiment",
    "Ask for missing details",
    "Choose a supported engine",
    "Build a reviewed plan",
    "Ask before running",
    "Explain the results",
    "Recommend validation data",
]


# --------------------------------------------------------------------------- #
# Per-run session keys
# --------------------------------------------------------------------------- #
def _state_key(run: str | None) -> str:
    return f"asst_state__{run or '_none_'}"


def _get_state(run: str | None) -> agent_state.AgentState:
    key = _state_key(run)
    if key not in st.session_state or not isinstance(st.session_state[key], agent_state.AgentState):
        st.session_state[key] = agent_state.AgentState()
    return st.session_state[key]


def _material_profile(run: str | None):
    return st.session_state.get(f"asst_mp__{run or '_none_'}")


def _release_model(run: str | None):
    return st.session_state.get(f"asst_release__{run or '_none_'}")


def _send(state, message, *, run, consent, cfg) -> None:
    """Send a message to the assistant (chip or chat input) and rerun."""
    orch.respond(state, message, use_ai=bool(consent and cfg.enabled),
                 material_profile=_material_profile(run), release_model=_release_model(run),
                 database_path=config.PHREEQC_DATABASE_PATH)
    st.rerun()


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def _render_assistant_tab(selected_run: str | None, dev_mode: bool = False) -> None:
    app_ui.render_page_header(
        "Materials Research Assistant",
        "Describe a materials experiment in plain language. I'll ask for what's missing, pick "
        "the right modelling pathway, run available simulations after you confirm, and help "
        "compare predictions with measured data.",
        eyebrow="The simple way in · technical controls live in the Advanced Mode tabs")
    _render_next_step(selected_run)

    cfg = ai_config.resolve_config()
    consent = _render_ai_status(cfg)
    state = _get_state(selected_run)

    _render_examples(state, run=selected_run, consent=consent, cfg=cfg)
    _render_chat_history(state)
    _render_pending_and_confirm(selected_run, state, consent, cfg)

    prompt = st.chat_input("Describe your experiment, answer a question, or say what to do next…")
    if prompt:
        _send(state, prompt, run=selected_run, consent=consent, cfg=cfg)

    # The live status cards (section 2): summary · domain/engine · missing · next action.
    _render_cards(state)
    # Planning-only domains get useful next actions instead of a dead-end (section 4).
    _render_planning_support(selected_run, state, consent, cfg)

    # Context the assistant needs (composition / release / database).
    _render_context_providers(selected_run, state)

    # What this assistant can do + how it works (sections 5 + 6).
    _render_capabilities_and_flow()

    # Everything technical, tucked away (section 7).
    _render_advanced(state, dev_mode)


# --------------------------------------------------------------------------- #
# AI status + consent
# --------------------------------------------------------------------------- #
def _render_ai_status(cfg) -> bool:
    if not cfg.enabled:
        st.info("Running in **deterministic assistant mode** (no AI key detected). The assistant "
                "still asks for missing details, plans, builds previews, and runs simulations on "
                "your confirmation — just with rule-based phrasing. " + (cfg.disabled_reason() or ""))
        return False
    st.caption(f"AI assistant available (model `{cfg.model}`). Tick consent to let it phrase + "
               "plan the conversation; the chemistry stays deterministic either way.")
    return st.checkbox(orch.AGENT_CONSENT_LABEL, key="asst_consent",
                       help=orch.AGENT_DATA_NOTICE)


# --------------------------------------------------------------------------- #
# Examples / prompt chips
# --------------------------------------------------------------------------- #
def _render_examples(state, *, run, consent, cfg) -> None:
    if state.history:                       # only show the starter chips before the chat begins
        return
    st.markdown("**What can I help with?** Try one of these, or just type below:")
    cols = st.columns(2)
    for i, chip in enumerate(EXAMPLE_CHIPS):
        if cols[i % 2].button(chip, key=f"asst_chip_{i}", use_container_width=True):
            _send(state, chip, run=run, consent=consent, cfg=cfg)


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
def _render_chat_history(state) -> None:
    if not state.history:
        with st.chat_message("assistant"):
            st.markdown("Hi! Tell me about your experiment — for example: *“I'm leaching Class C "
                        "fly ash with 0.5 M NaOH and want pH and calcium.”* I can also help plan "
                        "experiments in domains that don't have a simulation engine yet.")
        return
    for msg in state.history:
        with st.chat_message("user" if msg.role == agent_state.ROLE_USER else "assistant"):
            st.markdown(msg.content)
            if msg.reasoning_summary and msg.role == agent_state.ROLE_ASSISTANT:
                st.caption(f"🧭 {msg.reasoning_summary}")


def _render_pending_and_confirm(run, state, consent, cfg) -> None:
    if not (state.confirmation_required and state.pending_action is not None):
        return
    app_ui.render_warning_panel(
        "Confirmation required",
        f"Action **{state.pending_action.action_name}** is ready but will not run until you "
        "confirm. Nothing has executed.", level="warning")
    col1, col2 = st.columns(2)
    if col1.button("✅ Yes, run it", key="asst_confirm_yes", use_container_width=True):
        orch.confirm_pending_action(state)
        st.rerun()
    if col2.button("✋ No, change something", key="asst_confirm_no", use_container_width=True):
        orch.reject_pending_action(state)
        st.rerun()


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def _render_cards(state) -> None:
    c1, c2 = st.columns(2)
    with c1:
        app_ui.section_header("Experiment so far")
        summary = state.summary_card()
        st.table({"field": list(summary.keys()),
                  "value": [_fmt(v) for v in summary.values()]})
    with c2:
        app_ui.section_header("Domain & engine")
        dc = state.domain_card()
        app_ui.render_status_badge(
            f"{dc['domain_label']} — "
            + ("PHREEQC engine (runs after confirmation)" if dc["executable"]
               else "planning only — no executable engine yet"),
            "exact" if dc["executable"] else "scenario-level only")
        st.caption(domains.ENGINE_NOTE)

        app_ui.section_header("Still missing")
        missing = state.missing_card()
        if not missing:
            st.success("All core fields are present.")
        else:
            for m in missing:
                st.markdown(f"- **{m['label']}** ({m['severity']}) — {m['message']}")

        app_ui.section_header("Next action")
        st.markdown(_next_action_hint(state))


def _next_action_hint(state) -> str:
    if state.confirmation_required and state.pending_action is not None:
        return "✅ Confirm above to run, or ✋ change something."
    if not domains.is_executable(state.domain) and state.domain != domains.UNKNOWN:
        return "🧪 Planning-only domain — I can structure the plan or build a data template below."
    if state.missing_fields:
        return "💬 Answer the question above so I can complete the set-up."
    if state.domain == domains.LEACHING_GEOCHEMISTRY and not state.composition_usable:
        return "🧱 Provide a confirmed material composition (below) so a run is meaningful."
    if state.preview is not None and state.has_results:
        return "📊 Ask me to *explain the results*, then compare them with measured data."
    if state.preview is not None:
        return "▶️ Say *run it* — I'll ask you to confirm before anything executes."
    return "💬 Tell me about your experiment to get started."


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "—"
    return str(value)


# --------------------------------------------------------------------------- #
# Planning support (planning-only domains — useful next actions, not a dead-end)
# --------------------------------------------------------------------------- #
def _render_planning_support(run, state, consent, cfg) -> None:
    if domains.is_executable(state.domain) or state.domain == domains.UNKNOWN:
        return
    support = domains.planning_support(state.domain)
    app_ui.section_header(f"Planning support · {support['domain_label']}")
    st.caption("No executable simulation engine exists for this domain yet — but I can help you "
               "structure the experiment and build a dataset. (This is planning + data, not a "
               "prediction.)")
    st.markdown("**Suggested response variables to measure:** "
                + ", ".join(support["response_variables"]))
    st.markdown("**Inputs a future model would need:** " + ", ".join(support["input_variables"]))
    st.caption(f"Future engine path: {support['future_engine']}.")

    b1, b2, b3 = st.columns(3)
    if b1.button("📝 Structure the plan", key="asst_plan_btn", use_container_width=True):
        _send(state, "Please structure the experiment plan for this.", run=run,
              consent=consent, cfg=cfg)
    if b2.button("📋 Build data template", key="asst_tmpl_btn", use_container_width=True):
        _send(state, "Please create a data template for this experiment.", run=run,
              consent=consent, cfg=cfg)
    if b3.button("🔎 Identify missing variables", key="asst_miss_btn", use_container_width=True):
        _send(state, "What variables am I missing for this experiment?", run=run,
              consent=consent, cfg=cfg)

    cols, _labels = domains.data_template_columns(state.domain)
    st.download_button(
        "⬇️ Download data template (CSV header)", data=",".join(cols) + "\n",
        file_name=f"{state.domain}_data_template.csv", mime="text/csv",
        key="asst_tmpl_dl", use_container_width=True)


# --------------------------------------------------------------------------- #
# Context providers (composition / release / database)
# --------------------------------------------------------------------------- #
def _render_context_providers(run: str | None, state) -> None:
    with app_ui.advanced_expander("Material composition, release model & database",
                                  expanded=(state.domain == domains.LEACHING_GEOCHEMISTRY
                                            and not state.composition_usable)):
        st.markdown("**Material composition** — a confirmed composition is required before a "
                    "meaningful run. Composition is never invented.")
        basis = st.selectbox("Composition basis", list(mp.KNOWN_BASES),
                             format_func=lambda b: mp.BASIS_LABELS.get(b, b), key="asst_mp_basis")
        text = st.text_area("Paste `species value` lines (e.g. `CaO 24`, `SiO2 35`)",
                            key="asst_mp_text", height=110)
        name = st.text_input("Material name", value="Class C fly ash", key="asst_mp_name")
        confirm = st.checkbox("I confirm this composition is correct (makes it usable).",
                              key="asst_mp_confirm")
        if st.button("Save composition", key="asst_mp_save"):
            entries = mp.parse_composition_text(text)
            if not entries:
                st.warning("No `species value` lines parsed — nothing saved.")
            else:
                profile = mp.MaterialProfile(
                    profile_id=f"asst-{run or 'run'}", material_name=name or "material",
                    composition_basis=basis, entries=entries,
                    verification_status=(mp.STATUS_USER_CONFIRMED if confirm else mp.STATUS_DRAFT))
                st.session_state[f"asst_mp__{run or '_none_'}"] = profile
                st.success(f"Saved {len(entries)} component(s) — "
                           + ("usable." if profile.is_usable else "draft (tick confirm to use)."))
                st.rerun()
        mpf = _material_profile(run)
        if mpf is not None:
            st.caption(f"Current: {mpf.material_name} — "
                       + ("✅ usable" if mpf.is_usable else "⚠️ draft (not usable)"))

        st.divider()
        st.markdown("**Material release model** — release fractions are *your assumptions*, not "
                    "measured truth; they control the predicted dissolved totals.")
        mode = st.radio("Release model", [_RELEASE_NONE, _RELEASE_GLOBAL], key="asst_release_mode")
        if mode == _RELEASE_GLOBAL:
            pct = st.number_input("Global release (%)", min_value=0.0, max_value=100.0,
                                  value=1.0, step=0.5, key="asst_release_pct")
            st.session_state[f"asst_release__{run or '_none_'}"] = source_terms.global_release(
                pct / 100.0)
        else:
            st.session_state[f"asst_release__{run or '_none_'}"] = source_terms.no_release()

        st.divider()
        av = phreeqc_executor.check_availability()
        st.markdown(f"**PHREEQC engine:** {'✅ configured' if av.can_run else '⚠️ not configured'} "
                    f"— {av.message}")


# --------------------------------------------------------------------------- #
# Capabilities (engine status) + the assistant flow
# --------------------------------------------------------------------------- #
def _render_capabilities_and_flow() -> None:
    with app_ui.advanced_expander("What this assistant can do (engines & capabilities)",
                                  expanded=False):
        status = domains.engine_status()
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown("**✅ Available now**")
            for e in status["available_now"]:
                st.markdown(f"- {e['capability']} → **{e['engine'].upper()}**")
                st.caption(e["note"])
        with c2:
            st.markdown("**🧪 Planning support now**")
            for e in status["planning_now"]:
                st.markdown(f"- {e['capability']}")
            st.caption("Structured plans + data templates (no executable engine yet).")
        with c3:
            st.markdown("**🔮 Future (modular)**")
            for e in status["future"]:
                st.markdown(f"- {e}")

    with app_ui.advanced_expander("How the assistant works", expanded=False):
        st.markdown(" → ".join(f"**{i+1}. {s}**" for i, s in enumerate(ASSISTANT_FLOW)))
        st.caption("The AI handles conversation, clarification, planning, and explanation. "
                   "Every scientific calculation and the PHREEQC run are deterministic and "
                   "happen only after you confirm — the AI never invents chemistry.")


# --------------------------------------------------------------------------- #
# Advanced details (hidden complexity — section 7)
# --------------------------------------------------------------------------- #
def _render_advanced(state, dev_mode: bool) -> None:
    with app_ui.advanced_expander("Advanced details (scenario, domain, policy, preview, results)"):
        st.markdown("**Extracted scenario (JSON)**")
        st.json(state.scenario.to_flat_dict())

        st.markdown(f"**Domain classification:** `{state.domain}` "
                    f"(executable: {domains.is_executable(state.domain)}, "
                    f"engine: {state.engine or '—'})")

        if state.provenance:
            last = state.provenance[-1]
            st.markdown(f"**Last policy decision:** `{last.action_name}` → "
                        f"`{last.policy_code}`"
                        + (f" — {last.policy_reason}" if last.policy_reason else ""))

        if state.assumptions or state.warnings:
            st.markdown("**Assumptions & scientific caveats**")
            for a in state.assumptions:
                st.caption(f"• assumption — {a.field}: {a.reason}")
            for w in state.warnings:
                st.caption(f"⚠️ {w}")

        if state.preview is not None:
            st.markdown(f"**Generated PHREEQC input preview** (status: `{state.preview.status}`) — "
                        "deterministic draft; PHREEQC has not been run from this text unless you "
                        "confirmed a run.")
            st.code(state.preview.phreeqc_input_text, language="text")

        if state.database_report is not None:
            rep = state.database_report
            st.markdown(f"**Database / phases** — {getattr(rep, 'database_label', '—')} "
                        f"(family {getattr(rep, 'detected_family', '—')}, present "
                        f"{getattr(rep, 'database_exists', False)})")

        if state.has_result_table:
            st.markdown("**Result table** — *model estimates, not validated.*")
            st.dataframe(state.result_table, use_container_width=True, height=240)

        if state.last_explanation:
            st.markdown("**Result explanation (grounded — numbers from the tools)**")
            st.json(state.last_explanation)

        if state.provenance:
            st.markdown("**Action / provenance trace**")
            st.dataframe(
                [{"action": e.action_name, "policy": e.policy_code,
                  "confirm_required": e.confirmation_required, "confirmed": e.confirmed,
                  "tool": e.tool_called, "status": e.result_status}
                 for e in state.provenance], use_container_width=True, height=200)


# The app dispatches to ``render``.
render = _render_assistant_tab
