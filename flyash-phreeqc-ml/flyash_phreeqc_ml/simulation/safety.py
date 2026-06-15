"""Deterministic scientific safety analysis for a parsed scenario.

Crucially, the **scientific** warnings (missing fields, the precipitation caveat, leachant
support, the composition gap) are computed *here, in code* — **not** by the AI. The AI may
only suggest extra warnings; these canonical ones are always added by deterministic rules,
so AI output can never weaken a scientific caveat.
"""
from __future__ import annotations

from .scenario_schema import (
    PRECIPITATION_CAVEAT,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    TEMPLATE_SUPPORTED_LEACHANTS,
    MissingInput,
    SimulationScenario,
)

# Leachant strings treated as water leaching (no molar concentration expected).
WATER_LEACHANTS = ("water", "di water", "deionized water", "deionised water", "h2o",
                   "milliq", "milli-q", "ultrapure water", "distilled water")

# Keywords that mean the user is asking about precipitated / retained solids.
_PRECIP_KEYWORDS = ("precipit", "retain", "retention", "solid phase", "solids",
                    "precipitated_phases", "mass_balance")


def is_water_leachant(leachant) -> bool:
    s = str(leachant or "").strip().lower()
    return any(w in s for w in WATER_LEACHANTS)


def asks_about_precipitation(scenario: SimulationScenario) -> bool:
    blob = " ".join([
        " ".join(str(o) for o in scenario.outputs.desired_outputs),
        str(scenario.outputs.notes or ""),
        str(scenario.notes or ""),
    ]).lower()
    return any(k in blob for k in _PRECIP_KEYWORDS)


def required_missing(scenario: SimulationScenario) -> list:
    """Required-input gaps as :class:`MissingInput` (deterministic)."""
    out: list = []
    if scenario.material.solid_mass_g is None:
        out.append(MissingInput("solid_mass_g", "Solid mass", SEVERITY_ERROR,
                                "Solid mass is missing — needed for amounts and the L/S ratio."))
    if scenario.leachant.liquid_volume_mL is None:
        out.append(MissingInput("liquid_volume_mL", "Liquid volume", SEVERITY_ERROR,
                                "Liquid volume is missing — needed for amounts and the L/S ratio."))
    if not is_water_leachant(scenario.leachant.leachant_type):
        if scenario.leachant.leachant_concentration_M is None:
            out.append(MissingInput(
                "leachant_concentration_M", "Leachant concentration", SEVERITY_WARNING,
                "Leachant concentration (M) is missing — required for a non-water leachant."))
    if not (scenario.material.material_name or scenario.material.material_type):
        out.append(MissingInput("material_name", "Material", SEVERITY_WARNING,
                                "Material type is not identified."))
    return out


def scientific_warnings(scenario: SimulationScenario, *, assumptions=None) -> list:
    """The canonical scientific caveats for this scenario (always code-generated)."""
    warns: list = []

    # Material composition is never part of an NL description — PHREEQC needs the assay.
    warns.append(
        "Material composition (the dissolved element assay the deterministic model needs) "
        "is not part of a text description — supply it from the material profile or a "
        "measured / literature-confirmed assay before running.")

    # Temperature assumed (when a parser filled it as an assumption).
    assumed_fields = {a.field for a in (assumptions or [])}
    if "temperature_C" in assumed_fields:
        temp = scenario.process.temperature_C
        warns.append(f"Temperature was assumed ({temp:g} °C) — no explicit value was given."
                     if temp is not None else "Temperature was assumed — no explicit value was given.")

    # Leachant the on-demand PHREEQC template cannot yet build.
    lt = (scenario.leachant.leachant_type or "").strip()
    if lt and lt not in TEMPLATE_SUPPORTED_LEACHANTS:
        warns.append(
            f"PHREEQC template may not support this leachant yet (currently: "
            f"{', '.join(TEMPLATE_SUPPORTED_LEACHANTS)}). '{lt}' is recorded in the plan, "
            "but the on-demand PHREEQC generator templates NaOH activation only.")

    # Precipitation / retention cannot be concluded from liquid data alone.
    if asks_about_precipitation(scenario):
        warns.append(PRECIPITATION_CAVEAT)

    return warns


def analyze(scenario: SimulationScenario, *, assumptions=None):
    """Return ``(missing, warnings)`` for a scenario.

    ``missing`` is a list of :class:`MissingInput`; ``warnings`` is a de-duplicated list of
    strings combining the missing-field messages and the scientific caveats.
    """
    missing = required_missing(scenario)
    warns = [m.message for m in missing] + scientific_warnings(scenario, assumptions=assumptions)
    # de-dup, preserve order
    seen, out = set(), []
    for w in warns:
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return missing, out


# Re-export for callers that want to label severities.
__all__ = ["analyze", "required_missing", "scientific_warnings", "is_water_leachant",
           "asks_about_precipitation", "WATER_LEACHANTS",
           "SEVERITY_ERROR", "SEVERITY_WARNING", "SEVERITY_INFO"]
