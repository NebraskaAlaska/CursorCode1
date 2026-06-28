"""Synthesizer station — turn a name / formula / user composition into an honest Material Card.

The synthesizer is the *only* place a card is born, and it is the most important honesty gate in the
sandbox: **it must never invent phases, a crystal structure, or measured data for a material it does
not actually know.** Its decision order:

1. **User composition supplied** → a card whose composition is labelled ``user_provided`` /
   ``assumed``; phases stay empty (a composition is not a phase identity).
2. **Name/formula matches the internal reference catalog** → a ``reference`` card with the catalog's
   known phases + ``structure_source = reference_database``. (The demo fly ash maps to a clearly
   labelled ``synthetic_demo`` card with an *amorphous* note and no crystalline phases asserted.)
3. **Formula parses but is unknown** → a ``formula_only`` card: stoichiometry is known, phases and
   structure are NOT. Warnings spell out what XRD/PHREEQC still need.
4. **Nothing resolves** → an ``unknown`` card. No data is invented.

The reference catalog is deliberately tiny and uses the same phase names as the app's XRD advisory,
so the two stay consistent. ``material_id`` is a deterministic slug (no randomness), so the same
input always yields the same id.
"""
from __future__ import annotations

import hashlib
import re

import chem
import schemas
from schemas import (
    MaterialCard, station_eligibility,
    REFERENCE, SYNTHETIC_DEMO, ASSUMED, USER_PROVIDED, FORMULA_ONLY, UNKNOWN,
    STRUCT_REFERENCE_DB, STRUCT_USER_SUPPLIED, STRUCT_NONE,
    BASIS_OXIDE_WT_PCT, BASIS_ELEMENT_MOL,
)


def _phase(name, formula, note=""):
    return {"name": name, "formula": formula, "source": "reference_database", "note": note}


# Tiny internal reference catalog. Phase names mirror flyash_phreeqc_ml/instruments/xrd_advisory.py
# so a card minted here lines up with the XRD station's reference dictionary.
_CATALOG = {
    "quartz": {"display_name": "Quartz", "formula": "SiO2",
               "phases": [_phase("Quartz", "SiO2", "trigonal; dominant 26.6° 2θ reflection")]},
    "calcite": {"display_name": "Calcite", "formula": "CaCO3",
                "phases": [_phase("Calcite", "CaCO3", "rhombohedral carbonate")]},
    "portlandite": {"display_name": "Portlandite", "formula": "Ca(OH)2",
                    "phases": [_phase("Portlandite", "Ca(OH)2", "forms in high-Ca alkaline systems")]},
    "corundum": {"display_name": "Corundum", "formula": "Al2O3",
                 "phases": [_phase("Corundum", "Al2O3", "α-Al2O3; common internal standard")]},
    "gypsum": {"display_name": "Gypsum", "formula": "CaSO4·2H2O",
               "phases": [_phase("Gypsum", "CaSO4·2H2O", "low-angle 11.6° 2θ is diagnostic")]},
    "hematite": {"display_name": "Hematite", "formula": "Fe2O3",
                 "phases": [_phase("Hematite", "Fe2O3", "α-Fe2O3")]},
}

# Name/formula synonyms → catalog key (lower-cased, spaces removed for matching).
_SYNONYMS = {
    "quartz": "quartz", "sio2": "quartz", "silica": "quartz",
    "calcite": "calcite", "caco3": "calcite", "calciumcarbonate": "calcite",
    "portlandite": "portlandite", "ca(oh)2": "portlandite", "caoh2": "portlandite",
    "calciumhydroxide": "portlandite",
    "corundum": "corundum", "al2o3": "corundum", "alumina": "corundum",
    "gypsum": "gypsum", "caso4·2h2o": "gypsum", "caso4.2h2o": "gypsum", "caso42h2o": "gypsum",
    "hematite": "hematite", "fe2o3": "hematite",
}

# The labelled synthetic demo fly ash (oxide wt%) from the project's demo test input. Synthetic, not
# measured; fly ash is largely amorphous so NO crystalline phase is asserted here.
_DEMO_FLYASH = {
    "values": {"SiO2": 34, "Al2O3": 18, "CaO": 24, "Fe2O3": 7, "MgO": 5,
               "Na2O": 2, "K2O": 1, "SO3": 4, "LOI": 5},
    "note": "Synthetic demo composition (oxide wt%). Class C-like. Not a measurement.",
}
_DEMO_TRIGGERS = ("demo fly ash", "demo flyash", "synthetic fly ash", "class c demo",
                  "demo class c", "demo composition")


