"""Phase 3 measured-XRD import, external-reference, and tentative matching contracts.

Fixtures are synthetic teaching data; no licensed/proprietary reference records are bundled.
"""
from __future__ import annotations

import hashlib
import json
import re

import pytest

from flyash_phreeqc_ml.instruments import xrd_advisory as xrd


def _metadata(**overrides):
    values = {
        "project_id": "prj_synthetic",
        "material_id": "mat_synthetic",
        "sample_id": "SYN-XRD-1",
        "radiation_source": "Cu Kalpha",
        "wavelength_angstrom": 1.5406,
        "instrument": "Synthetic bench instrument",
        "method": "Synthetic teaching scan",
        "intensity_unit": "counts",
        "intensity_type": "background corrected counts",
        "operator": "Synthetic operator",
        "lab": "Synthetic lab",
        "measured_at": "2026-08-29T00:00:00Z",
        "step_size_deg": 0.02,
    }
    values.update(overrides)
    return values


def _reference_metadata(**overrides):
    values = {
        "source_name": "Synthetic user reference",
        "source_record_id": "SYN-CARD-1",
        "phase_name": "Synthetic Alpha",
        "formula": "A2B",
        "polymorph": "alpha synthetic form",
        "radiation_source": "Cu Kalpha",
        "wavelength_angstrom": 1.5406,
        "title": "Synthetic reference peak list",
        "authors": ["Synthetic Author"],
        "year": 2026,
        "url": "https://example.invalid/synthetic-reference",
        "review_status": "needs_review",
    }
    values.update(overrides)
    return values


def _reference_csv():
    return b"Position,Relative Intensity\n20.05,100\n29.35,60\n40.0,20\n"


def test_measured_csv_common_mapping_hash_metadata_order_and_plot_rows():
    raw = b"Intensity (counts),2theta\n5,20.0\n6,19.5\n,21.0\n"
    pattern = xrd.import_measured_pattern_csv(
        raw, source_filename="synthetic measured.csv", metadata=_metadata())

    assert pattern.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert pattern.pattern_id.startswith("xrdpat_")
    assert pattern.original_headings == ["Intensity (counts)", "2theta"]
    assert pattern.column_mapping["method"] == "common_heading_autodetect"
    assert pattern.column_mapping["canonical_to_original"] == {
        "two_theta": "2theta", "intensity": "Intensity (counts)"}
    assert pattern.raw_imported_row_count == pattern.accepted_row_count == 3
    assert [row["two_theta_deg"] for row in pattern.rows] == [20.0, 19.5, 21.0]
    assert pattern.source_order_preserved is True
    assert pattern.source_was_monotonic is False
    assert "no silent reordering" in " ".join(pattern.warnings).lower()
    assert pattern.rows[-1]["intensity"] is None
    assert pattern.plot_ready_rows()[-1]["y_intensity"] is None
    assert pattern.radiation_source == "Cu Kalpha"
    assert pattern.wavelength_angstrom == pytest.approx(1.5406)
    assert pattern.instrument == "Synthetic bench instrument"
    assert pattern.scan_start_deg == 19.5 and pattern.scan_end_deg == 21.0


def test_invalid_nonfinite_and_nonphysical_two_theta_rejected_with_reasons():
    raw = (
        b"Angle,Counts\n10,\n11,-3\nnan,1\ninf,2\n0,3\n180.1,4\nabc,5\n,6\n")
    pattern = xrd.import_measured_pattern_csv(
        raw, source_filename="synthetic invalid rows.csv", metadata=_metadata())

    assert [row["two_theta_deg"] for row in pattern.rows] == [10.0, 11.0]
    assert pattern.rows[0]["intensity"] is None  # absence is not converted to zero
    assert pattern.rows[1]["intensity"] == -3.0  # processed signal is not clamped
    assert "background" in pattern.rows[1]["intensity_issue"].lower()
    assert {row["reason_code"] for row in pattern.rejected_rows} == {
        "non_finite_2theta", "physically_impossible_2theta", "non_numeric_2theta",
        "missing_2theta",
    }
    warning_blob = " ".join(pattern.warnings).lower()
    assert "negative intensity" in warning_blob and "without clamping" in warning_blob


