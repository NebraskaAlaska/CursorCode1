"""Phase 1B ICP QC integrity regressions (synthetic values only)."""
from __future__ import annotations

import io
import itertools
import json
import math
from pathlib import Path

import pandas as pd
import pytest

from flyash_phreeqc_ml.compare import compare_measured_vs_phreeqc, comparison_inclusion
from flyash_phreeqc_ml import import_mapping, replicates
from flyash_phreeqc_ml.instruments import icp_processor as icp
from flyash_phreeqc_ml.instruments import virtual_lab_machine_runner as machine_runner
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines
from flyash_phreeqc_ml.ml import residual_model, residual_stats
from flyash_phreeqc_ml.parsers.icp_parser import parse_experimental_release


def _row(row_id: str, role: str, value, **extra) -> dict:
    return {
        "row_id": row_id,
        "sample_id": "S1",
        "element": "Ca",
        "concentration": value,
        "unit": "mM",
        "dilution_factor": 1.0,
        "measured_or_predicted": role,
        **extra,
    }


def _prediction_frame(*, duplicate: bool = False) -> pd.DataFrame:
    rows = [{"record_key": "pred-1", "state": "batch", "pH": 10.0, "mol_Ca": 0.001}]
    if duplicate:
        rows.append({"record_key": "pred-1", "state": "batch", "pH": 10.0, "mol_Ca": 0.002})
    return pd.DataFrame(rows)


def test_valid_pair_produces_expected_measured_minus_predicted_residual():
    result = icp.process([_row("m", "measured", 2.0), _row("p", "predicted", 1.25)])
    assert len(result.residuals) == 1
    assert result.residuals[0].residual_mM == pytest.approx(0.75)
    assert result.residuals[0].validation_eligible is True


@pytest.mark.parametrize(
    ("value", "code", "preserved"),
    [
        (-1.0, icp.QC_NEGATIVE_CONCENTRATION, -1.0),
        (float("nan"), icp.QC_NONFINITE_CONCENTRATION, "NaN"),
        (float("inf"), icp.QC_NONFINITE_CONCENTRATION, "Infinity"),
    ],
)
def test_hard_invalid_concentration_is_visible_but_cannot_enter_residual(value, code, preserved):
    result = icp.process([_row("m", "measured", value), _row("p", "predicted", 1.0)])
    measured = result.corrected[0]
    assert measured.supplied_concentration == preserved
    assert measured.qc_status == icp.QC_EXCLUDED
    assert code in measured.qc_codes
    assert measured.value_mM is None
    assert measured.validation_eligible is False
    assert result.residuals == []


@pytest.mark.parametrize("dilution", ["bad", 0, -2, float("nan"), float("inf")])
def test_invalid_dilution_never_falls_back_to_one(dilution):
    row = _row("m", "measured", 2.0, dilution_factor=dilution)
    processed = icp.process([row]).corrected[0]
    assert processed.dilution_factor is None or processed.dilution_factor <= 0
    assert processed.dilution_factor != 1.0
    assert processed.value_mM is None
    assert processed.qc_status == icp.QC_REVIEW_REQUIRED


def test_missing_dilution_requires_explicit_undiluted_intent():
    row = _row("m", "measured", 2.0)
    row.pop("dilution_factor")
    processed = icp.process([row]).corrected[0]
    assert processed.supplied_dilution_factor is None
    assert processed.dilution_factor is None
    assert icp.QC_MISSING_DILUTION in processed.qc_codes
    assert processed.validation_eligible is False


def test_below_detection_value_retains_evidence_but_is_censored_and_excluded():
    result = icp.process([
        _row("m", "measured", 0.02, detection_limit=0.05),
        _row("p", "predicted", 0.03),
    ])
    measured = result.corrected[0]
    assert measured.supplied_concentration == 0.02
    assert measured.supplied_detection_limit == 0.05
    assert measured.detection_limit == 0.05
    assert measured.qc_status == icp.QC_CENSORED
    assert measured.value_mM is None
    assert result.residuals == []


