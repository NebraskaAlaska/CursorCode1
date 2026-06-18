"""Agent **conversation state** + deterministic scenario merge (pure data; no AI, no PHREEQC).

This module owns everything the orchestrator mutates across a conversation: the chat
history, the structured :class:`SimulationScenario` being assembled, the deterministic
missing-field / assumption / warning analysis, the selected domain / engine, the latest
deterministic tool outputs (preview, database report, results), the pending action awaiting
confirmation, and the provenance trace.

It deliberately imports **no AI** and **no executor** — it is plain data + the
deterministic, correction-aware merge of a natural reply into the existing scenario
(``merge_user_message``). The merge only updates fields the reply *explicitly states*, so a
later "change temperature to 40 C" overrides an earlier value and an unrelated reply never
fabricates a temperature.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..simulation import rule_parser, safety
from ..simulation import scenario_schema as S
from ..simulation.scenario_schema import SimulationScenario
from . import domains

# --------------------------------------------------------------------------- #
# Conversation phases (the agent's state machine)
# --------------------------------------------------------------------------- #
IDLE = "idle"
COLLECTING_CONTEXT = "collecting_context"
ASKING_CLARIFICATION = "asking_clarification"
PLANNING = "planning"
AWAITING_PREVIEW_CONFIRMATION = "awaiting_preview_confirmation"
PREVIEW_READY = "preview_ready"
AWAITING_EXECUTION_CONFIRMATION = "awaiting_execution_confirmation"
RUNNING_TOOL = "running_tool"
RESULTS_READY = "results_ready"
VALIDATION_RECOMMENDED = "validation_recommended"
UNSUPPORTED_DOMAIN_PLANNING_ONLY = "unsupported_domain_planning_only"

PHASES = (
    IDLE, COLLECTING_CONTEXT, ASKING_CLARIFICATION, PLANNING, AWAITING_PREVIEW_CONFIRMATION,
    PREVIEW_READY, AWAITING_EXECUTION_CONFIRMATION, RUNNING_TOOL, RESULTS_READY,
    VALIDATION_RECOMMENDED, UNSUPPORTED_DOMAIN_PLANNING_ONLY,
)

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

# Standing honesty label carried into every saved provenance bundle.
NOT_VALIDATED_WARNING = (
    "Simulation outputs are model estimates under reviewed assumptions — they are NOT measured "
    "and NOT validated. Validation requires comparing against measured ICP / pH data.")

# --------------------------------------------------------------------------- #
# Status vocabularies (display strings the state tracks)
# --------------------------------------------------------------------------- #
MP_NONE = "not_provided"
MP_DRAFT = "draft_not_usable"
MP_USABLE = "usable"
RELEASE_NONE = "not_chosen"
RELEASE_CHOSEN = "chosen"
DB_UNKNOWN = "unknown"
DB_CHECKED = "checked"
PREVIEW_NONE = "not_built"
EXEC_NONE = "not_run"
EXEC_DONE = "run"

# Mirror the phreeqc_input_builder status values that mean "composition available → a run is
# meaningful". Kept as local literals so this pure-data module imports no builder/executor.
_RUNNABLE_PREVIEW_STATUSES = ("ready_for_review", "template_warning")

# --------------------------------------------------------------------------- #
# Explicit-field detection (presence regexes — a reply only updates what it states)
# --------------------------------------------------------------------------- #
_HAS_MASS = re.compile(r"\d+(?:\.\d+)?\s*(?:kg|mg|grams?|g)\b", re.IGNORECASE)
_HAS_VOLUME = re.compile(
    r"\d+(?:\.\d+)?\s*(?:milliliters?|millilitres?|ml|µl|ul|liters?|litres?|l)\b",
    re.IGNORECASE)
_HAS_MOLARITY = re.compile(r"\d+(?:\.\d+)?\s*M(?![a-zA-Z])")
_HAS_TIME = re.compile(r"\d+(?:\.\d+)?\s*(?:minutes?|mins?|min|hours?|hrs?|hr|h)\b", re.IGNORECASE)
_HAS_TEMP = re.compile(r"\d+(?:\.\d+)?\s*(?:°\s*|degrees?\s*)?C\b")
_HAS_FILTER = re.compile(r"\d+(?:\.\d+)?\s*(?:µm|um|micron|microns|micrometers?)\b", re.IGNORECASE)
_ROOM_TEMP_WORDS = ("room temperature", "room temp", "ambient")

# Fields merged as scalars (last explicit mention wins → corrections work).
_SCALAR_FIELDS = (
    "solid_mass_g", "liquid_volume_mL", "leachant_type", "leachant_concentration_M",
    "time_min", "temperature_C", "CO2_condition", "cover_condition", "filter_size_um",
    "centrifuge_used", "filtration_used", "material_name", "material_type",
)
# Fields merged as a union of items (never lose a previously-stated element/output).
_LIST_FIELDS = ("target_elements", "desired_outputs")


# --------------------------------------------------------------------------- #
# Chat + provenance records
# --------------------------------------------------------------------------- #
@dataclass
class ChatMessage:
    """One user-visible turn. ``content`` is the displayed text only — never raw model JSON
    and never hidden reasoning."""

    role: str
    content: str
    reasoning_summary: str = ""        # short, user-safe; shown in an expander, never the CoT

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class ProvenanceEvent:
    """One agent-driven action's audit record (no values, no secrets, no raw model text)."""

    user_message: str = ""
    extracted_fields: dict = field(default_factory=dict)    # the scenario delta this turn
    action_name: str = ""
    policy_code: str = ""
    policy_reason: str = ""
    confirmation_required: bool = False
    confirmed: bool = False
    tool_called: str | None = None
    result_status: str | None = None
    warnings: list = field(default_factory=list)
    timestamp: str | None = None

    def to_dict(self) -> dict:
        return {
            "user_message": self.user_message,
            "extracted_fields": dict(self.extracted_fields),
            "action_name": self.action_name,
            "policy_code": self.policy_code,
            "policy_reason": self.policy_reason,
            "confirmation_required": self.confirmation_required,
            "confirmed": self.confirmed,
            "tool_called": self.tool_called,
            "result_status": self.result_status,
            "warnings": list(self.warnings),
            "timestamp": self.timestamp,
        }


