from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import pytest

from flyash_phreeqc_ml.council_operator.backends import AttemptResult
from flyash_phreeqc_ml.council_operator.operator import OperatorError
from flyash_phreeqc_ml.council_operator import remote_preflight
from flyash_phreeqc_ml.council_operator.remote_preflight import (
    classify_git_failure,
    probe_operator_remotes,
)
from council_operator_helpers import create_code_remote, make_operator, make_policy, make_request


def _refs(remote: Path) -> str:
    return subprocess.check_output(
        ["git", "--git-dir", str(remote), "for-each-ref", "--format=%(objectname) %(refname)"],
        text=True,
    )


def test_phase5_ci_runs_every_r1_functional_contract_and_preserves_both_release_gates():
    workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    for required in (
        "python -m venv /tmp/wpi-phase5-ci-venv",
        "tests/test_council_operator_installation_phase5_r1.py",
        "tests/test_council_operator_runtime_phase5_r1.py",
        "tests/test_council_operator_stale_lock_r1.py",
        "tests/test_council_operator_worker_supervisor_phase5_r1.py",
        "Run complete Python suite",
        "Run full suite in exact test image",
        "phase4-release-gate:",
        "phase5-release-gate:",
    ):
        assert required in workflow


def test_local_bare_remote_permission_preflight_is_push_authorized_and_non_mutating(tmp_path):
    remote, live, _head = create_code_remote(tmp_path)
    control = tmp_path / "control.git"
    subprocess.run(["git", "init", "--bare", str(control)], check=True, capture_output=True)
    before = {"code": _refs(remote), "control": _refs(control)}
    result = probe_operator_remotes(
        live,
        str(remote),
        str(control),
        project_slug="wpi-virtual-lab",
    )
    after = {"code": _refs(remote), "control": _refs(control)}

    assert result["code"]["readable"] is True
    assert result["code"]["push_authorized"] is True
    assert result["control"]["readable"] is True
    assert result["control"]["push_authorized"] is True
    assert result["code"]["non_mutating"] is True
    assert result["control"]["non_mutating"] is True
    assert before == after
    assert "permission-preflight" not in after


@pytest.mark.parametrize(
    ("message", "operation", "expected"),
    [
        ("fatal: Authentication failed", "read", "authentication_failure"),
        ("Permission denied (publickey)", "push", "authentication_failure"),
        ("fatal: unable to access: Could not resolve host", "read", "network_failure"),
        ("fatal: unable to access: SSL certificate problem", "push", "network_failure"),
        ("send-pack: unexpected disconnect: Connection reset by peer", "push", "network_failure"),
        ("ssh: Could not resolve hostname: Temporary failure in name resolution", "push", "network_failure"),
        (
            "ssh: connect to host example port 22: Operation timed out\n"
            "fatal: Could not read from remote repository. Check access rights.",
            "push",
            "network_failure",
        ),
        ("remote: protected branch update failed", "push", "branch_ref_policy_refusal"),
        ("remote: pre-receive hook declined", "push", "branch_ref_policy_refusal"),
        ("fatal: an unfamiliar transport failure", "push", "unclassified_failure"),
    ],
)
def test_remote_permission_failures_have_distinct_safe_categories(message, operation, expected):
    assert classify_git_failure(message, operation=operation) == expected


def test_remote_probe_timeout_is_reported_as_network_failure(tmp_path, monkeypatch):
    remote, live, _head = create_code_remote(tmp_path)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "ls-remote"], timeout=60)

    monkeypatch.setattr(remote_preflight.subprocess, "run", timeout)
    result = remote_preflight.probe_remote_permission(
        live,
        str(remote),
        project_slug="wpi-virtual-lab",
        kind="code_branch",
    )
    assert result["readable"] is False
    assert result["push_authorized"] is False
    assert result["failure_kind"] == "network_failure"
    assert result["non_mutating"] is True


def test_doctor_reports_read_and_exact_namespace_push_permissions_without_remote_changes(tmp_path):
    remote, live, _head = create_code_remote(tmp_path)
    control = tmp_path / "control.git"
    subprocess.run(["git", "init", "--bare", str(control)], check=True, capture_output=True)
    operator = make_operator(tmp_path, remote, live, make_policy(remote), control_remote=control)
    before = {"code": _refs(remote), "control": _refs(control)}
    report = operator.doctor()
    after = {"code": _refs(remote), "control": _refs(control)}

    assert report["code_remote_readable"] is True
    assert report["code_remote_push_authorized"] is True
    assert report["control_remote_readable"] is True
    assert report["control_remote_push_authorized"] is True
    assert report["remote_permission_preflight_non_mutating"] is True
    assert before == after


