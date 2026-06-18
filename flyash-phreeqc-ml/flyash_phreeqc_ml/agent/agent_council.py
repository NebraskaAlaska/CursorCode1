"""Agent **Council** — an advisory review layer (a panel of research advisors, not one parser).

After the orchestrator has understood a message and chosen an action, the council produces a
short, structured **review** from five role perspectives plus one synthesized recommendation,
so the assistant feels like a team of advisors. It is **purely advisory**:

* It **never executes a tool** and never runs anything (it imports no executor / tool registry).
* It **never fabricates** a composition, release fraction, measured value, pH/strength result,
  validation status, or certainty.
* It **never decides the action** — the orchestrator + the policy gate own that. The council's
  ``safe_next_action`` merely *mirrors* the orchestrator's decision for the user.
* The **safety-critical synthesis fields** (engine status, scientific warnings, missing details)
  are **code-generated** from the existing deterministic validators — the AI can enrich the
  *prose* of the role assessments, but it can never weaken those canonical fields.

Two modes:

* **AI on** → one grounded call returns the five role assessments + advisory prose; deterministic
  code validates it and merges it onto the canonical deterministic baseline.
* **AI off / call failed** → a deterministic lightweight council built from the domain / safety /
  missing-field validators, labelled *"AI council unavailable; using deterministic review."*

Like the orchestrator + ``nlu_extractor`` it may import the AI client, but it imports **no
executor and no result-path** code (pinned by ``tests/test_ai_boundary.py``).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..ai import client as ai_client
from ..ai import config as ai_config
from ..ai.import_assist import _message_text, _parse_json
from . import agent_actions as A
from . import domains, nlu_extractor

MAX_TOKENS = 1500

# Per-session consent (same spirit as the other AI features).
COUNCIL_DATA_NOTICE = (
    "The council sends the current structured experiment state (not measured data, not files, "
    "not secrets) to the Anthropic API to produce an advisory review. It never runs anything and "
    "never decides the action — the orchestrator and the confirmation gate still control that.")

DETERMINISTIC_NOTE = "AI council unavailable; using deterministic review."

SRC_AI = "ai"
SRC_RULE = "rule"
SRC_RULE_FALLBACK = "rule_fallback"

# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #
ROLE_UNDERSTANDING = "Experiment Understanding Agent"
ROLE_ROUTER = "Domain & Engine Router"
ROLE_SCIENTIFIC_CRITIC = "Scientific Critic"
ROLE_DESIGN_ADVISOR = "Experiment Design Advisor"
ROLE_VALIDATION_CRITIC = "Results & Validation Critic"
ROLE_NAMES = (ROLE_UNDERSTANDING, ROLE_ROUTER, ROLE_SCIENTIFIC_CRITIC,
              ROLE_DESIGN_ADVISOR, ROLE_VALIDATION_CRITIC)


@dataclass
class RoleAssessment:
    """One advisor's structured opinion (concise summary — never hidden chain-of-thought)."""

    role_name: str
    short_assessment: str = ""
    concerns: list = field(default_factory=list)
    missing_information: list = field(default_factory=list)
    recommended_next_action: str = ""
    confidence: float = 0.0
    blocking_issues: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "role_name": self.role_name,
            "short_assessment": self.short_assessment,
            "concerns": list(self.concerns),
            "missing_information": list(self.missing_information),
            "recommended_next_action": self.recommended_next_action,
            "confidence": round(float(self.confidence or 0.0), 2),
            "blocking_issues": list(self.blocking_issues),
        }


