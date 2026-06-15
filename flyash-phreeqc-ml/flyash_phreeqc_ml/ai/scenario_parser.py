"""AI extractor for the natural-language simulation planner (suggestion-only).

Turns a free-text experiment description into a structured :class:`SimulationScenario`
using the shared, key-safe AI client. It is built so that:

* **The AI only extracts.** It forces strict JSON and the parser validates it. Invalid
  output returns a *controlled* parse error and never crashes the app.
* **It never runs PHREEQC, never overwrites data, never becomes verified data.** The
  result is a suggestion the user reviews and confirms in the UI.
* **The scientific caveats come from code, not the AI.** Missing-field and safety warnings
  are computed by :mod:`flyash_phreeqc_ml.simulation.safety`; AI-suggested warnings are
  merged in but can never replace them.
* **Graceful fallback.** When AI is disabled or returns invalid output, the planner falls
  back to the deterministic :mod:`flyash_phreeqc_ml.simulation.rule_parser`.

This module lives in ``ai/`` (it uses the AI client) but is **not** on the scientific
result path — pinned by ``tests/test_ai_boundary.py``.
"""
from __future__ import annotations

from ..simulation import rule_parser, safety
from ..simulation import scenario_schema as S
from ..simulation.scenario_schema import (
    Assumption,
    ExperimentProcess,
    LeachantInput,
    MaterialInput,
    ScenarioParseResult,
    SimulationScenario,
    TargetOutputs,
)
from . import client as ai_client
from . import config as ai_config
from .import_assist import _message_text, _parse_json   # reuse the tested defensive helpers

__all__ = ["parse_scenario", "parse_with_ai", "SYSTEM_PROMPT",
           "SCENARIO_DATA_NOTICE", "SCENARIO_CONSENT_LABEL"]

MAX_TOKENS = 1500

# Per-session consent (same spirit as the other AI features).
SCENARIO_DATA_NOTICE = (
    "This sends your experiment description (and desired outputs) to the Anthropic API to "
    "extract a structured scenario — data leaves this machine for this feature only. The "
    "extracted scenario is a suggestion you review and confirm; it never runs PHREEQC, "
    "overwrites measured data, or becomes verified data."
)
SCENARIO_CONSENT_LABEL = (
    "I understand and allow sending my experiment description to the API to extract a scenario."
)


# --------------------------------------------------------------------------- #
# System prompt (forces strict JSON; extraction only)
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = f"""\
You convert a free-text geochemistry batch-leaching experiment description into a STRICT
JSON scenario for a simulation PLANNER. You ONLY extract what the user states or clearly
implies. You NEVER invent numbers, NEVER guess a composition, and NEVER run anything.

Rules:
1. Extract only stated/implied values. Use null for anything not given. Do not fabricate a
   concentration, mass, volume, time, or temperature that is not in the text.
2. Units: convert to grams (solid_mass_g), millilitres (liquid_volume_mL), minutes
   (time_min), Celsius (temperature_C), molar (leachant_concentration_M). State the
   conversion only via the converted number; do not output units.
3. CO2_condition must be one of {list(S.CO2_CONDITION_ALLOWED)} or null. Map "open air" ->
   "OA", "plastic flap" -> "PF", "glass cover" -> "GS". NEVER output "sealed" — if the text
   says sealed/airtight, set CO2_condition null and add a warning that PF/GS are not
   confirmed airtight.
4. target_elements: chemical element symbols among {list(S.RECOGNIZED_ELEMENTS)}.
   desired_outputs: any of {list(S.DESIRED_OUTPUTS_VOCAB)} (e.g. liquid_composition,
   precipitated_phases, pH).
5. If the user asks about precipitated/retained solids, add a warning that precipitation
   cannot be proven from liquid data alone.
6. confidence is your 0..1 confidence in the extraction.

Respond with ONLY this JSON object (no prose, no code fences):
{{"material": {{"material_name": str|null, "material_type": str|null, "solid_mass_g": number|null}},
 "leachant": {{"leachant_type": str|null, "leachant_concentration_M": number|null,
              "liquid_volume_mL": number|null, "pH_initial": number|null}},
 "process": {{"time_min": number|null, "temperature_C": number|null,
             "CO2_condition": str|null, "cover_condition": str|null,
             "centrifuge_used": bool|null, "filtration_used": bool|null,
             "filter_size_um": number|null}},
 "outputs": {{"target_elements": [str], "desired_outputs": [str], "notes": str|null}},
 "assumptions": [{{"field": str, "assumed_value": any, "reason": str}}],
 "warnings": [str],
 "confidence": number}}
"""


