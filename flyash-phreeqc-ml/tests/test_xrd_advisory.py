"""Pins for the **XRD Advisory** module — expected/checklist only, approximate peaks, never a
measured identification.

The module plans an XRD measurement: it lists expected phases with approximate reference 2θ
(Cu Kα), flags phases it has no reference for, and turns PHREEQC-predicted precipitates into a
"check by XRD" checklist. It must never claim to have identified a phase, and every result must
carry the approximate/advisory labelling + the overlap / amorphous / confirm-with-reference caveats.
"""
from __future__ import annotations

from flyash_phreeqc_ml.instruments import xrd_advisory as xrd


def test_expected_peaks_for_common_phases():
    adv = xrd.expected_peaks(["calcite", "quartz", "portlandite"])
    by_phase = {e["phase"].lower(): e for e in adv.checklist}
    assert {"calcite", "quartz", "portlandite"} <= set(by_phase)
    for entry in adv.checklist:
        assert entry["status"] == xrd.STATUS_REFERENCE_AVAILABLE
        assert entry["approx_2theta"], f"{entry['phase']} has no approximate peaks"


def test_peaks_are_labelled_approximate_and_advisory():
    adv = xrd.expected_peaks(["quartz"])
    assert "approximate" in xrd.PEAK_BASIS.lower()
    assert "approximate" in adv.checklist[0]["label"].lower()
    # The standing explanation/disclaimer must frame this as planning, not identification.
    assert "not a measured" in adv.peak_basis.lower()
    assert "not a measured phase identification" in adv.disclaimer.lower()
    assert "advisory" in adv.explanation.lower() or "plans the measurement" in adv.explanation.lower()


def test_no_result_claims_measured_identification():
    adv = xrd.expected_peaks(["calcite", "hematite"])
    for entry in adv.checklist:
        label = entry["label"].lower()
        assert "expected" in label or "checklist" in label
        assert "identified" not in label and "measured" not in label


def test_unknown_phase_says_reference_data_needed():
    adv = xrd.expected_peaks(["calcite", "unobtainium"])
    needed = [e for e in adv.checklist if e["status"] == xrd.STATUS_REFERENCE_NEEDED]
    assert needed and needed[0]["phase"].lower() == "unobtainium"
    assert "unobtainium" in adv.unknown_phases
    assert not needed[0]["approx_2theta"]


def test_synonyms_resolve_to_reference_phase():
    adv = xrd.expected_peaks(["Ca(OH)2", "SiO2"])
    phases = {e["phase"].lower() for e in adv.checklist}
    assert "portlandite" in phases and "quartz" in phases


def test_overlap_and_amorphous_warnings_present():
    adv = xrd.expected_peaks(["quartz"])
    blob = " ".join(adv.warnings).lower()
    assert "overlap" in blob
    assert "amorphous" in blob
    assert "reference" in blob


def test_predicted_phases_become_a_check_by_xrd_checklist():
    # Accepts PHREEQC saturation-index dicts (phase + SI) and bare names; strips suffixes.
    adv = xrd.phases_to_check_from_predicted([{"phase": "Calcite", "SI": 0.3},
                                              {"phase": "Portlandite(d)", "SI": -1.2}])
    phases = {e["phase"].lower() for e in adv.checklist}
    assert "calcite" in phases and "portlandite" in phases
    assert any("predicted" in w.lower() and "confirm" in w.lower() for w in adv.warnings)


def test_context_suggestion_is_advisory_and_notes_amorphous_glass():
    adv = xrd.suggest_phases_for_context("NaOH leaching of Class C fly ash")
    assert adv.checklist                                    # a non-empty checklist
    assert any("amorphous" in w.lower() for w in adv.warnings)
    assert any("planning checklist" in w.lower() or "not a prediction" in w.lower()
               for w in adv.warnings)


def test_peak_table_rows_carry_the_basis_label():
    adv = xrd.expected_peaks(["calcite"])
    rows = adv.peak_table()
    assert rows and all(r["basis"] == xrd.PEAK_BASIS for r in rows)
    assert all("approx_2theta_deg" in r for r in rows)


# --------------------------------------------------------------------------- #
# v2 — preferred-orientation warning + formula/polymorph caution (Expected Peaks).
# --------------------------------------------------------------------------- #
def test_expected_peaks_warn_about_preferred_orientation():
    blob = " ".join(xrd.expected_peaks(["quartz"]).warnings).lower()
    assert "preferred orientation" in blob
    assert "position" in blob          # rely on positions, not intensities


def test_formula_only_input_is_flagged_as_polymorph_ambiguous():
    # A FORMULA (CaCO3) must not be treated as a definitive phase — it can be calcite/aragonite/vaterite.
    formula_entry = xrd.expected_peaks(["CaCO3"]).checklist[0]
    assert formula_entry["from_formula"] is True
    assert "aragonite" in formula_entry["polymorph_alternatives"]
    assert "formula" in formula_entry["note"].lower()
    # The same SYSTEM named as a phase carries no polymorph caution.
    named_entry = xrd.expected_peaks(["calcite"]).checklist[0]
    assert named_entry["from_formula"] is False
    assert named_entry["polymorph_alternatives"] == []
    # And the result-level warnings call out the formula/polymorph issue.
    warns = " ".join(xrd.expected_peaks(["CaCO3"]).warnings).lower()
    assert "formula" in warns and "polymorph" in warns