def _slug(text: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return base or "material"


def _material_id(*parts) -> str:
    """Deterministic id: a readable slug + a short hash of the inputs (stable across runs)."""
    key = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"{_slug(parts[0])}-{digest}"


def _canon_key(text) -> str:
    return re.sub(r"\s+", "", str(text or "").strip().lower())


def synthesize(name=None, formula=None, composition=None) -> dict:
    """Synthesize a Material Card from a ``name`` / ``formula`` / ``composition``. Returns a card dict.

    Honest by construction: phases and ``structure_source`` are only ever set from the reference
    catalog or an explicit user-supplied structure — never derived from a formula. See the module
    docstring for the decision order.
    """
    name = (name or "").strip() or None
    formula = (formula or "").strip() or None

    # (1) User-supplied composition: take it as given (assumed), assert no phases.
    if composition:
        disp = name or "User composition"
        comp = {"basis": BASIS_OXIDE_WT_PCT, "values": dict(composition),
                "status": USER_PROVIDED, "note": "Composition supplied by the user; taken as assumed."}
        card = MaterialCard(
            material_id=_material_id(disp, "user-comp"), display_name=disp,
            data_status=ASSUMED, formula=formula, phases=[], composition=comp,
            structure_source=STRUCT_NONE, provenance="user_composition",
            uncertainty_notes=["Composition is user-provided and unconfirmed (assumed)."],
            warnings=[
                "This composition is ASSUMED (user-provided), not measured.",
                "A composition is not a phase identity — XRD still needs phases + a structure.",
                "PHREEQC needs a leachant, source term, and database before even a preview.",
            ])
        card.allowed_lab_stations = station_eligibility(
            phases=card.phases, structure_source=card.structure_source, composition=card.composition)
        return card.to_dict()

    probe = name or formula or ""
    key = _canon_key(probe)

    # (2a) Demo fly ash → labelled synthetic_demo card (no crystalline phase asserted).
    if any(t in str(probe).lower() for t in _DEMO_TRIGGERS):
        comp = {"basis": BASIS_OXIDE_WT_PCT, "values": dict(_DEMO_FLYASH["values"]),
                "status": SYNTHETIC_DEMO, "note": _DEMO_FLYASH["note"]}
        card = MaterialCard(
            material_id=_material_id("demo-fly-ash"), display_name="Synthetic demo fly ash",
            data_status=SYNTHETIC_DEMO, formula=None, phases=[], composition=comp,
            structure_source=STRUCT_NONE, provenance="synthetic_demo",
            uncertainty_notes=["Synthetic demo only — not a real assay, not measured.",
                               "Fly ash is largely amorphous glass; no crystalline phase is asserted."],
            warnings=[
                "SYNTHETIC DEMO composition — clearly labelled; never treat as measured data.",
                "No phases are asserted (amorphous-dominated); XRD here can only suggest a checklist.",
            ])
        card.allowed_lab_stations = station_eligibility(
            phases=card.phases, structure_source=card.structure_source, composition=card.composition)
        return card.to_dict()

    # (2b) Reference catalog hit.
    canon = _SYNONYMS.get(key)
    if canon:
        ref = _CATALOG[canon]
        card = MaterialCard(
            material_id=_material_id(ref["display_name"], "ref"), display_name=ref["display_name"],
            data_status=REFERENCE, formula=ref["formula"], phases=[dict(p) for p in ref["phases"]],
            composition=None, structure_source=STRUCT_REFERENCE_DB, provenance="reference_catalog",
            uncertainty_notes=["Phases come from an internal reference, not from your sample."],
            warnings=[
                "Phase identity is REFERENCE data (a known structure), not a measurement of your sample.",
                "Confirm against measured XRD and a reference database (e.g. ICDD PDF) before reporting.",
            ])
        card.allowed_lab_stations = station_eligibility(
            phases=card.phases, structure_source=card.structure_source, composition=card.composition)
        return card.to_dict()

    # (3) Formula parses but is unknown → formula_only (stoichiometry known; phases/structure NOT).
    if formula or _looks_like_formula(probe):
        candidate = formula or probe
        try:
            counts = chem.parse_formula(candidate)
            comp = {"basis": BASIS_ELEMENT_MOL, "values": {k: counts[k] for k in counts},
                    "status": FORMULA_ONLY, "note": "Element counts parsed from the formula (stoichiometry only)."}
            try:
                comp["molar_mass_g_per_mol"] = chem.molar_mass(candidate)
            except chem.FormulaParseError as exc:
                comp["molar_mass_g_per_mol"] = None
                comp["note"] += f" Molar mass unavailable: {exc}."
            disp = name or candidate
            card = MaterialCard(
                material_id=_material_id(candidate, "formula"), display_name=disp,
                data_status=FORMULA_ONLY, formula=candidate, phases=[], composition=comp,
                structure_source=STRUCT_NONE, provenance="user_formula",
                uncertainty_notes=["Only stoichiometry is known — no phase or crystal structure."],
                warnings=[
                    "Formula parsed: stoichiometry only. Phase/crystal structure are UNKNOWN.",
                    "XRD cannot produce an exact pattern from a formula — it needs phases + a reference.",
                    "PHREEQC needs a leachant, source term, and database; composition alone is not enough.",
                ])
            card.allowed_lab_stations = station_eligibility(
                phases=card.phases, structure_source=card.structure_source, composition=card.composition)
            return card.to_dict()
        except chem.FormulaParseError:
            pass  # fall through to unknown

    # (4) Nothing resolved → honest unknown card. Invent nothing.
    disp = name or formula or "Unknown material"
    card = MaterialCard(
        material_id=_material_id(disp, "unknown"), display_name=disp,
        data_status=UNKNOWN, formula=formula, phases=[], composition=None,
        structure_source=STRUCT_NONE, provenance="unresolved",
        uncertainty_notes=["Could not resolve this material from the input."],
        warnings=[
            "UNKNOWN material — it is not in the reference catalog and did not parse as a formula.",
            "No phases, composition, or peaks were invented. Provide a formula or a composition.",
        ])
    card.allowed_lab_stations = station_eligibility(
        phases=card.phases, structure_source=card.structure_source, composition=card.composition)
    return card.to_dict()


def _looks_like_formula(text) -> bool:
    """Cheap heuristic: does this look more like a formula than a free-text name? (Only a hint.)"""
    s = str(text or "").strip()
    if not s or " " in s:
        return False
    # A formula is element symbols, digits, parens and hydrate dots — and usually has a capital start.
    return bool(re.fullmatch(r"[A-Za-z0-9()·•∙*.]+", s)) and bool(re.match(r"[A-Z]", s))
