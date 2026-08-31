from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import datetime as dt
import json
from pathlib import Path
import threading

import pytest

from flyash_phreeqc_ml.council_operator.contracts import (
    ContractError,
    TaskRequest,
    approval_artifact,
    new_state_record,
    transition_record,
    utc_now,
    validate_state_record,
)
from flyash_phreeqc_ml.council_operator.store import (
    GitRefStateStore,
    StateConflict,
    TaskStateController,
)
from council_operator_helpers import create_code_remote, make_operator, make_policy, make_request


def setup_state(tmp_path, *, allow_expiry=False, task_id="state-task"):
    remote, live, head = create_code_remote(tmp_path)
    policy = make_policy(remote, allow_expiry=allow_expiry)
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(policy, head, task_id=task_id)
    operator.state.submit(request, "controller", has_private_control_remote=False)
    return remote, live, policy, operator, request


def test_every_normal_state_transition_is_explicit(tmp_path):
    remote, _live, policy, _operator, request = setup_state(tmp_path)
    record = new_state_record(request, policy.project_slug, "controller")
    path = (
        "claimed", "routing", "planning", "coding", "coder_gate", "testing",
        "tester_gate", "reviewing", "automatic_gates_passed", "task_branch_pushed",
        "awaiting_human_review", "approved",
    )
    for target in path:
        record = transition_record(record, target, actor="worker-a", event=f"to_{target}")
        validate_state_record(record)
    assert record["state"] == "approved"
    assert [item["to"] for item in record["transitions"]][1:] == list(path)


@pytest.mark.parametrize(
    ("source", "target"),
    [("submitted", "testing"), ("claimed", "approved"), ("reviewing", "submitted"), ("approved", "rejected")],
)
def test_invalid_state_transitions_fail(tmp_path, source, target):
    _remote, _live, policy, _operator, request = setup_state(tmp_path)
    record = new_state_record(request, policy.project_slug, "controller")
    paths = {
        "submitted": (),
        "claimed": ("claimed",),
        "reviewing": ("claimed", "routing", "planning", "coding", "coder_gate", "testing", "tester_gate", "reviewing"),
        "approved": ("claimed", "routing", "planning", "coding", "coder_gate", "testing", "tester_gate", "reviewing", "automatic_gates_passed", "task_branch_pushed", "awaiting_human_review", "approved"),
    }
    for state in paths[source]:
        record = transition_record(record, state, actor="worker", event="advance")
    with pytest.raises(ContractError):
        transition_record(record, target, actor="worker", event="invalid")


def test_duplicate_claim_refused_and_same_worker_is_idempotent(tmp_path):
    _remote, _live, _policy, operator, request = setup_state(tmp_path)
    first = operator.state.claim(request.task_id, "worker-a")
    same = operator.state.claim(request.task_id, "worker-a")
    assert same == first
    with pytest.raises(ContractError):
        operator.state.claim(request.task_id, "worker-b")


def test_two_workers_race_and_only_one_remote_claim_wins(tmp_path):
    remote, live, policy, operator, request = setup_state(tmp_path)
    controller_a = operator.state
    controller_b = make_operator(tmp_path, remote, live, policy, worker="worker-b").state

    def claim(controller, worker):
        try:
            return controller.claim(request.task_id, worker)["worker_id"]
        except (StateConflict, ContractError):
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda item: claim(*item), ((controller_a, "worker-a"), (controller_b, "worker-b"))))
    assert values.count("refused") == 1
    assert set(values) & {"worker-a", "worker-b"}


def test_heartbeat_changes_only_lease_and_revision(tmp_path):
    _remote, _live, _policy, operator, request = setup_state(tmp_path)
    claimed = operator.state.claim(request.task_id, "worker-a")
    heartbeat = operator.state.heartbeat(request.task_id, "worker-a")
    assert heartbeat["request"] == claimed["request"]
    assert heartbeat["request_hash"] == claimed["request_hash"]
    assert heartbeat["state"] == "claimed"
    assert heartbeat["revision"] == claimed["revision"] + 1
    assert heartbeat["lock"]["state_revision"] == heartbeat["revision"]


def _make_lease_stale(operator, task_id):
    def stale(record):
        record["revision"] += 1
        record["updated_at"] = utc_now()
        record["lock"]["heartbeat_at"] = "2000-01-01T00:00:00Z"
        record["lock"]["lease_expires_at"] = "2000-01-01T00:00:01Z"
        record["lock"]["state_revision"] = record["revision"]
        return record
    operator.store.mutate(task_id, stale)


def test_expiry_visible_and_policy_bound_takeover(tmp_path):
    _remote, _live, _policy, operator, request = setup_state(tmp_path, allow_expiry=True)
    operator.state.claim(request.task_id, "worker-a")
    _make_lease_stale(operator, request.task_id)
    assert operator.state.lease_is_stale(operator.store.read(request.task_id))
    expired = operator.state.expire(request.task_id, "controller")
    assert expired["state"] == "expired"
    claimed = operator.state.claim(request.task_id, "worker-b")
    assert claimed["worker_id"] == "worker-b"


