"""Agent **action vocabulary** + structured action object (pure data, no AI, no tools).

The LLM (or the deterministic fallback planner) proposes exactly **one** :class:`AgentAction`
per turn. This module defines:

* the allowed ``action_name`` constants,
* :class:`AgentAction` — the structured action the model must emit (assistant message +
  one action + a *concise* reasoning summary + confidence + safety notes),
* :func:`parse_action` — a **defensive** parser that turns a raw model JSON payload into an
  :class:`AgentAction`, clamping the action name to the known set and **stripping any
  forbidden keys** (e.g. raw ``phreeqc_input_text`` the model must never supply), and
* :data:`ACTION_SPECS` — per-action *metadata only* (risk level, whether explicit user
  confirmation is required, which domains it is allowed in, its state preconditions, and a
  short output schema). **No executable tool function lives here** — the action→deterministic
  function binding is in :mod:`tool_registry`, so this module stays pure and import-safe and
  the policy layer can reason about actions without importing any executor.

Keeping the *metadata* (here) separate from the *tool callables* (in ``tool_registry``) is
deliberate: it lets :mod:`agent_policy` decide what is allowed using only this pure data, so
"can this run?" never depends on importing PHREEQC.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Action names (the only values an action may carry)
# --------------------------------------------------------------------------- #
ASK_USER = "ASK_USER"
UPDATE_SCENARIO = "UPDATE_SCENARIO"
CLASSIFY_DOMAIN = "CLASSIFY_DOMAIN"
PLAN_EXPERIMENT = "PLAN_EXPERIMENT"
REQUEST_MATERIAL_PROFILE = "REQUEST_MATERIAL_PROFILE"
REQUEST_RELEASE_MODEL = "REQUEST_RELEASE_MODEL"
CHECK_DATABASE = "CHECK_DATABASE"
BUILD_PHREEQC_PREVIEW = "BUILD_PHREEQC_PREVIEW"
REQUEST_RUN_CONFIRMATION = "REQUEST_RUN_CONFIRMATION"
RUN_SINGLE_SIMULATION = "RUN_SINGLE_SIMULATION"
BUILD_SWEEP_MATRIX = "BUILD_SWEEP_MATRIX"
REQUEST_SWEEP_CONFIRMATION = "REQUEST_SWEEP_CONFIRMATION"
RUN_SWEEP = "RUN_SWEEP"
RANK_RESULTS = "RANK_RESULTS"
TARGET_MATCH = "TARGET_MATCH"
SAVE_SIMULATION_RUN = "SAVE_SIMULATION_RUN"
CREATE_VALIDATION_TEMPLATE = "CREATE_VALIDATION_TEMPLATE"
EXPLAIN_RESULTS = "EXPLAIN_RESULTS"
OPEN_ADVANCED_WORKFLOW = "OPEN_ADVANCED_WORKFLOW"

ACTION_NAMES = (
    ASK_USER, UPDATE_SCENARIO, CLASSIFY_DOMAIN, PLAN_EXPERIMENT, REQUEST_MATERIAL_PROFILE,
    REQUEST_RELEASE_MODEL, CHECK_DATABASE, BUILD_PHREEQC_PREVIEW, REQUEST_RUN_CONFIRMATION,
    RUN_SINGLE_SIMULATION, BUILD_SWEEP_MATRIX, REQUEST_SWEEP_CONFIRMATION, RUN_SWEEP,
    RANK_RESULTS, TARGET_MATCH, SAVE_SIMULATION_RUN, CREATE_VALIDATION_TEMPLATE,
    EXPLAIN_RESULTS, OPEN_ADVANCED_WORKFLOW,
)

# Default action when the model returns nothing usable — always safe.
FALLBACK_ACTION = ASK_USER

# --------------------------------------------------------------------------- #
# Risk levels
# --------------------------------------------------------------------------- #
RISK_SAFE = "safe"          # runs immediately (no execution, no file writes)
RISK_PREVIEW = "preview"    # needs context, but runs/writes nothing (text/report only)
RISK_EXECUTE = "execute"    # runs PHREEQC — ALWAYS requires explicit user confirmation
RISK_SAVE = "save"          # writes a provenance bundle — requires explicit confirmation

# --------------------------------------------------------------------------- #
# Domain allow-lists (resolved against agent.domains)
# --------------------------------------------------------------------------- #
DOMAINS_ALL = "all"                 # allowed regardless of domain
DOMAINS_EXECUTABLE_ONLY = "executable_only"   # only when the domain has an executable engine

# --------------------------------------------------------------------------- #
# Precondition codes (interpreted by agent_policy against the AgentState)
# --------------------------------------------------------------------------- #
PRE_SCENARIO_CORE = "scenario_core"       # solid mass + liquid volume + leachant present
PRE_PREVIEW_RUNNABLE = "preview_runnable"  # a preview exists whose composition is usable
PRE_SWEEP_PREVIEWS = "sweep_previews"      # built sweep previews exist
PRE_HAS_RESULTS = "has_results"            # at least one executed result exists
PRE_HAS_RESULT_TABLE = "has_result_table"  # a non-empty result table exists


# --------------------------------------------------------------------------- #
# The structured action
# --------------------------------------------------------------------------- #
@dataclass
class AgentAction:
    """One structured action proposed by the model (or the deterministic planner).

    ``arguments`` is a sanitised dict (see :func:`parse_action` — forbidden keys removed).
    ``reasoning_summary`` is a SHORT, user-safe summary — never hidden chain-of-thought, and
    never the raw model JSON.
    """

    action_name: str = FALLBACK_ACTION
    arguments: dict = field(default_factory=dict)
    assistant_message: str = ""
    reasoning_summary: str = ""
    confidence: float = 0.0
    safety_notes: list = field(default_factory=list)

    @property
    def is_known(self) -> bool:
        return self.action_name in ACTION_NAMES

    def to_dict(self) -> dict:
        return {
            "action_name": self.action_name,
            "arguments": dict(self.arguments),
            "reasoning_summary": self.reasoning_summary,
            "confidence": self.confidence,
            "safety_notes": list(self.safety_notes),
        }


# Keys the model must NEVER be allowed to supply through an action's arguments — the
# deterministic tools own these. A model-supplied PHREEQC input / composition / release
# fraction is stripped here so it can never bypass the deterministic builders.
FORBIDDEN_ARGUMENT_KEYS = (
    "phreeqc_input_text", "phreeqc_input", "input_text", "pqi", "pqi_text",
    "composition", "assay", "element_assays", "release_fraction", "release_fractions",
    "release_fraction_values", "saturation_indices", "ph", "ph_initial", "element_totals",
    "result", "results", "raw_response", "api_key",
)


def _as_float(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _clamp_confidence(value) -> float:
    f = _as_float(value)
    if f is None:
        return 0.0
    return max(0.0, min(1.0, f))


def _as_str_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = [value]
    out: list[str] = []
    for it in items:
        s = str(it).strip()
        if s and s not in out:
            out.append(s)
    return out


def sanitize_arguments(raw) -> dict:
    """Return a plain dict of arguments with every :data:`FORBIDDEN_ARGUMENT_KEYS` removed.

    The model may *name* an action and supply *typed scalar hints* (a concentration, a time,
    a target element); it may never supply the generated PHREEQC text, a material composition,
    a release fraction, or a result. Those come only from the deterministic tools.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k, v in raw.items():
        key = str(k)
        if key.lower() in FORBIDDEN_ARGUMENT_KEYS:
            continue
        out[key] = v
    return out


