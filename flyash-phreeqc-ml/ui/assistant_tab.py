"""Research Assistant — the main workspace of the Materials Research Assistant.

A clean, conversational, two-column page: the chat (history + input + actions) on the left, a
card-based status side-panel on the right (experiment summary · domain / engine · missing
details · current assumptions · next recommended action). Technical detail (scenario JSON,
policy decision, generated PHREEQC input, database report, release model, raw result table,
provenance trace) stays **hidden under expanders** so the default view is simple.

UI only: every decision/calculation happens in the agent layer (which calls the existing
deterministic Simulate backend). It never runs PHREEQC itself, never writes input, and never
affects mapping/residuals/validation. AI is opt-in + consent-gated; with no key the
deterministic planner drives the same workflow. For domains without an executable engine it
gives planning + data-template support — it never pretends to simulate.
"""
from __future__ import annotations

import streamlit as st

import app_ui
from flyash_phreeqc_ml import config, run_manager
from flyash_phreeqc_ml.agent import agent_orchestrator as orch
from flyash_phreeqc_ml.agent import agent_state, domains, nlu_extractor
from flyash_phreeqc_ml.ai import config as ai_config
from flyash_phreeqc_ml.materials import profile_schema as mp
from flyash_phreeqc_ml.ml_models import model_registry as ml_registry
from flyash_phreeqc_ml.simulation import phreeqc_executor, source_terms
from flyash_phreeqc_ml.simulation import scenario_schema as S

from .common import _render_next_step

_RELEASE_NONE = "No release (assay stays comment-only)"
_RELEASE_GLOBAL = "Global % release (an assumption)"

# Short example chips that seed the conversation (label shown → message sent).
EXAMPLE_CHIPS = [
    ("Estimate leaching pH and calcium release",
     "Estimate pH and calcium release from a Class C fly ash NaOH leaching experiment"),
    ("Plan a fly ash + waste plastic strength test",
     "I am mixing fly ash with waste plastic and I want to predict compressive strength after 28 days"),
    ("Compare ICP data with model predictions",
     "I want to compare measured ICP data with the model predictions"),
    ("Create an experiment data template",
     "Create a data template for a new materials experiment"),
    ("Find conditions for a target pH",
     "Find leaching conditions that reach a target pH around 12"),
]

