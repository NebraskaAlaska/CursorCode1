"""XRD Advisory / Pattern Planning — a **safe** XRD helper (planning, not identification).

This module does **not** identify phases from a measured pattern, and it does not fabricate a
high-confidence diffractogram. It plans a measurement:

* take a list of **expected** phases and return their **approximate** principal 2θ positions
  (Cu Kα) from a small internal demo/reference dictionary — labelled approximate/advisory,
* for a phase not in the dictionary, say plainly that **reference data is needed**,
* turn **PHREEQC-predicted precipitates** into a "phases to check by XRD" checklist, and
* suggest a context checklist (e.g. after NaOH leaching of Class C fly ash) — again expected/
  checklist, never a measured result.

**XRD Advisory v2** organises this into four user-facing modes (all advisory, all cautious):

1. :func:`expected_peaks` — approximate Cu Kα peaks for known/suspected phase *names*. A bare
   *formula* (e.g. ``CaCO3``) is flagged as polymorph-ambiguous — a formula cannot fix a pattern.
2. :func:`match_measured_peaks` — compare measured 2θ positions against the internal references and
   return **tentative** possible phases with a capped confidence (never an identification).
3. :func:`phases_to_check_from_predicted` — turn PHREEQC-predicted/saturated phases into a
   "phases PHREEQC suggests you check by XRD" list (saturation is not XRD validation).
4. :func:`reference_data_notes` — what the internal approximate table covers and what needs external
   reference data (CIF / ICDD PDF / a library such as pymatgen) later.

:func:`classify_request` maps a free-text prompt to one of these modes (used by the router).

Safety properties (mirroring the project rules):

* Every output is labelled **expected / checklist**, never "identified" or "measured".
* Peak positions are **approximate demo/reference values** (Cu Kα) — the disclaimer says to confirm
  against measured XRD and a reference database (ICDD PDF). Overlap and amorphous-content caveats
  travel with every result.
* It never claims a phase is present; it only lists what *to check*.
"""
from __future__ import annotations

import re
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

# --------------------------------------------------------------------------- #
# v2 modes — the four user-facing XRD Advisory tasks (all advisory, never identification).
# --------------------------------------------------------------------------- #
MODE_EXPECTED_PEAKS = "expected_peaks"
MODE_MATCH_MEASURED = "match_measured_peaks"
MODE_PHREEQC_CHECKLIST = "phreeqc_phase_checklist"
MODE_CONTEXT_CHECKLIST = "context_checklist"
MODE_REFERENCE_NOTES = "reference_data_notes"

# Default 2θ match tolerance (degrees). ±0.2° suits typical lab Cu Kα data; widen toward ±0.3° for
# lower-resolution scans or shifted peaks (solid solution / strain). Documented + caller-overridable.
DEFAULT_MATCH_TOLERANCE_DEG = 0.2

# Confidence levels for measured-peak matching. Even the HIGHEST level stays tentative — the wording
# is always "tentatively consistent with", never "identified as".
CONFIDENCE_LOW = "low"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_HIGH = "high"
CONFIDENCE_WORDING = {
    CONFIDENCE_LOW: ("tentatively consistent with (weak / partial — a single peak, or high "
                     "ambiguity)"),
    CONFIDENCE_MEDIUM: ("tentatively consistent with (several peaks match, but ambiguity or a "
                        "missing major peak remains)"),
    CONFIDENCE_HIGH: ("tentatively consistent with (a strong candidate — multiple characteristic "
                      "peaks, low ambiguity) — still NOT an identification"),
}

MATCH_EXPLANATION = ("Match Measured Peaks compares your measured 2θ positions against the internal "
                     "approximate reference peaks and returns TENTATIVE possible phases — never an "
                     "identification. Confidence is capped by how many characteristic peaks match.")
REFERENCE_NOTES_EXPLANATION = ("Reference Data Notes lists which phases the internal approximate "
                               "table covers and which need external reference data — the internal "
                               "peaks are teaching/advisory references, not certified standards.")

# Dominant (strongest) reference reflection per phase — used only to caution when a candidate's
# dominant peak is absent from a measured list. Approximate Cu Kα; a planning aid, not a standard.
_DOMINANT_2THETA = {
    "quartz": 26.6, "calcite": 29.4, "portlandite": 18.0, "gypsum": 11.6, "hematite": 33.2,
    "magnetite": 35.5, "mullite": 26.0, "corundum": 35.1, "ettringite": 9.1,
}

