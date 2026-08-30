"""Tests for the gated PHREEQC execution layer (``simulation.phreeqc_executor``).

The real PHREEQC binary is always **mocked** — no binary / network needed (one optional
integration test runs only when a real binary + database are configured). Coverage:

* PHREEQC missing → graceful structured status, never a crash;
* execution happens **only** on the explicit ``execute_preview`` call;
* files are written **only** to a safe workspace (never ``data/raw`` / the source tree);
* a failed / timed-out run returns a structured error;
* the parser handles a missing SELECTED_OUTPUT file safely;
* running never touches the scientific result-path CSVs;
* generated simulation files are gitignored.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import pytest

import flyash_phreeqc_ml as pkg
from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.simulation import phreeqc_executor as E
from flyash_phreeqc_ml.simulation import phreeqc_input_builder as B
from flyash_phreeqc_ml.simulation import phreeqc_run_contract as C
from flyash_phreeqc_ml.simulation import source_terms as ST
from flyash_phreeqc_ml.resources import (
    CatalogStore,
    CompatibilityStatus,
    RedistributionState,
    ReleaseBootstrapResult,
    ResourceKind,
    ResourceManifest,
    RollbackState,
    TestStatus as ResourceTestStatus,
    build_knowledge_pack,
    make_installation_id,
    write_knowledge_pack,
)

PKG_DIR = Path(pkg.__file__).resolve().parent
REAL_PQO = config.RAW_DIR / "PHREEQC outputs" / "L-S_5_atmCO2.pqo"
_RELEASE_IDENTITY_ENV = (
    "PHREEQC_DATABASE_ID", "PHREEQC_DATABASE_MANIFEST_ID",
    "PHREEQC_DATABASE_SHA256", "PHREEQC_DATABASE_VERSION",
    "PHREEQC_RUNTIME_ID", "PHREEQC_RUNTIME_MANIFEST", "PHREEQC_VERSION",
    "PHREEQC_CONTAINER_IMAGE_DIGEST", "PHREEQC_SOURCE_MANIFEST",
    "PHREEQC_SOURCE_MANIFEST_SHA256", "VLAB_RESOURCE_CATALOG",
    "VLAB_RESOURCE_BOOTSTRAP_RESULT", "VLAB_ACTIVE_RUNTIME_INSTALLATION_ID",
    "VLAB_ACTIVE_DATABASE_INSTALLATION_ID", "VLAB_KNOWLEDGE_PACK_HASH",
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _preview(text="SOLUTION 1\n    pH 13\nEND\n", sid="SIM-001"):
    return B.PhreeqcInputPreview(
        scenario_id=sid, phreeqc_input_text=text, template_type=B.TEMPLATE_NAOH,
        status=B.STATUS_READY, includes_source_terms=True,
        source_term_mode=ST.MODE_GLOBAL, source_term_status=ST.STATUS_RELEASE_INCLUDED)


def _clear_release_identity_env(monkeypatch):
    for name in _RELEASE_IDENTITY_ENV:
        monkeypatch.delenv(name, raising=False)


def _confirmation(preview, *, exe=None, database=None):
    availability = E.check_availability(exe=exe, database=database)
    assert availability.environment_identity is not None
    return C.confirm_reviewed(C.review_preview(preview), availability.environment_identity)


def _execute(preview=None, **kwargs):
    preview = preview or _preview()
    confirmation = kwargs.pop("confirmation", None)
    if confirmation is None:
        confirmation = _confirmation(
            preview, exe=kwargs.get("exe"), database=kwargs.get("database"))
    return E.execute_preview(preview, confirmation=confirmation, **kwargs)


def _fake_exe_db(tmp_path):
    exe = tmp_path / "phreeqc"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    db = tmp_path / "cemdata.dat"
    db.write_text("# fake db")
    return str(exe), str(db)


def _script_exe_db(tmp_path, script: str):
    exe = tmp_path / "phreeqc-script"
    exe.write_text("#!/bin/sh\nset -eu\n" + script, encoding="utf-8")
    exe.chmod(0o755)
    db = tmp_path / "database.dat"
    db.write_text("# bounded-process test database", encoding="utf-8")
    return str(exe), str(db)


def _process_state(pid: int) -> str:
    """Return absent/running/zombie without treating an exited zombie as a live worker."""
    status = Path(f"/proc/{pid}/status")
    if status.is_file():
        try:
            for line in status.read_text(encoding="utf-8").splitlines():
                if line.startswith("State:"):
                    return "zombie" if "Z (zombie)" in line else "running"
        except OSError:
            pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "running"
    return "running"


def _assert_descendant_stopped_and_reaped(pid: int, *, label: str) -> None:
    initial = _process_state(pid)
    assert initial != "running", f"{label} descendant is still running"
    deadline = time.monotonic() + 1.0
    state = initial
    while state != "absent" and time.monotonic() < deadline:
        time.sleep(0.01)
        state = _process_state(pid)
    assert state == "absent", (
        f"{label} descendant exited but remains a zombie awaiting PID-1 reaping; "
        "the container execution contract requires an init/subreaper")


def _configure_release_identity(monkeypatch, tmp_path, exe: str, db: str):
    monkeypatch.setattr(config, "PHREEQC_EXE_PATH", exe)
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", db)
    monkeypatch.setenv("PHREEQC_EXE", exe)
    monkeypatch.setenv("PHREEQC_DATABASE", db)
    executable_hash = hashlib.sha256(Path(exe).read_bytes()).hexdigest()
    database_hash = hashlib.sha256(Path(db).read_bytes()).hexdigest()
    runtime = ResourceManifest(
        resource_id="test.phreeqc.runtime",
        installation_id=make_installation_id(
            "test.phreeqc.runtime", "3.8.6-17100", executable_hash),
        resource_kind=ResourceKind.PHREEQC_RUNTIME,
        display_name="Synthetic PHREEQC test runtime",
        provider="Synthetic test fixture",
        installed_version="3.8.6-17100",
        executable_sha256=executable_hash,
        content_sha256=executable_hash,
        install_path=str(Path(exe).resolve()),
        redistribution_state=RedistributionState.PERMITTED,
        compatibility_status=CompatibilityStatus.COMPATIBLE,
        test_status=ResourceTestStatus.PASSED,
        standalone_test_status=ResourceTestStatus.PASSED,
        rollback_state=RollbackState.CANDIDATE,
    )
    database_manifest = ResourceManifest(
        resource_id="test.phreeqc.database",
        installation_id=make_installation_id(
            "test.phreeqc.database", "3.8.6-17100", database_hash),
        resource_kind=ResourceKind.PHREEQC_OFFICIAL_DATABASE,
        display_name="Synthetic PHREEQC test database",
        provider="Synthetic test fixture",
        installed_version="3.8.6-17100",
        database_sha256=database_hash,
        content_sha256=database_hash,
        install_path=str(Path(db).resolve()),
        redistribution_state=RedistributionState.PERMITTED,
        compatibility_status=CompatibilityStatus.COMPATIBLE,
        test_status=ResourceTestStatus.PASSED,
        standalone_test_status=ResourceTestStatus.PASSED,
        rollback_state=RollbackState.CANDIDATE,
    )
    runtime_path = tmp_path / "runtime-manifest.json"
    runtime_path.write_text(json.dumps(runtime.to_dict()), encoding="utf-8")
    store = CatalogStore(tmp_path / "resource-store")
    store.register_many((runtime, database_manifest))
    store.bootstrap_activate(runtime.resource_id, runtime.installation_id)
    store.bootstrap_activate(
        database_manifest.resource_id, database_manifest.installation_id)
    pack = build_knowledge_pack(store)
    knowledge_path = write_knowledge_pack(pack, store.knowledge_dir)
    state = store.load()
    bootstrap = ReleaseBootstrapResult(
        phreeqc_version="3.8.6-17100",
        runtime_installation_id=runtime.installation_id,
        active_database_installation_id=database_manifest.installation_id,
        registered_database_installation_ids=(database_manifest.installation_id,),
        catalog_generation=state.generation,
        catalog_hash=state.catalog_hash,
        registry_file_sha256="d" * 64,
        knowledge_pack_hash=pack.pack_hash,
        knowledge_pack_path=str(knowledge_path.resolve()),
    )
    bootstrap_path = tmp_path / "release-bootstrap-result.json"
    bootstrap_path.write_text(json.dumps(bootstrap.to_dict()), encoding="utf-8")
    values = {
        "PHREEQC_RUNTIME_MANIFEST": str(runtime_path),
        "PHREEQC_RUNTIME_ID": runtime.resource_id,
        "PHREEQC_VERSION": runtime.installed_version,
        "PHREEQC_DATABASE_ID": database_manifest.resource_id,
        "PHREEQC_DATABASE_MANIFEST_ID": database_manifest.resource_id,
        "PHREEQC_DATABASE_VERSION": database_manifest.installed_version,
        "PHREEQC_DATABASE_SHA256": database_hash,
        "PHREEQC_CONTAINER_IMAGE_DIGEST": "sha256:" + "e" * 64,
        "VLAB_RESOURCE_CATALOG": str(store.catalog_path),
        "VLAB_RESOURCE_BOOTSTRAP_RESULT": str(bootstrap_path),
        "VLAB_ACTIVE_RUNTIME_INSTALLATION_ID": runtime.installation_id,
        "VLAB_ACTIVE_DATABASE_INSTALLATION_ID": database_manifest.installation_id,
        "VLAB_KNOWLEDGE_PACK_HASH": pack.pack_hash,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return runtime, database_manifest, runtime_path, bootstrap_path, state, pack


def _ok_run_writing(out_text):
    """A fake bounded process call that writes ``out_text`` and reports return code 0."""
    def _run(cmd, **kw):
        Path(cmd[2]).write_text(out_text)
        return 0, "done", "", False, None
    return _run


# --------------------------------------------------------------------------- #
# Missing PHREEQC → graceful (no crash)
# --------------------------------------------------------------------------- #
def test_missing_phreeqc_is_graceful(monkeypatch, tmp_path):
    preview = _preview()
    exe, db = _fake_exe_db(tmp_path)
    confirmation = _confirmation(preview, exe=exe, database=db)
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", None)
    res = _execute(preview, confirmation=confirmation, workdir=tmp_path / "ws",
                   exe="definitely_not_a_real_binary_xyz")
    assert res.status == E.STATUS_MISSING
    assert E.NOT_CONFIGURED_MESSAGE in res.error_message
    assert res.input_path is None                  # nothing written when it can't run
    assert not (tmp_path / "ws").exists()


def test_check_availability_reports_missing(monkeypatch):
    monkeypatch.setattr(config, "PHREEQC_EXE_PATH", "no_such_phreeqc_binary_zzz")
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", None)
    av = E.check_availability()
    assert not av.can_run
    assert "not configured" in av.message
    assert av.smoke_ok is None                     # no smoke attempted by default


def test_smoke_false_when_missing(monkeypatch):
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", None)
    assert E.smoke_test(exe="nope_binary", database=None) is False


# --------------------------------------------------------------------------- #
# Execution happens ONLY on the explicit call
# --------------------------------------------------------------------------- #
def test_no_execution_without_explicit_call(monkeypatch, tmp_path):
    calls = []

    def _tripwire(cmd, **kw):
        calls.append(cmd)
        Path(cmd[2]).write_text("TITLE ok\nEnd of Run\n")
        return 0, "", "", False, None

    monkeypatch.setattr(E, "_bounded_subprocess_run", _tripwire)
    _clear_release_identity_env(monkeypatch)
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(config, "PHREEQC_EXE_PATH", exe)
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", db)
    monkeypatch.setenv("PHREEQC_EXE", exe)
    monkeypatch.setenv("PHREEQC_DATABASE", db)

    # availability check + parsing must NOT execute PHREEQC
    E.check_availability(run_smoke=False)
    E.parse_outputs(E.ExecutionResult("SIM", E.STATUS_FAILED))
    assert calls == []

    # only the explicit execute call runs it
    _execute(workdir=tmp_path / "ws", exe=exe, database=db)
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Safe workspace
# --------------------------------------------------------------------------- #
def test_writes_only_to_given_workspace(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(E, "_bounded_subprocess_run", _ok_run_writing(
        "TITLE ok\nEnd of Run\n"))
    ws = tmp_path / "sims"
    res = _execute(_preview(sid="SIM-007"), workdir=ws, exe=exe, database=db)
    assert res.status == E.STATUS_SUCCESS
    assert res.job_id and res.workspace_path
    job_dir = Path(res.workspace_path)
    assert job_dir.parent == ws.resolve() or job_dir.parent == ws
    written = sorted(p.name for p in job_dir.iterdir())
    assert written == [".phreeqc-job.json", "SIM_007.pqi", "SIM_007.pqo"]
    assert Path(res.input_path).parent == job_dir


def test_same_scenario_concurrent_runs_are_isolated(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(E, "_bounded_subprocess_run", _ok_run_writing(
        "TITLE ok\nEnd of Run\n"))
    preview = _preview(sid="SAME-SCENARIO")
    confirmation = _confirmation(preview, exe=exe, database=db)

    def run_once(_):
        return E.execute_preview(
            preview, confirmation=confirmation, workdir=tmp_path / "jobs",
            exe=exe, database=db)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run_once, range(8)))
    assert all(result.status == E.STATUS_SUCCESS for result in results)
    assert len({result.job_id for result in results}) == len(results)
    assert len({result.input_path for result in results}) == len(results)
    assert all(Path(result.output_path).is_file() for result in results)


def test_stale_selected_output_in_workspace_root_is_never_reused(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    root = tmp_path / "jobs"
    root.mkdir()
    stale = root / "selected.out"
    stale.write_text("stale selected output")
    monkeypatch.setattr(E, "_bounded_subprocess_run", _ok_run_writing(
        "TITLE ok\nEnd of Run\n"))
    result = _execute(workdir=root, exe=exe, database=db)
    assert result.status == E.STATUS_SUCCESS
    assert result.selected_output_path is None


def test_explicit_cleanup_removes_only_one_isolated_job(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(E, "_bounded_subprocess_run", _ok_run_writing(
        "TITLE ok\nEnd of Run\n"))
    first = _execute(workdir=tmp_path / "jobs", exe=exe, database=db)
    second = _execute(workdir=tmp_path / "jobs", exe=exe, database=db)
    assert E.cleanup_job_workspace(first) is True
    assert not Path(first.workspace_path).exists()
    assert Path(second.workspace_path).is_dir()
    assert E.cleanup_job_workspace(E.ExecutionResult("x", E.STATUS_SUCCESS)) is False


def test_execution_records_exact_environment_hashes(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(E, "_bounded_subprocess_run", _ok_run_writing(
        "TITLE ok\nEnd of Run\n"))
    result = _execute(workdir=tmp_path / "jobs", exe=exe, database=db)
    environment = E.check_availability(exe=exe, database=db).environment_identity
    assert environment is not None
    assert result.executable_sha256 == environment.executable.sha256
    assert result.database_sha256 == environment.database.sha256
    assert result.environment_identity_hash == environment.identity_hash
    assert result.to_dict()["database_sha256"] == environment.database.sha256


def test_execution_records_verified_release_catalog_and_build_identity(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    runtime, database_manifest, runtime_path, bootstrap_path, state, pack = \
        _configure_release_identity(monkeypatch, tmp_path, exe, db)
    monkeypatch.setattr(E, "_bounded_subprocess_run", _ok_run_writing(
        "TITLE verified identity\nEnd of Run\n"))

    result = _execute(workdir=tmp_path / "jobs", exe=exe, database=db)

    assert result.status == E.STATUS_SUCCESS, result.error_message
    assert result.resource_identity_status == "verified_active_catalog"
    assert result.phreeqc_version == "3.8.6-17100"
    assert result.runtime_resource_id == runtime.resource_id
    assert result.runtime_installation_id == runtime.installation_id
    assert result.database_resource_id == database_manifest.resource_id
    assert result.database_installation_id == database_manifest.installation_id
    assert result.runtime_manifest_sha256 == hashlib.sha256(
        runtime_path.read_bytes()).hexdigest()
    assert result.resource_catalog_hash == state.catalog_hash
    assert result.resource_catalog_generation == state.generation
    assert result.resource_bootstrap_result_sha256 == hashlib.sha256(
        bootstrap_path.read_bytes()).hexdigest()
    assert result.knowledge_pack_hash == pack.pack_hash
    assert result.container_image_digest == "sha256:" + "e" * 64
    assert result.resource_manifest_version == "1"
    assert "cleanup_token" not in result.to_dict()


def test_active_catalog_path_mismatch_blocks_before_workspace(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    _configure_release_identity(monkeypatch, tmp_path, exe, db)
    preview = _preview()
    confirmation = _confirmation(preview, exe=exe, database=db)
    catalog_path = Path(os.environ["VLAB_RESOURCE_CATALOG"])
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    replacement = tmp_path / "same-bytes-different-database-path.dat"
    replacement.write_bytes(Path(db).read_bytes())
    active_database_id = os.environ["VLAB_ACTIVE_DATABASE_INSTALLATION_ID"]
    for manifest in catalog["resources"]:
        if manifest["installation_id"] == active_database_id:
            manifest["install_path"] = str(replacement.resolve())
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    result = E.execute_preview(
        preview, confirmation=confirmation, workdir=tmp_path / "jobs",
        exe=exe, database=db)

    assert result.status == E.STATUS_MISSING
    assert "catalog" in (result.error_message or "")
    assert not (tmp_path / "jobs").exists()


def test_declared_database_hash_mismatch_blocks_before_workspace(monkeypatch, tmp_path):
    _clear_release_identity_env(monkeypatch)
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(config, "PHREEQC_EXE_PATH", exe)
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", db)
    monkeypatch.setenv("PHREEQC_EXE", exe)
    monkeypatch.setenv("PHREEQC_DATABASE", db)
    preview = _preview()
    confirmation = _confirmation(preview, exe=exe, database=db)
    monkeypatch.setenv("PHREEQC_DATABASE_SHA256", "f" * 64)
    result = E.execute_preview(
        preview, confirmation=confirmation, workdir=tmp_path / "jobs",
        exe=exe, database=db)
    assert result.status == E.STATUS_MISSING
    assert "does not match" in (result.error_message or "")
    assert not (tmp_path / "jobs").exists()


def test_runtime_manifest_mismatch_blocks_before_workspace(monkeypatch, tmp_path):
    _clear_release_identity_env(monkeypatch)
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(config, "PHREEQC_EXE_PATH", exe)
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", db)
    monkeypatch.setenv("PHREEQC_EXE", exe)
    monkeypatch.setenv("PHREEQC_DATABASE", db)
    preview = _preview()
    confirmation = _confirmation(preview, exe=exe, database=db)
    manifest = ResourceManifest(
        resource_id="test.phreeqc.runtime",
        installation_id=make_installation_id(
            "test.phreeqc.runtime", "3.8.6-17100", "f" * 64),
        resource_kind=ResourceKind.PHREEQC_RUNTIME,
        display_name="Mismatched synthetic runtime",
        provider="Synthetic test fixture",
        installed_version="3.8.6-17100",
        executable_sha256="f" * 64,
        install_path=str(Path(exe).resolve()),
    )
    path = tmp_path / "runtime-manifest.json"
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    monkeypatch.setenv("PHREEQC_RUNTIME_MANIFEST", str(path))
    result = E.execute_preview(
        preview, confirmation=confirmation, workdir=tmp_path / "jobs",
        exe=exe, database=db)
    assert result.status == E.STATUS_MISSING
    assert "runtime manifest" in (result.error_message or "")
    assert not (tmp_path / "jobs").exists()


def test_malformed_image_digest_blocks_instead_of_recording_false_identity(
    monkeypatch, tmp_path,
):
    _clear_release_identity_env(monkeypatch)
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(config, "PHREEQC_EXE_PATH", exe)
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", db)
    monkeypatch.setenv("PHREEQC_EXE", exe)
    monkeypatch.setenv("PHREEQC_DATABASE", db)
    preview = _preview()
    confirmation = _confirmation(preview, exe=exe, database=db)
    monkeypatch.setenv("PHREEQC_CONTAINER_IMAGE_DIGEST", "latest")
    result = E.execute_preview(
        preview, confirmation=confirmation, workdir=tmp_path / "jobs",
        exe=exe, database=db)
    assert result.status == E.STATUS_MISSING
    assert "OCI sha256" in (result.error_message or "")
    assert not (tmp_path / "jobs").exists()


@pytest.mark.parametrize("bad_root", ["raw", "processed", "package"])
def test_unsafe_workspace_refused(monkeypatch, tmp_path, bad_root):
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(E.subprocess, "run", _ok_run_writing("ok"))
    target = {"raw": config.RAW_DIR, "processed": config.PROCESSED_DIR,
              "package": config.PACKAGE_DIR}[bad_root] / "evil_sim_dir"
    res = _execute(workdir=target, exe=exe, database=db)
    assert res.status == E.STATUS_FAILED
    assert "refusing" in (res.error_message or "")
    assert not target.exists()                     # never created the forbidden dir


def test_assert_safe_workspace_allows_outputs(monkeypatch):
    # the default workspace is under outputs/ and must be allowed
    assert E.assert_safe_workspace(E.default_workspace())
    with pytest.raises(ValueError):
        E.assert_safe_workspace(config.RAW_DIR / "x")


# --------------------------------------------------------------------------- #
# Failure / timeout → structured error
# --------------------------------------------------------------------------- #
def test_failed_run_returns_structured_error(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)

    def _bad(cmd, **kw):
        Path(cmd[2]).write_text("ERROR: did not converge\n")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="ERROR: boom")

    monkeypatch.setattr(E, "_bounded_subprocess_run", lambda cmd, **kw: (
        Path(cmd[2]).write_text("ERROR: did not converge\n") or 1,
        "", "ERROR: boom", False, None))
    res = _execute(workdir=tmp_path / "ws", exe=exe, database=db)
    assert res.status == E.STATUS_FAILED
    assert "did not converge" in res.error_message
    assert res.stderr_tail and res.runtime_seconds is not None
    # parsing a failed result is safe
    parsed = E.parse_outputs(res)
    assert parsed.parse_status == E.PARSE_FAILED


def test_timeout_returns_structured(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)

    monkeypatch.setattr(E, "_bounded_subprocess_run", lambda cmd, **kw: (
        None, "", "", True, "PHREEQC timed out after 1s."))
    res = _execute(workdir=tmp_path / "ws", exe=exe, database=db, timeout=1)
    assert res.status == E.STATUS_TIMEOUT
    assert "timed out" in res.error_message.lower()


def test_input_and_output_limits_fail_closed(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    preview = _preview(text="SOLUTION 1\n" + "# padding\n" * 10 + "END\n")
    confirmation = _confirmation(preview, exe=exe, database=db)
    monkeypatch.setattr(E, "MAX_INPUT_BYTES", 16)
    blocked = E.execute_preview(
        preview, confirmation=confirmation, workdir=tmp_path / "input-jobs",
        exe=exe, database=db)
    assert blocked.status == E.STATUS_BLOCKED
    assert not (tmp_path / "input-jobs").exists()

    monkeypatch.setattr(E, "MAX_INPUT_BYTES", 1024 * 1024)
    monkeypatch.setattr(E, "MAX_OUTPUT_BYTES", 4)
    monkeypatch.setattr(E, "_bounded_subprocess_run", _ok_run_writing("too large"))
    oversized = _execute(workdir=tmp_path / "output-jobs", exe=exe, database=db)
    assert oversized.status == E.STATUS_FAILED
    assert "output exceeds" in (oversized.error_message or "")


def test_zero_exit_malformed_output_is_not_success(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(E, "_bounded_subprocess_run", _ok_run_writing(
        "truncated output without completion marker\n"))
    result = _execute(workdir=tmp_path / "jobs", exe=exe, database=db)
    assert result.status == E.STATUS_FAILED
    assert "malformed" in (result.error_message or "").lower()


@pytest.mark.parametrize(
    ("stream", "script", "message"),
    [
        ("stdout", "i=0; while [ $i -lt 4096 ]; do printf x; i=$((i+1)); done\n", "stdout"),
        ("stderr", "i=0; while [ $i -lt 4096 ]; do printf x >&2; i=$((i+1)); done\n", "stderr"),
    ],
)
@pytest.mark.skipif(os.name != "posix", reason="uses a POSIX shell fixture")
def test_real_process_stream_caps_fail_closed(
    monkeypatch, tmp_path, stream, script, message,
):
    exe, db = _script_exe_db(
        tmp_path, script + "printf 'End of Run\\n' > \"$2\"\n")
    monkeypatch.setattr(E, "MAX_STDOUT_BYTES", 128)
    monkeypatch.setattr(E, "MAX_STDERR_BYTES", 128)
    result = _execute(workdir=tmp_path / "jobs", exe=exe, database=db, timeout=5)
    assert result.status == E.STATUS_FAILED
    assert message in (result.error_message or "").lower()


@pytest.mark.skipif(os.name != "posix", reason="uses a POSIX shell fixture")
def test_real_process_selected_output_cap_fails_closed(monkeypatch, tmp_path):
    exe, db = _script_exe_db(
        tmp_path,
        "dd if=/dev/zero of=selected.out bs=1024 count=4 2>/dev/null\n"
        "printf 'End of Run\\n' > \"$2\"\n",
    )
    monkeypatch.setattr(E, "MAX_SELECTED_OUTPUT_BYTES", 128)
    result = _execute(workdir=tmp_path / "jobs", exe=exe, database=db, timeout=5)
    assert result.status == E.STATUS_FAILED
    assert "selected" in (result.error_message or "").lower()


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX process-group semantics")
def test_timeout_kills_and_reaps_process_group(tmp_path):
    exe, db = _script_exe_db(
        tmp_path,
        "sleep 60 &\nchild=$!\nprintf '%s' \"$child\" > child.pid\nwait \"$child\"\n",
    )
    result = _execute(workdir=tmp_path / "jobs", exe=exe, database=db, timeout=1.0)
    assert result.status == E.STATUS_TIMEOUT
    child_pid = int((Path(result.workspace_path) / "child.pid").read_text())
    _assert_descendant_stopped_and_reaped(child_pid, label="timed-out PHREEQC")


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX process-group semantics")
def test_normal_parent_exit_does_not_orphan_background_descendant(tmp_path):
    exe, db = _script_exe_db(
        tmp_path,
        "sleep 60 &\n"
        "child=$!\n"
        "printf '%s' \"$child\" > child.pid\n"
        "printf 'End of Run\\n' > \"$2\"\n",
    )
    result = _execute(workdir=tmp_path / "jobs", exe=exe, database=db, timeout=5)
    assert result.status == E.STATUS_SUCCESS
    assert result.error_message is None
    child_pid = int((Path(result.workspace_path) / "child.pid").read_text())
    _assert_descendant_stopped_and_reaped(child_pid, label="normal-exit PHREEQC")


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX process-group semantics")
def test_repeated_process_group_cleanup_closes_descendant_pipes(tmp_path):
    for index in range(3):
        run_root = tmp_path / f"run-{index}"
        run_root.mkdir()
        exe, db = _script_exe_db(
            run_root,
            "sleep 60 &\n"
            "child=$!\n"
            "printf '%s' \"$child\" > child.pid\n"
            "printf 'End of Run\\n' > \"$2\"\n",
        )
        result = _execute(
            workdir=run_root / "jobs", exe=exe, database=db, timeout=5)
        assert result.status == E.STATUS_SUCCESS
        assert result.error_message is None
        child_pid = int((Path(result.workspace_path) / "child.pid").read_text())
        _assert_descendant_stopped_and_reaped(
            child_pid, label=f"repeated PHREEQC run {index}")


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX process-group semantics")
def test_concurrent_real_process_groups_are_isolated_and_reaped(tmp_path):
    jobs = []
    for index in range(4):
        run_root = tmp_path / f"concurrent-{index}"
        run_root.mkdir()
        jobs.append((run_root, *_script_exe_db(
            run_root,
            "sleep 60 &\n"
            "child=$!\n"
            "printf '%s' \"$child\" > child.pid\n"
            "printf 'End of Run\\n' > \"$2\"\n",
        )))

    def run(job):
        run_root, exe, db = job
        return _execute(
            workdir=run_root / "jobs", exe=exe, database=db, timeout=5)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, jobs))
    assert all(result.status == E.STATUS_SUCCESS for result in results)
    assert len({result.workspace_path for result in results}) == 4
    for index, result in enumerate(results):
        child_pid = int((Path(result.workspace_path) / "child.pid").read_text())
        _assert_descendant_stopped_and_reaped(
            child_pid, label=f"concurrent PHREEQC run {index}")


def test_posix_cleanup_signals_only_the_spawned_process_group(monkeypatch):
    class Process:
        pid = 24680

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            return 0

    signals = []
    monkeypatch.setattr(E.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(
        E.os, "kill", lambda *_: pytest.fail("cleanup must not signal a broad host PID"))
    E._terminate_process_tree(Process())
    assert signals == [
        (Process.pid, E.signal.SIGTERM),
        (Process.pid, E.signal.SIGKILL),
    ]


def test_windows_cleanup_remains_fail_closed_to_exact_process_tree(monkeypatch):
    class Process:
        pid = 13579

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(timeout=None):
            return 0

        @staticmethod
        def terminate():
            pytest.fail("taskkill tree contract should complete before fallback terminate")

        @staticmethod
        def kill():
            pytest.fail("taskkill tree contract should complete before fallback kill")

    calls = []
    monkeypatch.setattr(E.os, "name", "nt")
    monkeypatch.setattr(
        E.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    E._terminate_process_tree(Process())
    assert calls[0][0] == ["taskkill", "/PID", str(Process.pid), "/T", "/F"]
    assert calls[0][1]["check"] is False
    assert calls[0][1]["timeout"] == 2.0


@pytest.mark.parametrize("value", ["", "garbage", "0", "-1", "1.5", str(2**63)])
def test_invalid_limit_environment_uses_safe_default(monkeypatch, value):
    monkeypatch.setenv("PHREEQC_TEST_LIMIT", value)
    assert E._bounded_positive_int_env("PHREEQC_TEST_LIMIT", 123, maximum=1000) == 123


def test_invalid_executor_limit_environment_does_not_break_fresh_import(tmp_path):
    environment = dict(os.environ)
    environment.update({
        "PHREEQC_MAX_STDOUT_BYTES": "not-an-integer",
        "PHREEQC_MAX_STDERR_BYTES": "0",
        "PHREEQC_MAX_OUTPUT_BYTES": "-1",
        "PHREEQC_MAX_SELECTED_OUTPUT_BYTES": str(2**63),
        "PHREEQC_MAX_WORKSPACE_BYTES": "1.5",
        "PHREEQC_TIMEOUT_S": "not-a-finite-timeout",
    })
    completed = subprocess.run(
        [
            os.sys.executable,
            "-c",
            (
                "from flyash_phreeqc_ml.simulation import phreeqc_executor as e; "
                "assert e.MAX_STDOUT_BYTES > 0; assert e.MAX_STDERR_BYTES > 0; "
                "assert e.MAX_OUTPUT_BYTES > 0; assert e.MAX_SELECTED_OUTPUT_BYTES > 0; "
                "assert e.MAX_WORKSPACE_BYTES > 0; "
                "assert e.config.PHREEQC_RUN_TIMEOUT_S == 120.0"
            ),
        ],
        cwd=config.PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), "bad"])
def test_invalid_timeout_is_blocked_before_workspace(tmp_path, timeout):
    exe, db = _fake_exe_db(tmp_path)
    result = _execute(workdir=tmp_path / "jobs", exe=exe, database=db, timeout=timeout)
    assert result.status == E.STATUS_BLOCKED
    assert not (tmp_path / "jobs").exists()


def test_output_path_traversal_directive_is_blocked_before_workspace(tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    preview = _preview(
        "SOLUTION 1\nSELECTED_OUTPUT\n    -file ../../outside.out\nEND\n")
    result = _execute(preview, workdir=tmp_path / "jobs", exe=exe, database=db)
    assert result.status == E.STATUS_BLOCKED
    assert "plain workspace filename" in (result.error_message or "")
    assert not (tmp_path / "jobs").exists()


def test_external_include_is_blocked_before_workspace(tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    preview = _preview("INCLUDE$ ../../unreviewed-input.pqi\nEND\n")
    result = _execute(preview, workdir=tmp_path / "jobs", exe=exe, database=db)
    assert result.status == E.STATUS_BLOCKED
    assert "self-contained" in (result.error_message or "")
    assert not (tmp_path / "jobs").exists()


def test_post_confirmation_replacement_during_snapshot_never_executes(
    monkeypatch, tmp_path,
):
    exe, db = _fake_exe_db(tmp_path)
    preview = _preview()
    confirmation = _confirmation(preview, exe=exe, database=db)
    original = E._snapshot_file
    called = []

    def replace_before_snapshot(source, destination, expected, *, executable):
        if not executable:
            Path(source).write_text("# same path, replacement bytes", encoding="utf-8")
        return original(source, destination, expected, executable=executable)

    monkeypatch.setattr(E, "_snapshot_file", replace_before_snapshot)
    monkeypatch.setattr(E, "_bounded_subprocess_run", lambda *a, **k: called.append(a))
    result = E.execute_preview(
        preview, confirmation=confirmation, workdir=tmp_path / "jobs",
        exe=exe, database=db,
    )
    assert result.status == E.STATUS_FAILED
    assert "changed after confirmation" in (result.error_message or "")
    assert called == []


def test_cleanup_rejects_forged_result_without_workspace_marker(tmp_path):
    directory = tmp_path / "forged-job-0123456789abcdef"
    directory.mkdir()
    result = E.ExecutionResult(
        "forged", E.STATUS_FAILED,
        job_id=directory.name,
        workspace_path=str(directory),
    )
    assert E.cleanup_job_workspace(result) is False
    assert directory.is_dir()


def test_cleanup_rejects_forged_marker_without_unpredictable_capability(tmp_path):
    directory = tmp_path / "forged-job-0123456789abcdef"
    directory.mkdir()
    (directory / E._JOB_MARKER).write_text(json.dumps({
        "schema": "wpi.virtual-lab.phreeqc-job-workspace",
        "version": 1,
        "job_id": directory.name,
        "cleanup_token": "attacker-guess",
    }))
    result = E.ExecutionResult(
        "forged", E.STATUS_FAILED,
        job_id=directory.name,
        workspace_path=str(directory),
        cleanup_token="different-capability",
    )
    assert E.cleanup_job_workspace(result) is False
    assert directory.is_dir()


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_parse_handles_missing_selected_output(monkeypatch, tmp_path):
    out = tmp_path / "x.pqo"
    out.write_text("(pretend pqo)")
    results = pd.DataFrame([{"state": "batch", "pH": 12.5, "pe": 8.0, "mol_Ca": 0.001,
                             "mol_Si": 0.002}])
    sat = pd.DataFrame([{"state": "batch", "phase": "Calcite", "SI": -0.3}])
    monkeypatch.setattr(E, "parse_pqo_file", lambda p: ["rec"])
    monkeypatch.setattr(E, "records_to_frames", lambda recs: (results, sat, pd.DataFrame()))
    res = E.ExecutionResult("SIM", E.STATUS_SUCCESS, output_path=str(out),
                            selected_output_path=None)
    parsed = E.parse_outputs(res)
    assert parsed.parse_status == E.PARSE_PARSED
    assert parsed.pH == 12.5 and parsed.pe == 8.0
    assert parsed.element_totals_mM == {"Ca": 1.0, "Si": 2.0}      # molality ×1000 → mM
    assert any("SELECTED_OUTPUT" in w for w in parsed.warnings)


def test_parse_failed_is_safe(monkeypatch, tmp_path):
    out = tmp_path / "x.pqo"
    out.write_text("garbage")

    def _boom(p):
        raise ValueError("not a pqo")

    monkeypatch.setattr(E, "parse_pqo_file", _boom)
    parsed = E.parse_outputs(E.ExecutionResult("SIM", E.STATUS_SUCCESS, output_path=str(out)))
    assert parsed.parse_status == E.PARSE_FAILED
    assert parsed.warnings


def test_parse_partial_when_only_pH(monkeypatch, tmp_path):
    out = tmp_path / "x.pqo"
    out.write_text("x")
    results = pd.DataFrame([{"state": "batch", "pH": 12.0}])      # no mol_ columns
    monkeypatch.setattr(E, "parse_pqo_file", lambda p: ["r"])
    monkeypatch.setattr(E, "records_to_frames", lambda recs: (results, pd.DataFrame(),
                                                              pd.DataFrame()))
    parsed = E.parse_outputs(E.ExecutionResult("SIM", E.STATUS_SUCCESS, output_path=str(out)))
    assert parsed.parse_status == E.PARSE_PARTIAL
    assert "element totals" in parsed.missing


# --------------------------------------------------------------------------- #
# Result path is untouched
# --------------------------------------------------------------------------- #
def test_execution_does_not_touch_result_path_csv(monkeypatch, tmp_path):
    results_csv = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    before = results_csv.stat().st_mtime if results_csv.exists() else None
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(E, "_bounded_subprocess_run", _ok_run_writing(
        "TITLE ok\nEnd of Run\n"))
    _execute(workdir=tmp_path / "ws", exe=exe, database=db)
    after = results_csv.stat().st_mtime if results_csv.exists() else None
    assert before == after                         # the comparison CSV is never written/updated


# --------------------------------------------------------------------------- #
# Optional real-PHREEQC-output parse (no binary needed — parses a shipped .pqo)
# --------------------------------------------------------------------------- #
def test_parse_real_pqo_output(tmp_path):
    if REAL_PQO.exists():
        res = E.ExecutionResult("SIM", E.STATUS_SUCCESS, output_path=str(REAL_PQO))
    elif E.is_configured():
        preview = _preview(
            text=(
                "TITLE SYNTHETIC SOFTWARE PARSER TEST; not experimental validation\n"
                "SOLUTION 1\n"
                "    pH 7\n"
                "    temp 25\n"
                "    units mol/kgw\n"
                "    Na 0.001\n"
                "    Cl 0.001\n"
                "END\n"
            ),
            sid="REAL-PARSE-SMOKE",
        )
        res = _execute(preview, workdir=tmp_path / "real-parse")
        assert res.status == E.STATUS_SUCCESS
    else:
        pytest.skip("neither a shipped .pqo nor a configured real PHREEQC runtime is available")
    parsed = E.parse_outputs(res)
    assert parsed.parse_status in (E.PARSE_PARSED, E.PARSE_PARTIAL)
    assert parsed.pH is not None
    assert parsed.element_totals_mM            # real pqo carries element totals


# --------------------------------------------------------------------------- #
# Optional true integration (real binary + database)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not E.is_configured(),
                    reason="no real PHREEQC binary + PHREEQC_DATABASE configured")
def test_integration_real_execution(tmp_path):  # pragma: no cover - env-dependent
    pv = _preview(text=E.SMOKE_INPUT, sid="SMOKE")
    res = _execute(pv, workdir=tmp_path / "ws")
    assert res.status == E.STATUS_SUCCESS


# --------------------------------------------------------------------------- #
# .gitignore protects generated simulation files
# --------------------------------------------------------------------------- #
def test_gitignore_protects_simulation_outputs():
    import shutil as _sh
    if not _sh.which("git"):
        pytest.skip("git not available")
    rel = "outputs/simulations/SIM-001.pqo"
    proc = subprocess.run(["git", "check-ignore", rel], cwd=str(config.PROJECT_ROOT),
                          capture_output=True, text=True)
    if proc.returncode == 128:
        pytest.skip("not inside a git work tree")
    assert proc.returncode == 0, f"{rel!r} is NOT gitignored (check-ignore rc={proc.returncode})"
