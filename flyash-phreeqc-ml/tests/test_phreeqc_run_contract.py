"""Regression coverage for the authoritative PHREEQC scientific run contract."""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.materials import profile_schema as materials
from flyash_phreeqc_ml.simulation import batch_executor
from flyash_phreeqc_ml.simulation import phreeqc_executor as executor
from flyash_phreeqc_ml.simulation import phreeqc_input_builder as builder
from flyash_phreeqc_ml.simulation import phreeqc_run_contract as contract
from flyash_phreeqc_ml.simulation import source_terms
from flyash_phreeqc_ml.simulation.scenario_schema import SimulationScenario


def _scenario(**overrides):
    values = dict(
        material_name="Class C fly ash", solid_mass_g=2.0, liquid_volume_mL=10.0,
        leachant_type="NaOH", leachant_concentration_M=0.5, time_min=60.0,
        temperature_C=25.0, target_elements=["Ca", "Si", "Al"],
    )
    values.update(overrides)
    return SimulationScenario.from_flat_dict(values)


def _profile():
    return materials.MaterialProfile(
        profile_id="gate-test", material_name="Class C fly ash",
        composition_basis=materials.BASIS_OXIDE_WT,
        entries=materials.parse_composition_text("CaO 20\nSiO2 40\nAl2O3 30"),
        verification_status=materials.STATUS_USER_CONFIRMED,
    )


def _ready_preview(**scenario_overrides):
    return builder.build_phreeqc_input_preview(
        _scenario(**scenario_overrides), material_profile=_profile(),
        dissolution_model=source_terms.global_release(0.01))


def _confirm(preview, exe, database):
    availability = executor.check_availability(exe=exe, database=database)
    assert availability.environment_identity is not None
    return contract.confirm_reviewed(
        contract.review_preview(preview), availability.environment_identity)


def _fake_exe_db(tmp_path):
    exe = tmp_path / "phreeqc"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    database = tmp_path / "database.dat"
    database.write_text("# fake database")
    return str(exe), str(database)


def test_incomplete_source_state_stays_previewable_but_cannot_be_reviewed_for_execution():
    preview = builder.build_phreeqc_input_preview(
        _scenario(), material_profile=_profile(), dissolution_model=source_terms.no_release())
    assert preview.phreeqc_input_text
    assert preview.status == builder.STATUS_NEEDS_SOURCE_TERM
    readiness = contract.assess_preview(preview)
    assert not readiness.scientific_ready
    assert "source" in " ".join(readiness.reasons).lower()
    with pytest.raises(contract.RunContractError):
        contract.review_preview(preview)


def test_measured_liquid_mode_does_not_require_irrelevant_bulk_composition_or_solid_mass():
    preview = builder.build_phreeqc_input_preview(
        _scenario(material_name=None, solid_mass_g=None),
        material_profile=None,
        dissolution_model=source_terms.measured_liquid({"Ca": 1.2, "Si": 0.4}),
    )
    assert preview.status == builder.STATUS_READY
    assert preview.source_term_status == source_terms.STATUS_MEASURED_LIQUID
    assert contract.assess_preview(preview).run_mode == contract.RUN_MODE_MEASURED_LIQUID
    assert "MEASURED" in preview.phreeqc_input_text


@pytest.mark.parametrize("leachant,concentration", [("water", None), ("HCl", 0.5)])
def test_existing_non_naoh_templates_remain_previewable_but_not_executable(leachant, concentration):
    preview = builder.build_phreeqc_input_preview(
        _scenario(leachant_type=leachant, leachant_concentration_M=concentration),
        material_profile=_profile(), dissolution_model=source_terms.global_release(0.01))
    assert preview.phreeqc_input_text
    assert preview.status == builder.STATUS_TEMPLATE_WARNING
    assert not contract.assess_preview(preview).scientific_ready


def test_executor_requires_explicit_confirmation_and_does_not_start_subprocess(monkeypatch, tmp_path):
    preview = _ready_preview()
    exe, database = _fake_exe_db(tmp_path)
    calls = []
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: calls.append(a))
    result = executor.execute_preview(
        preview, workdir=tmp_path / "ws", exe=exe, database=database)
    assert result.status == executor.STATUS_BLOCKED
    assert "confirmation" in (result.error_message or "").lower()
    assert calls == []
    assert not (tmp_path / "ws").exists()


def test_changed_input_cannot_reuse_stale_confirmation(monkeypatch, tmp_path):
    preview = _ready_preview()
    exe, database = _fake_exe_db(tmp_path)
    confirmation = _confirm(preview, exe, database)
    preview.phreeqc_input_text += "\n# changed after confirmation\n"
    calls = []
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: calls.append(a))
    result = executor.execute_preview(
        preview, confirmation=confirmation, workdir=tmp_path / "ws",
        exe=exe, database=database)
    assert result.status == executor.STATUS_BLOCKED
    assert "changed after review" in (result.error_message or "").lower()
    assert calls == []