def test_blank_above_reading_is_not_silently_substituted_with_zero():
    result = icp.process([
        _row("m", "measured", 0.2, blank_value=0.5),
        _row("p", "predicted", 0.0),
    ])
    measured = result.corrected[0]
    assert measured.blank_corrected_value == pytest.approx(-0.3)
    assert measured.corrected_value == pytest.approx(-0.3)
    assert measured.qc_status == icp.QC_CENSORED
    assert measured.value_mM is None
    assert result.residuals == []


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("unit", "furlongs/L", icp.QC_UNKNOWN_UNIT),
        ("element", "Xx", icp.QC_UNKNOWN_ELEMENT),
        ("measured_or_predicted", "mystery", icp.QC_UNKNOWN_ROLE),
    ],
)
def test_unknown_unit_element_or_role_is_validation_ineligible(field, value, code):
    row = _row("m", "measured", 2.0)
    row[field] = value
    processed = icp.process([row]).corrected[0]
    assert code in processed.qc_codes
    assert processed.validation_eligible is False


def test_element_without_conversion_authority_is_excluded_even_for_identity_unit():
    processed = icp.process([_row(
        "m", "measured", 2.0, element="Mn")]).corrected[0]
    assert icp.QC_UNKNOWN_ELEMENT in processed.qc_codes
    assert processed.value_mM is None
    assert processed.validation_eligible is False


@pytest.mark.parametrize("limit", [-1.0, "bad", float("nan"), float("inf")])
def test_invalid_detection_limit_is_hard_excluded(limit):
    processed = icp.process([_row("m", "measured", 2.0, detection_limit=limit)]).corrected[0]
    assert processed.qc_status == icp.QC_EXCLUDED
    assert processed.value_mM is None


def test_duplicate_measured_rows_do_not_use_last_row_wins():
    rows = [_row("m1", "measured", 1.0), _row("m2", "measured", 2.0),
            _row("p", "predicted", 1.5)]
    result = icp.process(rows)
    assert result.residuals == []
    assert all(icp.QC_DUPLICATE_MEASURED in r.qc_codes for r in result.corrected[:2])


def test_duplicate_predicted_rows_do_not_use_last_row_wins():
    rows = [_row("m", "measured", 1.0), _row("p1", "predicted", 1.5),
            _row("p2", "predicted", 2.0)]
    result = icp.process(rows)
    assert result.residuals == []
    assert all(icp.QC_DUPLICATE_PREDICTED in r.qc_codes for r in result.corrected[1:])


def test_row_order_cannot_change_duplicate_eligibility_or_result():
    rows = [_row("m1", "measured", 1.0), _row("m2", "measured", 2.0),
            _row("p", "predicted", 1.5)]
    outcomes = []
    for order in itertools.permutations(rows):
        result = icp.process(order)
        outcomes.append((len(result.residuals), sorted(
            (r.row_id, r.qc_status, r.validation_eligible) for r in result.corrected)))
    assert all(outcome == outcomes[0] for outcome in outcomes)


def test_explicit_reviewable_correction_restores_eligibility_with_provenance():
    unresolved = _row("m", "measured", 2.0)
    unresolved.pop("dilution_factor")
    corrected = icp.resolve_reviewable_issue(
        unresolved, field="dilution_factor", replacement_value=1.0,
        resolved_by="synthetic-test-user", reason="confirmed undiluted synthetic fixture",
        resolved_at="2026-08-28T00:00:00+00:00",
    )
    processed = icp.process([corrected]).corrected[0]
    assert processed.qc_status == icp.QC_USABLE
    assert processed.validation_eligible is True
    assert processed.supplied_dilution_factor is None
    assert processed.dilution_factor == 1.0
    assert processed.resolutions[0]["original_value"] is None
    assert processed.resolutions[0]["replacement_value"] == 1.0


def test_hard_invalid_issue_cannot_be_approved_anyway():
    with pytest.raises(icp.HardInvalidResolutionError):
        icp.resolve_reviewable_issue(
            _row("m", "measured", -1.0), field="concentration", replacement_value=1.0,
            resolved_by="synthetic-test-user", reason="approve anyway")


