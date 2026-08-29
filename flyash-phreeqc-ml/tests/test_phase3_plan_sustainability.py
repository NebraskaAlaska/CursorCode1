"""Phase 3 advisory plan and sustainability-screen contracts (synthetic only)."""
from __future__ import annotations

import csv
from io import StringIO
import json

import pandas as pd
import pytest

from flyash_phreeqc_ml.experiments import plan_generator as plans
from flyash_phreeqc_ml.experiments import sustainability_score as sustainability
from flyash_phreeqc_ml import phase3_artifacts, workspace_store
from flyash_phreeqc_ml.instruments import virtual_lab_machine_runner as runner
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines
from ui import phase3_workflows


def _generic_plan(**overrides):
    values = {
        "material_id": "mat_" + "a" * 32,
        "goal": "Synthetic test data: compare supplied treatments",
        "factors": {"temperature_C": [20, 20, 40], "treatment": ["A", "B"]},
        "fixed_conditions": {"duration_min": 60},
        "replicates": 2,
        "max_run_count": 20,
        "controls": [{"name": "blank", "conditions": {"treatment": "none"}}],
        "sample_prefix": "SYN",
        "experiment_date": "2026-08-29",
    }
    values.update(overrides)
    return plans.build_generic_factor_plan(**values)


def test_generic_plan_is_deterministic_deduplicated_and_has_no_cfa_leak():
    first = _generic_plan()
    second = _generic_plan()
    assert first["run_count"] == 10  # four unique factor conditions + one control, two replicates
    assert first["duplicate_conditions_removed"] == 2
    assert first["plan"].to_dict(orient="records") == second["plan"].to_dict(orient="records")
    assert first["plan"]["sample_id"].is_unique
    assert set(first["plan"]["replicate"]) == {1, 2}
    assert not any("CFA" in str(value) for value in first["plan"].to_numpy().ravel())
    for column in plans.GENERIC_MEASUREMENT_COLUMNS:
        assert (first["plan"][column] == "").all()


def test_generic_plan_refuses_hidden_levels_nonfinite_and_over_cap():
    with pytest.raises(plans.ExperimentPlanError, match="non-empty list"):
        _generic_plan(factors={"temperature_C": 20})
    with pytest.raises(plans.ExperimentPlanError, match="finite"):
        _generic_plan(factors={"temperature_C": [float("nan")]})
    with pytest.raises(plans.ExperimentPlanLimitError, match="exceeds"):
        _generic_plan(max_run_count=9)


def test_cfa_preset_is_explicit_and_measurements_are_blank():
    preset = plans.build_cfa_preset_advisory(experiment_date="2026-08-29")
    assert preset["mode"] == "cfa_leaching_preset"
    assert preset["run_count"] == 14
    assert set(preset["plan"]["fly_ash_type"]) == {"CFA"}
    assert (preset["plan"]["Ca_mM"] == "").all()
    assert "not a universal material design" in " ".join(preset["assumptions"])


def test_plan_exports_guard_untrusted_text_without_mutating_internal_values():
    result = _generic_plan(factors={"label": ["=2+2"]}, controls=[])
    assert result["plan"].iloc[0]["label"] == "=2+2"
    csv_text = plans.plan_to_csv(result["plan"])
    assert "'=2+2" in csv_text
    exported = json.loads(plans.plan_to_json(result))
    assert exported["rows"][0]["label"] == "=2+2"


def test_plan_csv_blocks_leading_whitespace_and_control_formula_payloads():
    frame = pd.DataFrame([{
        "space_tab_formula": " \t=2+2",
        "newline_formula": "\n+SUM(1,1)",
        "carriage_formula": "\r@unsafe",
        "negative_numeric": -1.25,
        "negative_numeric_text": "-2.50",
        "negative_exponent_text": " -3e-4",
    }])
    original = frame.to_dict(orient="records")
    row = next(csv.DictReader(StringIO(plans.plan_to_csv(frame))))
    assert row["space_tab_formula"].startswith("' \t=")
    assert row["newline_formula"].startswith("'\n+")
    assert row["carriage_formula"].startswith("'\r@")
    assert row["negative_numeric"] == "-1.25"
    assert row["negative_numeric_text"] == "-2.50"
    assert row["negative_exponent_text"] == " -3e-4"
    assert frame.to_dict(orient="records") == original


