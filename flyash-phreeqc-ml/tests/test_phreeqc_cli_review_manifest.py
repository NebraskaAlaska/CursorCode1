"""Script-level exact-review manifest regression tests (no real PHREEQC process)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml import phreeqc_runner
from flyash_phreeqc_ml.simulation import phreeqc_executor, phreeqc_review_manifest


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
def sample_script():
    return _load_script("10_sample_design.py", "sample_design_review_test")


def _configured_files(monkeypatch, tmp_path):
    exe = tmp_path / "phreeqc"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    database = tmp_path / "cemdata18.dat"
    database.write_text(
        "PHASES\nCal\n\tCaCO3 = CO3-2 + Ca+2\n"
        "Portlandite\n\tCa(OH)2 = Ca+2 + 2 OH-\n")
    monkeypatch.setattr(config, "PHREEQC_EXE_PATH", str(exe))
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", str(database))
    return exe, database


def _prepare(sample_script, monkeypatch, tmp_path, *, n=3, seed=7):
    _configured_files(monkeypatch, tmp_path)
    surrogate = tmp_path / "surrogate"
    monkeypatch.setattr(sample_script.run_manager, "surrogate_dir", lambda _run: surrogate)
    run_calls = []

    def _tripwire(*args, **kwargs):
        run_calls.append((args, kwargs))
        raise AssertionError("preparation must not execute PHREEQC")

    monkeypatch.setattr(sample_script.phreeqc_runner, "run", _tripwire)
    manifest = tmp_path / "review" / "manifest.json"
    rc = sample_script.main([
        "--run", "review-test", "--n-samples", str(n), "--seed", str(seed),
        "--prepare-review-manifest", str(manifest),
    ])
    assert rc == 0
    assert run_calls == []
    assert manifest.exists()
    assert len(list(manifest.parent.glob("*.pqi"))) == n
    return manifest, surrogate, run_calls


def _allow_mock_execution(sample_script, monkeypatch, tmp_path, calls):
    def _run(generated_input, workdir, **kwargs):
        calls.append((generated_input.scenario_id, generated_input.pqi_text, kwargs))
        output = tmp_path / f"{generated_input.basename}.pqo"
        output.write_text("mock PHREEQC output")
        return output

    monkeypatch.setattr(sample_script.phreeqc_runner, "run", _run)
    monkeypatch.setattr(sample_script, "_batch_outputs", lambda _path: {"pH": 12.3})


def test_preparation_performs_no_phreeqc_execution(sample_script, monkeypatch, tmp_path):
    _prepare(sample_script, monkeypatch, tmp_path)


def test_unchanged_review_manifest_can_advance(sample_script, monkeypatch, tmp_path):
    manifest, _surrogate, calls = _prepare(sample_script, monkeypatch, tmp_path)
    _allow_mock_execution(sample_script, monkeypatch, tmp_path, calls)
    rc = sample_script.main([
        "--run", "review-test", "--n-samples", "3", "--seed", "7",
        "--review-manifest", str(manifest),
    ])
    assert rc == 0
    assert len(calls) == 3


def test_environment_replaced_after_verification_blocks_before_run_or_workspace(
        sample_script, monkeypatch, tmp_path):
    manifest, surrogate, calls = _prepare(sample_script, monkeypatch, tmp_path)
    original_verify = sample_script.phreeqc_review_manifest.verify_review_manifest

    def _verify_then_replace(*args, **kwargs):
        verified = original_verify(*args, **kwargs)
        Path(config.PHREEQC_EXE_PATH).write_text("#!/bin/sh\n# replaced after verification\n")
        Path(config.PHREEQC_EXE_PATH).chmod(0o755)
        return verified

    monkeypatch.setattr(
        sample_script.phreeqc_review_manifest, "verify_review_manifest", _verify_then_replace)
    _allow_mock_execution(sample_script, monkeypatch, tmp_path, calls)
    rc = sample_script.main([
        "--run", "review-test", "--n-samples", "3", "--seed", "7",
        "--review-manifest", str(manifest),
    ])

    assert rc == 2 and calls == []
    assert not (surrogate / "runs").exists()


def test_generate_script_uses_verified_environment_and_blocks_late_replacement(
        monkeypatch, tmp_path):
    script = _load_script("09_generate_simulations.py", "generate_environment_capture_test")
    exe, database = _configured_files(monkeypatch, tmp_path)
    availability = phreeqc_executor.check_availability(
        exe=str(exe), database=str(database))
    environment = availability.environment_identity
    assert environment is not None
    generated = phreeqc_runner.build_design_input(
        0.5, 5.0, 25.0, "atm_CO2", sample_id="generate-verified")
    verified = phreeqc_review_manifest.VerifiedReviewSet(
        tmp_path / "manifest.json", "manifest-id", "set-id", (generated,), environment)

    monkeypatch.setattr(script, "_available_environment", lambda: availability)
    monkeypatch.setattr(
        script, "_candidate_set", lambda _run: ([generated], {"source": "test"}, []))

    def _verify_then_replace(*args, **kwargs):
        database.write_text(
            "PHASES\nCal\n changed reaction\nPortlandite\n changed reaction\n# late\n")
        return verified

    monkeypatch.setattr(
        script.phreeqc_review_manifest, "verify_review_manifest", _verify_then_replace)
    run_calls = []
    workspace_calls = []
    monkeypatch.setattr(
        script.phreeqc_runner, "run", lambda *args, **kwargs: run_calls.append((args, kwargs)))
    monkeypatch.setattr(
        script.run_manager, "generated_simulations_dir",
        lambda _run: workspace_calls.append(_run))

    rc = script.main(["--run", "review-test", "--review-manifest", str(tmp_path / "x")])
    assert rc == 2
    assert run_calls == [] and workspace_calls == []


def test_changed_design_identity_blocks_before_execution(sample_script, monkeypatch, tmp_path):
    manifest, _surrogate, calls = _prepare(sample_script, monkeypatch, tmp_path)
    _allow_mock_execution(sample_script, monkeypatch, tmp_path, calls)
    rc = sample_script.main([
        "--run", "review-test", "--n-samples", "3", "--seed", "8",
        "--review-manifest", str(manifest),
    ])
    assert rc == 2 and calls == []


def test_changed_reviewed_input_bytes_block_before_execution(sample_script, monkeypatch,
                                                              tmp_path):
    manifest, _surrogate, calls = _prepare(sample_script, monkeypatch, tmp_path)
    reviewed_input = sorted(manifest.parent.glob("*.pqi"))[0]
    reviewed_input.write_text(reviewed_input.read_text() + "\nEQUILIBRIUM_PHASES 99\n")
    _allow_mock_execution(sample_script, monkeypatch, tmp_path, calls)
    rc = sample_script.main([
        "--run", "review-test", "--n-samples", "3", "--seed", "7",
        "--review-manifest", str(manifest),
    ])
    assert rc == 2 and calls == []


def test_reordered_or_additional_inputs_block_before_execution(sample_script, monkeypatch,
                                                                tmp_path):
    manifest, _surrogate, calls = _prepare(sample_script, monkeypatch, tmp_path)
    original = sample_script._design_set

    def _reordered(*args, **kwargs):
        design, inputs, source = original(*args, **kwargs)
        return design, list(reversed(inputs)), source

    monkeypatch.setattr(sample_script, "_design_set", _reordered)
    _allow_mock_execution(sample_script, monkeypatch, tmp_path, calls)
    rc = sample_script.main([
        "--run", "review-test", "--n-samples", "3", "--seed", "7",
        "--review-manifest", str(manifest),
    ])
    assert rc == 2 and calls == []

    # A separate fresh review proves additional on-disk .pqi files are rejected too.
    second_root = tmp_path / "second"
    second_root.mkdir()
    manifest2, _surrogate2, calls2 = _prepare(sample_script, monkeypatch, second_root)
    (manifest2.parent / "additional.pqi").write_text("SOLUTION 1\nEND\n")
    _allow_mock_execution(sample_script, monkeypatch, second_root, calls2)
    rc2 = sample_script.main([
        "--run", "review-test", "--n-samples", "3", "--seed", "7",
        "--review-manifest", str(manifest2),
    ])
    assert rc2 == 2 and calls2 == []


@pytest.mark.parametrize("changed", ["database_path", "database_contents", "executable"])
def test_changed_execution_environment_blocks_before_execution(sample_script, monkeypatch,
                                                               tmp_path, changed):
    manifest, _surrogate, calls = _prepare(sample_script, monkeypatch, tmp_path)
    if changed == "database_path":
        database = tmp_path / "other-cemdata.dat"
        database.write_text(
            "PHASES\nCal\n\tCaCO3 = CO3-2 + Ca+2\n"
            "Portlandite\n\tCa(OH)2 = Ca+2 + 2 OH-\n# other\n")
        monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", str(database))
    elif changed == "database_contents":
        Path(config.PHREEQC_DATABASE_PATH).write_text(
            "PHASES\nCal\n reaction changed\nPortlandite\n reaction changed\n")
    else:
        Path(config.PHREEQC_EXE_PATH).write_text("#!/bin/sh\n# replaced\n")
        Path(config.PHREEQC_EXE_PATH).chmod(0o755)
    _allow_mock_execution(sample_script, monkeypatch, tmp_path, calls)
    rc = sample_script.main([
        "--run", "review-test", "--n-samples", "3", "--seed", "7",
        "--review-manifest", str(manifest),
    ])
    assert rc == 2 and calls == []


@pytest.mark.parametrize(
    "filename,module_name,required",
    [
        ("09_generate_simulations.py", "generate_bool_test", ["--run", "x"]),
        ("10_sample_design.py", "design_bool_test", ["--run", "x"]),
    ],
)
def test_old_boolean_flag_alone_cannot_run(monkeypatch, filename, module_name, required):
    script = _load_script(filename, module_name)
    monkeypatch.setattr(
        script.phreeqc_runner, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("deprecated flag executed")))
    assert script.main([*required, "--confirm-reviewed-inputs"]) == 2