@dataclass
class CouncilReview:
    """The five role assessments + one synthesized recommendation (advisory; never an action)."""

    roles: list = field(default_factory=list)            # list[RoleAssessment]
    understood_scenario: str = ""
    likely_domain: str = ""
    executable_engine_status: str = ""
    planning_or_execution_status: str = ""
    key_missing_details: list = field(default_factory=list)
    assumptions_to_confirm: list = field(default_factory=list)
    scientific_warnings: list = field(default_factory=list)   # CANONICAL — code-generated
    recommended_next_user_question: str = ""
    safe_next_action: dict = field(default_factory=dict)
    # extra advisory context
    unsupported_elements: list = field(default_factory=list)
    pretreatment_temperature_C: float | None = None
    source: str = SRC_RULE
    note: str = ""

    @property
    def used_ai(self) -> bool:
        return self.source == SRC_AI

    def to_safe_dict(self) -> dict:
        """A JSON-safe, key-free council summary (derived fields only — NO raw model text)."""
        return {
            "source": self.source,
            "note": self.note,
            "understood_scenario": self.understood_scenario,
            "likely_domain": self.likely_domain,
            "executable_engine_status": self.executable_engine_status,
            "planning_or_execution_status": self.planning_or_execution_status,
            "key_missing_details": list(self.key_missing_details),
            "assumptions_to_confirm": list(self.assumptions_to_confirm),
            "scientific_warnings": list(self.scientific_warnings),
            "recommended_next_user_question": self.recommended_next_user_question,
            "safe_next_action": dict(self.safe_next_action),
            "unsupported_elements": list(self.unsupported_elements),
            "pretreatment_temperature_C": self.pretreatment_temperature_C,
            "roles": [r.to_dict() for r in self.roles],
        }


# --------------------------------------------------------------------------- #
# Small read helpers (no mutation of the scenario / domain / action)
# --------------------------------------------------------------------------- #
def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _missing_labels(state) -> list:
    return [m.label for m in state.missing_fields]


def _assumptions_to_confirm(state) -> list:
    return [f"{a.field}: {a.reason}" if a.reason else str(a.field) for a in state.assumptions]


def _scientific_warnings(state, unsupported, pretreat) -> list:
    """The CANONICAL scientific caveats — the code-generated safety warnings, plus the
    council-derived out-of-scope-element + thermal-pretreatment notes. Never weakened by AI."""
    warns = list(state.warnings)
    if unsupported:
        warns.append(
            f"{', '.join(unsupported)} are outside the engine's element set "
            "(Ca/Si/Al/Fe/Na/K/Sc/REE) — the current PHREEQC leaching engine does not handle them; "
            "they would need an extended model.")
    if pretreat is not None:
        warns.append(
            f"A calcination / thermal-treatment step (~{pretreat:g} °C) is planning-only — it is "
            "not the aqueous-leach temperature and is not simulated; only a subsequent aqueous "
            "leach can be modelled.")
    # de-dup, preserve order
    seen, out = set(), []
    for w in warns:
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


# Unsafe-intent cues — the council explicitly rejects automatic execution / fabricated data /
# fake validation (the orchestrator + policy gate already block them; the council says so plainly).
_UNSAFE_CUES = (
    "run everything", "run every", "run all", "ignore safety", "ignore all", "assume data",
    "assume the data", "assume my data", "validate my result", "exact answer", "fabricate",
    "skip safety", "skip checks", "skip the safety")
UNSAFE_REJECTION = (
    "I won't auto-run anything, invent your data/assumptions, or 'validate' from a simulation — "
    "nothing runs without your explicit confirmation, estimates are not validation, and missing "
    "values are asked for, never fabricated.")


def _has_unsafe_intent(text) -> bool:
    low = str(text or "").lower()
    return any(c in low for c in _UNSAFE_CUES)


def _wants_fly_ash_class(state) -> bool:
    """True when the material is a bare 'fly ash' (class unstated) — ask C vs F, don't assume."""
    flat = state.scenario.to_flat_dict()
    name = (flat.get("material_name") or "").lower()
    return "fly ash" in name and "class c" not in name and "class f" not in name