def parse_action(payload) -> AgentAction:
    """Defensively build an :class:`AgentAction` from a raw model payload (never raises).

    * An unknown / missing ``action`` name falls back to :data:`ASK_USER`.
    * ``arguments`` are sanitised (forbidden keys stripped).
    * ``reasoning_summary`` is truncated; the raw payload is **not** retained.
    """
    if not isinstance(payload, dict):
        return AgentAction(action_name=FALLBACK_ACTION,
                           assistant_message="Could you give me a bit more detail?")

    action_obj = payload.get("action")
    if isinstance(action_obj, dict):
        name = str(action_obj.get("action_name") or action_obj.get("name") or "").strip()
        args = action_obj.get("arguments")
    else:
        name = str(payload.get("action_name") or "").strip()
        args = payload.get("arguments")

    if name not in ACTION_NAMES:
        name = FALLBACK_ACTION

    reasoning = str(payload.get("reasoning_summary") or "").strip()
    if len(reasoning) > 600:
        reasoning = reasoning[:600] + "…"

    return AgentAction(
        action_name=name,
        arguments=sanitize_arguments(args),
        assistant_message=str(payload.get("assistant_message") or "").strip(),
        reasoning_summary=reasoning,
        confidence=_clamp_confidence(payload.get("confidence")),
        safety_notes=_as_str_list(payload.get("safety_notes")),
    )


