"""Agent **orchestrator** — the conversation loop (the only agent module that touches AI).

Per turn it: attaches any UI-provided context (material profile / release model / database),
appends the user message, **deterministically merges** the reply into the scenario, classifies
the domain, asks the LLM (or the deterministic planner when AI is off) for **one structured
action**, runs it through the **policy gate**, and then either runs the bound deterministic
tool, **parks** an execution/save action for explicit confirmation, or reports a block.

The hard safety invariants live here and in :mod:`agent_policy`:

* **The LLM only proposes.** It never executes a tool; deterministic code does, and only after
  the policy allows it. An execution/save action is *parked* — :func:`respond` never runs it.
* **Execution happens only via** :func:`confirm_pending_action` (the UI's explicit "Yes, run
  it" button, or an unambiguous affirmative reply to a parked action). The model cannot both
  propose and confirm in one turn.
* **AI never writes PHREEQC input** — the tool builds it deterministically from the scenario.
* **Numbers come from tools, not the model** — the assistant prose is led by the model, but the
  factual summary appended to it is the deterministic tool outcome.

AI is reached through the shared key-safe client (mirroring :mod:`ai.scenario_parser`); with
no key / SDK, the deterministic planner runs and the whole workflow still works.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import agent_actions as A
from . import agent_council, agent_policy, agent_state, domains, nlu_extractor, tool_registry

# Per-session consent notice (same spirit as the other AI features).
AGENT_DATA_NOTICE = (
    "The assistant sends your conversation (your experiment description + replies) and the "
    "current structured scenario to the Anthropic API to plan the next step — data leaves this "
    "machine for this feature only. It never sends measured data, API keys, or files; it only "
    "proposes the next action, and nothing runs or is saved without your explicit confirmation.")
AGENT_CONSENT_LABEL = (
    "I understand and allow sending my conversation to the API to plan the experiment.")

# Scenario fields whose change invalidates a previously-built input preview.
_PREVIEW_INVALIDATING = {
    "solid_mass_g", "liquid_volume_mL", "leachant_type", "leachant_concentration_M",
    "temperature_C", "time_min", "CO2_condition", "cover_condition", "target_elements",
}

# Affirmative / negative detection for replying to a *parked* action. The affirmative must be
# the WHOLE message (an affirmation, optionally trailing "run it" / "please"), so a longer
# instruction like "run the database check" is NOT read as a confirmation.
_AFFIRM_RE = re.compile(
    r"^\s*(yes|yep|yeah|yup|ok|okay|sure|go ahead|go|do it|run it|run|proceed|confirm|"
    r"please do|sounds good|👍)"
    r"(?:[\s,]+(?:please|go|ahead|it|and run it|run it|now))*[\s.!]*$", re.I)
_NEGATE_RE = re.compile(
    r"\b(no\b|nope\b|don'?t\b|do not\b|stop\b|wait\b|actually\b|instead\b|change\b|first\b|"
    r"hold on\b|cancel\b|nevermind\b|never mind\b)", re.I)
# A different intent expressed while a run is parked — must NOT be read as confirming the run.
_OTHER_INTENT_RE = re.compile(
    r"\b(database|check|build|preview|edit|explain|rank|sweep|target|matrix|import|validate|"
    r"compare|save|export|show|look|see|what|why|how|different|another|element|temperature|"
    r"concentration|time|mass|volume|material|composition)\b", re.I)


def _is_affirmation(message: str) -> bool:
    """True only for a short, unambiguous affirmative reply to a parked action."""
    return bool(_AFFIRM_RE.match(message) and not _NEGATE_RE.search(message)
                and not _OTHER_INTENT_RE.search(message))


# --------------------------------------------------------------------------- #
# Turn result
# --------------------------------------------------------------------------- #
@dataclass
class AgentTurnResult:
    """The outcome of one orchestrator turn (the state is mutated in place)."""

    state: agent_state.AgentState
    assistant_message: str = ""
    action: object = None                  # the AgentAction chosen this turn
    policy: object = None                  # the PolicyDecision
    tool_outcome: object = None            # the ToolOutcome (None when blocked/parked)
    executed: bool = False
    awaiting_confirmation: bool = False
    used_ai: bool = False
    council: object = None                 # an advisory CouncilReview (when council=True)


# --------------------------------------------------------------------------- #
# Context attach + status bookkeeping
# --------------------------------------------------------------------------- #
def attach_context(state, *, material_profile=None, release_model=None, database_path=None,
                   phase_template=None) -> None:
    """Attach UI-provided objects to the state and refresh the derived status strings."""
    if material_profile is not None:
        state.material_profile = material_profile
    if release_model is not None:
        state.release_model = release_model
    if database_path is not None:
        state.database_path = database_path
    if phase_template is not None:
        state.phase_template = phase_template

    mp = state.material_profile
    state.material_profile_status = (
        agent_state.MP_USABLE if (mp is not None and getattr(mp, "is_usable", False))
        else agent_state.MP_DRAFT if mp is not None else agent_state.MP_NONE)
    rm = state.release_model
    from ..simulation import source_terms as _st
    state.release_model_status = (
        agent_state.RELEASE_CHOSEN if (rm is not None and getattr(rm, "mode", _st.MODE_NONE)
                                       != _st.MODE_NONE)
        else agent_state.RELEASE_NONE)


# --------------------------------------------------------------------------- #
# The main turn
# --------------------------------------------------------------------------- #
def respond(state, user_message, *, client=None, model=None, use_ai: bool = True,
            council: bool = False, material_profile=None, release_model=None, database_path=None,
            phase_template=None) -> AgentTurnResult:
    """Process one user message and return an :class:`AgentTurnResult` (never raises).

    Execution/save actions are **never** run here — they are parked for explicit confirmation
    via :func:`confirm_pending_action`. Safe/preview actions run immediately once allowed.
    ``use_ai=False`` forces the deterministic planner even when a key is present (the UI passes
    this to honour the per-session "data leaves the machine" consent — no consent → no AI call).
    ``council=True`` additionally runs the **advisory** council (see :mod:`agent_council`) after
    the orchestrator has chosen its action — the council never changes the action.
    """
    message = str(user_message or "").strip()
    attach_context(state, material_profile=material_profile, release_model=release_model,
                   database_path=database_path, phase_template=phase_template)
    state.add_user_message(message)

    # --- responding to a PARKED action (affirm → confirm; negate → reject + edit) --------- #
    if state.pending_action is not None and state.confirmation_required:
        if _is_affirmation(message):
            return _finish(state, _execute_pending(state, client=client, model=model),
                           used_ai=False)
        if _NEGATE_RE.search(message):
            _clear_pending(state)
            note = "Okay — I won't run that. Tell me what to change."
            # fall through to process the edit, prepending the note.
            return _process_turn(state, message, client=client, model=model, use_ai=use_ai,
                                 council=council, lead_note=note)
        # ambiguous reply while parked → process normally (may invalidate the parked action)

    return _process_turn(state, message, client=client, model=model, use_ai=use_ai,
                         council=council)


def _process_turn(state, message, *, client, model, use_ai: bool = True, council: bool = False,
                  lead_note: str = "") -> AgentTurnResult:
    # Accumulate context, then UNDERSTAND the (possibly messy) message: one AI call returns the
    # structured understanding AND the proposed next action; with no AI a robust rule-based parse
    # is used. The understanding is validated/normalized in nlu_extractor before anything applies.
    state.experiment_text = (state.experiment_text + "\n" + message).strip() if message \
        else state.experiment_text
    extraction = nlu_extractor.extract(message, state=state, client=client, model=model,
                                       use_ai=use_ai)
    applied = state.apply_delta(extraction.delta, assumption_specs=extraction.assumption_specs,
                                drop_assumption_fields=extraction.drop_assumption_fields)
    state.ambiguous_fields = list(extraction.ambiguous_fields)
    used_ai = extraction.used_ai

    # A scenario change invalidates a stale preview (and any parked run on it).
    invalidate_note = ""
    if applied and (set(applied) & _PREVIEW_INVALIDATING) and state.preview is not None:
        state.preview = None
        state.preview_status = None
        state.sweep_previews = []
        _clear_pending(state)
        invalidate_note = ("(The scenario changed, so I cleared the old input preview — "
                           "I'll rebuild it when you're ready.)")

    # Keep the domain/engine current (typo-tolerant) from the full accumulated description.
    state.domain = nlu_extractor.classify_message(state.experiment_text,
                                                  hint=extraction.domain_hint)
    state.engine = domains.engine_for(state.domain)
    if state.phase == agent_state.IDLE and message:
        state.phase = agent_state.COLLECTING_CONTEXT

    # The next action: AI proposes (from the same call); deterministic planner is the fallback.
    action = extraction.action
    if action is None or action.action_name not in A.ACTION_NAMES:
        action = agent_policy.deterministic_plan(state, message)

    # A "propose run/sweep" action is normalised to the concrete execution action so it goes
    # through the confirmation gate (parked) — it must never resolve to a no-op that skips the gate.
    if action.action_name in (A.REQUEST_RUN_CONFIRMATION, A.REQUEST_SWEEP_CONFIRMATION):
        action = _action_to_park(action)

    # Human-facing notes: what changed this turn, what couldn't be used / was unclear, and a
    # one-time gentle "less robust without AI" note.
    change_note = _changes_note(extraction)
    clarify_note = _clarify_note(extraction)
    limited_note = ""
    if extraction.limited_without_ai and not state.nlu_notice_shown and applied:
        limited_note = nlu_extractor.LIMITED_WITHOUT_AI_NOTE
        state.nlu_notice_shown = True

    decision = agent_policy.evaluate(state, action)
    outcome = None
    executed = False
    awaiting = False
    confirmed_flag = False

    if decision.blocked:
        # Build a clear, honest block message (planning-only domains get the standing message).
        if decision.code == agent_policy.BLOCK_DOMAIN:
            block_msg = domains.planning_only_message(state.domain)
        else:
            block_msg = f"I can't do that yet — {decision.reason}"
        assistant_msg = _join(lead_note, _llm_lead(action), change_note, block_msg, clarify_note,
                              invalidate_note, limited_note)
    elif decision.requires_confirmation:
        # Park the execution/save action — DO NOT run it here.
        parked = _action_to_park(action)
        state.pending_action = parked
        state.confirmation_required = True
        awaiting = True
        state.phase = (agent_state.AWAITING_EXECUTION_CONFIRMATION
                       if parked.action_name in (A.RUN_SINGLE_SIMULATION, A.RUN_SWEEP)
                       else state.phase)
        assistant_msg = _join(lead_note, _llm_lead(action), change_note, _confirm_prompt(parked),
                              clarify_note, invalidate_note, limited_note)
    else:
        # Allowed safe/preview action — run the deterministic tool now.
        outcome = tool_registry.run(action, state)
        executed = True
        assistant_msg = _join(lead_note, _llm_lead(action), change_note, outcome.summary,
                              clarify_note, invalidate_note, limited_note)
        # An EXPLAIN/results turn always carries the not-validated caveat.
        if action.action_name == A.EXPLAIN_RESULTS:
            assistant_msg = _ensure_not_validated(assistant_msg)

    state.confirmation_required = awaiting
    # The "I understood this as…" card (plain dict the UI renders; reflects the merged scenario).
    state.last_understanding = nlu_extractor.build_understanding_card(state, extraction)

    # Advisory council review — runs AFTER the orchestrator has chosen its action; it never
    # changes the action (it only mirrors it as `safe_next_action`). Off by default.
    review = None
    if council:
        review = agent_council.run_council(state, action=action, client=client, model=model,
                                           use_ai=use_ai)
        state.last_council = review

    state.add_assistant_message(assistant_msg, reasoning_summary=action.reasoning_summary)
    _record(state, message, applied, action, decision, outcome,
            confirmation_required=awaiting, confirmed=confirmed_flag)

    return AgentTurnResult(
        state=state, assistant_message=assistant_msg, action=action, policy=decision,
        tool_outcome=outcome, executed=executed, awaiting_confirmation=awaiting, used_ai=used_ai,
        council=review)


# --------------------------------------------------------------------------- #
# Confirmation / rejection of a parked action (the only execution path)
# --------------------------------------------------------------------------- #
def confirm_pending_action(state, *, client=None, model=None) -> AgentTurnResult:
    """Execute the parked action after explicit user confirmation (the UI's confirm button)."""
    return _finish(state, _execute_pending(state, client=client, model=model), used_ai=False)


def _execute_pending(state, *, client, model):
    """Re-evaluate the parked action with ``confirmed=True`` and run it (never raises)."""
    action = state.pending_action
    if action is None:
        return _no_pending(state)

    decision = agent_policy.evaluate(state, action, confirmed=True)
    if decision.blocked:
        _clear_pending(state)
        msg = f"I couldn't run that — {decision.reason} Let's fix that first."
        state.add_assistant_message(msg)
        _record(state, "[confirm]", {}, action, decision, None,
                confirmation_required=False, confirmed=False)
        return (AgentTurnResult(state=state, assistant_message=msg, action=action,
                                policy=decision, executed=False, awaiting_confirmation=False))

    state.phase = agent_state.RUNNING_TOOL
    outcome = tool_registry.run(action, state)
    _clear_pending(state)
    msg = _ensure_not_validated(outcome.summary) if action.action_name in (
        A.RUN_SINGLE_SIMULATION, A.RUN_SWEEP) else outcome.summary
    state.add_assistant_message(msg)
    _record(state, "[confirm]", {}, action, decision, outcome,
            confirmation_required=False, confirmed=True)
    return AgentTurnResult(state=state, assistant_message=msg, action=action, policy=decision,
                           tool_outcome=outcome, executed=True, awaiting_confirmation=False)


def apply_correction(state, delta) -> AgentTurnResult:
    """Apply the user's direct field corrections from the "edit what I understood" UI.

    Deterministic only (no AI): merges the edited fields into the scenario (replacing list fields
    so an element/output can be removed), re-derives the domain, invalidates a stale preview, and
    refreshes the understanding card. It never runs or executes anything.
    """
    delta = dict(delta or {})
    extraction = nlu_extractor.ExtractionResult(delta=delta, source=nlu_extractor.SRC_RULE)
    extraction.changes = nlu_extractor.compute_changes(delta, state.scenario.to_flat_dict())
    applied = state.apply_delta(delta, replace_lists=True)

    state.domain = nlu_extractor.classify_message(state.experiment_text)
    state.engine = domains.engine_for(state.domain)
    if applied and (set(applied) & _PREVIEW_INVALIDATING) and state.preview is not None:
        state.preview = None
        state.preview_status = None
        state.sweep_previews = []
        _clear_pending(state)

    note = _changes_note(extraction) or "Updated your experiment details."
    state.last_understanding = nlu_extractor.build_understanding_card(state, extraction)
    state.add_assistant_message(note)
    correction_action = A.AgentAction(action_name=A.UPDATE_SCENARIO)
    _record(state, "[correction]", applied, correction_action,
            agent_policy.PolicyDecision(True, False, agent_policy.ALLOW, ""), None,
            confirmation_required=False, confirmed=False)
    return AgentTurnResult(state=state, assistant_message=note, action=correction_action,
                           executed=False, awaiting_confirmation=False)


def reject_pending_action(state) -> AgentTurnResult:
    """Discard the parked action without running it (the UI's 'No' button)."""
    action = state.pending_action
    _clear_pending(state)
    msg = "Okay — I won't run that. What would you like to change?"
    state.add_assistant_message(msg)
    if action is not None:
        _record(state, "[reject]", {}, action,
                agent_policy.PolicyDecision(False, False, "rejected", "user rejected"),
                None, confirmation_required=False, confirmed=False)
    return AgentTurnResult(state=state, assistant_message=msg, action=action, executed=False,
                           awaiting_confirmation=False)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _action_to_park(action):
    """Map a propose-run action to the concrete execution action that gets parked."""
    if action.action_name == A.REQUEST_RUN_CONFIRMATION:
        return A.AgentAction(action_name=A.RUN_SINGLE_SIMULATION,
                             assistant_message=action.assistant_message,
                             reasoning_summary=action.reasoning_summary)
    if action.action_name == A.REQUEST_SWEEP_CONFIRMATION:
        return A.AgentAction(action_name=A.RUN_SWEEP,
                             assistant_message=action.assistant_message,
                             reasoning_summary=action.reasoning_summary)
    return action


def _confirm_prompt(parked) -> str:
    if parked.action_name == A.RUN_SINGLE_SIMULATION:
        return ("Ready to run PHREEQC on the reviewed input. **It will not run until you "
                "confirm** — click **Yes, run it** (or reply 'yes').")
    if parked.action_name == A.RUN_SWEEP:
        return ("Ready to run the reviewed sweep. **It will not run until you confirm** — "
                "click **Yes, run it** (or reply 'yes').")
    if parked.action_name == A.SAVE_SIMULATION_RUN:
        return "Save this simulation run (with its provenance)? Confirm to save."
    return "Confirm to proceed."


def _clear_pending(state) -> None:
    state.pending_action = None
    state.confirmation_required = False


def _no_pending(state):
    msg = "There's nothing waiting to confirm right now."
    state.add_assistant_message(msg)
    return AgentTurnResult(state=state, assistant_message=msg, executed=False,
                           awaiting_confirmation=False)


def _llm_lead(action) -> str:
    """The conversational lead from the model/planner (safe to show; never numbers)."""
    return (action.assistant_message or "").strip()


def _fmt_val(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _changes_note(extraction) -> str:
    """A short "what changed" note when the user corrected a previously-understood value."""
    changes = getattr(extraction, "changes", None) or []
    if not changes:
        return ""
    bits = [f"{c['label']} {_fmt_val(c['old'])} → {_fmt_val(c['new'])}" for c in changes[:4]]
    return "✏️ Updated " + "; ".join(bits) + "."


def _clarify_note(extraction) -> str:
    """Surface impossible values (rejected) + ambiguous fields so the user can fix them."""
    parts: list = []
    rejected = getattr(extraction, "rejected", None) or []
    if rejected:
        parts.append("⚠️ I couldn't use: " + "; ".join(r["reason"] for r in rejected[:3])
                     + (". Could you re-state those?" if len(rejected) > 1
                        else ". Could you re-state that?"))
    ambiguous = getattr(extraction, "ambiguous_fields", None) or []
    if "leachant_type" in ambiguous:
        parts.append("❓ Which reagent did you mean (e.g. NaOH, KOH, HCl, H₂SO₄)?")
    other = [a for a in ambiguous if a != "leachant_type"]
    if other:
        labels = ", ".join(nlu_extractor.FIELD_LABELS.get(a, a) for a in other[:3])
        parts.append(f"❓ Could you clarify: {labels}?")
    return " ".join(parts)


def _join(*parts) -> str:
    return "\n\n".join(p.strip() for p in parts if p and str(p).strip())


def _ensure_not_validated(text: str) -> str:
    if agent_state.NOT_VALIDATED_WARNING in (text or ""):
        return text
    note = "Note: " + agent_state.NOT_VALIDATED_WARNING
    return _join(text, note)


def _record(state, message, applied, action, decision, outcome, *,
            confirmation_required, confirmed) -> None:
    state.record_event(agent_state.ProvenanceEvent(
        user_message=message,
        extracted_fields=dict(applied or {}),
        action_name=getattr(action, "action_name", ""),
        policy_code=getattr(decision, "code", ""),
        policy_reason=getattr(decision, "reason", ""),
        confirmation_required=confirmation_required,
        confirmed=confirmed,
        tool_called=(getattr(action, "action_name", None) if outcome is not None else None),
        result_status=(getattr(outcome, "status", None) if outcome is not None else None),
        warnings=list(getattr(outcome, "warnings", []) or []),
    ))


def _finish(state, result, *, used_ai) -> AgentTurnResult:
    result.used_ai = used_ai
    return result