def test_executor_writes_and_runs_exact_confirmed_snapshot(monkeypatch, tmp_path):
    preview = _ready_preview()
    exe, database = _fake_exe_db(tmp_path)
    confirmation = _confirm(preview, exe, database)
    captured = {}

    def _run(cmd, **kwargs):
        captured["input"] = Path(cmd[1]).read_text()
        Path(cmd[2]).write_text("TITLE successful mock\n")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(executor.subprocess, "run", _run)
    result = executor.execute_preview(
        preview, confirmation=confirmation, workdir=tmp_path / "ws",
        exe=exe, database=database)
    assert result.status == executor.STATUS_SUCCESS
    assert captured["input"] == confirmation.phreeqc_input_text
    assert result.input_hash == confirmation.input_hash == preview.input_hash


def test_executor_cannot_relabel_confirmed_snapshot_as_another_scenario(monkeypatch, tmp_path):
    preview = _ready_preview()
    exe, database = _fake_exe_db(tmp_path)
    confirmation = _confirm(preview, exe, database)
    calls = []
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: calls.append(a))
    result = executor.execute_preview(
        preview, confirmation=confirmation, scenario_id="DIFFERENT-SCENARIO",
        workdir=tmp_path / "ws", exe=exe, database=database)
    assert result.status == executor.STATUS_BLOCKED
    assert "identifier" in (result.error_message or "").lower()
    assert calls == []
    assert not (tmp_path / "ws").exists()


def test_unavailable_phreeqc_remains_honestly_unavailable(monkeypatch, tmp_path):
    preview = _ready_preview()
    exe, database = _fake_exe_db(tmp_path)
    confirmation = _confirm(preview, exe, database)
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", None)
    result = executor.execute_preview(
        preview, confirmation=confirmation, workdir=tmp_path / "ws",
        exe="definitely_not_a_real_phreeqc_binary")
    assert result.status == executor.STATUS_MISSING
    assert "not configured" in (result.error_message or "").lower()
    assert result.input_path is None


def test_batch_executor_cannot_bypass_per_preview_confirmation(monkeypatch):
    previews = [_ready_preview(), _ready_preview(leachant_concentration_M=1.0)]
    calls = []
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: calls.append(a))
    batch = batch_executor.run_batch(previews)
    assert batch.status_counts() == {executor.STATUS_BLOCKED: 2}
    assert calls == []


def test_legacy_runner_adapter_does_not_label_arbitrary_text_as_runnable():
    with pytest.raises(contract.RunContractError):
        contract.generated_text_preview("SOLUTION 1\nEND\n")


def test_database_path_change_after_confirmation_blocks_before_workspace(monkeypatch, tmp_path):
    preview = _ready_preview()
    exe, database_a = _fake_exe_db(tmp_path)
    confirmation = _confirm(preview, exe, database_a)
    database_b = tmp_path / "database-b.dat"
    database_b.write_text("# other database")
    calls = []
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: calls.append(a))
    result = executor.execute_preview(
        preview, confirmation=confirmation, workdir=tmp_path / "ws",
        exe=exe, database=str(database_b))
    assert result.status == executor.STATUS_BLOCKED
    assert "database changed" in (result.error_message or "").lower()
    assert calls == [] and not (tmp_path / "ws").exists()


def test_database_contents_replaced_at_same_path_blocks_before_workspace(monkeypatch, tmp_path):
    preview = _ready_preview()
    exe, database = _fake_exe_db(tmp_path)
    confirmation = _confirm(preview, exe, database)
    Path(database).write_text("# changed database contents")
    calls = []
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: calls.append(a))
    result = executor.execute_preview(
        preview, confirmation=confirmation, workdir=tmp_path / "ws",
        exe=exe, database=database)
    assert result.status == executor.STATUS_BLOCKED
    assert calls == [] and not (tmp_path / "ws").exists()


def test_executable_identity_change_after_confirmation_blocks_before_workspace(monkeypatch,
                                                                                tmp_path):
    preview = _ready_preview()
    exe, database = _fake_exe_db(tmp_path)
    confirmation = _confirm(preview, exe, database)
    Path(exe).write_text("#!/bin/sh\n# replaced executable\n")
    Path(exe).chmod(0o755)
    calls = []
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: calls.append(a))
    result = executor.execute_preview(
        preview, confirmation=confirmation, workdir=tmp_path / "ws",
        exe=exe, database=database)
    assert result.status == executor.STATUS_BLOCKED
    assert calls == [] and not (tmp_path / "ws").exists()


def test_database_content_identity_invalidates_preview_cache_key(tmp_path):
    database = tmp_path / "database.dat"
    database.write_text("database A")
    first = contract.optional_file_identity_hash(database)
    database.write_text("database B")
    second = contract.optional_file_identity_hash(database)
    assert first and second and first != second
