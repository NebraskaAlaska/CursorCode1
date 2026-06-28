"""Pins for the **Virtual LAB machine runner** — safe, limited executable workflows.

Every machine returns the standard self-describing result; nothing fabricates measured data; PHREEQC
is preview/gate only; and a ``validated_result`` is possible only from a measured comparison that meets
explicit criteria.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from flyash_phreeqc_ml.instruments import virtual_lab_machine_runner as run
from flyash_phreeqc_ml.instruments import virtual_lab_machines as vlm

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STANDARD_FIELDS = {"machine_id", "status", "output_data_type", "result_summary", "results",
                   "warnings", "missing_inputs", "assumptions", "provenance", "validation_status",
                   "can_be_used_for_validation_claim"}

_FULL_PHREEQC = {"composition": {"SiO2": 34, "CaO": 24}, "leachant": "0.5 M NaOH",
                 "source_term": "1% release of Ca, Si", "database": "phreeqc.dat",
                 "temperature": 25, "liquid_solid_ratio": 10}


# 1.
def test_unknown_machine_id_is_rejected():
    r = run.run_virtual_lab_machine("does_not_exist", {})
    assert r.status == run.STATUS_UNKNOWN_MACHINE
    assert r.can_be_used_for_validation_claim is False


# 2.
def test_every_machine_returns_the_standard_fields():
    for m in vlm.list_virtual_lab_machines():
        mid = m.machine_id
        d = run.run_virtual_lab_machine(mid, {}).to_dict()
        assert set(d) == STANDARD_FIELDS, f"{mid}: fields {set(d) ^ STANDARD_FIELDS}"
        assert d["machine_id"] == mid
        assert d["output_data_type"] in vlm.OUTPUT_DATA_TYPES


# 3. & 4.
def test_phreeqc_never_executes_and_reports_missing_inputs():
    miss = run.run_virtual_lab_machine(vlm.PHREEQC_LEACHING, {"composition": {"SiO2": 34}})
    assert miss.status == run.STATUS_MISSING_INPUTS
    assert set(miss.missing_inputs) >= {"leachant", "source_term", "database", "temperature",
                                        "liquid_solid_ratio"}

    prev = run.run_virtual_lab_machine(vlm.PHREEQC_LEACHING, _FULL_PHREEQC, confirm=False)
    assert prev.status == run.STATUS_PREVIEW_REQUIRED
    assert prev.results["executed"] is False and prev.results["auto_run"] is False
    assert prev.output_data_type != vlm.OUT_SIMULATED_MODEL_ESTIMATE  # nothing executed → not an estimate

    conf = run.run_virtual_lab_machine(vlm.PHREEQC_LEACHING, _FULL_PHREEQC, confirm=True)
    assert conf.status == run.STATUS_CONFIRMED_NOT_EXECUTED
    assert conf.results["executed"] is False
    assert any("measured" in w.lower() for w in conf.warnings)  # needs measured comparison to validate


# 5.
def test_xrd_does_not_claim_identification_and_keeps_measured_peaks_user_provided():
    r = run.run_virtual_lab_machine(vlm.XRD_ADVISORY,
                                    {"phases": ["calcite"], "measured_peaks": [26.6, 29.4]})
    blob = (r.result_summary + " " + " ".join(r.warnings) + " " + str(r.results)).lower()
    assert "identified" not in blob and "confirmed" not in blob
    assert "measured_peaks_user_provided" in r.results
    assert r.output_data_type == vlm.OUT_ADVISORY_INTERPRETATION


# 6.
def test_icp_processes_user_rows_and_refuses_fabrication():
    from flyash_phreeqc_ml.instruments import icp_processor as icp
    assert icp.can_synthesize_measured_from_composition() is False     # never invents measured data
    empty = run.run_virtual_lab_machine(vlm.ICP_PROCESSOR, {"rows": []})
    assert empty.status == run.STATUS_MISSING_INPUTS                   # no rows → nothing fabricated
    rows = [{"sample_id": "S1", "element": "Ca", "concentration": 40.078, "unit": "mg/L",
             "measured_or_predicted": "measured"}]
    r = run.run_virtual_lab_machine(vlm.ICP_PROCESSOR, {"rows": rows, "source": "measured"})
    assert len(r.results["corrected"]) == len(rows)                    # exactly the rows provided
    assert r.output_data_type == vlm.OUT_MEASURED_LAB_DATA
    assert any("does not" in w.lower() and "plasma" in w.lower() for w in r.warnings)


# 7.
def test_ftir_uses_only_user_provided_peaks():
    r = run.run_virtual_lab_machine(vlm.FTIR_RAMAN, {"peaks": [3400, 5.0]})
    used = [a["peak_cm1"] for a in r.results["assignments"]] + \
           [u["peak_cm1"] for u in r.results["unmatched_peaks"]]
    assert sorted(used) == [5.0, 3400.0]                              # only the user's peaks appear
    assert any(u["status"] == run.STATUS_REFERENCE_DATA_NEEDED for u in r.results["unmatched_peaks"])
    assert any("not a compound identification" in w.lower() for w in r.warnings)


# 8.
def test_sem_eds_does_not_infer_exact_phases():
    r = run.run_virtual_lab_machine(vlm.SEM_EDS, {"rows": [{"element": "Ca", "value": 12},
                                                           {"element": "Si", "value": 8}]})
    assert r.results["elements_present"] == ["Ca", "Si"]
    assert any("element" in w.lower() and "not phases" in w.lower() for w in r.warnings)
    assert "phase" not in str(r.results).lower()                      # no phase inferred in the results


# 9.
def test_tga_dsc_computes_from_supplied_arrays_only():
    r = run.run_virtual_lab_machine(vlm.TGA_DSC, {"tga": {"temperature": [30, 800], "mass": [100, 80]}})
    assert r.results["tga"]["total_mass_loss"] == 20.0
    assert any("no curve is fabricated" in w.lower() for w in r.warnings)
    # An empty payload reports missing inputs rather than inventing a curve.
    assert run.run_virtual_lab_machine(vlm.TGA_DSC, {}).status == run.STATUS_MISSING_INPUTS


# 10.
def test_mechanical_computes_stats_only_from_supplied_strengths():
    rows = [{"sample_id": "A", "strength": 30, "curing_age_days": 28},
            {"sample_id": "A", "strength": 32, "curing_age_days": 28},
            {"sample_id": "A", "strength": 31, "curing_age_days": 28}]
    r = run.run_virtual_lab_machine(vlm.MECHANICAL, {"rows": rows})
    grp = r.results["by_sample_age"][0]
    assert grp["n"] == 3 and grp["mean_strength"] == 31.0 and grp["std_dev"] == 1.0
    assert r.output_data_type == vlm.OUT_MEASURED_LAB_DATA
    assert any("compliance" in w.lower() for w in r.warnings)         # never claims code compliance
    assert run.run_virtual_lab_machine(vlm.MECHANICAL, {}).status == run.STATUS_MISSING_INPUTS


# 11.
def test_ml_surrogate_requires_trained_model():
    r = run.run_virtual_lab_machine(vlm.ML_SURROGATE, {})
    assert r.status == run.STATUS_TRAINED_MODEL_REQUIRED
    assert r.output_data_type != vlm.OUT_ML_PREDICTION
    assert any("accuracy" in w.lower() or "validation" in w.lower() for w in r.warnings)


# 12.
def test_literature_requires_provenance_and_human_review():
    r = run.run_virtual_lab_machine(vlm.LITERATURE_ENGINE,
                                    {"rows": [{"title": "X", "doi": "10.1/x", "extracted_value": 1.2}]})
    assert r.results["human_review_required"] is True
    assert r.results["candidates"][0]["has_provenance"] is True
    assert r.output_data_type == vlm.OUT_LITERATURE_EVIDENCE
    assert any("google scholar" in w.lower() for w in r.warnings)
    # A row with no provenance is downgraded away from literature_evidence.
    np = run.run_virtual_lab_machine(vlm.LITERATURE_ENGINE, {"rows": [{"title": "Y"}]})
    assert np.output_data_type == vlm.OUT_ADVISORY_INTERPRETATION


# 13.
def test_sustainability_is_advisory_order_of_magnitude():
    r = run.run_virtual_lab_machine(vlm.SUSTAINABILITY,
                                    {"assumptions": {"energy": 100, "energy_co2_factor": 0.5}})
    assert r.output_data_type == vlm.OUT_ADVISORY_INTERPRETATION
    assert r.results["co2_estimate_order_of_magnitude"] == 50.0       # only user amount × user factor
    assert any("order-of-magnitude" in w.lower() for w in r.warnings)
    assert any("lca" in w.lower() for w in r.warnings)


# 14.
def test_experimental_design_produces_a_plan_but_no_results():
    r = run.run_virtual_lab_machine(vlm.EXPERIMENTAL_DESIGN,
                                    {"goal": "measure leaching pH", "factors": {"naoh": [0.1, 0.5]}})
    assert r.output_data_type == vlm.OUT_ADVISORY_INTERPRETATION
    assert r.results["controls"] and r.results["recommended_replicates"] == 3
    assert r.results["matrix_size"] == 2
    assert any("no experimental results" in w.lower() for w in r.warnings)


# 15.
def test_validation_computes_residuals_from_measured_vs_predicted():
    r = run.run_virtual_lab_machine(vlm.VALIDATION_UNCERTAINTY,
                                    {"measured": {"Ca": 2.0, "Si": 1.0},
                                     "predicted": {"Ca": 2.2, "Si": 0.9}})
    res = {row["key"]: row for row in r.results["residuals"]}
    assert abs(res["Ca"]["residual"] - (-0.2)) < 1e-9
    assert abs(res["Ca"]["abs_error"] - 0.2) < 1e-9
    assert abs(res["Ca"]["percent_error"] - 10.0) < 1e-6


# 16.
def test_validation_cannot_validate_without_measured_data_and_criteria():
    no_measured = run.run_virtual_lab_machine(vlm.VALIDATION_UNCERTAINTY, {"predicted": {"Ca": 2.0}})
    assert no_measured.output_data_type != vlm.OUT_VALIDATED_RESULT
    assert no_measured.validation_status == run.VAL_NO_MEASURED_DATA
    assert no_measured.can_be_used_for_validation_claim is False

    no_criteria = run.run_virtual_lab_machine(vlm.VALIDATION_UNCERTAINTY,
                                              {"measured": {"Ca": 2.0}, "predicted": {"Ca": 2.05}})
    assert no_criteria.output_data_type != vlm.OUT_VALIDATED_RESULT
    assert no_criteria.can_be_used_for_validation_claim is False

    not_met = run.run_virtual_lab_machine(vlm.VALIDATION_UNCERTAINTY,
                                          {"measured": {"Ca": 2.0}, "predicted": {"Ca": 3.0},
                                           "criteria": {"max_percent_error": 5}})
    assert not_met.output_data_type != vlm.OUT_VALIDATED_RESULT       # criteria not met → not validated
    assert not_met.can_be_used_for_validation_claim is False

    passed = run.run_virtual_lab_machine(vlm.VALIDATION_UNCERTAINTY,
                                         {"measured": {"Ca": 2.0}, "predicted": {"Ca": 2.05},
                                          "criteria": {"max_percent_error": 5}})
    assert passed.output_data_type == vlm.OUT_VALIDATED_RESULT        # measured + criteria + met
    assert passed.validation_status == run.VAL_VALIDATED
    assert passed.can_be_used_for_validation_claim is True


# 17.
def test_importing_runner_does_not_import_streamlit():
    code = ("import sys; import flyash_phreeqc_ml.instruments.virtual_lab_machine_runner as m; "
            "assert 'streamlit' not in sys.modules, 'importing pulled in streamlit'; print('ok')")
    res = subprocess.run([sys.executable, "-c", code], cwd=_ROOT,
                         env={**os.environ, "PYTHONPATH": _ROOT}, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "ok" in res.stdout
    import inspect
    assert "streamlit" not in inspect.getsource(run)


# 18. & 19. & 20.
def _porcelain(*pathspec):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    res = subprocess.run(["git", "status", "--porcelain", "--", *pathspec],
                         cwd=_ROOT, capture_output=True, text=True)
    if res.returncode != 0:
        pytest.skip("not a git repo / git error")
    return res.stdout.strip()


def test_app_py_unchanged():
    assert _porcelain("app.py") == ""


def test_ui_unchanged():
    assert _porcelain("ui") == ""


def test_no_sandbox_or_pipeline_files_changed():
    changed = _porcelain(".")
    offending = [ln for ln in changed.splitlines()
                 if "lab_sandbox" in ln or "flyash-lab-data-pipeline" in ln]
    assert offending == [], f"sandbox/pipeline files changed: {offending}"


# Helper-API smoke checks.
def test_helpers_validate_and_label():
    assert run.validate_machine_inputs("nope", {}) == ["unknown machine_id"]
    assert "leachant" in run.validate_machine_inputs(vlm.PHREEQC_LEACHING, {"composition": {}})
    assert run.explain_missing_inputs(vlm.ICP_PROCESSOR, {})          # non-empty guidance
    assert run.get_machine_result_label(vlm.PHREEQC_LEACHING) == vlm.OUT_SIMULATED_MODEL_ESTIMATE
    assert run.get_machine_result_label("nope") is None
