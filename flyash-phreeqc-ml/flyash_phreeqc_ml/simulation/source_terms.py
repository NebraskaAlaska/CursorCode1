"""Material **source-term / dissolution** layer for PHREEQC input previews.

A confirmed material profile gives the *bulk* assay (e.g. 19.4 wt% CaO). That alone tells
PHREEQC nothing — the solid has to be *introduced into the reaction system* before any
material-derived element can be predicted. This module converts a confirmed profile + a
**user-chosen, reviewable release model** into deterministic PHREEQC source-term blocks.

It is deliberately conservative and honest:

* **It never invents a dissolution extent** and **never assumes 100% release** — the default
  is :data:`MODE_NONE` (no material chemistry introduced, with a warning).
* Release fractions are treated as **user assumptions, not measured truth**; literature /
  measured fractions are only used when the user explicitly **confirms** them.
* It is **pure + deterministic** — no AI (it imports none), no execution, no result-path /
  comparison module. It just templates text and reports the arithmetic.

Chemistry (validated against real PHREEQC 3.8.6): released elements are added as their
**oxides** through a ``REACTION`` block (``CaO`` etc.), so each addition is charge-safe
(oxide + water → dissolved ions), and the SOLUTION's ``-water`` is set to the actual liquid
volume so released *moles* map to the correct dissolved *concentration* (the real L/S).
This is an **equilibrium** source term — **not** a kinetic dissolution model.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..materials.profile_schema import atomic_weight

# --------------------------------------------------------------------------- #
# Modes + statuses
# --------------------------------------------------------------------------- #
MODE_NONE = "no_material_release"                       # A — default safe behaviour
MODE_GLOBAL = "user_defined_release_fraction"           # B — user fractions (global/per-element)
MODE_LITERATURE = "literature_or_measured_release_fraction"  # C — same maths, sourced + confirmed
MODE_MEASURED_LIQUID = "experimental_measured_liquid_composition"  # D — measured liquid input
MODES = (MODE_NONE, MODE_GLOBAL, MODE_LITERATURE, MODE_MEASURED_LIQUID)

STATUS_NO_RELEASE = "no_release"
STATUS_RELEASE_INCLUDED = "release_included"
STATUS_MEASURED_LIQUID = "measured_liquid"
STATUS_BLOCKED = "blocked"

NON_KINETIC_NOTE = "This is an equilibrium source term, NOT a kinetic dissolution model."

# Charge-safe vehicle for adding an element via REACTION: (oxide formula, element atoms/oxide).
ELEMENT_OXIDE_VEHICLE = {
    "Ca": ("CaO", 1), "Si": ("SiO2", 1), "Al": ("Al2O3", 2), "Fe": ("Fe2O3", 2),
    "Mg": ("MgO", 1), "Na": ("Na2O", 2), "K": ("K2O", 2), "S": ("SO3", 1),
    "Ti": ("TiO2", 1), "Mn": ("MnO", 1), "P": ("P2O5", 2), "Cr": ("Cr2O3", 2),
    "Sr": ("SrO", 1), "Ba": ("BaO", 1), "V": ("V2O5", 2),
}


# --------------------------------------------------------------------------- #
# Structures
# --------------------------------------------------------------------------- #
@dataclass
class ElementReleaseFraction:
    """A release fraction (0–1) for one element, with where it came from."""

    element: str
    fraction: float
    source: str = "assumed"          # "assumed" / "literature" / "measured"


@dataclass
class ReleasedElement:
    """The deterministic per-element conversion result."""

    element: str
    fraction: float
    grams_in_solid: float
    moles_total: float               # moles of the element in the whole solid sample
    moles_released: float            # moles actually released (× fraction)
    oxide: str | None
    oxide_moles: float | None        # moles of the oxide vehicle for the REACTION block
    concentration_mM: float | None   # resulting dissolved conc (needs liquid volume)
    source: str = "assumed"


@dataclass
class SourceTermWarning:
    code: str
    message: str


@dataclass
class DissolutionModel:
    """What the user chose: a mode + the fractions / measured values it needs."""

    mode: str = MODE_NONE
    global_fraction: float | None = None              # mode B/C, applied to every element
    per_element: dict = field(default_factory=dict)   # {element: fraction} overrides
    measured_liquid_mM: dict = field(default_factory=dict)   # mode D: {element: mM}
    provenance: str | None = None                     # literature/measured source label (C)
    confirmed: bool = False                           # mode C requires explicit confirmation
    allow_over_unity: bool = False                    # permit a >100% fraction (nonphysical)


@dataclass
class SourceTermResult:
    mode: str
    status: str = STATUS_NO_RELEASE
    released: list = field(default_factory=list)              # list[ReleasedElement]
    reaction_lines: list = field(default_factory=list)        # PHREEQC REACTION block (B/C)
    solution_extra_lines: list = field(default_factory=list)  # extra SOLUTION lines (D)
    solution_water_kg: float | None = None                    # SOLUTION -water (the L/S)
    assumptions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)              # list[SourceTermWarning]
    included_elements: list = field(default_factory=list)

    @property
    def has_source_terms(self) -> bool:
        return bool(self.reaction_lines) or bool(self.solution_extra_lines)

    def warning_messages(self) -> list:
        return [w.message for w in self.warnings]


# --------------------------------------------------------------------------- #
# Public: compute the source terms (pure, deterministic, never raises)
# --------------------------------------------------------------------------- #
def compute_source_terms(model, *, material_profile=None, solid_mass_g=None,
                         liquid_volume_mL=None, target_elements=None) -> SourceTermResult:
    """Convert a release model + confirmed profile into PHREEQC source-term blocks.

    Never raises. Returns a :class:`SourceTermResult` whose ``status`` is ``no_release`` (the
    safe default), ``release_included``, ``measured_liquid``, or ``blocked`` (with a warning
    explaining why). It **never invents** a dissolution extent and **never** defaults to 100%.
    """
    mode = getattr(model, "mode", MODE_NONE) if model is not None else MODE_NONE
    res = SourceTermResult(mode=mode)

    if model is None or mode == MODE_NONE:
        res.status = STATUS_NO_RELEASE
        res.warnings.append(SourceTermWarning(
            "no_release",
            "No material release model selected — the material assay appears only as comments "
            "and NO material elements enter the PHREEQC system, so predicted material totals "
            "(Ca/Si/Al/Fe/…) will be ~0. Choose a release model to introduce them."))
        return res

    water_kg = (float(liquid_volume_mL) / 1000.0) if liquid_volume_mL not in (None, 0) else None

    if mode == MODE_MEASURED_LIQUID:
        return _measured_liquid(model, water_kg, res)

    # --- release-fraction modes (B = user-defined, C = literature/measured) ---
    if mode == MODE_LITERATURE and not getattr(model, "confirmed", False):
        res.status = STATUS_BLOCKED
        res.warnings.append(SourceTermWarning(
            "literature_unconfirmed",
            "Literature / measured release fractions must be explicitly confirmed before they "
            "can be used for input generation."))
        return res
    if material_profile is None or not getattr(material_profile, "is_usable", False):
        res.status = STATUS_BLOCKED
        res.warnings.append(SourceTermWarning(
            "no_usable_profile",
            "A confirmed material profile is required to compute material release source terms."))
        return res
    try:
        solid = float(solid_mass_g)
    except (TypeError, ValueError):
        solid = None
    if not solid or solid <= 0:
        res.status = STATUS_BLOCKED
        res.warnings.append(SourceTermWarning(
            "no_solid_mass",
            "Solid mass (g) is required to convert release fractions into released moles."))
        return res
    if water_kg is None:
        res.warnings.append(SourceTermWarning(
            "no_liquid_volume",
            "Liquid volume is missing — released moles would land in PHREEQC's default 1 kg of "
            "water, misrepresenting the L/S ratio. Set the liquid volume for correct "
            "concentrations."))

    assays = material_profile.element_assays()
    src_label = "literature/measured" if mode == MODE_LITERATURE else "assumed"
    for el, assay in assays.items():
        frac = model.per_element.get(el, model.global_fraction)
        if frac is None:
            continue
        try:
            frac = float(frac)
        except (TypeError, ValueError):
            continue
        if frac == 0:
            continue
        if frac < 0:
            res.warnings.append(SourceTermWarning(
                "negative_rejected",
                f"Release fraction for {el} is negative ({frac:g}); rejected (not released)."))
            continue
        if frac > 1:
            if not model.allow_over_unity:
                res.warnings.append(SourceTermWarning(
                    "over_unity_rejected",
                    f"Release fraction for {el} is > 1 ({frac:g}); rejected — that would release "
                    "more than is present. Enable 'allow over 100%' to override."))
                continue
            res.warnings.append(SourceTermWarning(
                "over_unity_allowed",
                f"Release fraction for {el} is > 1 ({frac:g}) — allowed by override; nonphysical "
                "(more than 100% of the assay)."))
        molar = atomic_weight(el)
        if molar is None:
            res.warnings.append(SourceTermWarning(
                "no_molar_mass", f"No molar mass for {el}; cannot compute its release."))
            continue
        grams = solid * (float(assay.value) / 100.0)
        moles_total = grams / molar
        moles_released = moles_total * frac
        vehicle = ELEMENT_OXIDE_VEHICLE.get(el)
        oxide, n = vehicle if vehicle else (None, 1)
        oxide_moles = (moles_released / n) if oxide else None
        conc_mM = (moles_released / water_kg * 1000.0) if water_kg else None
        res.released.append(ReleasedElement(
            element=el, fraction=frac, grams_in_solid=grams, moles_total=moles_total,
            moles_released=moles_released, oxide=oxide, oxide_moles=oxide_moles,
            concentration_mM=conc_mM, source=src_label))

    if not res.released:
        res.status = STATUS_BLOCKED
        res.warnings.append(SourceTermWarning(
            "nothing_released",
            "No elements were released (all fractions are zero, rejected, or unmapped)."))
        return res

    res.included_elements = [r.element for r in res.released]
    res.solution_water_kg = water_kg
    res.reaction_lines = _reaction_block(res.released)
    res.assumptions = [
        f"material release model: {mode} ({src_label})",
        "release FRACTIONS are user assumptions, not measured truth — they control the "
        "predicted dissolved totals",
        "released amounts are added as oxides via a PHREEQC REACTION block (oxide + water → "
        "dissolved ions). " + NON_KINETIC_NOTE,
    ]
    if model.provenance:
        res.assumptions.append(f"release-fraction provenance: {model.provenance}")
    res.status = STATUS_RELEASE_INCLUDED
    return res


def _reaction_block(released) -> list:
    lines = ["REACTION 1  # material source term — USER-ASSUMED release, NOT measured"]
    for r in released:
        if r.oxide:
            lines.append(
                f"    {r.oxide:<7} {r.oxide_moles:.4g}   # {r.element}: {r.fraction * 100:g}% of "
                f"{r.grams_in_solid:.4g} g solid -> {r.moles_released:.4g} mol (as {r.oxide})")
        else:
            lines.append(
                f"    {r.element:<7} {r.moles_released:.4g}   # {r.element}: {r.fraction * 100:g}% "
                "release (no oxide vehicle — added as the element)")
    lines.append("    1.0 moles   # add the listed oxide moles once (equilibrium, not kinetic)")
    return lines


def _measured_liquid(model, water_kg, res) -> SourceTermResult:
    if not model.measured_liquid_mM:
        res.status = STATUS_BLOCKED
        res.warnings.append(SourceTermWarning(
            "no_measured_values", "No measured liquid concentrations were provided."))
        return res
    lines: list[str] = []
    for el, mM in model.measured_liquid_mM.items():
        try:
            conc = float(mM)
        except (TypeError, ValueError):
            continue
        if conc < 0:
            res.warnings.append(SourceTermWarning(
                "negative_measured", f"Measured {el} concentration is negative; skipped."))
            continue
        lines.append(f"    {el:<7} {conc / 1000.0:.6g}   # MEASURED {conc:g} mM "
                     "(measured input, not a prediction)")
        res.included_elements.append(el)
    if not lines:
        res.status = STATUS_BLOCKED
        res.warnings.append(SourceTermWarning(
            "no_measured_values", "No usable measured liquid concentrations."))
        return res
    res.solution_extra_lines = lines
    res.solution_water_kg = water_kg
    res.status = STATUS_MEASURED_LIQUID
    res.assumptions = [
        "measured-liquid mode: the listed concentrations are MEASURED INPUT, not a model "
        "release — PHREEQC speciates them, but this is interpretation of measured data, not a "
        "validated prediction.",
    ]
    res.warnings.append(SourceTermWarning(
        "measured_label",
        "Measured liquid concentrations are used as input; the resulting speciation / SIs are "
        "a model interpretation of measured data — still not validated."))
    return res


# --------------------------------------------------------------------------- #
# Convenience constructors (used by the UI + tests)
# --------------------------------------------------------------------------- #
def no_release() -> DissolutionModel:
    return DissolutionModel(mode=MODE_NONE)


def global_release(fraction: float, *, per_element: dict | None = None,
                   allow_over_unity: bool = False) -> DissolutionModel:
    return DissolutionModel(mode=MODE_GLOBAL, global_fraction=fraction,
                            per_element=dict(per_element or {}),
                            allow_over_unity=allow_over_unity)


def literature_release(fraction: float, *, provenance: str, confirmed: bool = False,
                       per_element: dict | None = None) -> DissolutionModel:
    return DissolutionModel(mode=MODE_LITERATURE, global_fraction=fraction,
                            per_element=dict(per_element or {}), provenance=provenance,
                            confirmed=confirmed)


def measured_liquid(concentrations_mM: dict) -> DissolutionModel:
    return DissolutionModel(mode=MODE_MEASURED_LIQUID,
                            measured_liquid_mM=dict(concentrations_mM or {}))