# --------------------------------------------------------------------------- #
# Per-action metadata (no callables — see tool_registry for the bound functions)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ActionSpec:
    """Static metadata for one action (risk / confirmation / domain / preconditions)."""

    action_name: str
    risk_level: str
    requires_user_confirmation: bool
    allowed_domains: str                  # DOMAINS_ALL / DOMAINS_EXECUTABLE_ONLY
    required_inputs: tuple = ()           # precondition codes (see PRE_*)
    output_schema: str = ""               # short human description of what the tool returns
    description: str = ""

    @property
    def needs_engine(self) -> bool:
        return self.allowed_domains == DOMAINS_EXECUTABLE_ONLY


ACTION_SPECS: dict = {
    ASK_USER: ActionSpec(
        ASK_USER, RISK_SAFE, False, DOMAINS_ALL, (),
        "a question for the user", "Ask the user for missing critical details."),
    UPDATE_SCENARIO: ActionSpec(
        UPDATE_SCENARIO, RISK_SAFE, False, DOMAINS_ALL, (),
        "the merged scenario + recomputed missing fields",
        "Merge stated values into the structured scenario (never invents)."),
    CLASSIFY_DOMAIN: ActionSpec(
        CLASSIFY_DOMAIN, RISK_SAFE, False, DOMAINS_ALL, (),
        "the detected domain + whether an executable engine exists",
        "Classify the experiment domain (deterministic; the model only hints)."),
    PLAN_EXPERIMENT: ActionSpec(
        PLAN_EXPERIMENT, RISK_SAFE, False, DOMAINS_ALL, (),
        "the structured plan (variables, missing fields, assumptions, warnings)",
        "Structure the experiment plan and list what is still missing."),
    REQUEST_MATERIAL_PROFILE: ActionSpec(
        REQUEST_MATERIAL_PROFILE, RISK_SAFE, False, DOMAINS_EXECUTABLE_ONLY, (),
        "a request for a confirmed material composition",
        "Ask for a confirmed material composition (never invents one)."),
    REQUEST_RELEASE_MODEL: ActionSpec(
        REQUEST_RELEASE_MODEL, RISK_SAFE, False, DOMAINS_EXECUTABLE_ONLY, (),
        "a request for the release model the user must choose",
        "Ask for the release model (release fractions are user assumptions)."),
    CHECK_DATABASE: ActionSpec(
        CHECK_DATABASE, RISK_PREVIEW, False, DOMAINS_EXECUTABLE_ONLY, (),
        "the database-compatibility report (family + available phases)",
        "Inspect the configured thermodynamic database (text only)."),
    BUILD_PHREEQC_PREVIEW: ActionSpec(
        BUILD_PHREEQC_PREVIEW, RISK_PREVIEW, False, DOMAINS_EXECUTABLE_ONLY,
        (PRE_SCENARIO_CORE,),
        "a deterministic draft .pqi preview + its status/warnings",
        "Build a deterministic PHREEQC input preview (runs nothing; AI writes no input)."),
    REQUEST_RUN_CONFIRMATION: ActionSpec(
        REQUEST_RUN_CONFIRMATION, RISK_SAFE, False, DOMAINS_EXECUTABLE_ONLY,
        (PRE_PREVIEW_RUNNABLE,),
        "a parked run awaiting explicit confirmation",
        "Ask the user to confirm running the reviewed input (does not run)."),
    RUN_SINGLE_SIMULATION: ActionSpec(
        RUN_SINGLE_SIMULATION, RISK_EXECUTE, True, DOMAINS_EXECUTABLE_ONLY,
        (PRE_PREVIEW_RUNNABLE,),
        "the executed result + parsed pH / totals / SIs",
        "Run PHREEQC on the confirmed input (explicit confirmation required)."),
    BUILD_SWEEP_MATRIX: ActionSpec(
        BUILD_SWEEP_MATRIX, RISK_PREVIEW, False, DOMAINS_EXECUTABLE_ONLY,
        (PRE_SCENARIO_CORE,),
        "a plan-only sweep matrix + its previews",
        "Build a small plan-only parameter sweep (runs nothing)."),
    REQUEST_SWEEP_CONFIRMATION: ActionSpec(
        REQUEST_SWEEP_CONFIRMATION, RISK_SAFE, False, DOMAINS_EXECUTABLE_ONLY,
        (PRE_SWEEP_PREVIEWS,),
        "a parked sweep run awaiting explicit confirmation",
        "Ask the user to confirm running the sweep (does not run)."),
    RUN_SWEEP: ActionSpec(
        RUN_SWEEP, RISK_EXECUTE, True, DOMAINS_EXECUTABLE_ONLY,
        (PRE_SWEEP_PREVIEWS,),
        "the executed sweep result table",
        "Run a small confirmed sweep (explicit confirmation required)."),
    RANK_RESULTS: ActionSpec(
        RANK_RESULTS, RISK_SAFE, False, DOMAINS_EXECUTABLE_ONLY,
        (PRE_HAS_RESULT_TABLE,),
        "a ranking of already-executed results against an objective",
        "Rank already-executed results (no execution; not validation)."),
    TARGET_MATCH: ActionSpec(
        TARGET_MATCH, RISK_PREVIEW, False, DOMAINS_EXECUTABLE_ONLY,
        (PRE_SCENARIO_CORE,),
        "a target spec + plan-only candidate grid (+ scoring if results exist)",
        "Parse a target + build a plan-only inverse-search grid (runs nothing)."),
    SAVE_SIMULATION_RUN: ActionSpec(
        SAVE_SIMULATION_RUN, RISK_SAVE, True, DOMAINS_EXECUTABLE_ONLY,
        (PRE_HAS_RESULTS,),
        "a saved provenance bundle path (not validated)",
        "Save the simulation run with its provenance (explicit confirmation required)."),
    CREATE_VALIDATION_TEMPLATE: ActionSpec(
        CREATE_VALIDATION_TEMPLATE, RISK_SAFE, False, DOMAINS_ALL, (),
        "a measured-data CSV template header + guidance",
        "Offer a measured-data template/checklist for validation."),
    EXPLAIN_RESULTS: ActionSpec(
        EXPLAIN_RESULTS, RISK_SAFE, False, DOMAINS_ALL, (),
        "a grounded plain-language explanation (numbers from the tools, not the model)",
        "Explain the estimated results, assumptions, and limitations."),
    OPEN_ADVANCED_WORKFLOW: ActionSpec(
        OPEN_ADVANCED_WORKFLOW, RISK_SAFE, False, DOMAINS_ALL, (),
        "a pointer to the relevant advanced tab",
        "Point the user to an advanced tab (Simulate / Validate / Compare)."),
}


def spec_for(action_name: str) -> ActionSpec | None:
    return ACTION_SPECS.get(action_name)