def test_duplicate_two_theta_requires_and_records_explicit_policy():
    raw = b"2theta,Intensity\n20,1\n20,3\n21,4\n"
    with pytest.raises(xrd.XrdDataError, match="explicit duplicate_policy"):
        xrd.import_measured_pattern_csv(
            raw, source_filename="synthetic duplicates.csv", metadata=_metadata())

    retained = xrd.import_measured_pattern_csv(
        raw, source_filename="synthetic duplicates.csv", metadata=_metadata(),
        duplicate_policy=xrd.DUPLICATE_KEEP_ALL)
    assert retained.duplicate_policy == xrd.DUPLICATE_KEEP_ALL
    assert retained.accepted_row_count == 3
    assert retained.duplicate_two_theta == [{
        "two_theta_deg": 20.0, "source_row_numbers": [2, 3]}]

    rejected = xrd.import_measured_pattern_csv(
        raw, source_filename="synthetic duplicates.csv", metadata=_metadata(),
        duplicate_policy=xrd.DUPLICATE_REJECT_LATER)
    assert [row["intensity"] for row in rejected.rows] == [1.0, 4.0]
    assert rejected.accepted_row_count == 2
    assert any(row["reason_code"] == "duplicate_2theta_rejected_later"
               for row in rejected.rejected_rows)


def test_user_peak_list_preserves_selection_and_source_provenance_json_safely():
    raw = b"2theta,Counts\n20,10\n29.4,8\n"
    pattern = xrd.import_measured_pattern_csv(
        raw, source_filename="synthetic pattern.csv", metadata=_metadata(),
        user_peak_list=[20.0, {"two_theta_deg": 29.4, "relative_intensity": 80,
                               "note": "user reviewed"}],
        peak_selection_provenance={
            "provided_by": "Synthetic reviewer",
            "selected_at": "2026-08-29T01:00:00Z",
            "user_edits": [{"action": "retained", "two_theta_deg": 29.4}],
            "parameters": {},
        })
    assert pattern.user_peak_list[1]["relative_intensity"] == 80.0
    provenance = pattern.peak_selection_provenance
    assert provenance["selection_method"] == "user_supplied_peak_list"
    assert provenance["source_pattern_id"] == pattern.pattern_id
    assert provenance["source_sha256"] == pattern.source_sha256
    assert provenance["provided_by"] == "Synthetic reviewer"
    json.dumps(pattern.to_dict(), allow_nan=False)


def test_reference_csv_requires_source_provenance_and_retains_unknown_license():
    raw = _reference_csv()
    with pytest.raises(xrd.XrdDataError, match="source_metadata"):
        xrd.import_reference_csv(raw, source_filename="synthetic reference.csv")
    with pytest.raises(xrd.XrdDataError, match="source_name"):
        xrd.import_reference_csv(
            raw, source_filename="synthetic reference.csv",
            source_metadata={"phase_name": "Synthetic Alpha"})

    reference = xrd.import_reference_csv(
        raw, source_filename="synthetic reference.csv", source_metadata=_reference_metadata())
    assert reference.reference_id.startswith("xrdref_")
    assert reference.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert reference.source_filename == "synthetic reference.csv"
    assert reference.source_name == "Synthetic user reference"
    assert reference.source_record_id == "SYN-CARD-1"
    assert reference.phase_name == "Synthetic Alpha"
    assert reference.formula == "A2B" and reference.polymorph == "alpha synthetic form"
    assert reference.license_status == xrd.LICENSE_UNKNOWN
    assert reference.redistribution_permission_status == xrd.REDISTRIBUTION_UNKNOWN
    assert reference.reference_2theta == [20.05, 29.35, 40.0]
    assert reference.peaks[0]["relative_intensity"] == 100.0
    json.dumps(reference.to_dict(), allow_nan=False)


def test_reference_json_accepts_embedded_metadata_and_optional_intensity():
    raw = json.dumps({
        "source_name": "Synthetic JSON source",
        "phase_name": "Synthetic Beta",
        "formula": "AB2",
        "polymorph": "beta synthetic form",
        "radiation": "Cu Kalpha",
        "wavelength": 1.5406,
        "license_status": "unknown",
        "redistribution_status": "unknown",
        "peaks": [{"2theta": 22.0, "relative_intensity": 100}, {"2theta": 31.0}],
    }).encode()
    reference = xrd.import_reference_json(raw, source_filename="synthetic reference.json")
    assert reference.data_format == "json"
    assert reference.phase_name == "Synthetic Beta"
    assert reference.peaks[1]["relative_intensity"] is None
    assert reference.license_status == "unknown"
    assert reference.redistribution_permission_status == "unknown"


