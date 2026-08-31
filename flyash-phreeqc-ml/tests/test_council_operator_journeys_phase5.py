from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from flyash_phreeqc_ml.council_operator.contracts import ContractError, SensitiveContentError
from flyash_phreeqc_ml.council_operator.backends import AttemptResult
from flyash_phreeqc_ml.council_operator.operator import OperatorError
from flyash_phreeqc_ml.council_operator.store import StateConflict
from council_operator_helpers import create_code_remote, make_operator, make_policy, make_request


def setup_operator(tmp_path, *, task_id="journey-task", **request_overrides):
    remote, live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(policy, head, task_id=task_id, **request_overrides)
    operator.state.submit(request, "controller", has_private_control_remote=False)
    operator.claim(task_id)
    return remote, live, head, policy, operator, request


def remote_sha(remote: Path, branch: str) -> str | None:
    output = subprocess.check_output(
        ["git", "ls-remote", "--heads", str(remote), f"refs/heads/{branch}"], text=True
    ).strip()
    return output.split("\t", 1)[0] if output else None


def test_journey_1_successful_bounded_task_pushes_only_task_branch(tmp_path):
    remote, live, head, _policy, operator, request = setup_operator(tmp_path)
    result = operator.run(request.task_id, fake_scenario="valid")
    assert result["state"] == "awaiting_human_review"
    assert result["task_branch"] == f"council/wpi/{request.task_id}"
    assert remote_sha(remote, "base") == head
    assert remote_sha(remote, result["task_branch"]) == result["final_task_commit"]
    assert subprocess.check_output(["git", "-C", live, "status", "--porcelain"], text=True) == ""
    branch_tree = subprocess.check_output(
        ["git", "--git-dir", remote, "ls-tree", "-r", "--name-only", result["final_task_commit"]], text=True
    ).splitlines()
    assert f"council-results/{request.task_id}.json" in branch_tree
    assert not any("transcript" in path or "role-output" in path for path in branch_tree)
    trusted_tests = result["evidence"]["attempts"][0]["evidence"]["trusted_required_tests"]
    assert trusted_tests[0]["exit_code"] == 0
    assert "stdout" not in trusted_tests[0]


@pytest.mark.parametrize("scenario", ["coder_fail_once", "tester_fail_once", "reviewer_reject_once"])
def test_journeys_2_to_4_automatic_corrections_preserve_failed_attempts(tmp_path, scenario):
    remote, _live, _head, _policy, operator, request = setup_operator(tmp_path, task_id=f"task-{scenario.replace('_', '-')}")
    result = operator.run(request.task_id, fake_scenario=scenario)
    assert result["state"] == "awaiting_human_review"
    assert result["correction_round"] == 1
    assert result["evidence"]["retained_failed_attempts"] == ["attempt-00"]
    assert len(result["evidence"]["attempts"]) == 2
    assert remote_sha(remote, result["task_branch"]) == result["final_task_commit"]


def test_repeated_failure_stops_at_bound_and_never_pushes(tmp_path):
    remote, _live, _head, _policy, operator, request = setup_operator(tmp_path, task_id="repeat-failure")
    result = operator.run(request.task_id, fake_scenario="repeat_failure")
    assert result["state"] == "failed"
    assert "same automatic failure" in result["failure"]
    assert result["evidence"]["retained_failed_attempts"] == ["attempt-00", "attempt-01"]
    assert remote_sha(remote, f"council/wpi/{request.task_id}") is None


def test_tester_failure_has_no_intermediate_branch_push(tmp_path):
    remote, _live, _head, _policy, operator, request = setup_operator(
        tmp_path, task_id="tester-no-push", max_correction_rounds=0
    )
    result = operator.run(request.task_id, fake_scenario="tester_fail_once")
    assert result["state"] == "failed"
    assert remote_sha(remote, f"council/wpi/{request.task_id}") is None


def test_journey_5_two_computer_monitor_lock_heartbeat_and_restart(tmp_path):
    remote, live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    controller = make_operator(tmp_path, remote, live, policy, worker="personal")
    worker_a = make_operator(tmp_path, remote, live, policy, worker="hermes-a")
    worker_b = make_operator(tmp_path, remote, live, policy, worker="hermes-b")
    request = make_request(policy, head, task_id="two-computer")
    controller.state.submit(request, "personal", has_private_control_remote=False)
    claimed = worker_a.claim(request.task_id)
    assert controller.status(request.task_id)["worker_id"] == "hermes-a"
    with pytest.raises((ContractError, StateConflict)):
        worker_b.claim(request.task_id)
    heartbeat = worker_a.heartbeat(request.task_id)
    restarted = make_operator(tmp_path, remote, live, policy, worker="hermes-a")
    assert restarted.store.read(request.task_id)["revision"] == heartbeat["revision"]
    released = restarted.release(request.task_id)
    assert released["state"] == "submitted"
    assert controller.status(request.task_id)["worker_id"] is None


def test_journey_7_public_privacy_boundary_and_no_state_ref_for_rejected_task(tmp_path):
    remote, live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    operator = make_operator(tmp_path, remote, live, policy)
    private = make_request(policy, head, task_id="private-task", security_privacy="confidential")
    with pytest.raises(SensitiveContentError):
        operator.state.submit(private, "controller", has_private_control_remote=False)
    assert remote_sha(remote, f"council-state/{policy.project_slug}/private-task") is None


