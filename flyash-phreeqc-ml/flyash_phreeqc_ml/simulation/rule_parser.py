"""Rule-based (non-AI) fallback parser for the simulation planner.

Extracts the obvious values from a plain-language description with hand-written regexes —
no network, no AI. It is intentionally imperfect and **labels itself low-confidence**; it
exists so the planner works fully when AI is disabled or unavailable.
"""
from __future__ import annotations

import re

from . import safety
from .scenario_schema import (
    ASSUMED_TEMPERATURE_C,
    SOURCE_RULE,
    Assumption,
    ExperimentProcess,
    LeachantInput,
    MaterialInput,
    ScenarioParseResult,
    SimulationScenario,
    TargetOutputs,
)

# Cap rule-based confidence well below a clean AI extraction.
_MAX_RULE_CONFIDENCE = 0.55

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #
_MASS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|mg|grams?|g)\b", re.IGNORECASE)
_VOLUME_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(milliliters?|millilitres?|ml|µl|ul|liters?|litres?|l)\b",
    re.IGNORECASE)
# Molarity: an UPPERCASE 'M' not part of 'mM' / 'min' / 'mL' (case-sensitive on the M).
_MOLARITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*M(?![a-zA-Z])")
_TIME_MIN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|min)\b", re.IGNORECASE)
_TIME_HR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|hr|h)\b", re.IGNORECASE)
_TEMP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:°\s*|degrees?\s*)?C\b")
_FILTER_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:µm|um|micron|microns|micrometers?)\b",
                             re.IGNORECASE)

# Leachant name → canonical token.
_LEACHANTS = [
    ("naoh", "NaOH"), ("sodium hydroxide", "NaOH"),
    ("koh", "KOH"), ("potassium hydroxide", "KOH"),
    ("hcl", "HCl"), ("hydrochloric", "HCl"),
    ("h2so4", "H2SO4"), ("sulfuric", "H2SO4"), ("sulphuric", "H2SO4"),
    ("hno3", "HNO3"), ("nitric", "HNO3"),
    ("acetic", "acetic acid"), ("citric", "citric acid"),
]

# Element full names → symbol.
_ELEMENT_NAMES = {
    "calcium": "Ca", "silicon": "Si", "silica": "Si", "silicate": "Si",
    "aluminium": "Al", "aluminum": "Al", "alumina": "Al",
    "iron": "Fe", "sodium": "Na", "potassium": "K", "scandium": "Sc",
    "rare earth": "REE", "rare-earth": "REE", "rees": "REE", "ree": "REE",
}
_ELEMENT_SYMBOLS = ("Ca", "Si", "Al", "Fe", "Na", "K", "Sc", "REE")
_ELEMENT_ORDER = ("Ca", "Si", "Al", "Fe", "Na", "K", "Sc", "REE")


def _mass_g(text):
    m = _MASS_RE.search(text)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2).lower()
    if unit == "kg":
        return val * 1000.0
    if unit == "mg":
        return val / 1000.0
    return val


def _volume_mL(text):
    m = _VOLUME_RE.search(text)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2).lower()
    if unit in ("µl", "ul"):
        return val / 1000.0
    if unit in ("l", "liter", "liters", "litre", "litres"):
        return val * 1000.0
    return val


def _leachant(text):
    low = text.lower()
    leachant = None
    for needle, canon in _LEACHANTS:
        if needle in low:
            leachant = canon
            break
    if leachant is None and safety.is_water_leachant(low):
        leachant = "water"
    conc = None
    mm = _MOLARITY_RE.search(text)
    if mm:
        conc = float(mm.group(1))
    # water has no molar concentration
    if leachant == "water":
        conc = None
    return leachant, conc


def _time_min(text):
    mm = _TIME_MIN_RE.search(text)
    if mm:
        return float(mm.group(1))
    hh = _TIME_HR_RE.search(text)
    if hh:
        return float(hh.group(1)) * 60.0
    return None


def _temperature(text):
    """Return ``(temperature_C, assumed)``."""
    m = _TEMP_RE.search(text)
    if m:
        return float(m.group(1)), False
    low = text.lower()
    if any(k in low for k in ("room temperature", "room temp", "ambient", " rt ", "(rt)")):
        return ASSUMED_TEMPERATURE_C, True
    # No temperature stated at all — a simulation needs one; assume ambient + flag it.
    return ASSUMED_TEMPERATURE_C, True