# The assistant flow in plain language (compact, not a technical diagram).
ASSISTANT_FLOW = [
    "Understand the experiment", "Ask for missing details", "Choose a supported engine",
    "Build a reviewed plan", "Ask before running", "Explain the results",
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


def _ml_model_available(run: str | None) -> bool:
    """True if a trained ML surrogate (mechanical-property) model exists for this run.

    Read-only + defensive — used so the assistant can *offer* the surrogate. It never trains,
    runs, or produces a number; that all lives in the Prediction Models section."""
    if not run:
        return False
    try:                                                # path is side-effect-free (no mkdir here)
        base = run_manager.run_outputs_dir(run) / "model_registry"
        return ml_registry.has_strength_model(base)
    except Exception:                                   # noqa: BLE001 - availability check, never crash
        return False


def _send(state, message, *, run, consent, cfg) -> None:
    """Send a message to the assistant (chip or chat input) and rerun."""
    council_on = bool(st.session_state.get("asst_council", True))
    orch.respond(state, message, use_ai=bool(consent and cfg.enabled), council=council_on,
                 material_profile=_material_profile(run), release_model=_release_model(run),
                 database_path=config.PHREEQC_DATABASE_PATH,
                 ml_model_available=_ml_model_available(run))
    st.rerun()


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def _render_assistant_tab(selected_run: str | None, dev_mode: bool = False) -> None:
    app_ui.render_page_header(
        "Materials Research Assistant",
        "Describe a materials experiment. I'll ask missing details, identify the right modelling "
        "pathway, run available simulations after confirmation, and help compare predictions with "
        "measured data.",
        eyebrow="Describe → clarify → plan → confirm → run → explain → validate")
    _render_next_step(selected_run)

    cfg = ai_config.resolve_config()
    consent = _render_ai_status(cfg)
    st.checkbox("🧑‍🔬 Council review (advisory panel of five research advisors)", value=True,
                key="asst_council",
                help="Five advisor roles review each step — advisory only; it never runs anything "
                     "and never decides the action. Uses AI when enabled + consented, else a "
                     "deterministic review.")
    state = _get_state(selected_run)

    # Two-column workspace: chat on the left, status cards on the right.
    col_main, col_side = st.columns([2, 1], gap="large")

    with col_main:
        _render_examples(state, run=selected_run, consent=consent, cfg=cfg)
        _render_chat_history(state)
        _render_understanding(selected_run, state)
        _render_council(state)
        _render_pending_and_confirm(selected_run, state, consent, cfg)
        _render_planning_support(selected_run, state, consent, cfg)
        # Technical detail stays hidden by default (expanders).
        _render_context_providers(selected_run, state)
        _render_capabilities_and_flow()
        _render_advanced(state, dev_mode)

    with col_side:
        _render_side_panel(state)

    # The chat input docks to the bottom of the page (outside the columns by design).
    prompt = st.chat_input("Describe your experiment, answer a question, or say what to do next…")
    if prompt:
        _send(state, prompt, run=selected_run, consent=consent, cfg=cfg)


# --------------------------------------------------------------------------- #
# AI status + consent
# --------------------------------------------------------------------------- #
def _render_ai_status(cfg) -> bool:
    if not cfg.enabled:
        st.caption("⚪ Deterministic assistant mode (no AI key) — still asks for missing details, "
                   "plans, builds previews, and runs on your confirmation. Configure AI in "
                   "**Settings**.")
        return False
    return st.checkbox(f"🟢 Use AI to phrase + plan the conversation (model `{cfg.model}`)",
                       key="asst_consent", help=orch.AGENT_DATA_NOTICE)


# --------------------------------------------------------------------------- #
# Side panel (cards)
# --------------------------------------------------------------------------- #
def _render_side_panel(state) -> None:
    flat = state.scenario.to_flat_dict()
    dc = state.domain_card()

    # A. Current experiment
    with st.container(border=True):
        app_ui.section_header("Current experiment")
        if dc["domain"] == domains.UNKNOWN and not any(
                flat.get(k) for k in ("material_name", "leachant_type", "solid_mass_g")):
            st.caption("Nothing captured yet — describe your experiment to begin.")
        else:
            material = flat.get("material_name") or "—"
            goal = (", ".join(flat.get("target_elements") or [])
                    or ", ".join(flat.get("desired_outputs") or []) or "—")
            st.markdown(f"- **Domain:** {dc['domain_label']}")
            st.markdown(f"- **Material system:** {material}")
            st.markdown(f"- **Goal:** {goal}")
            st.markdown(f"- **Status:** {_status_label(state)}")

    # B. Engine status
    with st.container(border=True):
        app_ui.section_header("Engine status")
        if dc["executable"]:
            app_ui.render_status_badge("PHREEQC engine available", "success")
        else:
            app_ui.render_status_badge("planning only — no engine yet", "warning")
        st.caption("✅ Available: PHREEQC — leaching / geochemistry")
        st.caption("🧪 Planning: composites · mechanical · thermal · cementitious · battery / corrosion")
        st.caption("🔮 Future: literature RAG · ML surrogate · atomistic · mechanical-property")

    # C. Missing details
    with st.container(border=True):
        app_ui.section_header("Missing details")
        missing = state.missing_card()
        if not missing:
            st.caption("✅ All core fields are present.")
        else:
            for m in missing:
                st.markdown(f"- **{m['label']}** ({m['severity']})")
        if state.assumptions:
            st.caption("Assumptions: " + "; ".join(f"{a.field}" for a in state.assumptions))

    # D. Recommended next action
    with st.container(border=True):
        app_ui.section_header("Recommended next action")
        st.markdown(_next_action_hint(state))


def _status_label(state) -> str:
    if state.has_results:
        return "results ready"
    if state.confirmation_required:
        return "awaiting your confirmation"
    if state.preview is not None:
        return "input preview built"
    if not domains.is_executable(state.domain) and state.domain != domains.UNKNOWN:
        return "planning only"
    if state.missing_fields:
        return "collecting details"
    return "ready to plan"


def _next_action_hint(state) -> str:
    if state.confirmation_required and state.pending_action is not None:
        return "✅ Confirm in the chat to run, or ✋ change something."
    if not domains.is_executable(state.domain) and state.domain != domains.UNKNOWN:
        return "🧪 Planning-only domain — I can structure the plan or build a data template."
    if state.missing_fields:
        return "💬 Answer the question in the chat to complete the set-up."
    if state.domain == domains.LEACHING_GEOCHEMISTRY and not state.composition_usable:
        return "🧱 Provide a confirmed material composition (Advanced details) for a meaningful run."
    if state.preview is not None and state.has_results:
        return "📊 Ask me to *explain the results*, then compare with measured data."
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
# Examples / prompt chips
# --------------------------------------------------------------------------- #
def _render_examples(state, *, run, consent, cfg) -> None:
    if state.history:                       # only show the starter chips before the chat begins
        return
    st.markdown("**What can I help with?** Try one of these, or just type below:")
    for i, (label, message) in enumerate(EXAMPLE_CHIPS):
        if st.button(label, key=f"asst_chip_{i}", use_container_width=True):
            _send(state, message, run=run, consent=consent, cfg=cfg)


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


# --------------------------------------------------------------------------- #
# "I understood this as…" card + correction affordance
# --------------------------------------------------------------------------- #
# Fields the inline corrector exposes (key, label, kind). Blank = leave unchanged.
_CORRECTABLE = [
    ("material_name", "Material", "text"),
    ("leachant_type", "Leachant", "text"),
    ("leachant_concentration_M", "Concentration (M)", "num"),
    ("solid_mass_g", "Solid mass (g)", "num"),
    ("liquid_volume_mL", "Liquid volume (mL)", "num"),
    ("time_min", "Time (min)", "num"),
    ("temperature_C", "Temperature (°C)", "num"),
]


def _render_understanding(run, state) -> None:
    """The compact 'I understood this as…' card + an inline corrector (no advanced-tab hunting).

    Renders from ``state.last_understanding`` (a plain dict built by the agent). It works whether
    AI is on or off (it shows the rule-based reading too), so the user always sees what was
    captured and can fix any field right here.
    """
    card = state.last_understanding
    if not card:
        return
    # Only show once something meaningful has been captured.
    if (card.get("domain") == domains.UNKNOWN and card.get("material") in (None, "—")
            and not card.get("key_variables") and not card.get("target_elements")):
        return

    with st.container(border=True):
        app_ui.section_header("🧠 I understood this as")
        conf = card.get("confidence")
        meta = card.get("source_label", "")
        if isinstance(conf, (int, float)) and conf:
            meta += f" · confidence {conf:.0%}"
        st.caption(meta)

        c1, c2 = st.columns(2)
        with c1:
            engine = " · ✅ PHREEQC engine" if card.get("executable") else " · 🧪 planning only"
            st.markdown(f"**Domain:** {card.get('domain_label', '—')}{engine}")
            st.markdown(f"**Material:** {card.get('material', '—')}")
            st.markdown(f"**Leachant:** {card.get('leachant', '—')}")
        with c2:
            kv = card.get("key_variables") or []
            st.markdown("**Conditions:** " + ("; ".join(kv) if kv else "—"))
            te = card.get("target_elements") or []
            st.markdown("**Target elements:** " + (", ".join(te) if te else "—"))
            do = card.get("desired_outputs") or []
            if do:
                st.markdown("**Desired outputs:** " + ", ".join(do))

        for ch in card.get("changes") or []:
            st.caption(f"✏️ updated {ch['label']}: {ch['old']} → {ch['new']}")
        if card.get("normalizations"):
            st.caption("Interpreted: " + "; ".join(str(n) for n in card["normalizations"][:4]))
        for a in card.get("assumptions") or []:
            st.caption(f"🟡 assumption to confirm — **{a['field']}** = {a.get('value')} "
                       f"({a.get('reason')})")
        for rj in card.get("rejected") or []:
            st.caption(f"⚠️ couldn't use **{rj['field']}**: {rj['reason']}")
        if card.get("missing"):
            st.caption("Still needed: " + ", ".join(card["missing"]))
        if card.get("ambiguous"):
            st.caption("Please clarify: " + ", ".join(card["ambiguous"]))

        st.caption("If anything's off, fix it below — you won't lose the rest.")
        _render_correction_editor(run, state)


def _build_correction_delta(flat, raw, target_elements) -> dict:
    """Build a delta from the corrector's text/number inputs (blank = unchanged; reject negatives)."""
    delta: dict = {}
    for key, _label, kind in _CORRECTABLE:
        text = (raw.get(key) or "").strip()
        if text == "":
            continue
        if kind == "num":
            val = S.as_float(text)
            if val is None or val < 0:
                continue
            if val != flat.get(key):
                delta[key] = val
        else:
            if key == "leachant_type":
                canon, _amb = nlu_extractor.canonical_leachant(text)
                val = canon or text
            else:
                val = text
            if val != flat.get(key):
                delta[key] = val
    if set(target_elements) != set(flat.get("target_elements") or []):
        delta["target_elements"] = list(target_elements)
    return delta


def _render_correction_editor(run, state) -> None:
    flat = state.scenario.to_flat_dict()
    with st.expander("✏️ Edit what I understood  ·  ✋ That's not right", expanded=False):
        st.caption("Correct any field directly — blanks are left unchanged. This updates the "
                   "experiment set-up; nothing runs.")
        with st.form(f"asst_correct__{run or '_none_'}"):
            cols = st.columns(2)
            raw: dict = {}
            for i, (key, label, _kind) in enumerate(_CORRECTABLE):
                cur = flat.get(key)
                raw[key] = cols[i % 2].text_input(
                    label, value=("" if cur is None else str(cur)),
                    key=f"asst_corr_{key}_{run or '_none_'}")
            target_elements = st.multiselect(
                "Target elements", list(S.RECOGNIZED_ELEMENTS),
                default=list(flat.get("target_elements") or []),
                key=f"asst_corr_te_{run or '_none_'}")
            submitted = st.form_submit_button("Apply corrections")
        if submitted:
            delta = _build_correction_delta(flat, raw, target_elements)
            if delta:
                orch.apply_correction(state, delta)
                st.rerun()
            else:
                st.info("No changes detected.")


# --------------------------------------------------------------------------- #
# Council Review card (advisory panel; synthesis up top, roles under an expander)
# --------------------------------------------------------------------------- #
def _render_council(state) -> None:
    """Render the advisory Council Review: synthesized recommendation up top, the five role
    perspectives hidden under 'Show council reasoning'. No raw LLM JSON, never an action."""
    review = state.last_council
    if review is None:
        return
    with st.container(border=True):
        app_ui.section_header("🧑‍🔬 Council Review")
        st.caption("An advisory panel of five research advisors — it reviews the plan but never "
                   "runs anything and never decides the next action.")
        if getattr(review, "note", ""):
            st.caption(f"ℹ️ {review.note}")

        st.markdown(f"**What we understood:** {review.understood_scenario or '—'}")
        st.markdown(f"**Domain / engine:** {review.likely_domain} — "
                    f"{review.executable_engine_status}")
        if review.planning_or_execution_status:
            st.caption(review.planning_or_execution_status)

        if review.scientific_warnings:
            st.markdown("**Scientific concerns:**")
            for w in review.scientific_warnings[:6]:
                st.caption(f"⚠️ {w}")
        if review.unsupported_elements:
            st.caption("Out-of-scope elements (not handled by the current engine): "
                       + ", ".join(review.unsupported_elements))
        if review.pretreatment_temperature_C is not None:
            st.caption(f"Thermal pretreatment ~{review.pretreatment_temperature_C:g} °C is "
                       "planning-only (not the leach temperature, not simulated).")
        if review.key_missing_details:
            st.markdown("**Missing information:** " + ", ".join(review.key_missing_details))
        if review.recommended_next_user_question:
            st.markdown(f"**Recommended next step:** {review.recommended_next_user_question}")

        with app_ui.advanced_expander("Show council reasoning"):
            for r in review.roles:
                st.markdown(f"**{r.role_name}** · confidence {float(r.confidence or 0):.0%}")
                if r.short_assessment:
                    st.caption(r.short_assessment)
                if r.concerns:
                    st.caption("Concerns: " + "; ".join(r.concerns))
                if r.missing_information:
                    st.caption("Missing: " + ", ".join(r.missing_information))
                if r.recommended_next_action:
                    st.caption("→ " + r.recommended_next_action)
                if r.blocking_issues:
                    st.caption("⛔ " + "; ".join(r.blocking_issues))


def _render_pending_and_confirm(run, state, consent, cfg) -> None:
    if not (state.confirmation_required and state.pending_action is not None):
        return
    app_ui.render_warning_panel(
        "Confirmation required",
        f"Action **{state.pending_action.action_name}** is ready but will not run until you "
        "confirm. Nothing has executed.", level="warning")
    col1, col2 = st.columns(2)
    if col1.button("✅ Yes, run it", key="asst_confirm_yes", use_container_width=True,
                   type="primary"):
        orch.confirm_pending_action(state)
        st.rerun()
    if col2.button("✋ No, change something", key="asst_confirm_no", use_container_width=True):
        orch.reject_pending_action(state)
        st.rerun()


# --------------------------------------------------------------------------- #
# Planning support (planning-only domains — useful next actions, not a dead-end)
# --------------------------------------------------------------------------- #
def _render_planning_support(run, state, consent, cfg) -> None:
    if domains.is_executable(state.domain) or state.domain == domains.UNKNOWN:
        return
    support = domains.planning_support(state.domain)
    with st.container(border=True):
        app_ui.section_header(f"Planning support · {support['domain_label']}")
        st.caption("No executable simulation engine for this domain yet — but I can help you "
                   "structure the experiment and build a dataset. (Planning + data, not a "
                   "prediction.)")
        st.markdown("**Suggested response variables:** " + ", ".join(support["response_variables"]))
        st.markdown("**Inputs a future model would need:** " + ", ".join(support["input_variables"]))
        st.caption(f"Future engine path: {support['future_engine']}.")

        b1, b2, b3 = st.columns(3)
        if b1.button("📝 Structure the plan", key="asst_plan_btn", use_container_width=True):
            _send(state, "Please structure the experiment plan for this.", run=run,
                  consent=consent, cfg=cfg)
        if b2.button("📋 Build data template", key="asst_tmpl_btn", use_container_width=True):
            _send(state, "Please create a data template for this experiment.", run=run,
                  consent=consent, cfg=cfg)
        if b3.button("🔎 Missing variables", key="asst_miss_btn", use_container_width=True):
            _send(state, "What variables am I missing for this experiment?", run=run,
                  consent=consent, cfg=cfg)

        cols, _labels = domains.data_template_columns(state.domain)
        st.download_button(
            "⬇️ Download data template (CSV header)", data=",".join(cols) + "\n",
            file_name=f"{state.domain}_data_template.csv", mime="text/csv",
            key="asst_tmpl_dl", use_container_width=True)


# --------------------------------------------------------------------------- #
# Context providers (composition / release / database) — under an expander
# --------------------------------------------------------------------------- #
def _render_context_providers(run: str | None, state) -> None:
    needs = state.domain == domains.LEACHING_GEOCHEMISTRY and not state.composition_usable
    with app_ui.advanced_expander("Material composition, release model & database",
                                  expanded=needs):
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
                    f"— {av.message} (configure in **Settings**).")