def _inventory_rows():
    return [
        {
            "item": "Synthetic sourced electricity",
            "amount": 10,
            "amount_unit": "kWh",
            "factor": 0.4,
            "factor_unit": "kg CO2e/kWh",
            "factor_source": "Synthetic literature fixture",
            "source_type": "literature_evidence",
            "evidence_id": "art_" + "e" * 32,
            "boundary": "synthetic gate-to-gate",
        },
        {
            "item": "Synthetic assumed reagent",
            "amount": 2,
            "amount_unit": "kg",
            "factor": 0,
            "factor_unit": "kg CO2e/kg",
            "factor_source": "Synthetic user assumption",
            "source_type": "user_assumption",
            "boundary": "synthetic gate-to-gate",
            "factor_min": 0,
            "factor_max": 0,
        },
        {
            "item": "Synthetic missing transport factor",
            "amount": 5,
            "amount_unit": "tkm",
            "factor": None,
            "boundary": "synthetic gate-to-gate",
        },
    ]


def test_inventory_calculates_only_compatible_supplied_factors_and_keeps_missing():
    result = sustainability.screen_inventory(_inventory_rows())
    by_item = {row["item"]: row for row in result["contributions"]}
    assert by_item["Synthetic sourced electricity"]["contribution"] == 4.0
    assert by_item["Synthetic assumed reagent"]["contribution"] == 0.0
    assert by_item["Synthetic assumed reagent"]["source_type"] == "user_assumption"
    assert by_item["Synthetic missing transport factor"]["contribution"] is None
    assert result["missing_factors"][0]["item"] == "Synthetic missing transport factor"
    assert result["included_count"] == 2
    assert result["excluded_count"] == 1
    assert result["totals"] == [{
        "boundary": "synthetic gate-to-gate",
        "result_unit": "kg CO2e",
        "total": 4.0,
        "sensitivity_min": 0.0,
        "sensitivity_max": 0.0,
    }]


def test_inventory_blocks_incompatible_units_missing_source_and_boundary():
    rows = [{
        "item": "Synthetic incompatible row",
        "amount": 2,
        "amount_unit": "kg",
        "factor": 3,
        "factor_unit": "kg CO2e/kWh",
        "factor_source": "Synthetic source",
        "source_type": "user_assumption",
        "boundary": "synthetic boundary",
    }]
    result = sustainability.screen_inventory(rows)
    assert result["included_count"] == 0
    assert "incompatible" in result["blocked_rows"][0]["reason"]

    rows[0]["factor_unit"] = "kg CO2e/kg"
    rows[0]["factor_source"] = ""
    result = sustainability.screen_inventory(rows)
    assert "requires a source" in result["blocked_rows"][0]["reason"]


def test_inventory_csv_blocks_leading_whitespace_and_control_formula_payloads():
    result = {"contributions": [{
        "item": " \t=HYPERLINK(\"https://example.invalid\")",
        "factor_source": "\n+cmd|' /C calc'!A0",
        "notes": "\r@unsafe",
        "negative_numeric": -1.25,
        "negative_numeric_text": "-2.50",
        "negative_exponent_text": " -3e-4",
    }]}
    row = next(csv.DictReader(StringIO(sustainability.inventory_to_csv(result))))
    assert row["item"].startswith("' \t=")
    assert row["factor_source"].startswith("'\n+")
    assert row["notes"].startswith("'\r@")
    assert row["negative_numeric"] == "-1.25"
    assert row["negative_numeric_text"] == "-2.50"
    assert row["negative_exponent_text"] == " -3e-4"


def test_sustainability_factor_gate_rejects_forged_generic_reviewed_evidence(tmp_path):
    store = workspace_store.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project("Synthetic forged factor project")
    material = store.create_material(project.project_id, "Synthetic forged factor material")
    forged = store.create_artifact(
        project.project_id,
        material.material_id,
        workspace_store.ARTIFACT_EVIDENCE,
        {"fixture": "synthetic forged evidence input"},
        status="reviewed",
        payload={
            "review_status": "reviewed",
            "provenance": {"title": "Synthetic forged factor"},
            "source_location": {"page": "1"},
        },
        creator="Synthetic forger",
        reviewer="Synthetic forger",
        reason="Synthetic generic-envelope forgery",
    )
    store.link_artifact_to_material(forged.artifact_id, evidence=True)
    with pytest.raises(workspace_store.WorkspaceStoreError, match="strict domain/envelope"):
        phase3_workflows._reviewed_factor_evidence(
            store,
            forged.artifact_id,
            project_id=project.project_id,
            material_id=material.material_id,
        )