# Formulas that are NOT a single phase: the same formula crystallises as several polymorphs with
# DIFFERENT patterns, so a formula alone cannot fix an XRD pattern — the phase must be named.
_POLYMORPHIC_FORMULAS = {
    "sio2": ("quartz", "cristobalite", "tridymite", "amorphous silica"),
    "caco3": ("calcite", "aragonite", "vaterite"),
    "al2o3": ("corundum (α-Al2O3)", "γ-Al2O3", "other transition aluminas"),
    "fe2o3": ("hematite (α-Fe2O3)", "maghemite (γ-Fe2O3)"),
    "tio2": ("anatase", "rutile", "brookite"),
}

# Common fly-ash / cementitious phases NOT in the internal table — they need external reference data
# (CIF / ICDD PDF) before they can be planned. NAMES ONLY; no peaks are fabricated for them.
_NEEDS_EXTERNAL_REFERENCE = (
    "C-S-H / C-A-S-H gel (poorly crystalline)", "hydrotalcite", "katoite / hydrogarnet",
    "anatase / rutile (TiO2)", "periclase (MgO)", "lime (CaO)",
    "feldspars (albite / anorthite)", "maghemite", "brucite (Mg(OH)2)",
    "thenardite / mirabilite (Na2SO4 phases)",
)

_FORMULA_HINT_RE = re.compile(r"\d|\(")


def _input_looks_like_formula(raw) -> bool:
    """A cheap hint that a token is a chemical formula (has a digit or a parenthesis), not a name."""
    return bool(_FORMULA_HINT_RE.search(str(raw or "")))


def _to_float(value):
    """Best-effort float (drops None / non-numeric / NaN) — used to clean measured peak input."""
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return f if f == f else None


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
    """Build a checklist entry for one requested phase name (found → peaks; else reference-needed).

    A bare *formula* is detected (``from_formula``) and, for a polymorphic system, the alternative
    polymorphs are listed (``polymorph_alternatives``) with a caution — a formula does not fix a
    pattern, so the peaks shown are only for the assumed polymorph.
    """
    raw = str(name).strip()
    from_formula = _input_looks_like_formula(raw)
    canon = canonical_phase(name)
    if canon is None:
        return {"phase": raw, "formula": "", "status": STATUS_REFERENCE_NEEDED,
                "approx_2theta": [], "label": "expected (reference data needed)",
                "from_formula": from_formula, "polymorph_alternatives": [],
                "note": "No internal reference for this phase — supply a reference pattern to check it."}
    ref = _REFERENCE[canon]
    polymorphs = list(_POLYMORPHIC_FORMULAS.get(raw.lower().replace(" ", ""), ()))
    note = ref.note
    if from_formula and polymorphs:
        note = (f"'{raw}' is a chemical FORMULA, not a phase — it can crystallise as "
                f"{', '.join(polymorphs)}. The peaks shown assume the {ref.name} polymorph; name the "
                f"actual phase to plan an exact pattern.")
    return {"phase": ref.name, "formula": ref.formula, "status": STATUS_REFERENCE_AVAILABLE,
            "approx_2theta": list(ref.main_2theta), "label": "expected / checklist (approximate peaks)",
            "from_formula": from_formula, "polymorph_alternatives": polymorphs, "note": note}


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
        0, "PHREEQC suggests these phases MAY be worth checking by XRD — they are model-PREDICTED "
           "candidates and a saturation index is NOT XRD validation. Confirm each by measured XRD "
           "before reporting it as present.")
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
    """The overlap + amorphous + preferred-orientation + confirm-with-reference warnings every result
    should carry (plus a formula/polymorph caution when a formula was given)."""
    warns = [
        "Peaks can overlap (e.g. quartz, mullite, and feldspars cluster near 26–28° 2θ) — a single "
        "2θ match is not proof of a phase.",
        "Amorphous content (fly-ash glass) is invisible to phase peaks but raises the background — "
        "do not infer absence of glass from sharp peaks.",
        "Preferred orientation and variable crystallinity change relative peak INTENSITIES — rely on "
        "peak POSITIONS, not heights, for planning; treat intensities qualitatively.",
        "Confirm every phase against measured XRD and a reference pattern database (ICDD PDF).",
    ]
    if any(e["status"] == STATUS_REFERENCE_NEEDED for e in checklist):
        warns.append("Some requested phases have no internal reference here — supply a reference "
                     "pattern to plan their peaks.")
    if any(e.get("polymorph_alternatives") for e in checklist):
        warns.append("A chemical FORMULA was given for a polymorphic system — a formula does not fix "
                     "an XRD pattern (e.g. CaCO3 = calcite / aragonite / vaterite). Name the phase.")
    return warns


