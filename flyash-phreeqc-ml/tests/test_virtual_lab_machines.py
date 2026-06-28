"""Pins for the **Virtual LAB machine catalogue** — backend-only honest metadata, no execution.

The catalogue declares, per machine, what it can honestly do, what it needs, how its output must be
labelled, what it must never claim, and how a result is verified in the real world. These tests pin
the scientific-safety invariants: nothing fabricates data, a ``validated_result`` needs measured data,
and the catalogue is not wired into the live website.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from flyash_phreeqc_ml.instruments import virtual_lab_machines as vlm

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _machine(mid):
    m = vlm.get_virtual_lab_machine(mid)
    assert m is not None, f"missing machine {mid!r}"
    return m


# --------------------------------------------------------------------------- #
# 1. Unique ids / 2. required metadata / 3. must_not_claim + safety_notes.
# --------------------------------------------------------------------------- #
def test_all_machine_ids_are_unique():
    ids = [m.machine_id for m in vlm.list_virtual_lab_machines()]
    assert len(ids) == len(set(ids)) == 12


def test_all_machines_have_required_metadata():
    for m in vlm.list_virtual_lab_machines():
        for f in vlm.REQUIRED_TEXT_FIELDS:
            v = getattr(m, f)
            assert isinstance(v, str) and v.strip(), f"{m.machine_id}: empty {f}"
        for f in vlm.REQUIRED_TUPLE_FIELDS:
            v = getattr(m, f)
            assert isinstance(v, tuple) and v, f"{m.machine_id}: empty {f}"
        assert m.mode in vlm.MODES
        assert m.execution_mode in vlm.EXECUTION_MODES
        assert m.status in vlm.STATUSES
        assert all(o in vlm.OUTPUT_DATA_TYPES for o in m.output_data_type)
        assert m.ui_activation_status == vlm.UI_NOT_ACTIVATED   # backend-only, never live-UI


def test_every_machine_has_must_not_claim_and_safety_notes():
    for m in vlm.list_virtual_lab_machines():
        assert m.must_not_claim, f"{m.machine_id}: no must_not_claim"
        assert m.safety_notes, f"{m.machine_id}: no safety_notes"
        assert m.verification_required, f"{m.machine_id}: no verification_required"
        assert m.real_world_verification_method.strip()


def test_audit_is_clean():
    assert vlm.audit_virtual_lab_machines() == []


# --------------------------------------------------------------------------- #
# 4–14. Per-machine scientific-safety invariants.
# --------------------------------------------------------------------------- #
def test_phreeqc_is_preview_then_confirm_and_never_validated():
    m = _machine(vlm.PHREEQC_LEACHING)
    assert m.execution_mode == vlm.EXEC_PREVIEW_THEN_CONFIRM
    assert m.output_data_type == (vlm.OUT_SIMULATED_MODEL_ESTIMATE,)
    assert any("validation" in c.lower() for c in m.must_not_claim)
    # A simulation can never be a validated result, even with measured data in hand.
    assert vlm.machine_can_produce_validated_result(vlm.PHREEQC_LEACHING, has_measured_data=True) is False


def test_xrd_states_formula_only_polymorph_limitation():
    m = _machine(vlm.XRD_ADVISORY)
    blob = " ".join(m.safety_notes + m.must_not_claim).lower()
    assert "formula" in blob
    assert "polymorph" in blob or "phase" in blob       # phase/polymorph ambiguity noted
    assert m.execution_mode == vlm.EXEC_ADVISORY_ONLY


def test_icp_is_data_processor_and_cannot_fabricate_measured():
    m = _machine(vlm.ICP_PROCESSOR)
    assert m.mode == vlm.MODE_DATA_PROCESSING
    assert any("fabricat" in c.lower() for c in m.must_not_claim)
    assert any("plasma" in c.lower() for c in m.must_not_claim)


def test_ftir_requires_measured_spectra_and_reference_for_strong_claims():
    m = _machine(vlm.FTIR_RAMAN)
    assert m.needs_reference_database is True
    blob = " ".join(m.safety_notes).lower()
    assert "measured" in blob and "reference" in blob
    assert any("definitive" in c.lower() or "weak" in c.lower() for c in m.must_not_claim)


def test_sem_eds_requires_measured_data_and_no_exact_phase_id():
    m = _machine(vlm.SEM_EDS)
    assert vlm.machine_requires_measured_data(vlm.SEM_EDS) is True
    assert any("phase" in c.lower() and ("alone" in c.lower() or "exact" in c.lower())
               for c in m.must_not_claim)


def test_tga_dsc_requires_measured_curves_and_no_fabrication():
    m = _machine(vlm.TGA_DSC)
    assert vlm.machine_requires_measured_data(vlm.TGA_DSC) is True
    assert any("fabricat" in c.lower() and "curve" in c.lower() for c in m.must_not_claim)


def test_mechanical_requires_measured_strength_data():
    m = _machine(vlm.MECHANICAL)
    assert vlm.machine_requires_measured_data(vlm.MECHANICAL) is True
    assert m.execution_mode == vlm.EXEC_MEASURED_DATA_REQUIRED
    assert m.output_data_type == (vlm.OUT_MEASURED_LAB_DATA,)
    assert any("before" in c.lower() and "test" in c.lower() for c in m.must_not_claim)


def test_ml_surrogate_requires_trained_model_and_no_accuracy_without_validation():
    m = _machine(vlm.ML_SURROGATE)
    assert vlm.machine_requires_trained_model(vlm.ML_SURROGATE) is True
    assert m.output_data_type == (vlm.OUT_ML_PREDICTION,)
    assert any("accuracy" in c.lower() and "validation" in c.lower() for c in m.must_not_claim)


def test_literature_requires_provenance_and_human_review_no_scholar_scraping():
    m = _machine(vlm.LITERATURE_ENGINE)
    blob = " ".join(m.verification_required + m.safety_notes).lower()
    assert "provenance" in blob
    assert "review" in blob
    assert any("google scholar" in c.lower() for c in m.must_not_claim)


def test_sustainability_is_advisory_order_of_magnitude():
    m = _machine(vlm.SUSTAINABILITY)
    assert m.mode == vlm.MODE_ADVISORY_PLANNING
    blob = (m.short_description + " " + m.what_it_can_do + " " + " ".join(m.safety_notes)).lower()
    assert "order-of-magnitude" in blob or "order of magnitude" in blob
    assert any("lca" in c.lower() or "feasibility" in c.lower() for c in m.must_not_claim)


def test_validation_assistant_cannot_validate_without_measured_data():
    assert vlm.machine_can_produce_validated_result(vlm.VALIDATION_UNCERTAINTY,
                                                    has_measured_data=False) is False
    assert vlm.machine_can_produce_validated_result(vlm.VALIDATION_UNCERTAINTY,
                                                    has_measured_data=True) is True
    m = _machine(vlm.VALIDATION_UNCERTAINTY)
    assert vlm.OUT_VALIDATED_RESULT in m.output_data_type
    assert any("validation without measured" in c.lower() for c in m.must_not_claim)


# --------------------------------------------------------------------------- #
# 15. Import-safety (no Streamlit) / 16. website untouched.
# --------------------------------------------------------------------------- #
def test_importing_virtual_lab_machines_does_not_import_streamlit():
    code = ("import sys; import flyash_phreeqc_ml.instruments.virtual_lab_machines as m; "
            "assert 'streamlit' not in sys.modules, 'importing pulled in streamlit'; print('ok')")
    res = subprocess.run([sys.executable, "-c", code], cwd=_ROOT,
                         env={**os.environ, "PYTHONPATH": _ROOT},
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "ok" in res.stdout
    # The module must not import streamlit in source either.
    import inspect
    assert "streamlit" not in inspect.getsource(vlm)


def test_app_py_and_ui_are_unchanged_by_this_task():
    if shutil.which("git") is None:
        pytest.skip("git not available")
    res = subprocess.run(["git", "status", "--porcelain", "--", "app.py", "ui"],
                         cwd=_ROOT, capture_output=True, text=True)
    if res.returncode != 0:
        pytest.skip("not a git repo / git error")
    changed = res.stdout.strip()
    assert changed == "", f"app.py / ui/ were modified by this task:\n{changed}"


# --------------------------------------------------------------------------- #
# Helper-function smoke checks.
# --------------------------------------------------------------------------- #
def test_helpers_filter_and_lookup():
    assert vlm.get_virtual_lab_machine("nope") is None
    assert {m.machine_id for m in vlm.list_machines_by_mode(vlm.MODE_DATA_PROCESSING)} == {
        vlm.ICP_PROCESSOR, vlm.SEM_EDS, vlm.TGA_DSC, vlm.MECHANICAL}
    assert vlm.list_machines_by_status(vlm.STATUS_ACTIVE_EXISTING)
    assert vlm.machine_requires_reference_database(vlm.PHREEQC_LEACHING) is True