# --------------------------------------------------------------------------- #
# v2 — Match Measured Peaks (TENTATIVE; confidence capped; never identification).
# --------------------------------------------------------------------------- #
def test_measured_peaks_give_tentative_matches_not_identification():
    res = xrd.match_measured_peaks([26.6, 29.4, 34.1])     # quartz / calcite / portlandite dominants
    phases = {c["phase"].lower() for c in res.candidates}
    assert {"quartz", "calcite", "portlandite"} <= phases
    for c in res.candidates:
        assert c["confidence"] == xrd.CONFIDENCE_LOW        # one peak each → weak/tentative
        assert "tentatively consistent with" in c["wording"].lower()
        assert "identified" not in c["wording"].lower()
    # phrasing guard travels with the result
    assert "tentatively consistent with" in res.wording_note.lower()


def test_confidence_cannot_be_high_from_a_single_peak():
    res = xrd.match_measured_peaks([26.6])
    assert res.candidates
    assert all(c["confidence"] != xrd.CONFIDENCE_HIGH for c in res.candidates)
    assert res.candidates[0]["confidence"] == xrd.CONFIDENCE_LOW


def test_confidence_high_needs_several_unique_peaks_and_stays_tentative():
    res = xrd.match_measured_peaks([20.9, 26.6, 50.1])      # quartz's three principal peaks
    quartz = next(c for c in res.candidates if c["phase"].lower() == "quartz")
    assert quartz["confidence"] == xrd.CONFIDENCE_HIGH
    assert quartz["n_matched"] == 3
    assert "tentatively consistent with" in quartz["wording"].lower()
    assert "identified" not in quartz["wording"].lower()    # still not an identification


def test_match_with_no_peaks_invents_nothing():
    res = xrd.match_measured_peaks([])
    assert res.candidates == [] and res.unmatched_measured == []
    assert any("no measured" in w.lower() for w in res.warnings)


def test_unmatched_measured_peaks_are_reported_not_forced():
    res = xrd.match_measured_peaks([26.6, 5.0], tolerance=0.2)   # 5.0° matches nothing internal
    assert 5.0 in res.unmatched_measured
    assert any("no candidate" in w.lower() for w in res.warnings)


# --------------------------------------------------------------------------- #
# v2 — PHREEQC Phase Checklist wording (check, not confirm/observe).
# --------------------------------------------------------------------------- #
def test_phreeqc_checklist_says_check_not_confirmed_phase():
    adv = xrd.phases_to_check_from_predicted([{"phase": "Calcite", "SI": 0.3}])
    assert any(e["phase"].lower() == "calcite" for e in adv.checklist)
    blob = " ".join(adv.warnings).lower()
    assert "suggest" in blob and "check" in blob
    assert "not xrd validation" in blob
    for e in adv.checklist:                                 # never asserts the phase is observed
        assert "confirmed" not in e["label"].lower() and "identified" not in e["label"].lower()


# --------------------------------------------------------------------------- #
# v2 — Reference Data Notes (coverage + honest gaps + teaching/advisory framing).
# --------------------------------------------------------------------------- #
def test_reference_data_notes_lists_coverage_and_gaps():
    notes = xrd.reference_data_notes()
    assert notes["covered_count"] == len(notes["covered_phases"]) >= 5
    names = {c["phase"].lower() for c in notes["covered_phases"]}
    assert "quartz" in names and "calcite" in names
    assert notes["needs_external_reference"]                # honest about what it does NOT cover
    note = notes["note"].lower()
    assert ("teaching" in note or "advisory" in note) and "not" in note  # not certified standards


# --------------------------------------------------------------------------- #
# v2 — output strings avoid unsupported affirmatives outside cautionary warnings.
# --------------------------------------------------------------------------- #
def test_affirmative_outputs_avoid_identified_confirmed_validated():
    import re
    banned = re.compile(r"\b(identified|confirmed|validated)\b", re.I)
    # Match assessments (per-candidate wording + note).
    res = xrd.match_measured_peaks([20.9, 26.6, 50.1, 29.4, 34.1])
    for c in res.candidates:
        assert not banned.search(c["wording"]), c["wording"]
        assert not banned.search(c["note"]), c["note"]
    # Expected-peak checklist labels + notes (named, formula, and unknown inputs).
    adv = xrd.expected_peaks(["calcite", "quartz", "CaCO3", "unobtainium"])
    for e in adv.checklist:
        assert not banned.search(e["label"]), e["label"]
        assert not banned.search(e["note"]), e["note"]


# --------------------------------------------------------------------------- #
# v2 — prompt → mode classification (used by the router).
# --------------------------------------------------------------------------- #
def test_classify_request_detects_each_mode():
    assert xrd.classify_request("expected XRD peaks for calcite and quartz")["mode"] \
        == xrd.MODE_EXPECTED_PEAKS
    match = xrd.classify_request("I measured peaks at 26.6, 29.4 2theta — what might match?")
    assert match["mode"] == xrd.MODE_MATCH_MEASURED
    assert match["measured_2theta"] == [26.6, 29.4]
    assert xrd.classify_request("PHREEQC predicted calcite saturation, check in XRD?")["mode"] \
        == xrd.MODE_PHREEQC_CHECKLIST
    assert xrd.classify_request("what XRD phases to check after NaOH leaching of fly ash?")["mode"] \
        == xrd.MODE_CONTEXT_CHECKLIST