# --------------------------------------------------------------------------- #
# Mode 2 — Match Measured Peaks (TENTATIVE possible phases; never an identification).
# --------------------------------------------------------------------------- #
@dataclass
class XrdMatchResult:
    """Tentative measured-peak matching: candidate phases (with capped confidence) + diagnostics.

    Every candidate is phrased "tentatively consistent with", never "identified". Confidence is
    capped by how many characteristic peaks match and how unique those matches are.
    """

    tolerance_deg: float = DEFAULT_MATCH_TOLERANCE_DEG
    measured_2theta: list = field(default_factory=list)
    candidates: list = field(default_factory=list)         # list[dict] (sorted strongest-first)
    unmatched_measured: list = field(default_factory=list)  # measured peaks with no internal candidate
    warnings: list = field(default_factory=list)
    disclaimer: str = DISCLAIMER
    explanation: str = MATCH_EXPLANATION
    wording_note: str = ("Matches are TENTATIVE — phrased 'tentatively consistent with', never "
                         "'identified as'. Confirm with reference patterns + full-pattern fitting.")

    def candidate_table(self) -> list[dict]:
        """Flat rows for the UI. The confidence column is labelled ``confidence (tentative)`` so a
        bare 'high' can never read as a confirmed identification — the underlying data key on each
        candidate (``candidates[i]["confidence"]``) is unchanged for callers/tests."""
        rows: list[dict] = []
        for c in self.candidates:
            rows.append({
                "phase": c["phase"], "formula": c["formula"],
                "confidence (tentative)": c["confidence"],
                "matched": f'{c["n_matched"]}/{c["n_reference"]}',
                "matched_2theta_deg": ", ".join(f'{m["measured"]:g}' for m in c["matched_peaks"]) or "—",
                "missing_major_2theta_deg": ", ".join(f"{x:g}" for x in c["missing_major_peaks"]) or "—",
                "assessment": c["wording"],
            })
        return rows


