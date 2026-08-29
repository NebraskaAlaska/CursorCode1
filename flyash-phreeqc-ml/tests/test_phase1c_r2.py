"""Phase 1C-R2 regressions using synthetic test fixtures only.

Nothing in this module is project research evidence: ICP rows and ML training rows are generated
solely to exercise epistemic, delegation, and compatibility contracts.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from flyash_phreeqc_ml.instruments import instrument_registry as registry
from flyash_phreeqc_ml.instruments import instrument_schema as schema
from flyash_phreeqc_ml.instruments import virtual_lab_machine_runner as runner
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines
from flyash_phreeqc_ml.ml_models import model_schema, train, training_data


def _icp_row(role=None, *, row_id="r1", concentration=1.0):
    row = {
        "row_id": row_id,
        "sample_id": "SYNTHETIC_R2_TEST",
        "element": "Ca",
        "concentration": concentration,
        "unit": "mM",
        "dilution_factor": 1.0,
    }
    if role is not None:
        row["measured_or_predicted"] = role
    return row


def _run_icp(rows, source=None, *, include_source=True):
    payload = {"rows": rows}
    if include_source:
        payload["source"] = source
    return runner.run_virtual_lab_machine(machines.ICP_PROCESSOR, payload)


def test_icp_measured_row_with_measured_source_is_measured():
    result = _run_icp([_icp_row("measured")], "measured")
    assert result.output_data_type == machines.OUT_MEASURED_LAB_DATA
    assert result.results["row_role_summary"]["authority"] == "processed_row_roles_and_qc"
    assert result.results["corrected"][0]["row_output_data_type"] == \
        machines.OUT_MEASURED_LAB_DATA


def test_icp_predicted_row_with_predicted_source_is_simulated_estimate():
    result = _run_icp([_icp_row("predicted")], "predicted")
    assert result.output_data_type == machines.OUT_SIMULATED_MODEL_ESTIMATE
    assert result.results["corrected"][0]["row_output_data_type"] == \
        machines.OUT_SIMULATED_MODEL_ESTIMATE


def test_icp_measured_row_with_predicted_source_fails_to_advisory():
    result = _run_icp([_icp_row("measured")], "predicted")
    assert result.output_data_type == machines.OUT_ADVISORY_INTERPRETATION
    assert result.results["corrected"][0]["role"] == "measured"
    assert result.results["row_role_summary"]["source_contradiction"] is True
    assert any("correct" in warning.lower() and "row" in warning.lower()
               for warning in result.warnings)


def test_icp_predicted_row_with_measured_source_fails_to_advisory():
    result = _run_icp([_icp_row("predicted")], "measured")
    assert result.output_data_type == machines.OUT_ADVISORY_INTERPRETATION
    assert result.results["corrected"][0]["role"] == "predicted"
    assert result.results["row_role_summary"]["source_contradiction"] is True


def test_icp_mixed_rows_are_advisory_and_preserve_each_row_type():
    rows = [_icp_row("measured", row_id="measured"),
            _icp_row("predicted", row_id="predicted", concentration=0.8)]
    result = _run_icp(rows, None, include_source=False)
    assert result.output_data_type == machines.OUT_ADVISORY_INTERPRETATION
    assert {row["role"] for row in result.results["corrected"]} == {"measured", "predicted"}
    assert {row["row_output_data_type"] for row in result.results["corrected"]} == {
        machines.OUT_MEASURED_LAB_DATA, machines.OUT_SIMULATED_MODEL_ESTIMATE}


@pytest.mark.parametrize("role", [None, "unknown_role"])
def test_icp_missing_or_unknown_role_never_becomes_measured(role):
    result = _run_icp([_icp_row(role)], "measured")
    assert result.output_data_type == machines.OUT_USER_PROVIDED_ASSUMPTION
    assert result.output_data_type != machines.OUT_MEASURED_LAB_DATA
    assert result.results["row_role_summary"]["unresolved_role_rows"] == 1


def test_icp_omitted_source_uses_processed_row_role():
    result = _run_icp([_icp_row("measured")], include_source=False)
    assert result.output_data_type == machines.OUT_MEASURED_LAB_DATA
    assert result.provenance["declared_source"] == "unspecified"


def test_icp_valid_residual_comparison_remains_advisory():
    rows = [_icp_row("measured", row_id="measured", concentration=1.0),
            _icp_row("predicted", row_id="predicted", concentration=0.8)]
    result = _run_icp(rows, "measured")
    assert result.results["residuals"]
    assert result.validation_status == runner.VAL_COMPARISON_AVAILABLE
    assert result.output_data_type == machines.OUT_ADVISORY_INTERPRETATION
    assert result.can_be_used_for_validation_claim is False


def test_icp_source_text_cannot_promote_predicted_row_to_measured():
    result = _run_icp([_icp_row("predicted")], "measured")
    assert result.output_data_type != machines.OUT_MEASURED_LAB_DATA
    assert result.results["corrected"][0]["row_output_data_type"] == \
        machines.OUT_SIMULATED_MODEL_ESTIMATE


def test_icp_source_text_cannot_relabel_measured_row_as_simulated():
    result = _run_icp([_icp_row("measured")], "predicted")
    assert result.output_data_type != machines.OUT_SIMULATED_MODEL_ESTIMATE
    assert result.results["corrected"][0]["row_output_data_type"] == \
        machines.OUT_MEASURED_LAB_DATA


@pytest.fixture(scope="module")
def synthetic_approved_test_model():
    if not train.sklearn_available():
        pytest.skip("scikit-learn not installed")
    rows = training_data.demo_rows(n=14, seed=37)
    for index, row in enumerate(rows):
        # Synthetic gate fixture only: this simulates approved lab provenance for runner testing.
        row.source_type = training_data.SOURCE_LAB
        row.source_id = f"synthetic-r2-test-fixture-{index}"
        row.user_review_status = training_data.REVIEW_APPROVED
        row.notes = "synthetic Phase 1C-R2 test fixture; not project research evidence"
    return train.train_model(
        rows, date="2026-08-29", name="synthetic_r2_approved_test_fixture")


def _run_ml(model):
    return runner.run_virtual_lab_machine(
        machines.ML_SURROGATE,
        {"model": model, "features": {"plastic_dosage_percent": 10.0}},
    )


def test_ml_valid_approved_test_model_produces_prediction(synthetic_approved_test_model):
    result = _run_ml(synthetic_approved_test_model)
    assert result.status == runner.STATUS_PROCESSED
    assert result.output_data_type == machines.OUT_ML_PREDICTION
    assert isinstance(result.results["value"], float)
    assert result.provenance["model_artifact_usable"] is True


def test_ml_valid_prediction_contains_model_and_training_provenance(
        synthetic_approved_test_model):
    result = _run_ml(synthetic_approved_test_model)
    assert result.provenance["model_name"] == synthetic_approved_test_model.name
    assert result.provenance["model_version"] == synthetic_approved_test_model.version
    assert result.provenance["training_data_status"] == model_schema.TRAINING_DATA_APPROVED
    assert result.provenance["source_of_model"] == training_data.SOURCE_LAB
    assert result.provenance["n_training_rows"] == synthetic_approved_test_model.n_train
    assert result.provenance["model_status_appeared_approved"] is True


def test_ml_demo_model_remains_blocked():
    if not train.sklearn_available():
        pytest.skip("scikit-learn not installed")
    result = _run_ml(train.train_demo_model(n=12, seed=41))
    assert result.status == runner.STATUS_TRAINED_MODEL_REQUIRED
    assert result.output_data_type != machines.OUT_ML_PREDICTION
    assert result.results == {}


@pytest.mark.parametrize("training_status", [
    model_schema.TRAINING_DATA_EXPLORATORY,
    model_schema.TRAINING_DATA_LEGACY_UNKNOWN,
])
def test_ml_exploratory_and_legacy_models_remain_blocked(
        synthetic_approved_test_model, training_status):
    model = replace(synthetic_approved_test_model, training_data_status=training_status)
    result = _run_ml(model)
    assert result.status == runner.STATUS_TRAINED_MODEL_REQUIRED
    assert result.output_data_type != machines.OUT_ML_PREDICTION
    assert result.provenance["prediction_delegated"] is False


@pytest.mark.parametrize("model", [None, object()])
def test_ml_missing_and_wrong_type_models_produce_no_prediction(model):
    result = _run_ml(model)
    assert result.status == runner.STATUS_TRAINED_MODEL_REQUIRED
    assert result.output_data_type != machines.OUT_ML_PREDICTION
    assert result.results == {}


def test_ml_approved_status_with_missing_pipeline_fails_closed(synthetic_approved_test_model):
    malformed = replace(synthetic_approved_test_model, pipeline=None)
    result = _run_ml(malformed)
    assert result.status == runner.STATUS_TRAINED_MODEL_REQUIRED
    assert result.output_data_type == machines.OUT_ADVISORY_INTERPRETATION
    assert result.provenance["model_status_appeared_approved"] is True
    assert result.provenance["model_artifact_usable"] is False
    assert result.results == {}
    assert malformed.pipeline is None


def test_ml_approved_status_with_unfitted_pipeline_fails_closed(synthetic_approved_test_model):
    from sklearn.base import clone

    unfitted = clone(synthetic_approved_test_model.pipeline)
    malformed = replace(synthetic_approved_test_model, pipeline=unfitted)
    result = _run_ml(malformed)
    assert result.status == runner.STATUS_TRAINED_MODEL_REQUIRED
    assert result.output_data_type != machines.OUT_ML_PREDICTION
    assert result.provenance["model_artifact_usable"] is False
    assert result.results == {}
    assert malformed.pipeline is unfitted


def test_ml_malformed_artifact_contains_no_numeric_prediction_or_exception_detail(
        synthetic_approved_test_model):
    malformed = replace(synthetic_approved_test_model, pipeline=None)
    result = _run_ml(malformed)
    rendered = str(result.to_dict()).lower()
    assert "value" not in result.results
    assert "traceback" not in rendered
    assert "nonetype" not in rendered


def _ids(items):
    return tuple(machine.machine_id for machine in items)


def test_legacy_requirement_status_values_are_distinct_from_maturity_values():
    requirement_statuses = {
        machines.STATUS_REQUIRES_TRAINED_MODEL,
        machines.STATUS_REQUIRES_MEASURED_DATA,
        machines.STATUS_REQUIRES_REFERENCE_DATA,
    }
    assert requirement_statuses.isdisjoint(machines.MATURITIES)


def test_legacy_trained_model_query_returns_only_actual_model_requirement():
    ids = set(_ids(machines.list_machines_by_status(
        machines.STATUS_REQUIRES_TRAINED_MODEL)))
    assert ids == {machines.ML_SURROGATE}
    assert not ids.intersection({machines.PHREEQC_LEACHING, machines.ICP_PROCESSOR,
                                 machines.LITERATURE_ENGINE, machines.VALIDATION_UNCERTAINTY})


def test_legacy_measured_data_query_excludes_unrelated_advisory_workflows():
    ids = set(_ids(machines.list_machines_by_status(
        machines.STATUS_REQUIRES_MEASURED_DATA)))
    assert ids == {machines.FTIR_RAMAN, machines.SEM_EDS, machines.TGA_DSC,
                   machines.MECHANICAL, machines.VALIDATION_UNCERTAINTY}
    assert machines.SUSTAINABILITY not in ids
    assert machines.EXPERIMENTAL_DESIGN not in ids


def test_legacy_reference_data_query_excludes_unrelated_limited_workflows():
    ids = set(_ids(machines.list_machines_by_status(
        machines.STATUS_REQUIRES_REFERENCE_DATA)))
    assert ids == {machines.PHREEQC_LEACHING, machines.XRD_ADVISORY,
                   machines.FTIR_RAMAN, machines.LITERATURE_ENGINE}
    assert not ids.intersection({machines.SUSTAINABILITY, machines.EXPERIMENTAL_DESIGN,
                                 machines.SEM_EDS, machines.TGA_DSC, machines.MECHANICAL})


def test_canonical_maturity_queries_filter_only_maturity():
    for maturity in machines.MATURITIES:
        selected = machines.list_machines_by_maturity(maturity)
        assert selected == tuple(
            machine for machine in machines.list_virtual_lab_machines()
            if machine.maturity == maturity)
    assert machines.list_machines_by_maturity(machines.STATUS_ACTIVE_EXISTING) == ()


def test_deprecated_active_and_advisory_statuses_are_narrow_and_deterministic():
    active = machines.list_machines_by_status(machines.STATUS_ACTIVE_EXISTING)
    advisory = machines.list_machines_by_status(machines.STATUS_PHASE_1_ADVISORY)
    assert _ids(active) == (machines.ICP_PROCESSOR,)
    assert _ids(advisory) == (machines.XRD_ADVISORY,)
    assert active == machines.list_machines_by_status(machines.STATUS_ACTIVE_EXISTING)
    assert advisory == machines.list_machines_by_status(machines.STATUS_PHASE_1_ADVISORY)


def test_legacy_mode_queries_do_not_collapse_into_canonical_advisory_mode():
    assert schema.MODE_SIGNAL_SIMULATION != schema.MODE_ADVISORY_PLANNING
    assert schema.MODE_TRAINED_MODEL != schema.MODE_TRAINED_MODEL_PREDICTION
    signal_ids = _ids(machines.list_machines_by_mode(schema.MODE_SIGNAL_SIMULATION))
    advisory_ids = _ids(machines.list_machines_by_mode(schema.MODE_ADVISORY_PLANNING))
    assert signal_ids == (machines.XRD_ADVISORY, machines.FTIR_RAMAN)
    assert set(signal_ids) < set(advisory_ids)
    assert _ids(spec.canonical_machine for spec in registry.by_mode(
        schema.MODE_SIGNAL_SIMULATION)) == signal_ids
    assert _ids(machines.list_machines_by_mode(schema.MODE_TRAINED_MODEL)) == (
        machines.ML_SURROGATE,)


def test_compatibility_queries_reuse_canonical_metadata_objects():
    canonical = machines.list_virtual_lab_machines()
    compatibility = registry.all_instruments()
    assert all(spec.canonical_machine is machine
               for spec, machine in zip(compatibility, canonical, strict=True))
    selected = machines.list_machines_by_status(machines.STATUS_REQUIRES_MEASURED_DATA)
    assert all(machine is machines.get_virtual_lab_machine(machine.machine_id)
               for machine in selected)
