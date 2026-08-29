"""Focused Phase 3 pins for canonical metadata and thin CLI adapters."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from flyash_phreeqc_ml.instruments import virtual_lab_machines as vlm


def _machine(machine_id: str):
    machine = vlm.get_virtual_lab_machine(machine_id)
    assert machine is not None
    return machine


def _load_script(filename: str, module_name: str):
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(module_name, scripts_dir / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def design_script():
    return _load_script("06_generate_experiment_plan.py", "phase3_design_script_test")


@pytest.fixture
def sustainability_script():
    return _load_script("08_sustainability_score.py", "phase3_sustainability_script_test")


def test_phase3_contract_alignment_preserves_exact_twelve_ids():
    assert vlm.machine_ids() == (
        "phreeqc_leaching_simulator",
        "xrd_advisory",
        "icp_data_processor",
        "ftir_raman_interpreter",
        "sem_eds_processor",
        "tga_dsc_processor",
        "mechanical_testing_processor",
        "ml_surrogate_predictor",
        "literature_evidence_engine",
        "sustainability_cost_screening",
        "experimental_design_assistant",
        "validation_uncertainty_assistant",
    )
    assert vlm.audit_virtual_lab_machines() == []


def test_xrd_contract_is_measured_reference_licensed_and_tentative():
    machine = _machine(vlm.XRD_ADVISORY)
    contract = " ".join((
        machine.short_description,
        machine.what_it_can_do,
        *machine.required_inputs,
        *machine.provenance_requirements,
        *machine.safety_notes,
        *machine.must_not_claim,
    )).lower()
    assert machine.maturity == vlm.MATURITY_ADVISORY
    assert set(machine.output_data_type) == {
        vlm.OUT_MEASURED_LAB_DATA, vlm.OUT_ADVISORY_INTERPRETATION}
    assert "user-supplied measured" in contract
    assert "user-supplied reference" in contract
    assert "licence" in contract and "source" in contract
    assert "tentative" in contract
    assert "confirm" in contract and "certif" in contract


def test_evidence_contract_exposes_manual_human_reviewed_revision_workflow():
    machine = _machine(vlm.LITERATURE_ENGINE)
    contract = " ".join((
        machine.short_description,
        machine.what_it_can_do,
        *machine.honest_outputs,
        *machine.provenance_requirements,
        *machine.safety_notes,
    )).lower()
    assert "manually" in contract
    assert "page/table/figure" in contract
    assert "draft" in contract and "needs-review" in contract
    assert "reviewed" in contract and "rejected" in contract and "revision" in contract
    assert "only a human" in contract
    assert "provenance-rich" in contract


def test_design_contract_requires_explicit_mode_and_never_invents_generic_inputs():
    machine = _machine(vlm.EXPERIMENTAL_DESIGN)
    contract = " ".join((
        machine.short_description,
        machine.what_it_can_do,
        *machine.required_inputs,
        *machine.runtime_requirements,
        *machine.safety_notes,
    )).lower()
    assert machine.maturity == vlm.MATURITY_LIMITED
    assert "explicit mode" in contract
    assert "generic" in contract and "cfa" in contract and "preset" in contract
    assert "never selected implicitly" in contract
    assert "invents no material" in contract
    assert "blank result fields" in contract
    assert vlm.OUT_VALIDATED_RESULT not in machine.output_data_type


def test_sustainability_contract_has_separate_supplied_input_proxy_modes():
    machine = _machine(vlm.SUSTAINABILITY)
    contract = " ".join((
        machine.short_description,
        machine.what_it_can_do,
        *machine.required_inputs,
        *machine.provenance_requirements,
        *machine.runtime_requirements,
        *machine.safety_notes,
        *machine.must_not_claim,
    )).lower()
    assert machine.maturity == vlm.MATURITY_LIMITED
    assert "condition" in contract and "inventory" in contract
    assert "explicit mode" in contract and "eligibility" in contract
    assert "amount ×" in contract
    assert "no inferred or default" in contract
    assert "missing factors are never treated as zero" in contract
    assert "lca" in contract and "tea" in contract and "certified" in contract
    assert vlm.OUT_VALIDATED_RESULT not in machine.output_data_type


def test_design_script_requires_mode_and_delegates_generic_spec_without_cfa_leak(
        design_script, tmp_path):
    with pytest.raises(SystemExit) as missing_mode:
        design_script.main([])
    assert missing_mode.value.code == 2

    spec = tmp_path / "generic.json"
    spec.write_text(json.dumps({
        "material_id": "mat_user_supplied",
        "goal": "Test only the explicitly supplied levels",
        "factors": {"dose_user_unit": [2, 4]},
        "fixed_conditions": {"cover": "sealed"},
        "replicates": 2,
        "max_run_count": 4,
        "controls": [],
        "sample_prefix": "USR",
    }), encoding="utf-8")
    csv_path = tmp_path / "plan.csv"
    json_path = tmp_path / "plan.json"

    assert design_script.main([
        "generic", "--spec", str(spec), "--output", str(csv_path),
        "--json-output", str(json_path),
    ]) == 0
    frame = pd.read_csv(csv_path, keep_default_na=False)
    exported = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(frame) == 4
    assert set(frame["material_id"]) == {"mat_user_supplied"}
    assert set(frame["dose_user_unit"].astype(int)) == {2, 4}
    assert set(frame["replicate"].astype(int)) == {1, 2}
    assert all(frame[column].eq("").all() for column in exported["measurement_columns"])
    assert "CFA" not in csv_path.read_text(encoding="utf-8")


def test_design_script_refuses_incomplete_generic_spec_without_writing(
        design_script, tmp_path):
    spec = tmp_path / "incomplete.json"
    spec.write_text(json.dumps({
        "goal": "No hidden material should be selected",
        "factors": {"x": [1]},
        "fixed_conditions": {},
        "replicates": 1,
        "max_run_count": 1,
    }), encoding="utf-8")
    output = tmp_path / "should-not-exist.csv"
    assert design_script.main([
        "generic", "--spec", str(spec), "--output", str(output)]) == 2
    assert not output.exists()


def test_sustainability_inventory_script_preserves_missing_and_explicit_zero_factors(
        sustainability_script, tmp_path):
    with pytest.raises(SystemExit) as missing_mode:
        sustainability_script.main([])
    assert missing_mode.value.code == 2

    source = tmp_path / "inventory.json"
    source.write_text(json.dumps([
        {
            "item": "explicit zero", "amount": 3, "amount_unit": "kg",
            "factor": 0, "factor_unit": "kg CO2e/kg", "factor_source": "user supplied",
            "source_type": "user_assumption", "boundary": "gate",
        },
        {"item": "unknown factor", "amount": 2, "amount_unit": "kg", "boundary": "gate"},
    ]), encoding="utf-8")
    csv_path = tmp_path / "inventory.csv"
    json_path = tmp_path / "inventory-result.json"

    assert sustainability_script.main([
        "inventory", "--input", str(source), "--csv-output", str(csv_path),
        "--json-output", str(json_path),
    ]) == 0
    result = json.loads(json_path.read_text(encoding="utf-8"))
    by_item = {row["item"]: row for row in result["contributions"]}
    assert by_item["explicit zero"]["status"] == "included"
    assert by_item["explicit zero"]["contribution"] == 0
    assert by_item["unknown factor"]["status"] == "missing_factor"
    assert by_item["unknown factor"]["factor"] is None
    assert result["totals"] == [{
        "boundary": "gate", "result_unit": "kg CO2e",
        "sensitivity_max": None, "sensitivity_min": None, "total": 0.0}]


def test_sustainability_condition_script_requires_explicit_eligibility_and_is_advisory(
        sustainability_script, tmp_path, capsys):
    source = tmp_path / "conditions.csv"
    pd.DataFrame([
        {"sample_id": "=unsafe", "include": True, "NaOH_M": 1, "time_min": 2,
         "Ca_mM": 2, "Sc_ppb": 4, "total_REE_ppb": 8},
        {"sample_id": "excluded", "include": False, "NaOH_M": 50, "time_min": 50},
    ]).to_csv(source, index=False)
    csv_path = tmp_path / "proxy.csv"
    json_path = tmp_path / "proxy.json"

    assert sustainability_script.main([
        "condition-proxy", "--input", str(source), "--eligibility-column", "include",
        "--csv-output", str(csv_path), "--json-output", str(json_path),
    ]) == 0
    result = json.loads(json_path.read_text(encoding="utf-8"))
    assert result["mode"] == "experimental_condition_screening_proxy"
    assert result["included_rows"] == 1 and result["excluded_rows"] == 1
    assert len(result["scores"]) == 1
    csv_export = pd.read_csv(csv_path, keep_default_na=False)
    assert csv_export.loc[0, "sample_id"] == "'=unsafe"
    output = capsys.readouterr().out.lower()
    assert "screening proxy only" in output
    assert "not lca" in output and "tea" in output and "certified" in output
