from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

import pytest

from flyash_phreeqc_ml.council_operator.worker_supervisor import (
    MetadataError,
    OwnershipError,
    SupervisorError,
    WorkerSupervisor,
    _group_alive,
    _replace_atomic,
    _write_new_atomic,
    inspect_process,
    read_metadata,
)
from council_operator_helpers import create_code_remote, make_operator, make_policy, make_request


PROJECT = Path(__file__).resolve().parents[1]


def wait_until(predicate, *, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before the deadline")


def make_supervisor(tmp_path: Path, *, timeout: float = 0.25, **kwargs) -> WorkerSupervisor:
    return WorkerSupervisor(
        state_dir=tmp_path / "worker-state",
        project_root=PROJECT,
        term_timeout_seconds=timeout,
        caffeinate_mode="off",
        **kwargs,
    )


def long_worker() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(60)"]


def stop_if_running(supervisor: WorkerSupervisor) -> None:
    try:
        supervisor.stop()
    except (MetadataError, OwnershipError, SupervisorError):
        pass


def test_normal_completion_records_exact_venv_runtime_and_private_metadata(tmp_path):
    supervisor = make_supervisor(tmp_path)
    observed = tmp_path / "python.json"
    code = (
        "import json,sys,time; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(json.dumps({'executable':sys.executable,'prefix':sys.prefix})); "
        "time.sleep(.3)"
    )
    started = supervisor.start("normal-completion", worker_argv=[sys.executable, "-c", code, str(observed)])
    metadata = read_metadata(supervisor.metadata_path, state_dir=supervisor.state_dir)
    assert stat.S_IMODE(supervisor.metadata_path.stat().st_mode) == 0o600
    assert metadata["operator_python"]["path"] == sys.executable
    assert metadata["operator_python"]["version"].startswith("3.12.")
    assert metadata["command"]["requested_argv"][0] == sys.executable
    assert metadata["command"]["effective_argv"][0] == sys.executable
    assert metadata["worker"]["process_group_id"] == started["worker_pid"]
    assert metadata["worker"]["session_id"] == started["worker_pid"]
    wait_until(lambda: not supervisor.metadata_path.exists())
    runtime = json.loads(observed.read_text(encoding="utf-8"))
    assert runtime == {"executable": sys.executable, "prefix": sys.prefix}
    finished = supervisor.stop()
    assert finished["status"] == "already_finished"
    assert stat.S_IMODE(supervisor.receipt_path.stat().st_mode) == 0o600


def test_clean_stop_reaps_worker_and_removes_entire_owned_group(tmp_path):
    supervisor = make_supervisor(tmp_path)
    started = supervisor.start("clean-stop", worker_argv=long_worker())
    metadata = read_metadata(supervisor.metadata_path, state_dir=supervisor.state_dir)
    result = supervisor.stop()
    assert result == {
        "status": "stopped",
        "task_id": "clean-stop",
        "instance_id": started["instance_id"],
        "sigterm_sent": True,
        "sigkill_sent": False,
        "child_reaped": True,
        "lease_recovery": "remote task state remains authoritative",
    }
    assert not supervisor.metadata_path.exists()
    assert not _group_alive(metadata["worker"]["process_group_id"])
    assert inspect_process(metadata["worker"]["pid"]) != metadata["worker"]


def test_pid_reuse_attempt_is_rejected_without_signalling_worker(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.start("pid-reuse", worker_argv=long_worker())
    original = read_metadata(supervisor.metadata_path, state_dir=supervisor.state_dir)
    forged = json.loads(json.dumps(original))
    forged["supervisor"]["process_start_identity"] += ":reused"
    _replace_atomic(supervisor.metadata_path, forged)
    try:
        with pytest.raises(OwnershipError, match="PID was reused"):
            supervisor.stop()
        assert _group_alive(original["worker"]["process_group_id"])
    finally:
        _replace_atomic(supervisor.metadata_path, original)
        supervisor.stop()


def test_malformed_or_non_private_metadata_is_rejected_and_retained(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.metadata_path.write_text('{"pid":"not-safe"}\n', encoding="utf-8")
    os.chmod(supervisor.metadata_path, 0o600)
    with pytest.raises(MetadataError, match="unknown or missing fields"):
        supervisor.stop()
    assert supervisor.metadata_path.exists()
    os.chmod(supervisor.metadata_path, 0o644)
    with pytest.raises(MetadataError, match="mode 0600"):
        supervisor.stop()
    assert supervisor.metadata_path.exists()


def test_unrelated_process_is_never_signalled_by_stale_metadata(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.start(
        "unrelated-fixture",
        worker_argv=[sys.executable, "-c", "import time; time.sleep(.3)"],
    )
    captured = read_metadata(supervisor.metadata_path, state_dir=supervisor.state_dir)
    wait_until(lambda: not supervisor.metadata_path.exists())
    unrelated = subprocess.Popen(
        long_worker(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    forged = json.loads(json.dumps(captured))
    forged["supervisor"]["pid"] = unrelated.pid
    # Retaining the expired start identity is the deterministic equivalent of
    # the kernel assigning an old PID to an unrelated new process.
    supervisor.receipt_path.unlink()
    _write_new_atomic(supervisor.metadata_path, forged)
    try:
        with pytest.raises(OwnershipError, match="PID was reused"):
            supervisor.stop()
        assert unrelated.poll() is None
        assert supervisor.metadata_path.exists()
    finally:
        supervisor.metadata_path.unlink(missing_ok=True)
        os.killpg(unrelated.pid, signal.SIGTERM)
        unrelated.wait(timeout=3)


def test_child_process_is_present_then_cleaned_with_the_complete_group(tmp_path):
    supervisor = make_supervisor(tmp_path)
    child_file = tmp_path / "child.pid"
    code = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)"
    )
    supervisor.start("child-cleanup", worker_argv=[sys.executable, "-c", code, str(child_file)])
    wait_until(child_file.exists)
    metadata = read_metadata(supervisor.metadata_path, state_dir=supervisor.state_dir)
    child_pid = int(child_file.read_text(encoding="utf-8"))
    child_identity = inspect_process(child_pid)
    assert child_identity is not None
    assert child_identity.process_group_id == metadata["worker"]["process_group_id"]
    result = supervisor.stop()
    assert result["child_reaped"] is True
    assert result["sigkill_sent"] is False
    assert not _group_alive(metadata["worker"]["process_group_id"])
    assert inspect_process(child_pid) != child_identity


def test_bounded_term_then_kill_escalation_cleans_stubborn_group(tmp_path):
    supervisor = make_supervisor(tmp_path, timeout=0.2)
    child_file = tmp_path / "stubborn-child.pid"
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    code = (
        "import signal,subprocess,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)"
    )
    supervisor.start("term-kill", worker_argv=[sys.executable, "-c", code, str(child_file)])
    wait_until(child_file.exists)
    metadata = read_metadata(supervisor.metadata_path, state_dir=supervisor.state_dir)
    child_identity = inspect_process(int(child_file.read_text(encoding="utf-8")))
    started_at = time.monotonic()
    result = supervisor.stop()
    elapsed = time.monotonic() - started_at
    assert result["sigterm_sent"] is True
    assert result["sigkill_sent"] is True
    assert result["child_reaped"] is True
    assert 0.15 <= elapsed < 5.0
    assert not _group_alive(metadata["worker"]["process_group_id"])
    assert inspect_process(child_identity.pid) != child_identity


def test_stale_finished_metadata_is_removed_without_sending_a_signal(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.start(
        "stale-local-file",
        worker_argv=[sys.executable, "-c", "import time; time.sleep(.3)"],
    )
    stale = read_metadata(supervisor.metadata_path, state_dir=supervisor.state_dir)
    wait_until(lambda: not supervisor.metadata_path.exists())
    _write_new_atomic(supervisor.metadata_path, stale)
    result = supervisor.stop()
    assert result["status"] == "already_finished"
    assert result["task_id"] == "stale-local-file"
    assert not supervisor.metadata_path.exists()


def test_restart_after_normal_completion_uses_a_new_instance(tmp_path):
    supervisor = make_supervisor(tmp_path)
    first = supervisor.start(
        "restart-task",
        worker_argv=[sys.executable, "-c", "import time; time.sleep(.3)"],
    )
    wait_until(lambda: not supervisor.metadata_path.exists())
    second = supervisor.start(
        "restart-task",
        worker_argv=[sys.executable, "-c", "import time; time.sleep(.3)"],
    )
    wait_until(lambda: not supervisor.metadata_path.exists())
    assert first["instance_id"] != second["instance_id"]
    assert supervisor.stop()["status"] == "already_finished"


def test_interruption_preserves_remote_lease_for_release_or_recovery(tmp_path):
    remote, live, head = create_code_remote(tmp_path / "remote")
    policy = make_policy(remote)
    controller = make_operator(tmp_path / "controller", remote, live, policy, worker="personal")
    worker = make_operator(tmp_path / "worker", remote, live, policy, worker="hermes-worker")
    request = make_request(policy, head, task_id="recoverable-lease")
    controller.state.submit(request, "personal", has_private_control_remote=False)
    claimed = worker.claim(request.task_id)

    supervisor = make_supervisor(tmp_path / "lifecycle")
    supervisor.start(request.task_id, worker_argv=long_worker())
    stopped = supervisor.stop()
    restarted_worker = make_operator(tmp_path / "worker", remote, live, policy, worker="hermes-worker")
    still_remote = restarted_worker.status(request.task_id)
    assert stopped["lease_recovery"] == "remote task state remains authoritative"
    assert still_remote["state"] == "claimed"
    assert still_remote["worker_id"] == "hermes-worker"
    assert still_remote["revision"] == claimed["revision"]
    assert restarted_worker.release(request.task_id)["state"] == "submitted"


def test_mac_os_caffeinate_wrapper_is_explicit_and_lifetime_scoped(tmp_path):
    fake_caffeinate = tmp_path / "caffeinate"
    fake_caffeinate.write_text(
        "#!/bin/sh\n"
        "test \"${1:-}\" = '-dimsu' || exit 2\n"
        "shift\n"
        "\"$@\" &\n"
        "child=$!\n"
        "trap 'kill -TERM \"$child\" 2>/dev/null || true' HUP INT TERM\n"
        "wait \"$child\"\n",
        encoding="utf-8",
    )
    fake_caffeinate.chmod(0o700)
    supervisor = WorkerSupervisor(
        state_dir=tmp_path / "worker-state",
        project_root=PROJECT,
        term_timeout_seconds=0.25,
        caffeinate_mode="on",
        caffeinate_path=fake_caffeinate,
    )
    assertion_file = tmp_path / "sleep-assertion.json"
    worker_code = (
        "import json,os,sys,time; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(json.dumps({"
        "'instance':os.environ.get('WPI_COUNCIL_SUPERVISED_INSTANCE'),"
        "'sleep':os.environ.get('WPI_COUNCIL_SLEEP_ASSERTION')})); time.sleep(60)"
    )
    started = supervisor.start(
        "caffeinate-task",
        worker_argv=[sys.executable, "-c", worker_code, str(assertion_file)],
    )
    wait_until(assertion_file.exists)
    metadata = read_metadata(supervisor.metadata_path, state_dir=supervisor.state_dir)
    assert metadata["caffeinate"]["enabled"] is True
    assert metadata["caffeinate"]["executable"] == str(fake_caffeinate)
    assert metadata["command"]["effective_argv"][:2] == [str(fake_caffeinate), "-dimsu"]
    assertion = json.loads(assertion_file.read_text(encoding="utf-8"))
    assert assertion == {"instance": started["instance_id"], "sleep": "caffeinate"}
    process_group_id = metadata["worker"]["process_group_id"]
    supervisor.stop()
    assert not _group_alive(process_group_id)


def test_command_hash_start_identity_and_no_ps_string_contract(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.start("identity-contract", worker_argv=long_worker())
    original = read_metadata(supervisor.metadata_path, state_dir=supervisor.state_dir)
    malformed = json.loads(json.dumps(original))
    malformed["command"]["requested_argv"].append("changed-without-new-hash")
    _replace_atomic(supervisor.metadata_path, malformed)
    try:
        with pytest.raises(MetadataError, match="command hash"):
            read_metadata(supervisor.metadata_path, state_dir=supervisor.state_dir)
    finally:
        _replace_atomic(supervisor.metadata_path, original)
        supervisor.stop()
    source = (PROJECT / "flyash_phreeqc_ml" / "council_operator" / "worker_supervisor.py").read_text(
        encoding="utf-8"
    )
    assert '"ps"' not in source
    assert "subprocess.getoutput" not in source
    assert "/proc" in source and "proc_pidinfo" in source


def test_only_one_task_instance_can_run_and_legacy_pid_file_is_never_used(tmp_path):
    supervisor = make_supervisor(tmp_path)
    supervisor.start("first-task", worker_argv=long_worker())
    try:
        with pytest.raises(SupervisorError, match="already running task first-task"):
            supervisor.start("second-task", worker_argv=long_worker())
    finally:
        supervisor.stop()

    supervisor.legacy_pid_path.write_text("99999\n", encoding="ascii")
    os.chmod(supervisor.legacy_pid_path, 0o600)
    with pytest.raises(MetadataError, match="legacy PID-only metadata"):
        supervisor.stop()
    assert supervisor.legacy_pid_path.read_text(encoding="ascii") == "99999\n"


def test_shell_wrappers_require_the_recorded_resolver_and_never_kill_a_pid():
    for name, command in (("start-hermes-worker.sh", "start"), ("stop-hermes-worker.sh", "stop")):
        source = (PROJECT / "scripts" / name).read_text(encoding="utf-8")
        assert 'resolve-council-python.sh"' in source
        assert "--runtime-record" in source
        assert "--require-record" in source
        assert f"worker_supervisor {command}" in source
        assert "kill " not in source
        assert "python3" not in source
    foreground = (PROJECT / "scripts" / "run-hermes-task.sh").read_text(encoding="utf-8")
    assert "--overnight" not in foreground