# --------------------------------------------------------------------------- #
# The agent state
# --------------------------------------------------------------------------- #
@dataclass
class AgentState:
    """Everything the agent tracks for one conversation (one selected run)."""

    # conversation + context
    phase: str = IDLE
    history: list = field(default_factory=list)            # list[ChatMessage]
    experiment_text: str = ""                              # accumulated context
    desired_outputs_text: str = ""

    # structured scenario + deterministic analysis
    scenario: SimulationScenario = field(default_factory=SimulationScenario)
    parse_source: str | None = None                        # ai / rule / rule_fallback / manual
    missing_fields: list = field(default_factory=list)     # list[MissingInput]
    assumptions: list = field(default_factory=list)        # list[Assumption]
    warnings: list = field(default_factory=list)           # list[str]

    # domain / engine
    domain: str = domains.UNKNOWN
    engine: str | None = None

    # material / release / database / preview status (display strings)
    material_profile_status: str = MP_NONE
    release_model_status: str = RELEASE_NONE
    database_status: str = DB_UNKNOWN
    preview_status: str | None = None
    execution_status: str = EXEC_NONE
    validation_status: str = "not_started"

    # deterministic tool objects (NOT serialized into provenance; only summaries are)
    material_profile: object = None        # duck-typed usable_assay / is_usable
    release_model: object = None           # source_terms.DissolutionModel
    database_path: str | None = None
    phase_template: object = None
    preview: object = None                 # phreeqc_input_builder.PhreeqcInputPreview
    database_report: object = None
    sweep_matrix: object = None            # pandas DataFrame (plan-only)
    sweep_previews: list = field(default_factory=list)
    execution_result: object = None        # phreeqc_executor.ExecutionResult
    parsed_result: object = None           # phreeqc_executor.ParsedSimulation
    batch_result: object = None            # batch_executor.BatchResult
    result_table: object = None            # pandas DataFrame
    ranking: object = None
    target_spec: object = None
    target_candidates: list = field(default_factory=list)
    target_match_result: object = None
    last_explanation: object = None        # a grounded ResultExplanation (tool output)
    last_run_id: str | None = None

    # confirmation gating
    pending_action: object = None          # AgentAction awaiting explicit confirmation
    confirmation_required: bool = False

    # natural-language understanding (the "I understood this as…" card + clarification state)
    last_understanding: dict = field(default_factory=dict)  # plain dict the UI renders
    ambiguous_fields: list = field(default_factory=list)    # fields to clarify (this turn)
    nlu_notice_shown: bool = False                          # gentle limited-without-AI note shown
    last_used_ai: bool | None = None                        # did the last response use live AI?
    last_ai_fell_back: bool = False                         # live AI was requested but failed (this turn)

    # advisory council review (a CouncilReview; duck-typed so this module imports no AI/council)
    last_council: object = None

    # audit
    provenance: list = field(default_factory=list)         # list[ProvenanceEvent]

    # ----------------------------------------------------------------- #
    # Conversation helpers
    # ----------------------------------------------------------------- #
    def add_user_message(self, text: str) -> None:
        self.history.append(ChatMessage(ROLE_USER, str(text or "")))

    def add_assistant_message(self, text: str, reasoning_summary: str = "") -> None:
        self.history.append(ChatMessage(ROLE_ASSISTANT, str(text or ""),
                                        reasoning_summary=str(reasoning_summary or "")))

    def record_event(self, event: ProvenanceEvent) -> None:
        self.provenance.append(event)

    # ----------------------------------------------------------------- #
    # Deterministic scenario merge (correction-aware; only explicit fields)
    # ----------------------------------------------------------------- #
    def apply_delta(self, delta: dict, *, assumption_specs=(), drop_assumption_fields=(),
                    replace_lists: bool = False) -> dict:
        """Merge a **pre-computed, validated** field delta into the scenario; return the applied
        subset.

        Pure data: the orchestrator computes ``delta`` (deterministically or from a validated AI
        understanding via :mod:`nlu_extractor`) and hands it here, so this module never touches
        AI. Scalars overwrite (a later explicit value wins → corrections work); list fields are
        unioned (a stated element/output is never lost) — unless ``replace_lists`` is set, used by
        the "edit what I understood" corrector so the user can *remove* an element/output.
        ``assumption_specs`` is a list of ``(field, value, reason)`` to record as
        assumptions-needing-confirmation (only for fields actually present after the merge);
        ``drop_assumption_fields`` removes now-obsolete assumptions (e.g. an explicit temperature
        replacing an assumed one). Never invents composition, release fractions, or results.
        Always recomputes missing/warnings.
        """
        flat = self.scenario.to_flat_dict()
        applied: dict = {}
        for key, value in (delta or {}).items():
            if key in _LIST_FIELDS:
                merged = (list(dict.fromkeys(list(value or []))) if replace_lists
                          else list(dict.fromkeys(list(flat.get(key) or []) + list(value or []))))
                if merged != list(flat.get(key) or []):
                    flat[key] = merged
                    applied[key] = merged
            elif value is not None and value != flat.get(key):
                flat[key] = value
                applied[key] = value

        if applied:
            self.scenario = SimulationScenario.from_flat_dict(flat)
            self.scenario.liquid_solid_ratio = self.scenario.computed_ls_ratio()

        for field_name in drop_assumption_fields or ():
            self.assumptions = [a for a in self.assumptions if a.field != field_name]
        current = self.scenario.to_flat_dict()
        for field_name, value, reason in assumption_specs or ():
            if current.get(field_name) is not None:     # only flag a field that is actually set
                self._set_assumption(field_name, value, reason)

        self.recompute_safety()
        return applied

    def merge_user_message(self, text: str) -> dict:
        """Deterministically merge a natural reply into the scenario (back-compat entry point).

        Equivalent to extracting the explicit field delta with the rule-based gating and applying
        it via :meth:`apply_delta`. The orchestrator's AI-first path computes the delta with
        :mod:`nlu_extractor` instead and calls :meth:`apply_delta` directly.
        """
        delta, temp_assumed = extract_explicit_delta(text)
        specs = ([("temperature_C", delta["temperature_C"],
                   "room temperature / ambient — assumed, no explicit value")]
                 if temp_assumed and "temperature_C" in delta else [])
        drop = ("temperature_C",) if ("temperature_C" in delta and not temp_assumed) else ()
        return self.apply_delta(delta, assumption_specs=specs, drop_assumption_fields=drop)

    def _set_assumption(self, field_name: str, value, reason: str) -> None:
        self.assumptions = [a for a in self.assumptions if a.field != field_name]
        self.assumptions.append(S.Assumption(field=field_name, assumed_value=value,
                                              reason=reason, source=S.SOURCE_RULE))

    def recompute_safety(self) -> None:
        """Recompute missing fields + warnings from the current scenario (deterministic)."""
        missing, warns = safety.analyze(self.scenario, assumptions=self.assumptions)
        self.missing_fields = missing
        self.warnings = warns
        self.scenario.warnings = warns

    # ----------------------------------------------------------------- #
    # Readiness helpers (used by the policy preconditions)
    # ----------------------------------------------------------------- #
    @property
    def has_scenario_core(self) -> bool:
        return (self.scenario.material.solid_mass_g is not None
                and self.scenario.leachant.liquid_volume_mL is not None
                and bool(self.scenario.leachant.leachant_type))

    @property
    def composition_usable(self) -> bool:
        mp = self.material_profile
        return bool(mp is not None and getattr(mp, "is_usable", False))

    @property
    def preview_runnable(self) -> bool:
        """A built preview whose composition is available (so a run is meaningful).

        Uses the stored ``preview_status`` string (set by the build tool) so this pure-data
        module never imports the builder/executor.
        """
        return self.preview is not None and self.preview_status in _RUNNABLE_PREVIEW_STATUSES

    @property
    def has_results(self) -> bool:
        return (self.batch_result is not None or self.execution_result is not None)

    @property
    def has_result_table(self) -> bool:
        rt = self.result_table
        return rt is not None and hasattr(rt, "empty") and not rt.empty

    # ----------------------------------------------------------------- #
    # Display cards
    # ----------------------------------------------------------------- #
    def summary_card(self) -> dict:
        """The extracted-experiment summary card (display)."""
        flat = self.scenario.to_flat_dict()
        keys = ("material_name", "leachant_type", "leachant_concentration_M", "solid_mass_g",
                "liquid_volume_mL", "liquid_solid_ratio", "time_min", "temperature_C",
                "CO2_condition", "target_elements", "desired_outputs")
        return {k: flat.get(k) for k in keys}

    def missing_card(self) -> list:
        """Missing-details card: [{field, label, severity, message}]."""
        return [{"field": m.field, "label": m.label, "severity": m.severity,
                 "message": m.message} for m in self.missing_fields]

    def domain_card(self) -> dict:
        return {"domain": self.domain, "domain_label": domains.label(self.domain),
                "engine": self.engine, "executable": domains.is_executable(self.domain)}

    # ----------------------------------------------------------------- #
    # Provenance (safe: no values, no secrets, no raw model text)
    # ----------------------------------------------------------------- #
    def transcript_summary(self) -> list:
        return [m.to_dict() for m in self.history]

    def action_trace(self) -> list:
        return [e.to_dict() for e in self.provenance]

    def confirmed_assumptions(self) -> list:
        return [{"field": a.field, "assumed_value": a.assumed_value, "reason": a.reason,
                 "source": a.source} for a in self.assumptions]

    def to_provenance_dict(self) -> dict:
        """A JSON-safe agent provenance bundle for a saved simulation run.

        Stores the **transcript summary** (user-visible messages only), the **action trace**,
        the **confirmed assumptions**, the domain/engine, and the standing not-validated
        warning. It deliberately stores **no raw model responses, no API key, and no measured
        data** (the agent never holds those).
        """
        mp_summary = None
        mp = self.material_profile
        if mp is not None:
            try:
                mp_summary = mp.summary()
            except Exception:                              # noqa: BLE001
                mp_summary = {"material_name": getattr(mp, "material_name", None),
                              "is_usable": getattr(mp, "is_usable", False)}
        # Council provenance is the DERIVED structured summary only (no raw model text), via the
        # duck-typed to_safe_dict() — so this pure module never imports the council/AI layer.
        council = None
        if self.last_council is not None:
            try:
                council = self.last_council.to_safe_dict()
            except Exception:                              # noqa: BLE001
                council = None
        return {
            "agent_assisted": True,
            "domain": self.domain,
            "domain_label": domains.label(self.domain),
            "engine": self.engine,
            "transcript_summary": self.transcript_summary(),
            "action_trace": self.action_trace(),
            "confirmed_assumptions": self.confirmed_assumptions(),
            "material_profile_summary": mp_summary,
            "release_model_status": self.release_model_status,
            "warnings": list(self.warnings),
            "council_review": council,
            "not_validated_warning": NOT_VALIDATED_WARNING,
        }


