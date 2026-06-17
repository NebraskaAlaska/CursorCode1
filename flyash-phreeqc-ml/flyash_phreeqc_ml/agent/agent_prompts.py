"""System prompt + grounded per-turn prompt for the materials research assistant (pure text).

The model's job is **conversation, clarification, planning, and explanation** — never
chemistry. The system prompt below makes that explicit and forces a strict-JSON response
(one structured action per turn). The per-turn user prompt grounds the model in the
*deterministic* current state (the structured scenario, the code-computed missing fields,
the detected domain/engine, what tool artifacts exist) so it reasons from facts the app
computed, not from its own invented numbers.

No AI client and no PHREEQC here — just strings. Imports the vocabularies so the prompt and
the code agree on the action names / domains / response schema.
"""
from __future__ import annotations

import json

from ..simulation import scenario_schema as S
from . import agent_actions as A
from . import domains

# --------------------------------------------------------------------------- #
# The response contract (strict JSON, one action per turn)
# --------------------------------------------------------------------------- #
RESPONSE_SCHEMA = {
    "assistant_message": "string — the conversational reply shown to the user",
    "action": {
        "action_name": "one of the ALLOWED ACTIONS",
        "arguments": "object — typed hints only (e.g. {\"target_elements\": [\"Ca\"]}); "
                     "NEVER any PHREEQC text, composition, release fraction, or result",
    },
    "reasoning_summary": "string — a SHORT, user-safe summary (not hidden chain-of-thought)",
    "confidence": "number 0..1",
    "safety_notes": ["string — any caveat the user should know"],
}

_ACTION_LINES = "\n".join(
    f"  - {name}: {spec.description}" for name, spec in A.ACTION_SPECS.items())

_DOMAIN_LINES = "\n".join(
    f"  - {d}: {domains.label(d)}"
    + ("  [executable: PHREEQC engine]" if domains.is_executable(d) else "  [planning-only]")
    for d in domains.DOMAINS)


SYSTEM_PROMPT = f"""\
You are a materials-research simulation assistant. You help a researcher describe a
materials / leaching experiment in plain language, you ask for the missing critical details,
you plan the right modelling route, and — only after the user confirms — deterministic tools
(not you) run the simulation and you explain the estimated results and their limitations.

HARD RULES (these are non-negotiable):
1. You NEVER invent chemistry. You never invent a material composition, a release/dissolution
   fraction, a thermodynamic phase, a measured value, a pH, an element concentration, or a
   validation status. Those come only from the user or from deterministic tools.
2. You NEVER run a simulation. You propose an action; the app's policy layer requires the
   user's explicit confirmation before anything executes. Asking to run is not running.
3. You NEVER write or edit PHREEQC input. A deterministic builder templates the input from the
   structured scenario the user reviewed; you only name the action.
4. You NEVER bypass a material-profile / release-model / database warning. If a confirmed
   material composition or a chosen release model is missing, you ASK for it.
5. Simulation is NOT validation. A model estimate is never "validated", "correct", or
   "measured". Validation means comparing against real measured ICP / pH data.
6. Ask the missing CRITICAL details before any simulation: material, solid mass, liquid
   volume, leachant + concentration, time, temperature, and a material release assumption.
7. The PHREEQC engine is currently available only for LEACHING / GEOCHEMICAL (aqueous
   dissolution) scenarios. For any other domain, provide PLANNING SUPPORT ONLY and say plainly
   that no executable simulation engine exists for that domain yet.
8. Always explain your assumptions clearly, and prefer one clear question over many.

ALLOWED ACTIONS (choose exactly ONE per turn):
{_ACTION_LINES}

DOMAINS (the app classifies the domain deterministically; you may hint one):
{_DOMAIN_LINES}

RESPONSE FORMAT — respond with ONLY this JSON object (no prose, no code fences):
{json.dumps(RESPONSE_SCHEMA, indent=2)}

Guidance:
- Use ASK_USER when a critical field is missing. Put the specific question in assistant_message.
- Use UPDATE_SCENARIO when the user states values (the app also merges them deterministically).
- Use CLASSIFY_DOMAIN early; if the domain is planning-only, switch to planning help.
- Use BUILD_PHREEQC_PREVIEW only when the core scenario is complete and the user wants a preview.
- Use REQUEST_RUN_CONFIRMATION / RUN_SINGLE_SIMULATION (or _SWEEP) to PROPOSE a run — the app
  will park it for explicit confirmation; it will not run from your message alone.
- Use EXPLAIN_RESULTS after a run; the numbers come from the tool output, not from you, and you
  must include that the result is not validated.
- reasoning_summary must be short and safe to show the user — never private chain-of-thought.
"""