def _next_user_question(state, *, unsupported, pretreat) -> str:
    """One focused question (the single most useful), derived from the deterministic state."""
    ambiguous = list(getattr(state, "ambiguous_fields", []) or [])
    flat = state.scenario.to_flat_dict()
    executable = domains.is_executable(state.domain)

    if "leachant_type" in ambiguous or (executable and not flat.get("leachant_type")):
        return "Which reagent are you leaching with — NaOH, KOH, HCl, or something else?"
    if pretreat is not None and "temperature_C" in ambiguous:
        return ("That looks like a calcination step, then a leach — what's the *leach* "
                "temperature and solution (separate from the ~{:g} °C heat step)?".format(pretreat))
    if _wants_fly_ash_class(state):
        return "Is this Class C or Class F fly ash? (they behave quite differently)"
    miss = _missing_labels(state)
    if "Solid mass" in miss or "Liquid volume" in miss:
        return "What were the solid mass and the liquid volume (so I can get the L/S ratio)?"
    if executable and not state.composition_usable:
        return "Can you provide a confirmed material composition (an oxide / element assay)?"
    if not executable and state.domain != domains.UNKNOWN:
        s = domains.planning_support(state.domain)
        first = (s["response_variables"] or ["the outcome you care about"])[0]
        return f"Which outcome matters most to measure first — e.g. {first}?"
    if unsupported:
        return (f"The current engine doesn't cover {', '.join(unsupported)} — do you want to plan a "
                "dataset for those, or focus on the elements it does handle?")
    return "What would you like to do next?"


def _safe_next_action(state, action) -> dict:
    """Mirror the orchestrator's chosen action (advisory). The council never picks the action;
    it reports what the safe next step is, emphasising the confirmation gate for execution."""
    name = getattr(action, "action_name", None)
    spec = A.spec_for(name) if name else None
    descriptions = {
        A.ASK_USER: "ask the user the next clarifying question",
        A.UPDATE_SCENARIO: "record the stated values",
        A.CLASSIFY_DOMAIN: "confirm the experiment domain",
        A.PLAN_EXPERIMENT: "structure the experiment plan (planning-only)",
        A.REQUEST_MATERIAL_PROFILE: "ask for a confirmed material composition",
        A.REQUEST_RELEASE_MODEL: "ask the user to choose a release model",
        A.CHECK_DATABASE: "inspect the thermodynamic database",
        A.BUILD_PHREEQC_PREVIEW: "build a reviewable PHREEQC input preview (runs nothing)",
        A.REQUEST_RUN_CONFIRMATION: "propose a run — it will not run until you confirm",
        A.RUN_SINGLE_SIMULATION: "run PHREEQC (only after your explicit confirmation)",
        A.BUILD_SWEEP_MATRIX: "build a plan-only parameter sweep",
        A.RUN_SWEEP: "run the sweep (only after your explicit confirmation)",
        A.RANK_RESULTS: "rank already-executed results",
        A.TARGET_MATCH: "set up an inverse-search target",
        A.SAVE_SIMULATION_RUN: "save the run with its provenance (after confirmation)",
        A.CREATE_VALIDATION_TEMPLATE: "offer a data-collection template",
        A.EXPLAIN_RESULTS: "explain the model estimates (not validated)",
        A.OPEN_ADVANCED_WORKFLOW: "point to an advanced tab",
    }
    if name is None:
        # No action passed — recommend the deterministic next step descriptively.
        if not domains.is_executable(state.domain) and state.domain != domains.UNKNOWN:
            return {"action": A.PLAN_EXPERIMENT, "description": descriptions[A.PLAN_EXPERIMENT],
                    "requires_confirmation": False}
        if state.missing_fields:
            return {"action": A.ASK_USER, "description": descriptions[A.ASK_USER],
                    "requires_confirmation": False}
        return {"action": A.ASK_USER, "description": "ask what to do next",
                "requires_confirmation": False}
    return {
        "action": name,
        "description": descriptions.get(name, "the next step"),
        "requires_confirmation": bool(getattr(spec, "requires_user_confirmation", False)),
    }


