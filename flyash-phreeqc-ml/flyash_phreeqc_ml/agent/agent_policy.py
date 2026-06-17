"""Agent **policy layer** — the strict gate between a proposed action and running it (pure).

This module decides, for a proposed :class:`agent_actions.AgentAction` against the current
:class:`agent_state.AgentState`, whether it may run **now**, must wait for **explicit user
confirmation**, or is **blocked**. It also provides the **deterministic fallback planner**
(used when AI is disabled or the model returns nothing usable) so the workflow proceeds
manually with no API key.

It imports only the pure action metadata + domain rules + the state — **no AI, no executor,
no tool registry** — so "can this run?" can never depend on importing PHREEQC. The policy is
the single enforcement point for the project's safety rules:

* **execution / save require explicit confirmation** (never run from a model message alone),
* **execution is blocked when required fields are missing** or the preview's material
  composition is not usable (no run on an invented composition / release fraction),
* **PHREEQC is blocked for non-executable (planning-only) domains** (a strength / thermal /
  battery / unframed scenario never pretends to simulate).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import agent_actions as A
from . import domains

# --------------------------------------------------------------------------- #
# Decision codes
# --------------------------------------------------------------------------- #
ALLOW = "allow"
NEEDS_CONFIRMATION = "needs_confirmation"
BLOCK_UNKNOWN = "blocked_unknown_action"
BLOCK_DOMAIN = "blocked_unsupported_domain"
BLOCK_PRECONDITION = "blocked_missing_precondition"


@dataclass
class PolicyDecision:
    """The outcome of evaluating one action against the state."""

    allowed: bool
    requires_confirmation: bool
    code: str
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return not self.allowed


# Human messages for an unmet precondition.
_PRECONDITION_REASON = {
    A.PRE_SCENARIO_CORE: ("the core scenario is incomplete — I still need the solid mass, the "
                          "liquid volume, and the leachant"),
    A.PRE_PREVIEW_RUNNABLE: ("a runnable input preview is required first — that needs a confirmed "
                             "material composition and a chosen release model, then a built "
                             "preview"),
    A.PRE_SWEEP_PREVIEWS: "a simulation matrix must be built before a sweep can run",
    A.PRE_HAS_RESULTS: "there are no executed results yet",
    A.PRE_HAS_RESULT_TABLE: "there is no executed result table to work from yet",
}


def _precondition_met(code: str, state) -> bool:
    return {
        A.PRE_SCENARIO_CORE: state.has_scenario_core,
        A.PRE_PREVIEW_RUNNABLE: state.preview_runnable,
        A.PRE_SWEEP_PREVIEWS: bool(state.sweep_previews),
        A.PRE_HAS_RESULTS: state.has_results,
        A.PRE_HAS_RESULT_TABLE: state.has_result_table,
    }.get(code, True)


def evaluate(state, action, *, confirmed: bool = False) -> PolicyDecision:
    """Decide whether ``action`` may run against ``state`` (never raises).

    Returns an *allow* (run now), a *needs-confirmation* (park until the user explicitly
    confirms), or a *block* (with a reason the assistant explains). ``confirmed=True`` is
    passed only by the explicit-confirmation path.
    """
    spec = A.spec_for(getattr(action, "action_name", None))
    if spec is None:
        return PolicyDecision(False, False, BLOCK_UNKNOWN,
                              "that is not an action I can take.")

    # 1) Domain gate — PHREEQC-engine actions are blocked for planning-only domains.
    if spec.needs_engine and not domains.is_executable(state.domain):
        return PolicyDecision(
            False, False, BLOCK_DOMAIN,
            f"there is no executable simulation engine for a {domains.label(state.domain)} "
            "experiment yet — I can only help plan it.")

    # 2) Precondition gate — required state must be present.
    for code in spec.required_inputs:
        if not _precondition_met(code, state):
            return PolicyDecision(False, False, BLOCK_PRECONDITION,
                                  _PRECONDITION_REASON.get(code, f"precondition '{code}' unmet"))

    # 3) Confirmation gate — execution / save never run from a model message alone.
    if spec.requires_user_confirmation and not confirmed:
        return PolicyDecision(True, True, NEEDS_CONFIRMATION,
                              "this runs/saves — it needs your explicit confirmation first.")

    return PolicyDecision(True, False, ALLOW, "")


# --------------------------------------------------------------------------- #
# Deterministic fallback planner (AI off, or model output unusable)
# --------------------------------------------------------------------------- #
def _missing_question(state) -> str:
    needed = [m.label.lower() for m in state.missing_fields]
    if not needed:
        return "a few more details"
    if len(needed) == 1:
        return needed[0]
    return ", ".join(needed[:-1]) + f", and {needed[-1]}"


def deterministic_plan(state, user_message: str) -> A.AgentAction:
    """Pick the next action with rules only (no AI). Used when AI is disabled or fails.

    Walks the same arc the assistant would: classify the domain, hand off planning-only
    domains, ask for missing critical fields, request a material profile / release model,
    propose the input preview, propose a run (parked for confirmation), then explain.
    Never proposes an execution without the user; the policy still gates it.
    """
    # Domain not yet known → classify first.
    if state.domain == domains.UNKNOWN and state.experiment_text.strip():
        return A.AgentAction(
            action_name=A.CLASSIFY_DOMAIN,
            assistant_message="Let me work out what kind of experiment this is.",
            reasoning_summary="Domain unknown — classify before planning.", confidence=0.4)

    # Planning-only domain → structure the plan, no execution. A short lead here; the
    # PLAN_EXPERIMENT tool supplies the full planning detail (so it is not duplicated).
    if not domains.is_executable(state.domain):
        return A.AgentAction(
            action_name=A.PLAN_EXPERIMENT,
            assistant_message="Here's how I'd approach this experiment.",
            reasoning_summary="Planning-only domain — structure the plan, no execution.",
            confidence=0.5)

    # Executable (leaching) domain — walk the readiness ladder.
    if not state.has_scenario_core:
        return A.AgentAction(
            action_name=A.ASK_USER,
            assistant_message=("To set this up I still need " + _missing_question(state)
                               + ". Could you give me those?"),
            reasoning_summary="Core scenario incomplete — ask for missing fields.",
            confidence=0.6)
    if not state.composition_usable:
        return A.AgentAction(
            action_name=A.REQUEST_MATERIAL_PROFILE,
            assistant_message=("I have the experiment set-up. Next I need a confirmed material "
                               "composition (I never invent one) — you can provide it in the "
                               "Simulate tab's material manager and confirm it."),
            reasoning_summary="No usable material composition.", confidence=0.6)
    if state.release_model is None:
        return A.AgentAction(
            action_name=A.REQUEST_RELEASE_MODEL,
            assistant_message=("Now choose a material release model (e.g. a 1% global release). "
                               "Release fractions are assumptions you choose — they control the "
                               "predicted dissolved totals."),
            reasoning_summary="No release model chosen.", confidence=0.6)
    if state.preview is None:
        return A.AgentAction(
            action_name=A.BUILD_PHREEQC_PREVIEW,
            assistant_message=("Everything's in place — I'll build the deterministic PHREEQC "
                               "input preview for you to review. Nothing runs yet."),
            reasoning_summary="Ready to build the input preview.", confidence=0.7)
    if state.preview_runnable and not state.has_results:
        return A.AgentAction(
            action_name=A.REQUEST_RUN_CONFIRMATION,
            assistant_message=("The input is ready. Shall I run PHREEQC on it? It will only run "
                               "after you confirm."),
            reasoning_summary="Preview ready — propose a run (confirmation required).",
            confidence=0.7)
    if state.has_results:
        return A.AgentAction(
            action_name=A.EXPLAIN_RESULTS,
            assistant_message="Here is what the model estimates, and its limitations.",
            reasoning_summary="Results exist — explain them.", confidence=0.7)

    return A.AgentAction(
        action_name=A.ASK_USER,
        assistant_message="What would you like to do next?",
        reasoning_summary="Fallback prompt.", confidence=0.4)
