"""Tests for the simulation run registry / provenance layer (``simulation.run_registry``).

Pins: a safe run folder is created (and an unsafe one refused); the metadata JSON carries the
full provenance chain; the parsed-results CSV is written; nothing lands in ``data/raw`` /
``data/processed`` / the source tree; the folder is gitignored; saving never touches the
validation result path; runs list/reload; a missing material profile is recorded as a
warning; and the exported package contains no secrets (incl. no raw AI response).
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import types
import zipfile
from pathlib import Path

import pytest

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.simulation import batch_executor as BE
from flyash_phreeqc_ml.simulation import phreeqc_executor as E
from flyash_phreeqc_ml.simulation import run_registry as RR


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _batch(tmp_path, n=2):
    results = []
    for i in range(1, n + 1):
        sid = f"SIM-{i:03d}"
        pqi = tmp_path / f"{sid}.pqi"
        pqi.write_text("SOLUTION 1\nEND\n")
        ex = E.ExecutionResult(sid, E.STATUS_SUCCESS, input_path=str(pqi),
                               output_path=str(tmp_path / f"{sid}.pqo"), runtime_seconds=0.1,
                               phreeqc_executable="/usr/bin/phreeqc",
                               database_path="/db/cemdata.dat")
        pa = E.ParsedSimulation(sid, E.PARSE_PARSED, pH=12.0 + i / 10, pe=8.0,
                                element_totals_mM={"Ca": 1.0, "Si": 2.0},
                                saturation_indices=[{"phase": "Calcite", "SI": -0.3}])
        results.append(BE.BatchScenarioResult(sid, ex, pa))
    return BE.BatchResult(results=results, requested=n, max_scenarios=20)


def _scenario():
    from flyash_phreeqc_ml.simulation.scenario_schema import SimulationScenario
    return SimulationScenario.from_flat_dict(dict(
        material_name="Class C fly ash", solid_mass_g=2, liquid_volume_mL=10,
        leachant_type="NaOH", leachant_concentration_M=0.5, time_min=60, temperature_C=25,
        target_elements=["Ca", "Si"]))


def _matrix():
    from flyash_phreeqc_ml.simulation import matrix as MX
    return MX.build_simulation_matrix(_scenario(),
                                      ranges={"leachant_concentration_M": [0.1, 0.5]})


def _record(tmp_path, *, material_profile=None, parse_result=None, n=2):
    return RR.build_run_record(
        run_id="sim-20260616-000000-test", created_at="2026-06-16T00:00:00",
        batch=_batch(tmp_path, n), matrix=_matrix(), scenario=_scenario(),
        parse_result=parse_result, material_profile=material_profile,
        experiment_text="2 g Class C fly ash in 10 mL 0.5 M NaOH",
        desired_outputs_text="liquid composition", label="test run", notes="a note")


# --------------------------------------------------------------------------- #
# Save / safe folder
# --------------------------------------------------------------------------- #
def test_creates_safe_run_folder_with_all_files(tmp_path):
    reg = RR.SimulationRunRegistry(base_dir=tmp_path / "simulation_runs")
    d = reg.save_run(_record(tmp_path))
    assert d.is_dir()
    names = {p.name for p in d.iterdir()}
    assert {RR.RUN_METADATA_FILE, RR.ASSUMPTIONS_WARNINGS_FILE, RR.SCENARIO_MATRIX_FILE,
            RR.PARSED_RESULTS_FILE, RR.INPUTS_SUBDIR} <= names
    # the exact reviewed inputs were copied in for self-containment
    assert {p.name for p in (d / RR.INPUTS_SUBDIR).iterdir()} == {"SIM-001.pqi", "SIM-002.pqi"}


@pytest.mark.parametrize("root", ["raw", "processed", "package"])
def test_refuses_unsafe_path(tmp_path, root):
    base = {"raw": config.RAW_DIR, "processed": config.PROCESSED_DIR,
            "package": config.PACKAGE_DIR}[root] / "evil_runs"
    reg = RR.SimulationRunRegistry(base_dir=base)
    with pytest.raises(ValueError):
        reg.save_run(_record(tmp_path))
    assert not base.exists()


def test_safe_run_id_blocks_traversal():
    assert "/" not in RR._safe_run_id("../../etc/passwd")
    assert RR._safe_run_id("../../etc") not in ("", "..")


# --------------------------------------------------------------------------- #
# Provenance content
# --------------------------------------------------------------------------- #
def test_metadata_has_required_provenance_fields(tmp_path):
    reg = RR.SimulationRunRegistry(base_dir=tmp_path / "runs")
    d = reg.save_run(_record(tmp_path))
    meta = json.loads((d / RR.RUN_METADATA_FILE).read_text())
    for key in ("run_id", "created_at", "user_label", "original_experiment_text",
                "desired_outputs_text", "parser_source", "scenario_json",
                "material_profile_summary", "material_profile_verification_status",
                "phreeqc_executable_path", "phreeqc_database_path", "phreeqc_input_paths",
                "phreeqc_output_paths", "execution_status_summary", "plot_axis",
                "scenarios", "outputs"):
        assert key in meta, key
    assert "not validated" in meta["label_note"].lower()
    assert meta["scenario_json"]["leachant_type"] == "NaOH"
    assert meta["execution_status_summary"] == {E.STATUS_SUCCESS: 2}
    assert meta["plot_axis"] == "leachant_concentration_M"
    assert {o["scenario_id"] for o in meta["outputs"]} == {"SIM-001", "SIM-002"}


def test_parsed_results_csv_written(tmp_path):
    import pandas as pd
    reg = RR.SimulationRunRegistry(base_dir=tmp_path / "runs")
    d = reg.save_run(_record(tmp_path))
    df = pd.read_csv(d / RR.PARSED_RESULTS_FILE)
    assert set(df["scenario_id"]) == {"SIM-001", "SIM-002"}
    assert "pH" in df.columns and "Ca_mM" in df.columns
    assert (df["status"] == E.STATUS_SUCCESS).all()


def test_missing_material_profile_recorded_as_warning(tmp_path):
    reg = RR.SimulationRunRegistry(base_dir=tmp_path / "runs")
    d = reg.save_run(_record(tmp_path, material_profile=None))
    aw = json.loads((d / RR.ASSUMPTIONS_WARNINGS_FILE).read_text())
    assert any("material profile" in w.lower() for w in aw["warnings"])
    meta = json.loads((d / RR.RUN_METADATA_FILE).read_text())
    assert meta["material_profile_summary"] is None


def test_usable_material_profile_recorded(tmp_path):
    from flyash_phreeqc_ml.materials import profile_schema as S
    mp = S.MaterialProfile(profile_id="m", material_name="Class C fly ash",
                           composition_basis=S.BASIS_OXIDE_WT,
                           entries=S.parse_composition_text("CaO 24\nSiO2 38"),
                           verification_status=S.STATUS_USER_CONFIRMED)
    reg = RR.SimulationRunRegistry(base_dir=tmp_path / "runs")
    d = reg.save_run(_record(tmp_path, material_profile=mp))
    meta = json.loads((d / RR.RUN_METADATA_FILE).read_text())
    assert meta["material_profile_verification_status"] == S.STATUS_USER_CONFIRMED
    assert meta["material_profile_summary"]["material_name"] == "Class C fly ash"


# --------------------------------------------------------------------------- #
# List / reload
# --------------------------------------------------------------------------- #
def test_list_and_reload(tmp_path):
    reg = RR.SimulationRunRegistry(base_dir=tmp_path / "runs")
    reg.save_run(_record(tmp_path))
    runs = reg.list_runs()
    assert len(runs) == 1
    summary = runs[0]
    assert summary["run_id"] == "sim-20260616-000000-test"
    assert summary["n_scenarios"] == 2 and summary["n_success"] == 2
    assert summary["material"] == "Class C fly ash" and summary["leachant"] == "NaOH"
    assert summary["sweep_axis"] == "leachant_concentration_M"
    assert reg.load_run("sim-20260616-000000-test")["run_id"] == "sim-20260616-000000-test"
    assert reg.load_run("missing") is None


def test_list_runs_empty_when_no_dir(tmp_path):
    assert RR.SimulationRunRegistry(base_dir=tmp_path / "nope").list_runs() == []


# --------------------------------------------------------------------------- #
# Off the result path + no secrets
# --------------------------------------------------------------------------- #
def test_save_does_not_touch_validation_result_path(tmp_path):
    results_csv = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    before = results_csv.stat().st_mtime if results_csv.exists() else None
    RR.SimulationRunRegistry(base_dir=tmp_path / "runs").save_run(_record(tmp_path))
    after = results_csv.stat().st_mtime if results_csv.exists() else None
    assert before == after


def test_export_package_contains_no_secrets(tmp_path):
    # a parse result carrying a (fake) secret in its raw AI response must NOT be persisted
    parse_result = types.SimpleNamespace(
        source="ai", assumptions=[], warnings=["temperature assumed 25 C"], scenario=None,
        raw_response="here is the key sk-ABCDEF0123456789 and api_key=topsecret")
    reg = RR.SimulationRunRegistry(base_dir=tmp_path / "runs")
    reg.save_run(_record(tmp_path, parse_result=parse_result))
    zf = zipfile.ZipFile(io.BytesIO(reg.export_zip("sim-20260616-000000-test")))
    text = " ".join(zf.read(n).decode("utf-8", "replace") for n in zf.namelist())
    assert not re.search(r"sk-[A-Za-z0-9]{16}", text)
    assert "topsecret" not in text
    assert "raw_response" not in text                 # the field is never written


def test_export_zip_contains_the_bundle(tmp_path):
    reg = RR.SimulationRunRegistry(base_dir=tmp_path / "runs")
    reg.save_run(_record(tmp_path))
    names = zipfile.ZipFile(io.BytesIO(reg.export_zip("sim-20260616-000000-test"))).namelist()
    assert any(n.endswith(RR.RUN_METADATA_FILE) for n in names)
    assert any(n.endswith(RR.PARSED_RESULTS_FILE) for n in names)


# --------------------------------------------------------------------------- #
# Run id + JSON safety
# --------------------------------------------------------------------------- #
def test_generate_run_id_is_deterministic_and_safe():
    import datetime as dt
    rid = RR.generate_run_id(dt.datetime(2026, 6, 16, 18, 30, 0), label="My Fly Ash! Sweep")
    assert rid == "sim-20260616-183000-my-fly-ash-sweep"
    assert re.fullmatch(r"[0-9A-Za-z._-]+", rid)


def test_json_safe_drops_nan_and_numpy():
    import numpy as np
    out = RR._json_safe({"a": float("nan"), "b": np.int64(3), "c": np.float64(1.5),
                         "d": [float("inf"), "x"]})
    assert out == {"a": None, "b": 3, "c": 1.5, "d": [None, "x"]}
    json.dumps(out, allow_nan=False)                  # serialises cleanly


# --------------------------------------------------------------------------- #
# Gitignore
# --------------------------------------------------------------------------- #
def test_simulation_runs_dir_is_gitignored():
    import shutil
    if not shutil.which("git"):
        pytest.skip("git not available")
    proc = subprocess.run(["git", "check-ignore", "outputs/simulation_runs/x/run_metadata.json"],
                          cwd=str(config.PROJECT_ROOT), capture_output=True, text=True)
    if proc.returncode == 128:
        pytest.skip("not inside a git work tree")
    assert proc.returncode == 0
    tracked = subprocess.run(["git", "ls-files", "outputs/simulation_runs/"],
                             cwd=str(config.PROJECT_ROOT), capture_output=True, text=True)
    assert tracked.stdout.strip() == ""