@pytest.mark.parametrize(("field", "replacement"), [
    ("unit", "mg/L"),
    ("dilution_factor", 2.0),
    ("role", "predicted"),
    ("measured_or_predicted", "predicted"),
    ("element", "Si"),
    ("sample_id", "S2"),
])
def test_already_valid_authoritative_metadata_cannot_be_relabelled(field, replacement):
    with pytest.raises(icp.QcResolutionError, match="already valid"):
        icp.resolve_reviewable_issue(
            _row("m", "measured", 1.0),
            field=field,
            replacement_value=replacement,
            resolved_by="synthetic-test-user",
            reason="adversarial valid-metadata mutation",
        )


def test_embedded_role_and_sample_mutations_cannot_bypass_scientific_boundaries():
    predicted = _row("p", "predicted", 1.5)
    predicted["qc_resolutions"] = [{
        "field": "role",
        "original_value": "predicted",
        "replacement_value": "measured",
        "resolved_by": "synthetic-attacker",
        "reason": "attempt predicted-as-measured",
    }]
    duplicate = _row("m2", "measured", 2.0)
    duplicate["qc_resolutions"] = [{
        "field": "sample_id",
        "original_value": "S1",
        "replacement_value": "S2",
        "resolved_by": "synthetic-attacker",
        "reason": "attempt duplicate ambiguity bypass",
    }]
    result = icp.process([_row("m1", "measured", 1.0), duplicate, predicted])
    by_id = {row.row_id: row for row in result.corrected}
    assert by_id["p"].role == "predicted"
    assert by_id["m2"].sample_id == "S1"
    assert icp.QC_INVALID_RESOLUTION in by_id["p"].qc_codes
    assert icp.QC_INVALID_RESOLUTION in by_id["m2"].qc_codes
    assert all(icp.QC_DUPLICATE_MEASURED in by_id[row_id].qc_codes
               for row_id in ("m1", "m2"))
    assert result.residuals == []


def test_missing_metadata_correction_requires_valid_replacement_and_is_single_use():
    unresolved = _row("m", "", 1.0)
    with pytest.raises(icp.QcResolutionError, match="unknown"):
        icp.resolve_reviewable_issue(
            unresolved,
            field="role",
            replacement_value="validated measurement",
            resolved_by="synthetic-test-user",
            reason="invalid replacement",
        )
    corrected = icp.resolve_reviewable_issue(
        unresolved,
        field="role",
        replacement_value="measured",
        resolved_by="synthetic-test-user",
        reason="restore missing role metadata",
    )
    assert icp.process([corrected]).corrected[0].validation_eligible is True
    with pytest.raises(icp.QcResolutionError, match="already has a correction"):
        icp.resolve_reviewable_issue(
            corrected,
            field="role",
            replacement_value="predicted",
            resolved_by="synthetic-test-user",
            reason="second relabelling attempt",
        )


def test_missing_primary_role_cannot_contradict_recognized_predicted_alias():
    row = _row("p", "predicted", 1.0)
    row["role"] = "predicted"
    row["measured_or_predicted"] = None
    with pytest.raises(icp.QcResolutionError, match="cannot relabel"):
        icp.resolve_reviewable_issue(
            row,
            field="role",
            replacement_value="measured",
            resolved_by="synthetic-test-user",
            reason="attempt predicted-as-measured through a blank primary alias",
        )
    corrected = icp.resolve_reviewable_issue(
        row,
        field="role",
        replacement_value="predicted",
        resolved_by="synthetic-test-user",
        reason="restore the recognized predicted identity",
    )
    processed = icp.process([corrected]).corrected[0]
    assert processed.role == "predicted"
    assert processed.validation_eligible is True