def _user_prompt(description: str, desired_outputs: str = "") -> str:
    lines = ["Extract a structured scenario from this experiment description.", "",
             f"Description: {description or ''}"]
    if desired_outputs:
        lines.append(f"Desired variables / outputs: {desired_outputs}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Defensive payload → scenario
# --------------------------------------------------------------------------- #
def _clamp_co2(value):
    v = S.as_str(value)
    if v is None:
        return None
    return v if v in S.CO2_CONDITION_ALLOWED else None


def _clamp_cover(value):
    v = S.as_str(value)
    if v is None:
        return None
    return v if v in S.COVER_CONDITIONS else None


def _elements(value):
    out = []
    for el in S.as_str_list(value):
        norm = "REE" if el.upper() == "REE" else el[:1].upper() + el[1:].lower() if len(el) > 1 else el.upper()
        if norm in S.RECOGNIZED_ELEMENTS and norm not in out:
            out.append(norm)
        elif el not in out and norm not in S.RECOGNIZED_ELEMENTS:
            out.append(el)            # keep an unrecognised token rather than silently dropping
    return out


def _scenario_from_payload(payload: dict):
    """Build a :class:`SimulationScenario` + assumptions from a validated AI payload."""
    mat = payload.get("material") or {}
    lea = payload.get("leachant") or {}
    proc = payload.get("process") or {}
    outs = payload.get("outputs") or {}

    scenario = SimulationScenario(
        material=MaterialInput(
            material_name=S.as_str(mat.get("material_name")),
            material_type=S.as_str(mat.get("material_type")),
            solid_mass_g=S.as_float(mat.get("solid_mass_g"))),
        leachant=LeachantInput(
            leachant_type=S.as_str(lea.get("leachant_type")),
            leachant_concentration_M=S.as_float(lea.get("leachant_concentration_M")),
            liquid_volume_mL=S.as_float(lea.get("liquid_volume_mL")),
            pH_initial=S.as_float(lea.get("pH_initial"))),
        process=ExperimentProcess(
            time_min=S.as_float(proc.get("time_min")),
            temperature_C=S.as_float(proc.get("temperature_C")),
            CO2_condition=_clamp_co2(proc.get("CO2_condition")),
            cover_condition=_clamp_cover(proc.get("cover_condition")),
            centrifuge_used=S.as_bool(proc.get("centrifuge_used")),
            filtration_used=S.as_bool(proc.get("filtration_used")),
            filter_size_um=S.as_float(proc.get("filter_size_um"))),
        outputs=TargetOutputs(
            target_elements=_elements(outs.get("target_elements")),
            desired_outputs=S.as_str_list(outs.get("desired_outputs")),
            notes=S.as_str(outs.get("notes"))),
    )

    assumptions = []
    for a in (payload.get("assumptions") or []):
        if isinstance(a, dict) and S.as_str(a.get("field")):
            assumptions.append(Assumption(
                field=S.as_str(a.get("field")),
                assumed_value=a.get("assumed_value"),
                reason=S.as_str(a.get("reason")) or "",
                source=S.SOURCE_AI))
    return scenario, assumptions


def _clamp_confidence(value):
    f = S.as_float(value)
    if f is None:
        return 0.0
    return max(0.0, min(1.0, f))


def _merge_warnings(code_warnings, ai_payload):
    out = list(code_warnings)
    for w in (ai_payload.get("warnings") or []):
        s = str(w).strip()
        if s and s not in out:
            out.append(s)
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def parse_with_ai(description: str, desired_outputs: str = "", *,
                  client=None, model=None, profile=None) -> ScenarioParseResult:
    """Extract a scenario via the AI client. Returns ``ok=False`` (never raises) when the
    AI is disabled or returns invalid output."""
    resolved = ai_client.get_client(client, model=model)
    if not resolved.ok or resolved.client is None:
        return ScenarioParseResult(
            scenario=SimulationScenario(), source=S.SOURCE_AI, ok=False,
            error=resolved.message or "AI is disabled.", confidence=0.0)

    try:
        resp = resolved.client.messages.create(
            model=ai_config.resolve_model(model), max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _user_prompt(description, desired_outputs)}])
    except Exception as exc:    # never leak details; only the type
        return ScenarioParseResult(
            scenario=SimulationScenario(), source=S.SOURCE_AI, ok=False,
            error=f"AI request failed ({type(exc).__name__}).", confidence=0.0)

    raw = _message_text(resp)
    payload = _parse_json(raw)
    if not isinstance(payload, dict):
        return ScenarioParseResult(
            scenario=SimulationScenario(), source=S.SOURCE_AI, ok=False,
            error="AI returned a response that was not valid scenario JSON.",
            confidence=0.0, raw_response=raw)

    scenario, assumptions = _scenario_from_payload(payload)
    scenario.liquid_solid_ratio = scenario.computed_ls_ratio()
    scenario.confidence = _clamp_confidence(payload.get("confidence"))

    missing, code_warnings = safety.analyze(scenario, assumptions=assumptions)
    warnings = _merge_warnings(code_warnings, payload)
    scenario.warnings = warnings

    return ScenarioParseResult(
        scenario=scenario, source=S.SOURCE_AI, ok=True, error=None,
        missing=missing, assumptions=assumptions, warnings=warnings,
        confidence=scenario.confidence, raw_response=raw)


def parse_scenario(description: str, desired_outputs: str = "", *,
                   client=None, model=None, prefer_ai: bool = True,
                   profile=None) -> ScenarioParseResult:
    """Parse a description into a scenario, using AI when available and confirmed, else the
    rule-based fallback.

    * ``prefer_ai`` + an enabled/injected client → try AI; on success return it.
    * AI disabled / not preferred → rule-based parsing (``source = rule``).
    * AI returned invalid output → controlled fallback to rules (``source = rule_fallback``),
      with the AI error surfaced as a warning. Never raises.
    """
    ai_available = (client is not None) or ai_config.is_enabled()
    if prefer_ai and ai_available:
        res = parse_with_ai(description, desired_outputs, client=client, model=model, profile=profile)
        if res.ok:
            return res
        # Controlled fallback — keep the app working, surface the AI error.
        fb = rule_parser.parse(description, desired_outputs, profile=profile)
        fb.source = S.SOURCE_RULE_FALLBACK
        fb.error = res.error
        note = f"AI extraction unavailable ({res.error}) — used rule-based fallback (low confidence)."
        fb.warnings = [note] + [w for w in fb.warnings if w != note]
        fb.scenario.warnings = fb.warnings
        fb.raw_response = res.raw_response
        return fb

    return rule_parser.parse(description, desired_outputs, profile=profile)