def test_external_match_records_identities_pairs_unmatched_dominant_and_tolerance():
    pattern = xrd.import_measured_pattern_csv(
        b"2theta,Counts\n20.0,5\n29.4,6\n55,2\n",
        source_filename="synthetic measured.csv", metadata=_metadata(),
        user_peak_list=[20.0, 29.4, 55.0],
        peak_selection_provenance={"provided_by": "Synthetic reviewer"})
    reference = xrd.import_reference_csv(
        _reference_csv(), source_filename="synthetic reference.csv",
        source_metadata=_reference_metadata())
    result = xrd.match_measured_peaks(pattern, tolerance=0.1, references=[reference])

    assert result.measured_peak_identity["pattern_id"] == pattern.pattern_id
    assert result.reference_identities == [reference.identity()]
    assert result.tolerance_deg == 0.1
    candidate = result.candidates[0]
    assert candidate["reference_identity"] == reference.identity()
    assert candidate["reference_provenance"]["license_status"] == "unknown"
    assert [(pair["measured"], pair["reference"], pair["delta_deg"])
            for pair in candidate["matched_peaks"]] == [
                (20.0, 20.05, 0.05), (29.4, 29.35, 0.05)]
    assert candidate["unmatched_reference_peaks"] == [40.0]
    assert result.unmatched_measured == [55.0]
    assert candidate["dominant_reference_2theta_deg"] == 20.05
    assert candidate["dominant_missing"] is False
    assert candidate["radiation_compatibility"] == xrd.RADIATION_COMPATIBLE
    assert candidate["wavelength_conversion_performed"] is False
    assert "tentative" in candidate["wording"].lower()
    assert "check against an appropriate reference source" in result.disclaimer.lower()


@pytest.mark.parametrize(
    ("measured_radiation", "measured_wavelength", "expected"),
    [
        ("unknown", None, xrd.RADIATION_UNKNOWN),
        ("Mo Kalpha", 0.7107, xrd.RADIATION_INCOMPATIBLE),
    ],
)
def test_unknown_or_incompatible_radiation_blocks_stronger_comparison(
        measured_radiation, measured_wavelength, expected):
    reference = xrd.import_reference_csv(
        _reference_csv(), source_filename="synthetic reference.csv",
        source_metadata=_reference_metadata())
    result = xrd.match_measured_peaks(
        [20.05, 29.35, 40.0], references=[reference],
        measured_identity={"peak_list_id": "synthetic-peaks"},
        measured_radiation=measured_radiation,
        measured_wavelength_angstrom=measured_wavelength)
    candidate = result.candidates[0]
    assert candidate["radiation_compatibility"] == expected
    assert candidate["stronger_comparison_blocked"] is True
    assert candidate["tentative_confidence"] == xrd.CONFIDENCE_LOW
    assert candidate["wavelength_conversion_performed"] is False
    assert result.radiation_compatibility[0]["wavelength_conversion_performed"] is False


def test_overlap_ambiguity_one_peak_weakness_and_formula_polymorph_caution():
    first = xrd.import_reference_csv(
        b"2theta,Intensity\n26.60,100\n40,50\n",
        source_filename="synthetic alpha.csv",
        source_metadata=_reference_metadata(
            phase_name="Synthetic Formula Candidate", formula="AB2", polymorph="",
            source_record_id="SYN-A"))
    second = xrd.import_reference_csv(
        b"2theta,Intensity\n26.65,100\n45,50\n",
        source_filename="synthetic beta.csv",
        source_metadata=_reference_metadata(
            phase_name="Synthetic Other Form", formula="AB2", polymorph="beta form",
            source_record_id="SYN-B"))
    result = xrd.match_measured_peaks(
        [26.62], references=[first, second], measured_radiation="Cu Kalpha",
        measured_wavelength_angstrom=1.5406)
    assert result.overlap_ambiguity_count == 1
    assert all(candidate["ambiguity_overlap_count"] == 1 for candidate in result.candidates)
    assert all(candidate["confidence"] == xrd.CONFIDENCE_LOW for candidate in result.candidates)
    formula_candidate = next(
        candidate for candidate in result.candidates if candidate["reference_id"] == first.reference_id)
    assert formula_candidate["formula_polymorph_caution"] is True
    caution_blob = " ".join(formula_candidate["limitations"] + result.warnings).lower()
    assert "formula" in caution_blob and "polymorph" in caution_blob
    assert "ambiguous" in " ".join(result.warnings).lower()


def test_external_result_language_and_serialization_remain_advisory_json_safe():
    reference = xrd.import_reference_csv(
        _reference_csv(), source_filename="synthetic reference.csv",
        source_metadata=_reference_metadata())
    result = xrd.match_measured_peaks(
        [20.05, 29.35, 40], references=[reference], measured_radiation="Cu Kalpha",
        measured_wavelength_angstrom=1.5406)
    encoded = json.dumps(result.to_dict(), sort_keys=True, allow_nan=False)
    low = encoded.lower()
    assert "tentative" in low and "advisory" in low and "possible match" in low
    assert "check against an appropriate reference source" in low
    assert not re.search(r"\b(identified|confirmed|validated|quantified)\b", low)
    assert "proprietary" not in low  # no restricted record or redistribution inference is bundled