def _co2_cover(text):
    """Return ``(CO2_condition, cover_condition, warning_or_None)``."""
    low = text.lower()
    if "plastic flap" in low or re.search(r"\bpf\b", text):
        return "PF", "plastic_flap", None
    if "glass cover" in low or "glass lid" in low or re.search(r"\bgs\b", text):
        return "GS", "glass_cover", None
    if any(k in low for k in ("open air", "open to air", "open to the air",
                              "open to atmosphere", "atmospheric", "open-air")):
        return "OA", "open_air", None
    if any(k in low for k in ("sealed", "airtight", "air-tight", "hermetic")):
        return (None, None,
                "Description mentions a sealed/airtight container, but PF/GS covers are not "
                "confirmed airtight — the planner does not assume 'sealed'. Specify the actual "
                "cover (OA = open air, PF = plastic flap, GS = glass cover).")
    if any(k in low for k in ("covered", "lid", "capped", "closed cup")):
        return (None, None,
                "Description mentions the cup was covered, but the cover type is ambiguous — "
                "specify OA / PF / GS so the CO2 exposure is unambiguous.")
    return None, None, None


def _filter_size(text):
    m = _FILTER_SIZE_RE.search(text)
    return float(m.group(1)) if m else None


def _elements(text):
    found = []
    low = text.lower()
    for name, sym in _ELEMENT_NAMES.items():
        if name in low and sym not in found:
            found.append(sym)
    for sym in _ELEMENT_SYMBOLS:
        if re.search(r"\b" + re.escape(sym) + r"\b", text) and sym not in found:
            found.append(sym)
    return [s for s in _ELEMENT_ORDER if s in found]


def _desired_outputs(text):
    low = text.lower()
    out = []
    if any(k in low for k in ("in the liquid", "in solution", "liquid", "released", "release",
                              "dissolved", "leach", "leached", "solution")):
        out.append("liquid_composition")
    if any(k in low for k in ("precipit", "what may have precipitated", "solid phase",
                              "retain", "retention")):
        out.append("precipitated_phases")
    if re.search(r"\bph\b", low):
        out.append("pH")
    if "saturation" in low:
        out.append("saturation_indices")
    if "mass balance" in low or "recovery" in low:
        out.append("mass_balance")
    # de-dup, preserve order
    seen, ordered = set(), []
    for o in out:
        if o not in seen:
            seen.add(o)
            ordered.append(o)
    return ordered


def _material(text):
    low = text.lower()
    if "class c" in low and ("fly ash" in low or "flyash" in low):
        return "Class C fly ash", "class_c_fly_ash"
    if "class f" in low and ("fly ash" in low or "flyash" in low):
        return "Class F fly ash", "class_f_fly_ash"
    if "fly ash" in low or "flyash" in low:
        return "fly ash", "fly_ash"
    if "red mud" in low or "bauxite residue" in low:
        return "red mud", "red_mud"
    return None, None


def _confidence(scenario: SimulationScenario, is_water: bool) -> float:
    core = [scenario.material.solid_mass_g, scenario.leachant.liquid_volume_mL,
            scenario.leachant.leachant_type, scenario.process.time_min]
    found = sum(1 for x in core if x is not None)
    if (scenario.leachant.leachant_concentration_M is not None) or is_water:
        found += 1
    if scenario.outputs.target_elements:
        found += 1
    return round(min(_MAX_RULE_CONFIDENCE, _MAX_RULE_CONFIDENCE * found / 6.0), 2)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def parse(description: str, desired_outputs: str = "", *, profile=None) -> ScenarioParseResult:
    """Parse a description into a :class:`ScenarioParseResult` (source = ``rule``)."""
    text = f"{description or ''}\n{desired_outputs or ''}".strip()
    assumptions: list = []

    solid_mass_g = _mass_g(text)
    liquid_volume_mL = _volume_mL(text)
    leachant_type, conc = _leachant(text)
    time_min = _time_min(text)
    temperature_C, temp_assumed = _temperature(text)
    if temp_assumed:
        assumptions.append(Assumption(
            "temperature_C", temperature_C,
            "room temperature / no explicit value — assumed ambient", SOURCE_RULE))
    co2, cover, cover_warn = _co2_cover(text)
    low = text.lower()
    centrifuge = True if "centrifug" in low else None
    filtration = True if ("filter" in low or "filtrat" in low) else None
    filter_size = _filter_size(text)
    elements = _elements(text)
    outputs_list = _desired_outputs(text)
    material_name, material_type = _material(text)

    scenario = SimulationScenario(
        material=MaterialInput(material_name, material_type, solid_mass_g),
        leachant=LeachantInput(leachant_type, conc, liquid_volume_mL, None),
        process=ExperimentProcess(time_min, temperature_C, co2, cover,
                                  centrifuge, filtration, filter_size),
        outputs=TargetOutputs(elements, outputs_list, None),
    )
    scenario.liquid_solid_ratio = scenario.computed_ls_ratio()
    scenario.confidence = _confidence(scenario, safety.is_water_leachant(leachant_type))

    missing, warns = safety.analyze(scenario, assumptions=assumptions)
    if cover_warn:
        warns = warns + [cover_warn]
    scenario.warnings = warns

    return ScenarioParseResult(
        scenario=scenario, source=SOURCE_RULE, ok=True, error=None,
        missing=missing, assumptions=assumptions, warnings=warns,
        confidence=scenario.confidence)
