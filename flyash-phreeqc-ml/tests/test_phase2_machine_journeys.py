"""Phase 2 machine-to-durable-result journeys (synthetic fixtures only)."""
from __future__ import annotations

from flyash_phreeqc_ml import workspace_store
from flyash_phreeqc_ml.instruments import virtual_lab_machine_runner as runner
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines
from flyash_phreeqc_ml.instruments import icp_processor
from ui import digital_lab


def _context(tmp_path):
    store = workspace_store.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project("Synthetic machine journey")
    material = store.create_material(
        project.project_id, "Synthetic fixture material", material_type="test fixture",
        composition_provenance={"source_type": "synthetic_demo"})
    return store, project, material


def _save(store, project, material, machine_id, payload, result):
    return store.create_run(
        project.project_id, material.material_id, machine_id, payload,
        status=result.status, output_type=result.output_data_type,
        epistemic_type=result.output_data_type, result_data=result.results,
        warnings=result.warnings, validation_state=result.validation_status,
        model_identity={"model_name": result.provenance.get("model_name"),
                        "model_version": result.provenance.get("model_version")}
        if result.provenance.get("model_name") else {},
        environment_identity=result.results.get("runtime_availability", {}),
    )


def test_icp_qc_states_save_and_reopen_without_invalid_validation_rows(tmp_path):
    store, project, material = _context(tmp_path)
    payload = {"source": "measured", "rows": [
        {"row_id": "usable", "sample_id": "S1", "element": "Ca", "concentration": 40.078,
         "unit": "mg/L", "dilution_factor": 1.0, "measured_or_predicted": "measured"},
        {"row_id": "censored", "sample_id": "S1", "element": "Sc", "concentration": 0.02,
         "unit": "ppb", "dilution_factor": 1.0, "detection_limit": 0.05,
         "measured_or_predicted": "measured"},
        {"row_id": "review", "sample_id": "S1", "element": "Si", "concentration": 1.0,
         "unit": "mg/L", "dilution_factor": None, "measured_or_predicted": "measured"},
        {"row_id": "excluded", "sample_id": "S2", "element": "Ca", "concentration": -1.0,
         "unit": "mg/L", "dilution_factor": 1.0, "measured_or_predicted": "measured"},
    ]}
    result = runner.run_virtual_lab_machine(machines.ICP_PROCESSOR, payload)
    assert result.status == runner.STATUS_PROCESSED
    assert result.results["qc_summary"] == {
        "usable": 1, "censored": 1, "review_required": 1, "excluded": 1}
    by_id = {row["row_id"]: row for row in result.results["corrected"]}
    assert by_id["usable"]["validation_eligible"] is True
    assert by_id["censored"]["validation_eligible"] is False
    assert by_id["review"]["validation_eligible"] is False
    assert by_id["excluded"]["validation_eligible"] is False
    saved = _save(store, project, material, machines.ICP_PROCESSOR, payload, result)
    reopened = workspace_store.WorkspaceStore(store.root).get_run(saved.run_id)
    assert reopened.result_data["qc_summary"] == result.results["qc_summary"]
    assert store.run_staleness(reopened) == (False, [])


def test_icp_display_frame_is_arrow_safe_for_mixed_raw_provenance():
    rows = [dict(item) for item in digital_lab._ICP_DEMO_ROWS]
    rows[1]["blank_value"] = "NaN"
    result = icp_processor.process(rows)
    frame = digital_lab._icp_display_frame(result)
    assert str(frame["supplied_blank_value"].dtype) == "string"
    assert str(frame["supplied_concentration"].dtype) == "string"
    # Streamlit uses this conversion; it must not need its noisy mixed-type fallback.
    import pyarrow as pa
    pa.Table.from_pandas(frame, preserve_index=False)


def test_xrd_advisory_saved_result_remains_tentative(tmp_path):
    store, project, material = _context(tmp_path)
    payload = {"measured_peaks": [20.0, 29.4], "fixture": "synthetic_test_only"}
    result = runner.run_virtual_lab_machine(machines.XRD_ADVISORY, payload)
    assert result.output_data_type == machines.OUT_ADVISORY_INTERPRETATION
    assert result.can_be_used_for_validation_claim is False
    assert "identif" not in result.result_summary.lower()
    saved = _save(store, project, material, machines.XRD_ADVISORY, payload, result)
    reopened = store.get_run(saved.run_id)
    assert reopened.epistemic_type == machines.OUT_ADVISORY_INTERPRETATION
    assert reopened.validation_state != runner.VAL_VALIDATED


def test_ml_without_approved_model_is_blocked_and_has_no_prediction(tmp_path):
    store, project, material = _context(tmp_path)
    payload = {"features": {"curing_age_days": 28}, "model": None}
    result = runner.run_virtual_lab_machine(machines.ML_SURROGATE, payload)
    assert result.status == runner.STATUS_TRAINED_MODEL_REQUIRED
    assert result.output_data_type != machines.OUT_ML_PREDICTION
    assert result.results == {}
    # Non-serializable model objects are never put in the durable snapshot; a controlled blocked
    # record retains only the explicit missing-model identity.
    durable_payload = {"features": payload["features"], "model_identity": None}
    saved = _save(store, project, material, machines.ML_SURROGATE, durable_payload, result)
    assert store.get_run(saved.run_id).result_data == {}


def test_validation_criteria_gate_and_scoped_validated_result(tmp_path):
    store, project, material = _context(tmp_path)
    base = {"measured": {"Ca": 1.0}, "predicted": {"Ca": 1.2}}
    advisory = runner.run_virtual_lab_machine(machines.VALIDATION_UNCERTAINTY, base)
    assert advisory.validation_status == runner.VAL_COMPARISON_AVAILABLE
    assert advisory.output_data_type == machines.OUT_ADVISORY_INTERPRETATION
    unmet_payload = {**base, "criteria": {"max_abs_error": 0.1}}
    unmet = runner.run_virtual_lab_machine(machines.VALIDATION_UNCERTAINTY, unmet_payload)
    assert unmet.validation_status == runner.VAL_COMPARISON_AVAILABLE
    assert unmet.can_be_used_for_validation_claim is False
    met_payload = {**base, "criteria": {"max_abs_error": 0.25}}
    met = runner.run_virtual_lab_machine(machines.VALIDATION_UNCERTAINTY, met_payload)
    assert met.validation_status == runner.VAL_VALIDATED
    assert met.output_data_type == machines.OUT_VALIDATED_RESULT
    assert met.can_be_used_for_validation_claim is True
    saved = _save(store, project, material, machines.VALIDATION_UNCERTAINTY, met_payload, met)
    reopened = store.get_run(saved.run_id)
    assert reopened.validation_state == runner.VAL_VALIDATED
    assert reopened.epistemic_type == machines.OUT_VALIDATED_RESULT


def test_material_change_keeps_machine_result_only_in_history(tmp_path):
    store, project, material = _context(tmp_path)
    payload = {"goal": "synthetic test design"}
    result = runner.run_virtual_lab_machine(machines.EXPERIMENTAL_DESIGN, payload)
    saved = _save(store, project, material, machines.EXPERIMENTAL_DESIGN, payload, result)
    store.update_material(material.material_id,
                          process_conditions={"temperature": "changed synthetic input"})
    assert store.run_staleness(saved)[0] is True
    assert store.list_runs(project_id=project.project_id)[0].run_id == saved.run_id
    assert store.current_runs(project_id=project.project_id, material_id=material.material_id) == []