def match_measured_peaks(measured_2theta, tolerance=DEFAULT_MATCH_TOLERANCE_DEG) -> XrdMatchResult:
    """Compare measured 2θ positions to the internal references → TENTATIVE candidate phases.

    ``measured_2theta`` is a list of degrees-2θ (numbers or numeric strings); ``tolerance`` is the
    half-window in degrees (default :data:`DEFAULT_MATCH_TOLERANCE_DEG`). Confidence is deliberately
    cautious and **cannot** reach ``high`` from a single matched peak:

    * ``low`` — 0–1 matched peaks (a single peak is always low), or all matches ambiguous;
    * ``medium`` — 2 matched peaks, or 3+ with ambiguity / a missing dominant peak;
    * ``high`` — 3+ matched peaks, at least 2 of them unique (not overlapping another candidate), a
      high matched fraction, and the dominant reflection present — and still only *tentative*.
    """
    measured = [round(f, 3) for f in (_to_float(v) for v in (measured_2theta or [])) if f is not None]
    tol = _to_float(tolerance)
    if tol is None or tol <= 0:
        tol = DEFAULT_MATCH_TOLERANCE_DEG

    if not measured:
        return XrdMatchResult(
            tolerance_deg=tol,
            warnings=["No measured 2θ peaks provided — nothing to match. Enter peak positions in "
                      "degrees 2θ (Cu Kα)."])

    # Match each phase's principal peaks to the nearest measured peak within tolerance.
    raw_candidates = []
    for key, ref in _REFERENCE.items():
        matched = []
        for ref_peak in ref.main_2theta:
            best = None
            for mp in measured:
                d = abs(mp - ref_peak)
                if d <= tol and (best is None or d < best[1]):
                    best = (mp, d)
            if best is not None:
                matched.append({"reference": ref_peak, "measured": best[0], "delta": round(best[1], 3)})
        if matched:
            raw_candidates.append((key, ref, matched))

    # Which measured peaks are claimed by more than one candidate phase (overlap / ambiguity)?
    claims: dict = {}
    for key, ref, matched in raw_candidates:
        for m in matched:
            claims.setdefault(m["measured"], set()).add(key)

    candidates = []
    for key, ref, matched in raw_candidates:
        n_matched = len(matched)
        n_ref = len(ref.main_2theta)
        unique = sum(1 for m in matched if len(claims.get(m["measured"], ())) == 1)
        frac = n_matched / n_ref if n_ref else 0.0
        matched_refs = {m["reference"] for m in matched}
        missing = [p for p in ref.main_2theta if p not in matched_refs]
        dominant = _DOMINANT_2THETA.get(key)
        dominant_missing = dominant is not None and dominant not in matched_refs

        if n_matched <= 1:
            conf = CONFIDENCE_LOW
        elif n_matched == 2:
            conf = CONFIDENCE_MEDIUM
        else:
            conf = CONFIDENCE_HIGH if (unique >= 2 and frac >= 0.66) else CONFIDENCE_MEDIUM
        if conf == CONFIDENCE_HIGH and dominant_missing:
            conf = CONFIDENCE_MEDIUM        # never 'high' without the dominant reflection present

        note_bits = []
        if dominant_missing:
            note_bits.append(f"dominant {dominant:g}° peak not in your list")
        if unique == 0:
            note_bits.append("all matched peaks overlap other candidates (ambiguous)")
        candidates.append({
            "phase": ref.name, "formula": ref.formula, "key": key, "confidence": conf,
            "wording": f"{ref.name}: {CONFIDENCE_WORDING[conf]}", "n_matched": n_matched,
            "n_reference": n_ref, "unique_matches": unique, "matched_peaks": matched,
            "missing_major_peaks": missing, "dominant_missing": dominant_missing,
            "note": "; ".join(note_bits) or "matched on peak positions only — confirm with the full pattern.",
        })

    rank = {CONFIDENCE_HIGH: 3, CONFIDENCE_MEDIUM: 2, CONFIDENCE_LOW: 1}
    candidates.sort(key=lambda c: (rank[c["confidence"]], c["n_matched"]), reverse=True)

    matched_vals = set(claims)
    unmatched = [mp for mp in measured if mp not in matched_vals]
    return XrdMatchResult(tolerance_deg=tol, measured_2theta=measured, candidates=candidates,
                          unmatched_measured=unmatched,
                          warnings=_match_warnings(candidates, unmatched, tol))


def _match_warnings(candidates, unmatched, tol) -> list[str]:
    """The standing caution set for tentative measured-peak matching."""
    warns = [
        f"Matching is TENTATIVE and position-only at ±{tol:g}° 2θ — it is NOT a phase identification. "
        "Confirm with measured reference patterns (ICDD PDF) and full-pattern (Rietveld) fitting.",
        "A single matched peak is weak evidence — peaks overlap (especially 26–35° 2θ), so several "
        "characteristic peaks are needed before a phase is even a strong candidate.",
        "Only the small internal reference set is searched — a real sample may contain phases not "
        "listed here, and amorphous content shows no peaks at all.",
    ]
    if unmatched:
        warns.append("Measured peaks with no candidate in the internal table: "
                     + ", ".join(f"{x:g}" for x in unmatched)
                     + " — these need external reference data to interpret.")
    if any(c["confidence"] == CONFIDENCE_HIGH for c in candidates):
        warns.append("Even a 'high' tentative match stays 'tentatively consistent with', never "
                     "'identified' — quantitative confirmation needs reference standards.")
    return warns