# --------------------------------------------------------------------------- #
# Deterministic council (always available — built from the existing validators)
# --------------------------------------------------------------------------- #
def deterministic_council(state, *, action=None) -> CouncilReview:
    """A lightweight council from the domain / safety / missing-field validators (no AI)."""
    flat = state.scenario.to_flat_dict()
    domain_label = domains.label(state.domain)
    executable = domains.is_executable(state.domain)
    missing = _missing_labels(state)
    ambiguous = list(getattr(state, "ambiguous_fields", []) or [])
    assumptions = _assumptions_to_confirm(state)

    pretreat = (nlu_extractor.pretreatment_temperature(state.experiment_text)
                if nlu_extractor.is_thermal_pretreatment_then_leach(state.experiment_text) else None)
    unsupported = nlu_extractor.detect_unsupported_elements(state.experiment_text)
    warnings = _scientific_warnings(state, unsupported, pretreat)
    unsafe = _has_unsafe_intent(state.experiment_text)
    if unsafe and UNSAFE_REJECTION not in warnings:
        warnings = [UNSAFE_REJECTION] + warnings

    material = flat.get("material_name") or "an unspecified material"
    leachant = flat.get("leachant_type")
    goal = (", ".join(flat.get("target_elements") or [])
            or ", ".join(flat.get("desired_outputs") or []) or "the property of interest")

    if executable:
        engine_status = "PHREEQC available (aqueous leaching / geochemistry)"
        plan_exec = ("Execution is possible after you provide a confirmed composition + a release "
                     "model and confirm the run — nothing runs automatically.")
        understood = (f"A leaching / geochemistry study of {material}"
                      + (f" with {leachant}" if leachant else "")
                      + f"; goal: {goal}.")
    elif state.domain == domains.UNKNOWN:
        engine_status = "no engine selected yet (domain unclear)"
        plan_exec = "Planning-only until the goal/domain is clear."
        understood = f"An experiment involving {material}; the goal/domain is still unclear."
    else:
        engine_status = f"no executable engine for a {domain_label} study yet (planning-only)"
        plan_exec = ("Planning-only — there is no validated simulation engine for this domain; I can "
                     "structure the experiment and build a data-collection template.")
        understood = f"A {domain_label} study of {material}; goal: {goal}."

    # --- the five roles -------------------------------------------------- #
    roles = [
        RoleAssessment(
            ROLE_UNDERSTANDING,
            short_assessment=understood,
            concerns=(["fly-ash class (C vs F) not stated"] if _wants_fly_ash_class(state) else [])
                     + (["the goal/domain is ambiguous"] if state.domain == domains.UNKNOWN else []),
            missing_information=missing,
            recommended_next_action="Confirm this reading and fill the missing core details.",
            confidence=round(float(getattr(state.scenario, "confidence", 0.0) or 0.0), 2),
            blocking_issues=[]),
        RoleAssessment(
            ROLE_ROUTER,
            short_assessment=f"Domain: {domain_label}. Engine: {engine_status}.",
            concerns=(["domain is unclear — confirm the goal before choosing an engine"]
                      if state.domain == domains.UNKNOWN else [])
                     + (["PHREEQC could help with pore-solution / leaching chemistry only if you "
                         "specifically want aqueous chemistry — strength is not modelled"]
                        if state.domain == domains.CEMENTITIOUS_BINDER else []),
            missing_information=[],
            recommended_next_action=("Plan the experiment (planning-only)." if not executable
                                     else "Set up a PHREEQC run after a confirmed composition."),
            confidence=0.8 if state.domain != domains.UNKNOWN else 0.3,
            blocking_issues=(["no usable material composition for a meaningful run"]
                             if executable and not state.composition_usable else [])),
        RoleAssessment(
            ROLE_SCIENTIFIC_CRITIC,
            short_assessment=("Scientific review: the canonical caveats below are code-generated "
                              "and must be respected."),
            concerns=warnings,
            missing_information=(["a confirmed material composition"]
                                 if executable and not state.composition_usable else []),
            recommended_next_action=("Provide a confirmed composition + a release model (release "
                                     "fractions are assumptions) before running." if executable
                                     else "Define the response variables + how they'll be measured."),
            confidence=0.7,
            blocking_issues=([f"{', '.join(unsupported)} are outside the engine's element set"]
                             if unsupported else [])),
        _design_advisor_role(state, executable),
        RoleAssessment(
            ROLE_VALIDATION_CRITIC,
            short_assessment=(UNSAFE_REJECTION if unsafe else
                              "Results exist — treat as model estimates, not validated."
                              if state.has_results else
                              "No results yet. A simulation is not validation."),
            concerns=(["the request asks for unsafe/auto behaviour — refused"] if unsafe else [])
                     + (["model estimates are not measurements"] if state.has_results else []),
            missing_information=["measured ICP / pH data for this exact condition"],
            recommended_next_action=("Compare these estimates against measured data (Validate / "
                                     "Compare) — that is the only validation."),
            confidence=0.8,
            blocking_issues=(["automatic execution / fabricated data / fake validation refused"]
                             if unsafe else [])),
    ]

    return CouncilReview(
        roles=roles,
        understood_scenario=understood,
        likely_domain=domain_label,
        executable_engine_status=engine_status,
        planning_or_execution_status=plan_exec,
        key_missing_details=(missing + [f"clarify: {a}" for a in ambiguous])[:5],
        assumptions_to_confirm=assumptions,
        scientific_warnings=warnings,
        recommended_next_user_question=_next_user_question(
            state, unsupported=unsupported, pretreat=pretreat),
        safe_next_action=_safe_next_action(state, action),
        unsupported_elements=unsupported,
        pretreatment_temperature_C=pretreat,
        source=SRC_RULE)