def test_stale_takeover_requires_exact_human_approval_when_policy_disallows(tmp_path):
    _remote, _live, _policy, operator, request = setup_state(tmp_path, allow_expiry=False)
    operator.state.claim(request.task_id, "worker-a")
    _make_lease_stale(operator, request.task_id)
    expired = operator.state.expire(request.task_id, "controller")
    with pytest.raises(ContractError):
        operator.state.claim(request.task_id, "worker-b")
    approval = approval_artifact("stale_lock_takeover", expired, approved_by="human:maintainer")
    forged = json.loads(json.dumps(approval))
    forged["bindings"]["base_commit"] = "0" * 40
    with pytest.raises(ContractError):
        operator.state.claim(request.task_id, "worker-b", human_override=forged)
    claimed = operator.state.claim(request.task_id, "worker-b", human_override=approval)
    assert claimed["worker_id"] == "worker-b"


def test_release_is_idempotent_and_does_not_delete_history(tmp_path):
    _remote, _live, _policy, operator, request = setup_state(tmp_path)
    claimed = operator.state.claim(request.task_id, "worker-a")
    released = operator.state.release(request.task_id, "worker-a")
    again = operator.state.release(request.task_id, "worker-a")
    assert released == again
    assert released["state"] == "submitted"
    assert len(released["transitions"]) == len(claimed["transitions"]) + 1


def test_resume_after_process_restart_reads_remote_state(tmp_path):
    remote, live, policy, operator, request = setup_state(tmp_path)
    claimed = operator.state.claim(request.task_id, "worker-a")
    restarted = make_operator(tmp_path, remote, live, policy, worker="worker-a")
    assert restarted.store.read(request.task_id) == claimed
    assert restarted.state.heartbeat(request.task_id, "worker-a")["state"] == "claimed"


def test_remote_compare_and_swap_failure_is_visible(tmp_path):
    remote, live, policy, operator, request = setup_state(tmp_path)
    operator.state.claim(request.task_id, "worker-a")
    store_a = operator.store
    store_b = make_operator(tmp_path, remote, live, policy, worker="worker-b").store
    barrier = threading.Barrier(2)

    def update(store, marker):
        def callback(record):
            barrier.wait(timeout=5)
            record["revision"] += 1
            record["updated_at"] = utc_now()
            record["lock"]["state_revision"] = record["revision"]
            record["evidence"]["summary"] = {"marker": marker}
            return record
        try:
            return store.mutate(request.task_id, callback)["evidence"]["summary"]["marker"]
        except StateConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: update(*item), ((store_a, "a"), (store_b, "b"))))
    assert results.count("conflict") == 1


def test_claimed_request_is_immutable_and_revision_before_claim_is_allowed(tmp_path):
    _remote, _live, _policy, operator, request = setup_state(tmp_path)
    revised_value = request.to_dict()
    revised_value.update({"revision": 2, "title": "Revised public docs", "canonical_request_hash": ""})
    revised = TaskRequest.seal(revised_value)
    updated = operator.state.submit(revised, "controller", has_private_control_remote=False)
    assert updated["request_hash"] == revised.canonical_request_hash
    operator.state.claim(revised.task_id, "worker-a")
    third_value = revised.to_dict()
    third_value.update({"revision": 3, "title": "Third", "canonical_request_hash": ""})
    with pytest.raises(ContractError, match="immutable"):
        operator.state.submit(TaskRequest.seal(third_value), "controller", has_private_control_remote=False)


def _advance_to_review(operator, task_id):
    operator.state.claim(task_id, "worker-a")
    for target in ("routing", "planning", "coding", "coder_gate", "testing", "tester_gate", "reviewing", "automatic_gates_passed"):
        operator.state.transition(task_id, "worker-a", target, event="advance")
    operator.state.set_summary(
        task_id,
        "worker-a",
        {"patch_hash": "1" * 64, "test_evidence_hash": "2" * 64, "reviewer_report_hash": "3" * 64},
    )
    operator.state.transition(
        task_id,
        "worker-a",
        "task_branch_pushed",
        event="push",
        mutation=lambda item: item.update({"task_branch": "council/wpi/state-task", "final_task_commit": "4" * 40}),
    )
    operator.state.transition(task_id, "worker-a", "awaiting_human_review", event="await")


def test_hash_bound_approval_and_separate_later_approvals(tmp_path):
    _remote, _live, _policy, operator, request = setup_state(tmp_path)
    _advance_to_review(operator, request.task_id)
    approved = operator.state.approve(request.task_id, "task_branch", "human:maintainer")
    assert approved["state"] == "approved"
    assert approved["approvals"][0]["bindings"]["patch_hash"] == "1" * 64
    later = operator.state.approve(request.task_id, "pull_request", "human:maintainer")
    assert later["state"] == "approved"
    assert [item["kind"] for item in later["approvals"]] == ["task_branch", "pull_request"]
    assert later["task_branch"] == "council/wpi/state-task"


def test_reviewer_cannot_self_approve(tmp_path):
    _remote, _live, _policy, operator, request = setup_state(tmp_path)
    _advance_to_review(operator, request.task_id)
    with pytest.raises(ContractError, match="human"):
        operator.state.approve(request.task_id, "task_branch", "reviewer:model")