def test_branch_collision_fails_closed_without_overwriting_remote(tmp_path):
    remote, live, head, _policy, operator, request = setup_operator(tmp_path, task_id="collision-task")
    subprocess.run(
        ["git", "-C", live, "push", "origin", f"{head}:refs/heads/council/wpi/{request.task_id}"],
        check=True,
        capture_output=True,
    )
    with pytest.raises(OperatorError, match="collision"):
        operator.run(request.task_id, fake_scenario="valid")
    assert remote_sha(remote, f"council/wpi/{request.task_id}") == head
    assert remote_sha(remote, "base") == head


def test_task_from_project_a_cannot_operate_on_project_b(tmp_path):
    remote_a, _live_a, head_a = create_code_remote(tmp_path / "a")
    remote_b, live_b, _head_b = create_code_remote(tmp_path / "b")
    policy_b = make_policy(remote_b)
    operator_b = make_operator(tmp_path / "controller-b", remote_b, live_b, policy_b)
    request_a = make_request(replace(policy_b, allowed_repository=str(remote_a)), head_a, task_id="cross-project")
    with pytest.raises(ContractError, match="repository"):
        operator_b.state.submit(request_a, "controller", has_private_control_remote=False)


def test_task_branch_approval_does_not_merge_or_deploy(tmp_path):
    remote, _live, head, _policy, operator, request = setup_operator(tmp_path, task_id="approval-task")
    result = operator.run(request.task_id, fake_scenario="valid")
    approved = operator.approve(request.task_id, "task_branch", "human:maintainer")
    assert approved["state"] == "approved"
    assert remote_sha(remote, "base") == head
    assert remote_sha(remote, result["task_branch"]) == result["final_task_commit"]
    assert [item["kind"] for item in approved["approvals"]] == ["task_branch"]


def test_fake_backend_is_never_represented_as_real_council_evidence(tmp_path):
    remote, _live, _head, _policy, operator, request = setup_operator(tmp_path, task_id="fake-label")
    result = operator.run(request.task_id, fake_scenario="valid")
    attempt = result["evidence"]["attempts"][0]
    assert attempt["evidence"]["synthetic_test_only"] is True
    assert attempt["evidence"]["real_council_evidence"] is False
    manifest = operator.review_manifest(request.task_id)
    assert manifest["raw_role_transcripts_included"] is False


def test_every_required_test_command_runs_and_failure_blocks_push(tmp_path):
    remote, live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    policy = replace(
        policy,
        allowed_test_command_prefixes=(
            ("python3", "-m", "compileall"),
            ("python3", "-c"),
        ),
    )
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(
        policy,
        head,
        task_id="required-tests",
        max_correction_rounds=0,
        required_test_commands=[
            ["python3", "-m", "compileall", "-q", "."],
            ["python3", "-c", "raise SystemExit(7)"],
        ],
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
    assert checked.passed is False
    assert checked.correction_kind == "tester_gate"
    tests = checked.evidence["trusted_required_tests"]
    assert [item["exit_code"] for item in tests] == [0, 7]
    assert remote_sha(remote, f"council/wpi/{request.task_id}") is None


def test_resume_retains_an_interrupted_attempt_and_uses_a_new_attempt_id(tmp_path):
    _remote, _live, _head, _policy, operator, request = setup_operator(tmp_path, task_id="resume-boundary")
    operator.sandboxes.create_trusted_workspace(request)
    operator.sandboxes.create_attempt_root(request.task_id, "attempt-00")
    restarted = make_operator(
        tmp_path,
        Path(operator.config.code_remote),
        operator.config.repository_path,
        operator.policy,
        worker=operator.config.worker_id,
    )
    result = restarted.resume(request.task_id, fake_scenario="valid")
    assert result["state"] == "awaiting_human_review"
    assert result["evidence"]["retained_failed_attempts"] == ["attempt-00"]
    assert [item["attempt_id"] for item in result["evidence"]["attempts"]] == ["attempt-00", "attempt-01"]


@pytest.mark.parametrize("payload", [b"\x00" * 2048, b"x" * 20000])
def test_final_boundary_rejects_binary_or_oversized_untracked_content(tmp_path, payload):
    remote, live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(policy, head, task_id="final-boundary", max_patch_bytes=100)
    workspace, _journal = operator.sandboxes.create_trusted_workspace(request)
    (workspace / "README.md").write_bytes(payload)
    result = SimpleNamespace(
        route={"selected_level": "STANDARD"},
        changed_paths=("README.md",),
        patch_hash="1" * 64,
        test_evidence_hash="2" * 64,
        reviewer_report_hash="3" * 64,
        reviewer_verdict="APPROVE",
        evidence={"invocation_evidence_sha256": {}},
    )
    with pytest.raises(OperatorError, match="binary|patch-size"):
        operator._commit_and_push(request, workspace, result, "fake")
    assert remote_sha(remote, "council/wpi/final-boundary") is None


def test_no_force_push_or_main_push_contract_in_operator_source():
    source = (Path(__file__).resolve().parents[1] / "flyash_phreeqc_ml" / "council_operator" / "operator.py").read_text(encoding="utf-8")
    assert "--force" not in source
    assert "HEAD:refs/heads/main" not in source
    assert "merge" not in [line.strip() for line in source.splitlines() if "subprocess" in line]