def test_duplicate_selection_is_explicit_and_provenance_preserving():
    rows = [_row("m1", "measured", 1.0), _row("m2", "measured", 2.0),
            _row("p", "predicted", 1.5)]
    resolution = [{"sample_id": "S1", "element": "Ca", "role": "measured",
                   "selected_row_id": "m2", "resolved_by": "synthetic-test-user",
                   "reason": "explicit synthetic replicate selection"}]
    result = icp.process(rows, duplicate_resolutions=resolution)
    assert result.residuals[0].residual_mM == pytest.approx(0.5)
    assert result.residuals[0].measured_row_id == "m2"
    assert result.resolution_provenance == resolution


def test_conflicting_duplicate_selections_remain_ambiguous():
    rows = [_row("m1", "measured", 1.0), _row("m2", "measured", 2.0),
            _row("p", "predicted", 1.5)]
    resolutions = [
        {"sample_id": "S1", "element": "Ca", "role": "measured",
         "selected_row_id": selected, "resolved_by": "synthetic-test-user",
         "reason": "conflicting synthetic selections"}
        for selected in ("m1", "m2")
    ]
    result = icp.process(rows, duplicate_resolutions=resolutions)
    assert result.residuals == []
    assert any("selection remains ambiguous" in warning for warning in result.warnings)


def test_raw_correction_and_qc_provenance_survives_csv_serialization():
    result = icp.process([{
        "row_id": "m", "sample_id": "S1", "element": "Ca", "concentration": 84.0,
        "unit": "mg/L", "dilution_factor": 10.0, "blank_value": 0.5,
        "detection_limit": 0.05, "measured_or_predicted": "measured",
    }])
    exported = pd.DataFrame(result.corrected_export_table()).to_csv(index=False)
    restored = pd.read_csv(io.StringIO(exported)).iloc[0]
    assert restored["supplied_concentration"] == 84.0
    assert restored["supplied_unit"] == "mg/L"
    assert restored["supplied_dilution_factor"] == 10.0
    assert restored["supplied_blank_value"] == 0.5
    assert restored["blank_corrected_value"] == 83.5
    assert restored["corrected_value"] == 835.0
    assert restored["supplied_detection_limit"] == 0.05
    assert restored["conversion_id"] == "mgL_to_mM"
    assert restored["qc_status"] == icp.QC_USABLE
    assert json.loads(restored["qc_codes"]) == []
    assert json.loads(restored["resolutions"]) == []
    assert bool(restored["validation_eligible"])


def test_explicit_resolution_provenance_survives_csv_serialization():
    unresolved = _row("m", "measured", 2.0)
    unresolved.pop("dilution_factor")
    corrected = icp.resolve_reviewable_issue(
        unresolved, field="dilution_factor", replacement_value=1.0,
        resolved_by="synthetic-test-user", reason="confirmed undiluted fixture",
        resolved_at="2026-08-28T00:00:00+00:00",
    )
    result = icp.process([corrected])
    exported = pd.DataFrame(result.corrected_export_table()).to_csv(index=False)
    restored = pd.read_csv(io.StringIO(exported)).iloc[0]
    provenance = json.loads(restored["resolutions"])
    assert provenance == [{
        "field": "dilution_factor", "original_value": None,
        "reason": "confirmed undiluted fixture", "replacement_value": 1.0,
        "resolved_at": "2026-08-28T00:00:00+00:00",
        "resolved_by": "synthetic-test-user",
    }]


def test_virtual_lab_icp_runner_preserves_machine_readable_qc_state():
    rows = [_row("valid", "measured", 1.0, sample_id="S1"),
            _row("censored", "measured", 0.01, sample_id="S2", detection_limit=0.1),
            _row("invalid", "measured", -1.0, sample_id="S3")]
    result = machine_runner.run_virtual_lab_machine(
        machines.ICP_PROCESSOR, {"rows": rows, "source": "measured"})
    statuses = [row["qc_status"] for row in result.results["corrected"]]
    assert statuses == [icp.QC_USABLE, icp.QC_CENSORED, icp.QC_EXCLUDED]
    assert result.results["qc_summary"][icp.QC_CENSORED] == 1
    assert all("validation_eligible" in row for row in result.results["corrected"])


