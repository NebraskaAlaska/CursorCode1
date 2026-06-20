"""XRD Advisory / Pattern Planning — a **safe** XRD helper (planning, not identification).

This module does **not** identify phases from a measured pattern, and it does not fabricate a
high-confidence diffractogram. It plans a measurement:

* take a list of **expected** phases and return their **approximate** principal 2θ positions
  (Cu Kα) from a small internal demo/reference dictionary — labelled approximate/advisory,
* for a phase not in the dictionary, say plainly that **reference data is needed**,
* turn **PHREEQC-predicted precipitates** into a "phases to check by XRD" checklist, and
* suggest a context checklist (e.g. after NaOH leaching of Class C fly ash) — again expected/
  checklist, never a measured result.

Safety properties (mirroring the project rules):

* Every output is labelled **expected / checklist**, never "identified" or "measured".
* Peak positions are **approximate demo/reference values** (Cu Kα) — the disclaimer says to confirm
  against measured XRD and a reference database (ICDD PDF). Overlap and amorphous-content caveats
  travel with every result.
* It never claims a phase is present; it only lists what *to check*.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CU_KALPHA_WAVELENGTH_A = 1.5406
PEAK_BASIS = ("approximate demo / reference 2θ values for Cu Kα (λ≈1.5406 Å) — a planning aid, "
              "NOT a measured pattern")
DISCLAIMER = (
    "These are EXPECTED phases and APPROXIMATE reference peak positions to plan a measurement — "
    "not a measured phase identification. Confirm every phase against your measured XRD and a "
    "reference database (e.g. ICDD PDF). Peaks overlap, and amorphous (glassy) content — common in "
    "fly ash — can hide or mimic crystalline phases.")
EXPLANATION = ("XRD Advisory plans the measurement: it lists expected phases and approximate "
               "reference peaks (advisory). It does not identify phases from data — compare with "
               "measured XRD and a reference database to confirm.")

# Checklist entry statuses.
STATUS_REFERENCE_AVAILABLE = "reference_available"
STATUS_REFERENCE_NEEDED = "reference_data_needed"


@dataclass(frozen=True)
class PhaseRef:
    """A reference phase: display name, formula, a few approximate principal 2θ peaks, a note."""

    name: str
    formula: str
    main_2theta: tuple = ()        # approximate principal peaks, degrees (Cu Kα)
    note: str = ""


# --------------------------------------------------------------------------- #
# Internal demo / reference dictionary — APPROXIMATE principal peaks (Cu Kα).
# These are well-known textbook positions for the strongest reflections, rounded and kept to a few
# major peaks each. They are labelled approximate everywhere and are for planning only.
# --------------------------------------------------------------------------- #
_REFERENCE: dict[str, PhaseRef] = {
    "quartz": PhaseRef("Quartz", "SiO2", (20.9, 26.6, 50.1),
                       "26.6° is the dominant reflection; very common in fly ash."),
    "calcite": PhaseRef("Calcite", "CaCO3", (29.4, 39.4, 43.1),
                        "29.4° (104) is dominant; a common carbonation product."),
    "portlandite": PhaseRef("Portlandite", "Ca(OH)2", (18.0, 34.1, 47.1),
                            "18.0° (001) is diagnostic; forms in high-Ca alkaline systems."),
    "gypsum": PhaseRef("Gypsum", "CaSO4·2H2O", (11.6, 20.7, 29.1),
                       "11.6° (020) at low angle is diagnostic."),
    "hematite": PhaseRef("Hematite", "Fe2O3", (24.1, 33.2, 35.6),
                         "33.2° (104) and 35.6° (110) are the main pair."),
    "magnetite": PhaseRef("Magnetite", "Fe3O4", (30.1, 35.5, 62.6),
                          "35.5° (311) is dominant; overlaps maghemite/spinels."),
    "mullite": PhaseRef("Mullite", "3Al2O3·2SiO2", (16.4, 26.0, 40.9),
                        "the 26°/40.9° group is characteristic; common in Class F fly ash."),
    "corundum": PhaseRef("Corundum", "Al2O3", (25.6, 35.1, 43.4),
                         "35.1° (104) is dominant; an internal-standard phase."),
    "ettringite": PhaseRef("Ettringite", "Ca6Al2(SO4)3(OH)12·26H2O", (9.1, 15.8, 22.9),
                           "9.1° at low angle is diagnostic; forms in sulfate-rich alkaline cure."),
}

# Synonyms → canonical reference key (so "Ca(OH)2" / "calcium hydroxide" → portlandite).
_SYNONYMS: dict[str, str] = {
    "quartz": "quartz", "sio2": "quartz", "silica": "quartz",
    "calcite": "calcite", "caco3": "calcite", "calcium carbonate": "calcite",
    "portlandite": "portlandite", "ca(oh)2": "portlandite", "caoh2": "portlandite",
    "calcium hydroxide": "portlandite", "ch": "portlandite",
    "gypsum": "gypsum", "caso4·2h2o": "gypsum", "caso4.2h2o": "gypsum", "caso42h2o": "gypsum",
    "hematite": "hematite", "fe2o3": "hematite", "haematite": "hematite",
    "magnetite": "magnetite", "fe3o4": "magnetite",
    "mullite": "mullite",
    "corundum": "corundum", "al2o3": "corundum", "alumina": "corundum",
    "ettringite": "ettringite", "aft": "ettringite",
}

# Phases a researcher commonly checks for after alkaline (NaOH) leaching of Class C fly ash, plus a
# note that the bulk of fly ash is amorphous glass. Advisory only.
_CLASS_C_NAOH_CHECKLIST = ("quartz", "calcite", "portlandite", "ettringite", "mullite",
                           "hematite", "magnetite")


@dataclass
class XrdAdvisory:
    """The advisory output: an expected-phase checklist + warnings + the standing disclaimer."""

    checklist: list = field(default_factory=list)      # list[dict]: phase/formula/status/peaks/note
    unknown_phases: list = field(default_factory=list)  # names with no reference data
    warnings: list = field(default_factory=list)
    disclaimer: str = DISCLAIMER
    explanation: str = EXPLANATION
    peak_basis: str = PEAK_BASIS

    def checklist_table(self) -> list[dict]:
        return list(self.checklist)

    def peak_table(self) -> list[dict]:
        """Flat (phase, approximate 2θ) rows for the phases that have reference peaks."""
        rows: list[dict] = []
        for entry in self.checklist:
            for two_theta in entry.get("approx_2theta", []) or []:
                rows.append({"phase": entry["phase"], "formula": entry["formula"],
                             "approx_2theta_deg": two_theta, "basis": PEAK_BASIS})
        return rows


# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #
def reference_phase_names() -> list[str]:
    """Display names of the phases in the internal reference dictionary."""
    return [ref.name for ref in _REFERENCE.values()]


def canonical_phase(name) -> str | None:
    """Canonical reference key for a phase name/formula (``Ca(OH)2`` → ``portlandite``), else None."""
    if not name:
        return None
    key = str(name).strip().lower()
    if key in _SYNONYMS:
        return _SYNONYMS[key]
    # tolerate trailing punctuation / extra spaces
    key2 = key.replace(" ", "")
    return _SYNONYMS.get(key2)


def _entry_for(name: str) -> dict:
    """Build a checklist entry for one requested phase name (found → peaks; else reference-needed)."""
    canon = canonical_phase(name)
    if canon is None:
        return {"phase": str(name).strip(), "formula": "", "status": STATUS_REFERENCE_NEEDED,
                "approx_2theta": [], "label": "expected (reference data needed)",
                "note": "No internal reference for this phase — supply a reference pattern to check it."}
    ref = _REFERENCE[canon]
    return {"phase": ref.name, "formula": ref.formula, "status": STATUS_REFERENCE_AVAILABLE,
            "approx_2theta": list(ref.main_2theta), "label": "expected / checklist (approximate peaks)",
            "note": ref.note}


def expected_peaks(phases) -> XrdAdvisory:
    """Build an advisory checklist of **expected** phases with **approximate** reference peaks.

    ``phases`` is a list of phase names/formulas. Each is matched against the internal reference
    dictionary; matches get approximate principal 2θ (Cu Kα), the rest are flagged
    *reference data needed*. Every entry is labelled expected/advisory — never identified.
    """
    checklist: list[dict] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for name in (phases or []):
        if name is None or not str(name).strip():
            continue
        entry = _entry_for(name)
        dedupe_key = (entry["phase"] or str(name)).lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        checklist.append(entry)
        if entry["status"] == STATUS_REFERENCE_NEEDED:
            unknown.append(entry["phase"])

    warnings = _standard_warnings(checklist)
    return XrdAdvisory(checklist=checklist, unknown_phases=unknown, warnings=warnings)


def phases_to_check_from_predicted(predicted_phases) -> XrdAdvisory:
    """Turn PHREEQC-predicted precipitates into a 'phases to check by XRD' checklist.

    Accepts a list of phase **names**, or a list of ``{"phase": ..., "SI": ...}`` dicts as produced
    by the PHREEQC executor's saturation indices. These are *candidate* phases to look for — the
    advisory says so. It never asserts the phase is present; only measured XRD can confirm it.
    """
    names: list[str] = []
    for item in (predicted_phases or []):
        if isinstance(item, dict):
            name = item.get("phase") or item.get("name")
        else:
            name = item
        if name is None or not str(name).strip():
            continue
        names.append(_strip_phreeqc_suffix(str(name)))

    advisory = expected_peaks(names)
    advisory.warnings.insert(
        0, "These are PHREEQC-PREDICTED candidate phases (a model estimate) — confirm each by "
           "measured XRD before reporting it as present.")
    return advisory


def suggest_phases_for_context(text: str) -> XrdAdvisory:
    """Suggest an **advisory** phase checklist from a free-text context (keyword-based, deterministic).

    Currently recognises alkaline (NaOH/KOH) leaching of Class C / high-calcium fly ash and returns
    a common checklist plus the amorphous-glass caveat. Otherwise returns a minimal, clearly-advisory
    list. Always labelled expected/checklist; it identifies nothing.
    """
    low = str(text or "").lower()
    is_alkaline = any(w in low for w in ("naoh", "koh", "alkal", "caustic", "hydroxide"))
    is_flyash = any(w in low for w in ("fly ash", "flyash", "fli ash", "coal ash",
                                       "class c", "class f"))
    advisory = expected_peaks(list(_CLASS_C_NAOH_CHECKLIST)) if (is_alkaline or is_flyash) \
        else expected_peaks(["quartz", "calcite"])
    advisory.warnings.insert(
        0, "Suggested phases are a planning checklist, not a prediction of what is present. Fly ash "
           "is largely AMORPHOUS glass — a flat hump near 20–35° 2θ is expected and is not a "
           "crystalline phase.")
    return advisory


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _strip_phreeqc_suffix(name: str) -> str:
    """Best-effort strip of PHREEQC phase decorations (e.g. ``Calcite(d)`` → ``Calcite``)."""
    return name.split("(")[0].strip() or name.strip()


def _standard_warnings(checklist) -> list[str]:
    """The overlap + amorphous + confirm-with-reference warnings every result should carry."""
    warns = [
        "Peaks can overlap (e.g. quartz, mullite, and feldspars cluster near 26–28° 2θ) — a single "
        "2θ match is not proof of a phase.",
        "Amorphous content (fly-ash glass) is invisible to phase peaks but raises the background — "
        "do not infer absence of glass from sharp peaks.",
        "Confirm every phase against measured XRD and a reference pattern database (ICDD PDF).",
    ]
    if any(e["status"] == STATUS_REFERENCE_NEEDED for e in checklist):
        warns.append("Some requested phases have no internal reference here — supply a reference "
                     "pattern to plan their peaks.")
    return warns