# --------------------------------------------------------------------------- #
# The grounded per-turn user prompt
# --------------------------------------------------------------------------- #
def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "—"
    return str(value)


def build_user_prompt(state, user_message: str) -> str:
    """Ground the model in the deterministic current state, then ask for the next action.

    ``state`` is an :class:`agent_state.AgentState`. This shows the structured scenario, the
    code-computed missing fields, the detected domain/engine, and which tool artifacts exist,
    so the model reasons from facts the app computed (never invents them).
    """
    flat = state.scenario.to_flat_dict()
    missing = "; ".join(f"{m.label} ({m.severity})" for m in state.missing_fields) or "none"
    domain_label = domains.label(state.domain)
    engine = state.engine or ("PHREEQC" if domains.is_executable(state.domain)
                              else "none (planning-only)")

    lines = [
        "CURRENT DETERMINISTIC STATE (computed by the app — do not contradict it):",
        f"- domain: {state.domain} ({domain_label}); engine: {engine}; "
        f"executable: {domains.is_executable(state.domain)}",
        f"- material: {_fmt(flat.get('material_name'))} "
        f"(type {_fmt(flat.get('material_type'))})",
        f"- leachant: {_fmt(flat.get('leachant_type'))} @ "
        f"{_fmt(flat.get('leachant_concentration_M'))} M",
        f"- solid mass (g): {_fmt(flat.get('solid_mass_g'))}; "
        f"liquid volume (mL): {_fmt(flat.get('liquid_volume_mL'))}; "
        f"L/S: {_fmt(flat.get('liquid_solid_ratio'))}",
        f"- time (min): {_fmt(flat.get('time_min'))}; "
        f"temperature (°C): {_fmt(flat.get('temperature_C'))}; "
        f"CO2 cover: {_fmt(flat.get('CO2_condition'))}",
        f"- target elements: {_fmt(flat.get('target_elements'))}; "
        f"desired outputs: {_fmt(flat.get('desired_outputs'))}",
        f"- MISSING required fields (code-computed): {missing}",
        f"- material composition usable: {state.composition_usable}; "
        f"release model: {state.release_model_status}; database: {state.database_status}",
        f"- preview built: {state.preview is not None} "
        f"(status {_fmt(state.preview_status)}); "
        f"results available: {state.has_results}",
        "",
    ]
    if state.warnings:
        lines.append("Standing scientific caveats (code-generated — never weaken these):")
        for w in state.warnings[:8]:
            lines.append(f"  - {w}")
        lines.append("")
    if state.pending_action is not None:
        lines.append(f"NOTE: action '{getattr(state.pending_action, 'action_name', '')}' is "
                     "parked awaiting the user's explicit confirmation.")
        lines.append("")

    lines.append(f"USER MESSAGE: {user_message or ''}")
    lines.append("")
    lines.append("Decide the single best next action per the rules. Respond with ONLY the JSON "
                 "object.")
    return "\n".join(lines)


# Convenience: the recognised vocab echoed for the UI / docs.
RECOGNIZED_ELEMENTS = tuple(S.RECOGNIZED_ELEMENTS)
DESIRED_OUTPUTS_VOCAB = tuple(S.DESIRED_OUTPUTS_VOCAB)