def _design_advisor_role(state, executable) -> RoleAssessment:
    if executable:
        return RoleAssessment(
            ROLE_DESIGN_ADVISOR,
            short_assessment="To make the run meaningful, fix the composition + release assumptions.",
            concerns=["a release fraction is a USER ASSUMPTION unless measured / literature-confirmed"],
            missing_information=["material composition", "release model"],
            recommended_next_action=("Provide a confirmed composition, choose a release model, "
                                     "then build the input preview to review."),
            confidence=0.7, blocking_issues=[])
    if state.domain == domains.UNKNOWN:
        return RoleAssessment(
            ROLE_DESIGN_ADVISOR,
            short_assessment="Clarify the goal so the right variables can be planned.",
            concerns=[], missing_information=["the property of interest"],
            recommended_next_action="Tell me whether the goal is leaching chemistry, mechanical "
                                    "performance, thermal processing, durability, or reuse screening.",
            confidence=0.4, blocking_issues=[])
    support = domains.planning_support(state.domain)
    return RoleAssessment(
        ROLE_DESIGN_ADVISOR,
        short_assessment=(f"Planning-only — measure: {', '.join(support['response_variables'][:4])}. "
                          "Build a dataset now for a future ML / surrogate model."),
        concerns=["no validated engine yet — accurate prediction needs measured data first"],
        missing_information=list(support["input_variables"][:6]),
        recommended_next_action=("Structure the plan + a data-collection template; collect data to "
                                 "train a future model — don't claim accurate prediction without it."),
        confidence=0.6, blocking_issues=[])


