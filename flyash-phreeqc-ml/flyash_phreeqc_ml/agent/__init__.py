"""AI **agent orchestration layer** for the materials research assistant.

The agent turns the manual Simulate workflow into a conversation: the user describes an
experiment, the assistant asks for the missing critical details, plans the right modelling
route, and — only after explicit user confirmation — deterministic tools run the simulation
and the assistant explains the estimated results and their limitations.

Architecture (one structured action per turn):

    conversation state → LLM call (propose ONE action) → policy check
    → optional explicit user confirmation → deterministic tool execution
    → updated state → assistant response

Hard boundaries (pinned by ``tests/test_ai_boundary.py``):

* The LLM only **proposes**; deterministic code executes, and only after the policy allows it.
* **Execution / save always require explicit user confirmation** (never from a model message
  alone) — see :func:`agent_orchestrator.confirm_pending_action`.
* **AI never writes PHREEQC input, never invents chemistry, never bypasses a material /
  release / database warning, and is off the scientific result path.**
* With no API key / SDK, the deterministic planner runs and the workflow still works.

Modules: :mod:`agent_state` (state + deterministic scenario merge), :mod:`agent_actions`
(action vocabulary + specs), :mod:`domains` (domain classification + engine map),
:mod:`agent_prompts` (system + grounded prompt), :mod:`tool_registry` (action → deterministic
backend), :mod:`agent_policy` (the gate + deterministic planner), :mod:`agent_orchestrator`
(the loop; the only module that touches AI). Import-safe — no Streamlit at module load.

**LangGraph-compatible by design (no dependency added).** The pieces map onto a graph:
``AgentState`` is the graph state, each action in the vocabulary is a node, :mod:`agent_policy`
is the edge/condition function, and :mod:`tool_registry` is the node implementation. The engine
map in :mod:`domains` is the plugin-engine registry (today ``leaching_geochemistry → PHREEQC``).
The loop could be re-expressed as a LangGraph-style stateful orchestrator without changing the
safety model (one action per turn, confirmation before execution, AI invents no chemistry and
writes no input). See ``docs/ai_architecture.md``.
"""
from __future__ import annotations

from . import (
    agent_actions,
    agent_orchestrator,
    agent_policy,
    agent_prompts,
    agent_state,
    domains,
    nlu_extractor,
    tool_registry,
)
from .agent_actions import AgentAction
from .agent_orchestrator import (
    AGENT_CONSENT_LABEL,
    AGENT_DATA_NOTICE,
    AgentTurnResult,
    apply_correction,
    confirm_pending_action,
    reject_pending_action,
    respond,
)
from .agent_state import AgentState

__all__ = [
    "AgentState", "AgentAction", "AgentTurnResult",
    "respond", "confirm_pending_action", "reject_pending_action", "apply_correction",
    "AGENT_DATA_NOTICE", "AGENT_CONSENT_LABEL",
    "agent_state", "agent_actions", "agent_prompts", "agent_policy",
    "agent_orchestrator", "tool_registry", "domains", "nlu_extractor",
]