def test_wide_comparison_excludes_invalid_and_duplicate_rows_from_all_residual_consumers():
    measured = pd.DataFrame([
        {"sample_id": "S1", "Ca_mM": -1.0},
        {"sample_id": "S1", "Ca_mM": 2.0},
    ])
    comparison = compare_measured_vs_phreeqc(
        measured, _prediction_frame(), mapping={"S1": "pred-1"})
    assert comparison["residual_Ca"].isna().all()
    assert not comparison["residual_Ca_validation_eligible"].any()
    statuses = {"S1": "exact"}
    assert residual_stats.bias_table(comparison, statuses).empty


def test_duplicate_prediction_mapping_is_visible_and_residual_is_blocked():
    measured = pd.DataFrame([{"sample_id": "S1", "Ca_mM": 2.0}])
    comparison = compare_measured_vs_phreeqc(
        measured, _prediction_frame(duplicate=True), mapping={"S1": "pred-1"})
    assert len(comparison) == 2
    assert comparison["prediction_mapping_ambiguous"].all()
    assert comparison["residual_Ca"].isna().all()


def test_replicate_comparisons_fail_closed_for_duplicate_prediction_keys():
    measured = pd.DataFrame([{"sample_id": "S1", "Ca_mM": 2.0}])
    sample_map = pd.DataFrame([
        {"sample_id": "S1", "phreeqc_record_key": "pred-1"}])
    condition_map = {
        replicates.condition_key(measured.iloc[0].to_dict()): "pred-1"}
    manifest_rows = [
        {"phreeqc_record_key": "pred-1", "predicted_Ca_mM": 1.0},
        {"phreeqc_record_key": "pred-1", "predicted_Ca_mM": 1.5},
    ]
    outcomes = []
    for order in (manifest_rows, list(reversed(manifest_rows))):
        manifest = pd.DataFrame(order)
        individual = replicates.individual_replicate_comparison(
            measured, sample_map, manifest)
        condition = replicates.condition_mean_comparison(
            measured, condition_map, manifest)
        outcomes.append((
            pd.isna(individual.loc[0, "residual_Ca"]),
            pd.isna(condition.loc[0, "residual_Ca"]),
        ))
    assert outcomes == [(True, True), (True, True)]


def test_serialized_ineligible_residual_cannot_enter_statistics_or_training_gate():
    comparison = pd.DataFrame([{
        "sample_id": "S1", "condition_key": "C1", "residual_Ca": 999.0,
        "residual_Ca_validation_eligible": False,
    }])
    statuses = {"S1": replicates.MAPPING_STATUS_EXACT}
    assert residual_stats.bias_table(comparison, statuses).empty
    assert residual_stats.exact_residuals(comparison, statuses, "Ca").empty
    gate = residual_model.gate_status(
        comparison, statuses, "Ca", min_pairs=1, min_conditions=1)
    assert gate.n_exact_pairs == 0
    assert gate.meets is False


def test_active_file_path_requires_explicit_final_corrected_stage(tmp_path: Path):
    raw = pd.DataFrame([{"Sample": "S1", "Calcium": 2.0}])
    mapping = {"sample_id": "Sample", "Ca_mM": "Calcium"}

    confirmed = import_mapping.build_schema_frame(
        raw, mapping, icp_input_stage=icp.FINAL_CORRECTED_STAGE,
        icp_stage_confirmed=True, icp_role=icp.MEASURED,
    )
    confirmed_path = tmp_path / "confirmed.csv"
    confirmed.to_csv(confirmed_path, index=False)
    parsed_confirmed = parse_experimental_release(confirmed_path)
    comparison = compare_measured_vs_phreeqc(
        parsed_confirmed, _prediction_frame(), mapping={"S1": "pred-1"},
        require_explicit_icp_stage=True,
    )
    assert comparison.loc[0, "residual_Ca"] == pytest.approx(1.0)
    assert bool(comparison.loc[0, "residual_Ca_validation_eligible"])

    unconfirmed = import_mapping.build_schema_frame(
        raw, mapping, icp_input_stage=icp.UNKNOWN_STAGE,
        icp_stage_confirmed=False, icp_role=icp.MEASURED,
    )
    unconfirmed_path = tmp_path / "unconfirmed.csv"
    unconfirmed.to_csv(unconfirmed_path, index=False)
    parsed_unconfirmed = parse_experimental_release(unconfirmed_path)
    blocked = compare_measured_vs_phreeqc(
        parsed_unconfirmed, _prediction_frame(), mapping={"S1": "pred-1"},
        require_explicit_icp_stage=True,
    )
    assert pd.isna(blocked.loc[0, "residual_Ca"])
    assert not bool(blocked.loc[0, "residual_Ca_validation_eligible"])
    assert icp.QC_STAGE_UNCONFIRMED in blocked.loc[0, "Ca_mM_qc_codes"]