def test_condition_proxy_requires_explicit_row_eligibility():
    frame = pd.DataFrame([
        {"sample_id": "SYN-OK", "eligible": True, "NaOH_M": 0.5, "time_min": 10},
        {"sample_id": "SYN-BLOCK", "eligible": False, "NaOH_M": 1.0, "time_min": 20},
    ])
    result = sustainability.screen_condition_proxy(frame, eligibility_column="eligible")
    assert result["included_rows"] == 1
    assert result["excluded_rows"] == 1
    assert result["scores"].iloc[0]["sample_id"] == "SYN-OK"
    with pytest.raises(sustainability.SustainabilityScreenError, match="eligibility"):
        sustainability.screen_condition_proxy(frame.drop(columns="eligible"),
                                               eligibility_column="eligible")


def test_canonical_runners_delegate_to_shared_advisory_modules_without_outcome_claims():
    design = runner.run_virtual_lab_machine(machines.EXPERIMENTAL_DESIGN, {
        "mode": "generic_user_defined",
        "material_id": "mat_" + "a" * 32,
        "goal": "Synthetic test plan",
        "factors": {"temperature_C": [20, 40]},
        "fixed_conditions": {},
        "replicates": 1,
        "max_run_count": 2,
    })
    assert design.status == runner.STATUS_ADVISORY
    assert design.results["run_count"] == 2
    assert all(row["measured_response"] == "" for row in design.results["rows"])
    design_blob = json.dumps(design.to_dict()).lower()
    assert "optimum" not in design.result_summary.lower()
    assert "guarantee" not in design.result_summary.lower()
    assert "cfa" not in design_blob

    screen = runner.run_virtual_lab_machine(machines.SUSTAINABILITY, {
        "mode": "user_inventory_screen",
        "inventory_rows": _inventory_rows(),
    })
    assert screen.status == runner.STATUS_ADVISORY
    assert screen.results["missing_factors"]
    assert screen.can_be_used_for_validation_claim is False
    assert "certified" not in screen.result_summary.lower()


def test_plan_and_screen_artifacts_save_reload_and_bind_exact_runs(tmp_path):
    store = workspace_store.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project("Synthetic test advisory project")
    material = store.create_material(project.project_id, "Synthetic test advisory material")
    plan = _generic_plan(material_id=material.material_id, controls=[])
    plan_payload = {key: value for key, value in plan.items() if key != "plan"}
    plan_payload["rows"] = plan["plan"].to_dict(orient="records")
    saved_plan = phase3_artifacts.save_advisory_artifact_run(
        store,
        project_id=project.project_id,
        material_id=material.material_id,
        record_type=workspace_store.ARTIFACT_EXPERIMENT_PLAN,
        machine_id=machines.EXPERIMENTAL_DESIGN,
        input_snapshot={"fixture": "Synthetic test plan", "goal": plan["goal"]},
        payload=plan_payload,
        source_identity={},
        creator="Synthetic test planner",
        reason="Synthetic test durable plan",
        warnings=["Synthetic test data; advisory plan only."],
    )
    assert saved_plan.artifact.status == "finalized"
    assert saved_plan.artifact.related_run_id == saved_plan.run.run_id

    screen_result = sustainability.screen_inventory(_inventory_rows())
    saved_screen = phase3_artifacts.save_advisory_artifact_run(
        store,
        project_id=project.project_id,
        material_id=material.material_id,
        record_type=workspace_store.ARTIFACT_SUSTAINABILITY_SCREEN,
        machine_id=machines.SUSTAINABILITY,
        input_snapshot={"fixture": "Synthetic test inventory", "rows": _inventory_rows()},
        payload=screen_result,
        source_identity={},
        creator="Synthetic test screener",
        reason="Synthetic test durable screen",
        evidence_identity=[{"evidence_id": "art_" + "e" * 32}],
    )
    reopened = workspace_store.WorkspaceStore(store.root)
    assert reopened.get_artifact(saved_plan.artifact.artifact_id).payload["run_count"] \
        == plan["run_count"]
    assert reopened.get_artifact(saved_screen.artifact.artifact_id).payload["missing_factors"]
    assert reopened.get_run(saved_screen.run.run_id).input_snapshot["artifact_identities"]
    assert reopened.run_staleness(saved_plan.run) == (False, [])