# --------------------------------------------------------------------------- #
# Explicit-field extraction (module-level so the orchestrator/tests can reuse it)
# --------------------------------------------------------------------------- #
def extract_explicit_delta(text: str) -> tuple[dict, bool]:
    """Return ``(delta, temperature_assumed)`` — only fields the reply explicitly states.

    Reuses the tested :mod:`rule_parser` extraction for the values, but gates each numeric
    field on a presence regex so a reply that does not mention (e.g.) temperature never sets
    one. ``temperature_assumed`` is True only when the reply says "room temperature/ambient"
    (an explicit value is not an assumption). Never raises, never invents.
    """
    s = str(text or "")
    if not s.strip():
        return {}, False
    parsed = rule_parser.parse(s).scenario.to_flat_dict()
    low = s.lower()
    delta: dict = {}

    if _HAS_MASS.search(s) and parsed.get("solid_mass_g") is not None:
        delta["solid_mass_g"] = parsed["solid_mass_g"]
    if _HAS_VOLUME.search(s) and parsed.get("liquid_volume_mL") is not None:
        delta["liquid_volume_mL"] = parsed["liquid_volume_mL"]
    if parsed.get("leachant_type"):
        delta["leachant_type"] = parsed["leachant_type"]
    if _HAS_MOLARITY.search(s) and parsed.get("leachant_concentration_M") is not None:
        delta["leachant_concentration_M"] = parsed["leachant_concentration_M"]
    if _HAS_TIME.search(s) and parsed.get("time_min") is not None:
        delta["time_min"] = parsed["time_min"]

    temp_assumed = False
    if _HAS_TEMP.search(s) and parsed.get("temperature_C") is not None:
        delta["temperature_C"] = parsed["temperature_C"]
    elif any(w in low for w in _ROOM_TEMP_WORDS) and parsed.get("temperature_C") is not None:
        delta["temperature_C"] = parsed["temperature_C"]
        temp_assumed = True

    if _HAS_FILTER.search(s) and parsed.get("filter_size_um") is not None:
        delta["filter_size_um"] = parsed["filter_size_um"]
    if parsed.get("CO2_condition"):
        delta["CO2_condition"] = parsed["CO2_condition"]
    if parsed.get("cover_condition"):
        delta["cover_condition"] = parsed["cover_condition"]
    if "centrifug" in low:
        delta["centrifuge_used"] = True
    if "filter" in low or "filtrat" in low:
        delta["filtration_used"] = True
    if parsed.get("material_name"):
        delta["material_name"] = parsed["material_name"]
        delta["material_type"] = parsed.get("material_type")
    if parsed.get("target_elements"):
        delta["target_elements"] = parsed["target_elements"]
    if parsed.get("desired_outputs"):
        delta["desired_outputs"] = parsed["desired_outputs"]
    return delta, temp_assumed
