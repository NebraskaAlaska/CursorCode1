"""XRD station — plan EXPECTED peaks from known phases. Never an identification, never measured.

This is a measurement *planner*, not a diffractometer and not a phase-identification engine. The one
rule it exists to enforce: **a formula or composition alone cannot yield an exact diffraction
pattern.** XRD needs a phase identity *and* a reference crystal structure. So:

* Given phases that have a reference, it returns their **approximate** principal 2θ positions
  (Cu Kα), every one labelled *expected / approximate*, with ``measured = False`` and
  ``exact_pattern = False``.
* Given a card or phase with no known structure, it **refuses** to produce a pattern and explains the
  missing data (``result_type = "reference_data_needed"``).

The reference peak table mirrors ``flyash_phreeqc_ml/instruments/xrd_advisory.py`` (the same approximate
Cu Kα textbook values). In the integrated platform this station delegates to that module via
``science_core`` as the single source of truth; the embedded mirror keeps the scaffold self-contained
and testable on its own.
"""
from __future__ import annotations

import schemas

PEAK_BASIS = ("approximate reference 2θ for Cu Kα (λ≈1.5406 Å) — a planning aid, NOT a measured pattern")
DISCLAIMER = (
    "EXPECTED phases and APPROXIMATE reference peaks to plan a measurement — not a measured "
    "identification. Confirm every phase against measured XRD and a reference database (ICDD PDF). "
    "Peaks overlap, and amorphous content can hide or mimic crystalline phases.")

STATUS_REFERENCE_AVAILABLE = "reference_available"
STATUS_REFERENCE_NEEDED = "reference_data_needed"

# Mirror of xrd_advisory._REFERENCE — approximate principal Cu Kα peaks (degrees 2θ).
_REFERENCE = {
    "quartz": ("Quartz", "SiO2", (20.9, 26.6, 50.1)),
    "calcite": ("Calcite", "CaCO3", (29.4, 39.4, 43.1)),
    "portlandite": ("Portlandite", "Ca(OH)2", (18.0, 34.1, 47.1)),
    "gypsum": ("Gypsum", "CaSO4·2H2O", (11.6, 20.7, 29.1)),
    "hematite": ("Hematite", "Fe2O3", (24.1, 33.2, 35.6)),
    "magnetite": ("Magnetite", "Fe3O4", (30.1, 35.5, 62.6)),
    "mullite": ("Mullite", "3Al2O3·2SiO2", (16.4, 26.0, 40.9)),
    "corundum": ("Corundum", "Al2O3", (25.6, 35.1, 43.4)),
    "ettringite": ("Ettringite", "Ca6Al2(SO4)3(OH)12·26H2O", (9.1, 15.8, 22.9)),
}
_SYNONYMS = {
    "quartz": "quartz", "sio2": "quartz", "silica": "quartz",
    "calcite": "calcite", "caco3": "calcite",
    "portlandite": "portlandite", "ca(oh)2": "portlandite", "caoh2": "portlandite",
    "gypsum": "gypsum", "caso4·2h2o": "gypsum", "caso4.2h2o": "gypsum",
    "hematite": "hematite", "fe2o3": "hematite",
    "magnetite": "magnetite", "fe3o4": "magnetite",
    "mullite": "mullite",
    "corundum": "corundum", "al2o3": "corundum", "alumina": "corundum",
    "ettringite": "ettringite",
}


def _canon(name):
    key = str(name or "").strip().lower().replace(" ", "")
    return _SYNONYMS.get(key)


def _phase_names_from_input(card_or_phases):
    """Accept a Material Card dict (use its phases) or an explicit list of phase names/formulas."""
    if isinstance(card_or_phases, dict):
        phases = card_or_phases.get("phases") or []
        names = []
        for ph in phases:
            if isinstance(ph, dict):
                names.append(ph.get("name") or ph.get("formula") or "")
            elif ph:
                names.append(str(ph))
        return [n for n in names if n]
    if isinstance(card_or_phases, (list, tuple)):
        return [str(p.get("name") if isinstance(p, dict) else p) for p in card_or_phases if p]
    return []


def expected(card_or_phases) -> dict:
    """Return an EXPECTED-peak plan for the given card/phases, or an honest refusal if none are known.

    Output always carries ``measured = False`` and ``exact_pattern = False``. When the input has no
    phase with a known structure, ``result_type`` is ``reference_data_needed`` and ``peaks`` is empty
    — the station refuses to fabricate a pattern from stoichiometry.
    """
    names = _phase_names_from_input(card_or_phases)

    if not names:
        return {
            "station": schemas.STATION_XRD,
            "result_type": STATUS_REFERENCE_NEEDED,
            "measured": False,
            "exact_pattern": False,
            "peaks": [],
            "entries": [],
            "message": ("No phase identity available. XRD needs phases + a reference crystal structure; "
                        "a formula or composition alone cannot produce an exact diffractogram."),
            "peak_basis": PEAK_BASIS,
            "disclaimer": DISCLAIMER,
        }

    entries, peaks, unknown = [], [], []
    for name in names:
        canon = _canon(name)
        if canon is None:
            entries.append({"phase": str(name), "formula": "", "status": STATUS_REFERENCE_NEEDED,
                            "approx_2theta_deg": [], "label": "expected (reference data needed)",
                            "note": "No internal reference for this phase — supply a reference pattern."})
            unknown.append(str(name))
            continue
        disp, formula, two_theta = _REFERENCE[canon]
        entries.append({"phase": disp, "formula": formula, "status": STATUS_REFERENCE_AVAILABLE,
                        "approx_2theta_deg": list(two_theta),
                        "label": "expected / checklist (approximate peaks)",
                        "note": "Approximate principal Cu Kα reflections — plan, do not identify."})
        for tt in two_theta:
            peaks.append({"phase": disp, "formula": formula, "approx_2theta_deg": tt, "basis": PEAK_BASIS})

    any_ref = any(e["status"] == STATUS_REFERENCE_AVAILABLE for e in entries)
    warnings = [
        "These are EXPECTED phases / APPROXIMATE peaks — never a measured identification.",
        "Peaks overlap (quartz/mullite/feldspars cluster near 26–28° 2θ) — one match is not proof.",
        "Amorphous (glassy) content is invisible to phase peaks but raises the background.",
    ]
    if unknown:
        warnings.append("Some phases have no internal reference — supply a reference pattern to plan them.")

    return {
        "station": schemas.STATION_XRD,
        "result_type": STATUS_REFERENCE_AVAILABLE if any_ref else STATUS_REFERENCE_NEEDED,
        "measured": False,
        "exact_pattern": False,
        "peaks": peaks,
        "entries": entries,
        "unknown_phases": unknown,
        "warnings": warnings,
        "peak_basis": PEAK_BASIS,
        "disclaimer": DISCLAIMER,
        "message": ("Expected-peak plan built from reference structures. Confirm against measured XRD."),
    }
