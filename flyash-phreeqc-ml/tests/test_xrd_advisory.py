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