# --------------------------------------------------------------------------- #
# AI council (one grounded call → advisory role prose; validated + merged)
# --------------------------------------------------------------------------- #
COUNCIL_SYSTEM_PROMPT = f"""\
You are a COUNCIL of five materials-research advisors reviewing one experiment plan and giving a
concise, structured review. The roles are:
  - {ROLE_UNDERSTANDING}: did we read the experiment correctly? what's missing/ambiguous?
  - {ROLE_ROUTER}: is the domain + engine choice right? (only leaching/geochemistry has an
    executable engine, PHREEQC; everything else is planning-only)
  - {ROLE_SCIENTIFIC_CRITIC}: scientific concerns + caveats (composition needed, release fractions
    are assumptions, Sc/REE + out-of-scope elements need data, precipitation can't be proven from
    liquid alone, a calcination temperature is not the leach temperature)
  - {ROLE_DESIGN_ADVISOR}: how to improve the design / what variables to capture / build a dataset
  - {ROLE_VALIDATION_CRITIC}: a simulation is NOT validation; measured data is required

HARD RULES (non-negotiable):
1. You are ADVISORY ONLY. You never run or execute anything, and you never decide the action —
   the app's orchestrator and its confirmation gate do that.
2. You NEVER fabricate a composition, release fraction, measured value, pH/strength result,
   validation status, or any false certainty. If something is unknown, say it's unknown.
3. Respect the deterministic state given to you (domain, engine status, missing fields, warnings)
   — you may add concerns, but never contradict or weaken those facts.
4. Give CONCISE role summaries — never hidden chain-of-thought, never long essays.
5. recommended_next_user_question must be ONE focused question.

Respond with ONLY this JSON object (no prose, no code fences):
{{"roles": [{{"role_name": "<one of the five role names>", "short_assessment": "1-2 sentences",
  "concerns": ["..."], "missing_information": ["..."], "recommended_next_action": "...",
  "confidence": 0.0, "blocking_issues": ["..."]}}],
 "understood_scenario": "one sentence",
 "recommended_next_user_question": "ONE question",
 "scientific_concerns": ["any extra advisory caveat (does not replace the canonical warnings)"]}}
"""


def _council_user_prompt(state, action, det: CouncilReview) -> str:
    flat = state.scenario.to_flat_dict()
    lines = [
        "DETERMINISTIC STATE (authoritative — do not contradict):",
        f"- domain: {state.domain} ({det.likely_domain}); engine: {det.executable_engine_status}",
        f"- status: {det.planning_or_execution_status}",
        f"- material: {_fmt(flat.get('material_name'))}; leachant: {_fmt(flat.get('leachant_type'))} "
        f"@ {_fmt(flat.get('leachant_concentration_M'))} M",
        f"- solid mass (g): {_fmt(flat.get('solid_mass_g'))}; liquid (mL): "
        f"{_fmt(flat.get('liquid_volume_mL'))}; time (min): {_fmt(flat.get('time_min'))}; "
        f"leach temp (°C): {_fmt(flat.get('temperature_C'))}",
        f"- target elements: {_fmt(flat.get('target_elements'))}; desired outputs: "
        f"{_fmt(flat.get('desired_outputs'))}",
        f"- out-of-scope elements: {_fmt(det.unsupported_elements)}; pretreatment temp (°C): "
        f"{_fmt(det.pretreatment_temperature_C)}",
        f"- code-computed MISSING: {_fmt(det.key_missing_details)}",
        f"- code-generated SCIENTIFIC WARNINGS (never weaken): {_fmt(det.scientific_warnings)}",
        f"- composition usable: {state.composition_usable}; results available: {state.has_results}",
        f"- the orchestrator's safe next action: {det.safe_next_action.get('action')}",
        "",
        "Give the five-role council review per the rules. Respond with ONLY the JSON object.",
    ]
    return "\n".join(lines)


def _sanitize_role(payload: dict) -> RoleAssessment | None:
    if not isinstance(payload, dict):
        return None
    name = str(payload.get("role_name") or "").strip()
    if name not in ROLE_NAMES:
        return None
    return RoleAssessment(
        role_name=name,
        short_assessment=_trim(payload.get("short_assessment"), 320),
        concerns=_str_list(payload.get("concerns")),
        missing_information=_str_list(payload.get("missing_information")),
        recommended_next_action=_trim(payload.get("recommended_next_action"), 320),
        confidence=_clamp01(payload.get("confidence")),
        blocking_issues=_str_list(payload.get("blocking_issues")))


