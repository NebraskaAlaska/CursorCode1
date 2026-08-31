"""Tests for small-sweep PHREEQC execution (``simulation.batch_executor``).

PHREEQC is mocked throughout. Coverage: all-success / mixed / missing batches, one failure
never stops the batch, the default scenario cap, sweep-axis detection, the plot frames only
exist when runs succeeded, and the batch never touches the scientific result-path CSVs or
writes outside the safe ``outputs/simulations/`` workspace.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pandas as pd
import pytest

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.simulation import batch_executor as BE
from flyash_phreeqc_ml.simulation import phreeqc_executor as E
from flyash_phreeqc_ml.simulation import phreeqc_input_builder as B
from flyash_phreeqc_ml.simulation import phreeqc_run_contract as C
from flyash_phreeqc_ml.simulation import source_terms as ST


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _previews(n, prefix="SIM"):
    return [B.PhreeqcInputPreview(
                scenario_id=f"{prefix}-{i:03d}", phreeqc_input_text="SOLUTION 1\nEND\n",
                template_type=B.TEMPLATE_NAOH, status=B.STATUS_READY,
                includes_source_terms=True, source_term_mode=ST.MODE_GLOBAL,
                source_term_status=ST.STATUS_RELEASE_INCLUDED)
            for i in range(1, n + 1)]


def _confirmed(previews, *, exe=None, database=None):
    availability = E.check_availability(exe=exe, database=database)
    if availability.environment_identity is not None:
        environment = availability.environment_identity
    else:
        file_identity = C.FileIdentity("/mock", "a" * 64, 1, 1, 0o755)
        environment = C.ExecutionEnvironmentIdentity(file_identity, file_identity)
    return [C.confirm_reviewed(C.review_preview(p), environment) for p in previews]


def _run_batch(previews, **kwargs):
    return BE.run_batch(
        previews,
        confirmations=_confirmed(
            previews, exe=kwargs.get("exe"), database=kwargs.get("database")),
        **kwargs)


def _fake_exec(status_for):
    """Return a fake ``execute_preview`` that yields a chosen status per scenario_id."""
    def _run(pv, **kw):
        status = status_for(pv.scenario_id)
        err = None if status == E.STATUS_SUCCESS else f"{status} for {pv.scenario_id}"
        return E.ExecutionResult(pv.scenario_id, status, output_path="x.pqo",
                                 error_message=err, runtime_seconds=0.01)
    return _run


def _fake_parse(ph=12.0, totals=None):
    def _parse(result):
        return E.ParsedSimulation(result.scenario_id, E.PARSE_PARSED, pH=ph, pe=8.0,
                                  element_totals_mM=dict(totals or {"Ca": 1.0}),
                                  saturation_indices=[{"phase": "Calcite", "SI": -0.4}])
    return _parse


# --------------------------------------------------------------------------- #
# Batch outcomes
# --------------------------------------------------------------------------- #
def test_all_success(monkeypatch):
    monkeypatch.setattr(BE._exec, "execute_preview", _fake_exec(lambda s: E.STATUS_SUCCESS))
    monkeypatch.setattr(BE._exec, "parse_outputs", _fake_parse())
    batch = _run_batch(_previews(3))
    assert batch.executed == 3 and batch.n_success == 3
    assert batch.status_counts() == {E.STATUS_SUCCESS: 3}
    assert not batch.truncated
    assert all(r.parse_status == E.PARSE_PARSED for r in batch.results)


def test_mixed_success_and_failure(monkeypatch):
    def status(sid):
        return E.STATUS_FAILED if sid.endswith("002") else E.STATUS_SUCCESS
    monkeypatch.setattr(BE._exec, "execute_preview", _fake_exec(status))
    monkeypatch.setattr(BE._exec, "parse_outputs", _fake_parse())
    batch = _run_batch(_previews(3))
    assert batch.status_counts() == {E.STATUS_SUCCESS: 2, E.STATUS_FAILED: 1}
    # the failed scenario has no parsed values but is still in the batch
    failed = [r for r in batch.results if r.status == E.STATUS_FAILED][0]
    assert failed.pH is None and failed.parse_status is None
    assert failed.warnings                      # carries the error message


def test_phreeqc_missing_is_graceful(monkeypatch):
    monkeypatch.setattr(BE._exec, "execute_preview", _fake_exec(lambda s: E.STATUS_MISSING))
    batch = _run_batch(_previews(3))
    assert batch.status_counts() == {E.STATUS_MISSING: 3}
    assert batch.n_success == 0                  # no crash, structured status


def test_one_failure_does_not_stop_the_batch(monkeypatch):
    # the *first* scenario raises inside execute_preview — the batch must continue
    calls = []

    def _run(pv, **kw):
        calls.append(pv.scenario_id)
        if pv.scenario_id.endswith("001"):
            raise RuntimeError("boom")
        return E.ExecutionResult(pv.scenario_id, E.STATUS_SUCCESS, runtime_seconds=0.01)

    monkeypatch.setattr(BE._exec, "execute_preview", _run)
    monkeypatch.setattr(BE._exec, "parse_outputs", _fake_parse())
    batch = _run_batch(_previews(3))
    assert calls == ["SIM-001", "SIM-002", "SIM-003"]      # all attempted
    assert batch.executed == 3
    assert batch.status_counts().get(E.STATUS_FAILED) == 1
    assert batch.status_counts().get(E.STATUS_SUCCESS) == 2


def test_default_scenario_limit_enforced(monkeypatch):
    monkeypatch.setattr(BE._exec, "execute_preview", _fake_exec(lambda s: E.STATUS_SUCCESS))
    monkeypatch.setattr(BE._exec, "parse_outputs", _fake_parse())
    n = BE.DEFAULT_MAX_SCENARIOS + 10
    batch = _run_batch(_previews(n))
    assert batch.requested == n
    assert batch.executed == BE.DEFAULT_MAX_SCENARIOS
    assert batch.truncated


def test_progress_callback(monkeypatch):
    monkeypatch.setattr(BE._exec, "execute_preview", _fake_exec(lambda s: E.STATUS_SUCCESS))
    monkeypatch.setattr(BE._exec, "parse_outputs", _fake_parse())
    seen = []
    _run_batch(_previews(2), on_progress=lambda i, n, sid, st: seen.append((i, n, sid, st)))
    assert seen == [(1, 2, "SIM-001", E.STATUS_SUCCESS), (2, 2, "SIM-002", E.STATUS_SUCCESS)]


# --------------------------------------------------------------------------- #
# Result table + sweep detection
# --------------------------------------------------------------------------- #
def _matrix(ranges):
    from flyash_phreeqc_ml.simulation import matrix as MX
    from flyash_phreeqc_ml.simulation.scenario_schema import SimulationScenario
    sc = SimulationScenario.from_flat_dict(dict(
        material_name="fly ash", solid_mass_g=2, liquid_volume_mL=10, leachant_type="NaOH",
        leachant_concentration_M=0.5, time_min=60, temperature_C=25))
    return MX.build_simulation_matrix(sc, ranges=ranges)


def test_result_table_has_required_columns(monkeypatch):
    monkeypatch.setattr(BE._exec, "execute_preview", _fake_exec(lambda s: E.STATUS_SUCCESS))
    monkeypatch.setattr(BE._exec, "parse_outputs", _fake_parse(totals={"Ca": 1.0, "Si": 2.0}))
    mtx = _matrix({"leachant_concentration_M": [0.1, 0.5, 1.0]})
    batch = _run_batch(_previews(3))
    table = BE.build_result_table(batch, mtx)
    for col in ("scenario_id", "leachant_type", "leachant_concentration_M", "time_min",
                "temperature_C", "status", "parse_status", "pH", "pe", "Ca_mM", "Si_mM",
                "key_SI", "runtime_seconds", "warnings"):
        assert col in table.columns, col
    assert (table["status"] == E.STATUS_SUCCESS).all()
    assert table["key_SI"].iloc[0].startswith("Calcite:")


@pytest.mark.parametrize("ranges,expected", [
    ({"leachant_concentration_M": [0.1, 0.5, 1.0]}, "leachant_concentration_M"),
    ({"time_min": [10, 20, 30]}, "time_min"),
    ({"temperature_C": [25, 40]}, "temperature_C"),
    (None, None),
])
def test_sweep_axis_detection(ranges, expected):
    mtx = _matrix(ranges)
    col, label = BE.detect_sweep_axis(mtx)
    assert col == expected
    assert label == (expected or "scenario_id")


def test_sweep_axis_none_when_no_matrix():
    assert BE.detect_sweep_axis(None) == (None, "scenario_id")


# --------------------------------------------------------------------------- #
# Plots only when results exist
# --------------------------------------------------------------------------- #
def test_plot_frame_empty_without_successes():
    table = pd.DataFrame([{"scenario_id": "SIM-001", "status": E.STATUS_FAILED, "pH": None,
                           "leachant_concentration_M": 0.5}])
    assert BE.sweep_plot_frame(table, "leachant_concentration_M", "pH").empty


def test_plot_frame_uses_axis_and_sorts():
    table = pd.DataFrame([
        {"scenario_id": "SIM-002", "status": E.STATUS_SUCCESS, "pH": 12.5,
         "leachant_concentration_M": 1.0},
        {"scenario_id": "SIM-001", "status": E.STATUS_SUCCESS, "pH": 12.1,
         "leachant_concentration_M": 0.1}])
    fr = BE.sweep_plot_frame(table, "leachant_concentration_M", "pH")
    assert list(fr["x"]) == [0.1, 1.0]            # sorted by the sweep axis
    assert list(fr["y"]) == [12.1, 12.5]


def test_plot_frame_categorical_when_no_axis():
    table = pd.DataFrame([{"scenario_id": "SIM-001", "status": E.STATUS_SUCCESS, "pH": 12.0}])
    fr = BE.sweep_plot_frame(table, None, "pH")
    assert list(fr["x"]) == ["SIM-001"]           # falls back to scenario_id


# --------------------------------------------------------------------------- #
# Off the result path + safe workspace + git
# --------------------------------------------------------------------------- #
def _fake_exe_db(tmp_path):
    exe = tmp_path / "phreeqc"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    db = tmp_path / "cemdata.dat"
    db.write_text("# db")
    return str(exe), str(db)


def test_batch_does_not_touch_result_path_and_writes_only_to_workspace(monkeypatch, tmp_path):
    results_csv = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    before = results_csv.stat().st_mtime if results_csv.exists() else None

    ws = tmp_path / "sims"
    monkeypatch.setattr(E, "default_workspace", lambda: ws)
    def successful_phreeqc(cmd, **kwargs):
        Path(cmd[2]).write_text("TITLE ok\nEnd of Run\n")
        return 0, "", "", False, None

    monkeypatch.setattr(E, "_bounded_subprocess_run", successful_phreeqc)
    exe, db = _fake_exe_db(tmp_path)
    _run_batch(_previews(2), exe=exe, database=db)

    after = results_csv.stat().st_mtime if results_csv.exists() else None
    assert before == after                        # result-path CSV untouched
    job_dirs = sorted(path for path in ws.iterdir() if path.is_dir())
    assert len(job_dirs) == 2
    written = {p.name for job_dir in job_dirs for p in job_dir.iterdir()}
    assert written == {
        E._JOB_MARKER, "SIM_001.pqi", "SIM_001.pqo", "SIM_002.pqi", "SIM_002.pqo",
    }


def test_simulation_files_are_gitignored_and_untracked():
    import shutil
    if not shutil.which("git"):
        pytest.skip("git not available")
    root = str(config.PROJECT_ROOT)
    ign = subprocess.run(["git", "check-ignore", "outputs/simulations/SIM-001.pqo"],
                         cwd=root, capture_output=True, text=True)
    if ign.returncode == 128:
        pytest.skip("not inside a git work tree")
    assert ign.returncode == 0                    # ignored
    tracked = subprocess.run(["git", "ls-files", "outputs/simulations/"],
                             cwd=root, capture_output=True, text=True)
    assert tracked.stdout.strip() == ""           # nothing under it is tracked


# --------------------------------------------------------------------------- #
# App-level: the sweep plots appear only once execution results exist
# --------------------------------------------------------------------------- #
def _sweep_app(monkeypatch, with_results):
    from streamlit.testing.v1 import AppTest

    from flyash_phreeqc_ml.ai import scenario_parser as sp
    from flyash_phreeqc_ml.materials import profile_schema as MS
    from flyash_phreeqc_ml.simulation import matrix as MX, phreeqc_input_builder as PB
    from flyash_phreeqc_ml.simulation import source_terms as ST2
    from flyash_phreeqc_ml.simulation.scenario_schema import SimulationScenario

    # report PHREEQC as configured so the run section (and its plots) can render
    file_identity = C.FileIdentity("/mock", "a" * 64, 1, 1, 0o755)
    environment = C.ExecutionEnvironmentIdentity(file_identity, file_identity)
    monkeypatch.setattr(E, "check_availability",
                        lambda **k: E.PhreeqcAvailability(True, True, True, True, "phreeqc",
                                                          "/db.dat", "ready",
                                                          environment_identity=environment))
    sc = SimulationScenario.from_flat_dict(dict(
        material_name="fly ash", solid_mass_g=2, liquid_volume_mL=10, leachant_type="NaOH",
        leachant_concentration_M=0.5, time_min=60, temperature_C=25, target_elements=["Ca"]))
    mtx = MX.build_simulation_matrix(sc, ranges={"leachant_concentration_M": [0.1, 0.5, 1.0]})
    profile = MS.MaterialProfile(
        profile_id="sweep-test", material_name="fly ash",
        composition_basis=MS.BASIS_OXIDE_WT,
        entries=MS.parse_composition_text("CaO 20\nSiO2 40\nAl2O3 30"),
        verification_status=MS.STATUS_USER_CONFIRMED)
    previews = PB.build_previews_for_matrix(
        sc, mtx, material_profile=profile, dissolution_model=ST2.global_release(0.01))

    at = AppTest.from_file(str(config.PROJECT_ROOT / "app.py"), default_timeout=60)
    at.run()
    # The Simulate workflow lives in the Workspace section (the assistant is the default).
    at.session_state["nav_page"] = "Material Workspace"
    at.session_state["sim_parse_result"] = sp.parse_scenario("2 g fly ash", "liquid",
                                                             prefer_ai=False)
    at.session_state["sim_matrix"] = mtx
    at.session_state["sim_scenario"] = sc
    at.session_state["sim_previews"] = previews
    if with_results:
        def _mk(sid, ph):
            ex = E.ExecutionResult(sid, E.STATUS_SUCCESS, output_path="x.pqo",
                                   runtime_seconds=0.1)
            pa = E.ParsedSimulation(sid, E.PARSE_PARSED, pH=ph, pe=8.0,
                                    element_totals_mM={"Ca": 1.0})
            return BE.BatchScenarioResult(sid, ex, pa)
        at.session_state["sim_batch_result"] = BE.BatchResult(
            results=[_mk("SIM-001", 12.1), _mk("SIM-002", 12.5), _mk("SIM-003", 12.9)],
            requested=3, max_scenarios=20)
        at.session_state["sim_batch_matrix"] = mtx
    at.run()
    return at


def test_sweep_plots_only_after_results(monkeypatch):
    # before any run: the sweep section renders, but NO result plot
    at = _sweep_app(monkeypatch, with_results=False)
    assert not at.exception
    md = " ".join(m.value for m in at.markdown)
    assert "Run confirmed sweep" in md
    assert "Predicted pH vs" not in md

    # after an actual (injected) batch result: the pH-vs-sweep plot appears
    at2 = _sweep_app(monkeypatch, with_results=True)
    assert not at2.exception
    md2 = " ".join(m.value for m in at2.markdown)
    assert "Predicted pH vs leachant_concentration_M" in md2