# --------------------------------------------------------------------------- #
# Capabilities (engine status) + the assistant flow — under expanders
# --------------------------------------------------------------------------- #
def _render_capabilities_and_flow() -> None:
    with app_ui.advanced_expander("How the assistant works"):
        st.markdown(" → ".join(f"**{i+1}. {s}**" for i, s in enumerate(ASSISTANT_FLOW)))
        st.caption("The AI handles conversation, clarification, planning, and explanation. Every "
                   "scientific calculation and the PHREEQC run are deterministic and happen only "
                   "after you confirm — the AI never invents chemistry. The full engine roadmap "
                   "is in **Engine Library**.")


# --------------------------------------------------------------------------- #
# Advanced details (hidden complexity)
# --------------------------------------------------------------------------- #
def _render_advanced(state, dev_mode: bool) -> None:
    with app_ui.advanced_expander("Advanced details (scenario, policy, preview, results, provenance)"):
        st.markdown("**Extracted scenario (JSON)**")
        st.json(state.scenario.to_flat_dict())

        st.markdown(f"**Domain classification:** `{state.domain}` "
                    f"(executable: {domains.is_executable(state.domain)}, "
                    f"engine: {state.engine or '—'})")

        if state.provenance:
            last = state.provenance[-1]
            st.markdown(f"**Policy decision (last):** `{last.action_name}` → `{last.policy_code}`"
                        + (f" — {last.policy_reason}" if last.policy_reason else ""))

        rm = state.release_model
        if rm is not None:
            st.markdown(f"**Material release model:** `{getattr(rm, 'mode', '—')}`"
                        + (f" (global {getattr(rm, 'global_fraction', None)})"
                           if getattr(rm, 'global_fraction', None) is not None else ""))

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
            st.markdown("**Raw result table** — *model estimates, not validated.*")
            st.dataframe(state.result_table, use_container_width=True, height=240)

        if state.last_explanation:
            st.markdown("**Result explanation (grounded — numbers from the tools)**")
            st.json(state.last_explanation)

        if state.provenance:
            st.markdown("**Provenance / action trace**")
            st.dataframe(
                [{"action": e.action_name, "policy": e.policy_code,
                  "confirm_required": e.confirmation_required, "confirmed": e.confirmed,
                  "tool": e.tool_called, "status": e.result_status}
                 for e in state.provenance], use_container_width=True, height=200)


# The app dispatches to ``render``.
render = _render_assistant_tab