def test_active_file_path_requires_explicit_measured_role(tmp_path: Path):
    frame = import_mapping.build_schema_frame(
        pd.DataFrame([{"Sample": "S1", "Calcium": 2.0}]),
        {"sample_id": "Sample", "Ca_mM": "Calcium"},
        icp_input_stage=icp.FINAL_CORRECTED_STAGE,
        icp_stage_confirmed=True, icp_role="",
    )
    path = tmp_path / "role_missing.csv"
    frame.to_csv(path, index=False)
    parsed = parse_experimental_release(path)
    blocked = compare_measured_vs_phreeqc(
        parsed, _prediction_frame(), mapping={"S1": "pred-1"},
        require_explicit_icp_stage=True,
    )
    assert pd.isna(blocked.loc[0, "residual_Ca"])
    assert icp.QC_MISSING_ROLE in blocked.loc[0, "Ca_mM_qc_codes"]


def test_inclusion_consumes_residual_qc_instead_of_recomputing_invalid_residual():
    measured = pd.DataFrame([{"sample_id": "S1", "Ca_mM": -1.0}])
    mapping = pd.DataFrame([{"sample_id": "S1", "phreeqc_record_key": "pred-1"}])
    comparison = compare_measured_vs_phreeqc(measured, _prediction_frame(), mapping=mapping)
    inclusion = comparison_inclusion(
        measured, mapping, comparison, "Ca_mM", manifest=pd.DataFrame([
            {"phreeqc_record_key": "pred-1", "predicted_Ca_mM": 1.0}]),
        include_unsafe=True)
    assert inclusion["plotted"].empty
    assert inclusion["reason_counts"]["ICP QC blocks ordinary residual validation"] == 1


def test_generic_validation_machine_rejects_invalid_recognized_icp_values():
    result = machine_runner.run_virtual_lab_machine(
        machines.VALIDATION_UNCERTAINTY,
        {"measured": {"Ca": -1.0}, "predicted": {"Ca": 1.0},
         "criteria": {"max_abs_error": 10}},
    )
    assert result.results.get("residuals") in (None, [])
    assert result.can_be_used_for_validation_claim is False
    assert result.validation_status == machine_runner.VAL_NO_MEASURED_DATA


def test_active_icp_result_ui_displays_usable_censored_and_excluded_rows():
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    script = """
from flyash_phreeqc_ml.instruments import icp_processor as icp
from ui.digital_lab import _render_icp_result
rows = [
    {"row_id":"valid","sample_id":"S","element":"Ca","concentration":1.0,"unit":"mM","dilution_factor":1.0,"role":"measured"},
    {"row_id":"censored","sample_id":"S","element":"Si","concentration":0.01,"unit":"mM","dilution_factor":1.0,"detection_limit":0.1,"role":"measured"},
    {"row_id":"invalid","sample_id":"S","element":"Al","concentration":-1.0,"unit":"mM","dilution_factor":1.0,"role":"measured"},
]
_render_icp_result(icp.process(rows))
"""
    app = AppTest.from_string(script, default_timeout=60).run()
    assert not app.exception
    assert app.dataframe
    table = app.dataframe[0].value
    assert set(table["qc_status"]) == {icp.QC_USABLE, icp.QC_CENSORED, icp.QC_EXCLUDED}
    assert set(table["validation_eligible"].astype(bool)) == {True, False}