def test_trusted_python_command_uses_operator_312_not_path_python39_and_strips_environment(
    tmp_path,
    monkeypatch,
):
    remote, live, head = create_code_remote(tmp_path / "repository")
    policy = replace(
        make_policy(remote),
        allowed_test_command_prefixes=(("python3", "-c"), ("python", "-c")),
    )
    operator = make_operator(tmp_path, remote, live, policy)
    fake_bin = tmp_path / "system-bin"
    fake_bin.mkdir()
    marker = tmp_path / "system-python39-selected"
    system_python = fake_bin / "python3"
    system_python.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"--version\" ]; then\n"
        "  printf '%s\\n' 'Python 3.9.18'\n"
        "  exit 0\n"
        "fi\n"
        f"printf '%s\\n' selected > '{marker}'\n"
        "exit 39\n",
        encoding="utf-8",
    )
    system_python.chmod(0o755)
    leaked_path = tmp_path / "unapproved-pythonpath"
    leaked_path.mkdir()
    (leaked_path / "unapproved_dependency.py").write_text("VALUE = 39\n", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PYTHONPATH", str(leaked_path))
    monkeypatch.setenv("WPI_TEST_UNAPPROVED_SECRET", "must-not-leak")
    assert subprocess.check_output(["python3", "--version"], text=True).strip() == "Python 3.9.18"

    proof = live / "trusted-runtime-proof.json"
    code = (
        "import importlib.util, json, os, pathlib, sys; "
        "assert sys.version_info[:2] == (3, 12); "
        "assert 'PYTHONPATH' not in os.environ; "
        "assert 'WPI_TEST_UNAPPROVED_SECRET' not in os.environ; "
        "assert importlib.util.find_spec('unapproved_dependency') is None; "
        f"pathlib.Path({str(proof)!r}).write_text(json.dumps({{'executable':sys.executable,'prefix':sys.prefix,'version':list(sys.version_info[:3])}}))"
    )
    request = make_request(
        policy,
        head,
        task_id="pinned-python",
        required_test_commands=[["python3", "-c", code]],
    )
    result = AttemptResult(
        attempt_id="attempt-00",
        passed=True,
        correction_kind=None,
        correction_detail="",
        workspace=live,
        route={"selected_level": "STANDARD"},
        changed_paths=(),
        patch_hash="1" * 64,
        test_evidence_hash="2" * 64,
        reviewer_report_hash="3" * 64,
        reviewer_verdict="APPROVE",
        evidence={},
    )

    checked = operator._run_required_tests(request, result, time.monotonic())
    record = checked.evidence["trusted_required_tests"][0]
    identity = checked.evidence["trusted_test_python"]
    observed = json.loads(proof.read_text(encoding="utf-8"))

    assert checked.passed is True
    assert record["requested_arguments"] == ["python3", "-c", code]
    assert record["effective_arguments"][0] == identity["executable"]
    assert Path(identity["executable"]).is_absolute()
    assert identity["version"].startswith("3.12.")
    assert identity["executable_sha256"] == hashlib.sha256(Path(identity["executable"]).read_bytes()).hexdigest()
    assert identity["environment_prefix"] == sys.prefix
    assert re.fullmatch(r"[0-9a-f]{64}", identity["dependency_contract_sha256"])
    assert observed["executable"] == identity["executable"]
    assert observed["prefix"] == sys.prefix
    assert observed["version"][:2] == [3, 12]
    assert not marker.exists()


def test_trusted_runtime_rejects_dependency_contract_drift_before_test_execution(
    tmp_path,
):
    remote, live, head = create_code_remote(tmp_path)
    policy = replace(make_policy(remote), allowed_test_command_prefixes=(("python3", "-c"),))
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(
        policy,
        head,
        task_id="dependency-drift",
        required_test_commands=[["python3", "-c", "raise SystemExit(0)"]],
    )
    result = AttemptResult(
        attempt_id="attempt-00",
        passed=True,
        correction_kind=None,
        correction_detail="",
        workspace=live,
        route={"selected_level": "STANDARD"},
        changed_paths=(),
        patch_hash="1" * 64,
        test_evidence_hash="2" * 64,
        reviewer_report_hash="3" * 64,
        reviewer_verdict="APPROVE",
        evidence={},
    )
    operator.python_runtime = replace(
        operator.python_runtime,
        dependency_contract_sha256="0" * 64,
    )
    with pytest.raises(OperatorError, match="dependency contract changed"):
        operator._run_required_tests(request, result, time.monotonic())


@pytest.mark.parametrize("missing_remote", ["code", "control"])
def test_worker_refuses_model_stages_when_either_remote_push_permission_is_missing(
    tmp_path,
    monkeypatch,
    missing_remote,
):
    remote, live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(policy, head, task_id="preflight-block")
    operator.state.submit(request, "controller", has_private_control_remote=False)
    operator.claim(request.task_id)
    invoked = False

    def should_not_select_backend(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("model backend was selected after failed permission preflight")

    monkeypatch.setattr(operator, "_backend", should_not_select_backend)
    monkeypatch.setattr(
        operator,
        "remote_permission_preflight",
        lambda: {
            "code": {
                "readable": True,
                "push_authorized": missing_remote != "code",
                "failure_kind": "branch_ref_policy_refusal" if missing_remote == "code" else None,
                "non_mutating": True,
            },
            "control": {
                "readable": True,
                "push_authorized": missing_remote != "control",
                "failure_kind": "authentication_failure" if missing_remote == "control" else None,
                "non_mutating": True,
            },
        },
    )
    with pytest.raises(OperatorError, match=rf"{missing_remote} remote push permission"):
        operator.run(request.task_id, fake_scenario="valid")
    assert invoked is False


def test_raw_overnight_run_is_blocked_before_remote_or_model_work(tmp_path, monkeypatch):
    remote, live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(policy, head, task_id="raw-overnight-block")
    operator.state.submit(request, "controller", has_private_control_remote=False)
    operator.claim(request.task_id)
    monkeypatch.delenv("WPI_COUNCIL_SUPERVISED_INSTANCE", raising=False)
    invoked = False

    def forbidden(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("overnight execution reached remote or model work")

    monkeypatch.setattr(operator, "remote_permission_preflight", forbidden)
    monkeypatch.setattr(operator, "_backend", forbidden)
    with pytest.raises(OperatorError, match="safe worker supervisor"):
        operator.run(request.task_id, overnight=True)
    assert invoked is False