def _ai_council(state, action, det: CouncilReview, *, client, model) -> CouncilReview | None:
    """Run the grounded council call. Returns None (never raises) on any failure."""
    resolved = ai_client.get_client(client, model=model)
    if not resolved.ok or resolved.client is None:
        return None
    try:
        resp = resolved.client.messages.create(
            model=ai_config.resolve_model(model), max_tokens=MAX_TOKENS,
            system=COUNCIL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _council_user_prompt(state, action, det)}])
    except Exception:                                       # noqa: BLE001 — never crash the chat
        return None
    payload = _parse_json(_message_text(resp))
    if not isinstance(payload, dict):
        return None

    roles = [r for r in (_sanitize_role(p) for p in (payload.get("roles") or [])) if r]
    if not roles:
        return None                                         # unusable → fall back to deterministic

    # Merge: AI enriches the ROLE PROSE + understood_scenario + the next question; the canonical
    # synthesis fields (domain, engine, warnings, missing, safe action) stay deterministic so the
    # AI can never weaken a fact or a caveat. Extra AI concerns ride in the role assessments only.
    merged = CouncilReview(
        roles=_order_roles(roles, det.roles),
        understood_scenario=_trim(payload.get("understood_scenario"), 400) or det.understood_scenario,
        likely_domain=det.likely_domain,
        executable_engine_status=det.executable_engine_status,
        planning_or_execution_status=det.planning_or_execution_status,
        key_missing_details=list(det.key_missing_details),
        assumptions_to_confirm=list(det.assumptions_to_confirm),
        scientific_warnings=list(det.scientific_warnings),          # CANONICAL
        recommended_next_user_question=(_first_question(payload.get("recommended_next_user_question"))
                                        or det.recommended_next_user_question),
        safe_next_action=dict(det.safe_next_action),                # CANONICAL (orchestrator's)
        unsupported_elements=list(det.unsupported_elements),
        pretreatment_temperature_C=det.pretreatment_temperature_C,
        source=SRC_AI)
    return merged


def _order_roles(ai_roles, det_roles) -> list:
    """Return the five roles in the canonical order; fill any role the AI omitted from the
    deterministic baseline so the council always has all five perspectives."""
    by_name = {r.role_name: r for r in ai_roles}
    det_by_name = {r.role_name: r for r in det_roles}
    return [by_name.get(name) or det_by_name.get(name) or RoleAssessment(name)
            for name in ROLE_NAMES]


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def run_council(state, *, action=None, client=None, model=None, use_ai: bool = True) -> CouncilReview:
    """Produce the advisory council review (never raises, never executes, never decides the action).

    AI when available + ``use_ai`` (validated + merged onto the deterministic baseline so canonical
    facts/caveats can't be weakened); otherwise a deterministic lightweight review with a note.
    """
    det = deterministic_council(state, action=action)
    ai_on = (client is not None) or ai_config.is_enabled()
    if use_ai and ai_on:
        ai = _ai_council(state, action, det, client=client, model=model)
        if ai is not None:
            return ai
        det.source = SRC_RULE_FALLBACK
        det.note = "AI council was unavailable for this turn — showing the deterministic review."
        return det
    det.note = DETERMINISTIC_NOTE
    return det


# --------------------------------------------------------------------------- #
# Tiny defensive coercion helpers
# --------------------------------------------------------------------------- #
def _trim(value, n: int) -> str:
    s = str(value or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _str_list(value) -> list:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out: list = []
    for it in items:
        s = str(it).strip()
        if s and s not in out:
            out.append(_trim(s, 240))
    return out


def _clamp01(value) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f)) if f == f else 0.0


def _first_question(value) -> str:
    """Keep only the first question (≤ one '?') so the council asks ONE focused question."""
    s = str(value or "").strip()
    if not s:
        return ""
    idx = s.find("?")
    return (s[:idx + 1]).strip() if idx != -1 else _trim(s, 240)