# --------------------------------------------------------------------------- #
# Mode 4 — Reference Data Notes (coverage of the internal approximate table).
# --------------------------------------------------------------------------- #
def reference_data_notes() -> dict:
    """What the internal approximate table covers, and what needs external reference data later.

    Returns a plain dict. The internal peaks are explicitly framed as teaching/advisory references,
    not certified standards; phases outside the small set are listed by NAME only (no fabricated
    peaks) as needing external reference data (CIF / ICDD PDF / a library such as pymatgen).
    """
    covered = [{"phase": ref.name, "formula": ref.formula,
                "n_reference_peaks": len(ref.main_2theta),
                "dominant_2theta_deg": _DOMINANT_2THETA.get(key),
                "approx_2theta_deg": list(ref.main_2theta), "note": ref.note}
               for key, ref in _REFERENCE.items()]
    return {
        "covered_phases": covered,
        "covered_count": len(covered),
        "needs_external_reference": list(_NEEDS_EXTERNAL_REFERENCE),
        "peak_basis": PEAK_BASIS,
        "explanation": REFERENCE_NOTES_EXPLANATION,
        "note": ("The internal peaks are TEACHING / ADVISORY approximate references (a few principal "
                 "Cu Kα reflections each), NOT certified phase-identification standards. They plan a "
                 "measurement; they do not identify phases."),
        "future_work": ("External reference data (ICDD PDF cards or CIF files, optionally via a "
                        "library such as pymatgen) would be needed for full-pattern / quantitative "
                        "work and for phases outside this small set. Not used yet."),
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- #
# Prompt → mode classification (deterministic; used by the instrument router).
# --------------------------------------------------------------------------- #
_TWO_THETA_TOKEN_RE = re.compile(r"2\s*-?\s*(?:theta|θ)", re.I)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_MEASURED_HINT_RE = re.compile(
    r"(measured|observed|might\s+match|possible\s+phase|what\s+phase|which\s+phase|\bmatch\w*\b|"
    r"\bpeaks?\b)", re.I)
_PHREEQC_HINT_RE = re.compile(r"\b(phreeqc|saturat\w*|predicted|prediction\w*|precipitat\w*)\b", re.I)
_CONTEXT_HINT_RE = re.compile(
    r"\b(after|leach\w*|naoh|koh|fly\s*ash|flyash|fli\s*ash|class\s*[cf])\b", re.I)
_REFNOTES_HINT_RE = re.compile(
    r"(reference\s+data|coverage|covered\s+by|external\s+reference|cif\b|icdd\b|pymatgen|"
    r"(?:which|what)\s+phases?[^?.]{0,40}(?:cover|reference\s+table|in\s+the\s+table))", re.I)


def _extract_two_theta(text) -> list:
    """Pull plausible 2θ values (2–90°) from free text, ignoring the literal '2theta'/'2θ' token."""
    cleaned = _TWO_THETA_TOKEN_RE.sub(" ", str(text or ""))
    vals = []
    for m in _NUMBER_RE.finditer(cleaned):
        try:
            v = float(m.group())
        except ValueError:
            continue
        if 2.0 <= v <= 90.0:
            vals.append(round(v, 3))
    return vals


def _extract_phase_names(text) -> list:
    """Find known reference phase NAMES/synonyms mentioned in free text (deterministic, de-duped)."""
    low = str(text or "").lower()
    found, seen = [], set()
    for token in sorted(_SYNONYMS, key=len, reverse=True):       # longest first (multi-word names)
        if re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", low):
            name = _REFERENCE[_SYNONYMS[token]].name
            if name not in seen:
                seen.add(name)
                found.append(name)
    return found


def classify_request(text) -> dict:
    """Map a free-text XRD prompt to one of the v2 modes + extract its inputs (deterministic).

    Returns ``{"mode", "measured_2theta", "phases", "rationale"}``. Priority: explicit measured 2θ
    peaks → matching; a PHREEQC/saturation reference → the PHREEQC checklist; a reference-coverage
    question → reference notes; a leaching/fly-ash context (with no named phases) → a context
    checklist; otherwise expected peaks for the named phases.
    """
    raw = str(text or "")
    low = raw.lower()
    measured = _extract_two_theta(raw)
    phases = _extract_phase_names(raw)

    if measured and (_TWO_THETA_TOKEN_RE.search(raw) or _MEASURED_HINT_RE.search(low)):
        return {"mode": MODE_MATCH_MEASURED, "measured_2theta": measured, "phases": phases,
                "rationale": "Measured 2θ peaks given with a 'what phases might match' framing."}
    if _PHREEQC_HINT_RE.search(low):
        return {"mode": MODE_PHREEQC_CHECKLIST, "measured_2theta": [], "phases": phases,
                "rationale": "Prompt references PHREEQC-predicted / saturated phases to check by XRD."}
    if _REFNOTES_HINT_RE.search(low):
        return {"mode": MODE_REFERENCE_NOTES, "measured_2theta": [], "phases": phases,
                "rationale": "Prompt asks which phases the internal reference table covers."}
    if _CONTEXT_HINT_RE.search(low) and not phases:
        return {"mode": MODE_CONTEXT_CHECKLIST, "measured_2theta": [], "phases": phases,
                "rationale": "Leaching / fly-ash context — an advisory phase checklist to plan XRD."}
    return {"mode": MODE_EXPECTED_PEAKS, "measured_2theta": [], "phases": phases,
            "rationale": "Named / expected phases — list approximate reference peaks (advisory)."}
