from __future__ import annotations

import datetime as dt
import json

import pytest

from flyash_phreeqc_ml.council_operator.contracts import (
    ContractError,
    approval_artifact,
    canonical_expired_state_sha256,
    digest_json,
    utc_now,
    validate_approval_artifact,
)
from council_operator_helpers import create_code_remote, make_operator, make_policy, make_request


def _setup_expired(tmp_path, *, allow_expiry: bool = False, task_id: str = "stale-r1-task"):
    remote, live, head = create_code_remote(tmp_path)
    policy = make_policy(remote, allow_expiry=allow_expiry)
    operator = make_operator(tmp_path, remote, live, policy)
    request = make_request(policy, head, task_id=task_id)
    operator.state.submit(request, "controller", has_private_control_remote=False)
    operator.state.claim(task_id, "worker-a")
    _make_current_lease_stale(operator, task_id)
    expired = operator.state.expire(task_id, "human:controller")
    return remote, live, policy, operator, request, expired


def _make_current_lease_stale(operator, task_id: str) -> None:
    def stale(record):
        record["revision"] += 1
        record["updated_at"] = utc_now()
        record["lock"]["heartbeat_at"] = "2000-01-01T00:00:00Z"
        record["lock"]["lease_expires_at"] = "2000-01-01T00:00:01Z"
        record["lock"]["state_revision"] = record["revision"]
        return record

    operator.store.mutate(task_id, stale)


def _rehash(artifact: dict) -> None:
    artifact["approval_hash"] = digest_json(
        {
            "schema_version": artifact["schema_version"],
            "kind": artifact["kind"],
            "approved_by": artifact["approved_by"],
            "approved_at": artifact["approved_at"],
            "bindings": artifact["bindings"],
        }
    )


def test_stale_approval_binds_the_complete_exact_expired_state(tmp_path):
    _remote, _live, _policy, operator, request, expired = _setup_expired(tmp_path)

    approval = operator.create_stale_takeover_approval(
        request.task_id, "human:maintainer"
    )
    bindings = approval["bindings"]

    assert bindings["task_id"] == request.task_id
    assert bindings["request_hash"] == request.canonical_request_hash
    assert bindings["base_commit"] == request.base_commit
    assert bindings["expired_state_sha256"] == canonical_expired_state_sha256(expired)
    assert bindings["expired_state_revision"] == str(expired["revision"])
    assert bindings["previous_worker_id"] == "worker-a"
    assert bindings["lease_expires_at"] == expired["lock"]["lease_expires_at"]
    assert bindings["expired_at"] == expired["updated_at"]
    assert bindings["approved_at"] == approval["approved_at"]
    assert bindings["approved_by"] == approval["approved_by"]

    claimed = operator.state.claim(
        request.task_id, "worker-b", human_override=approval
    )
    assert claimed["worker_id"] == "worker-b"
    assert claimed["approvals"][-1] == approval


def test_stale_approval_created_before_the_current_expiry_is_rejected(tmp_path):
    _remote, _live, _policy, operator, request, expired = _setup_expired(tmp_path)
    approval = approval_artifact(
        "stale_lock_takeover", expired, approved_by="human:maintainer"
    )
    before_expiry = (
        dt.datetime.fromisoformat(expired["updated_at"].replace("Z", "+00:00"))
        - dt.timedelta(seconds=1)
    ).isoformat().replace("+00:00", "Z")
    approval["approved_at"] = before_expiry
    approval["bindings"]["approved_at"] = before_expiry
    _rehash(approval)
    validate_approval_artifact(approval)

    with pytest.raises(ContractError, match="predates"):
        operator.state.claim(request.task_id, "worker-b", human_override=approval)


@pytest.mark.parametrize(
    ("binding", "replacement"),
    [
        ("task_id", "another-task"),
        ("request_hash", "0" * 64),
        ("base_commit", "0" * 40),
        ("expired_state_sha256", "f" * 64),
        ("expired_state_revision", "999"),
        ("previous_worker_id", "another-worker"),
        ("lease_expires_at", "1999-12-31T23:59:59Z"),
        ("expired_at", "2001-01-01T00:00:00Z"),
    ],
)
def test_rehashed_approval_for_any_other_expired_identity_is_rejected(
    tmp_path, binding, replacement
):
    _remote, _live, _policy, operator, request, expired = _setup_expired(tmp_path)
    approval = approval_artifact(
        "stale_lock_takeover", expired, approved_by="human:maintainer"
    )
    approval["bindings"][binding] = replacement
    _rehash(approval)
    validate_approval_artifact(approval)

    with pytest.raises(ContractError, match="exact expired state"):
        operator.state.claim(request.task_id, "worker-b", human_override=approval)


def test_approval_is_invalid_after_any_remote_state_revision(tmp_path):
    remote, live, policy, operator, request, expired = _setup_expired(tmp_path)
    approval = approval_artifact(
        "stale_lock_takeover", expired, approved_by="human:maintainer"
    )
    other_controller = make_operator(
        tmp_path, remote, live, policy, worker="worker-other-controller"
    )

    def revise(record):
        record["revision"] += 1
        record["evidence"]["summary"]["post_approval_state_change"] = True
        return record

    other_controller.store.mutate(request.task_id, revise)

    with pytest.raises(ContractError, match="exact expired state"):
        operator.state.claim(request.task_id, "worker-b", human_override=approval)

    current = operator.store.read(request.task_id)
    replacement = approval_artifact(
        "stale_lock_takeover", current, approved_by="human:maintainer"
    )
    assert operator.state.claim(
        request.task_id, "worker-b", human_override=replacement
    )["worker_id"] == "worker-b"


def test_approval_from_an_earlier_expiry_cycle_fails_after_heartbeat(tmp_path):
    _remote, _live, _policy, operator, request, first_expired = _setup_expired(
        tmp_path, allow_expiry=True
    )
    old_approval = approval_artifact(
        "stale_lock_takeover", first_expired, approved_by="human:maintainer"
    )

    operator.state.claim(request.task_id, "worker-b")
    operator.state.heartbeat(request.task_id, "worker-b")
    _make_current_lease_stale(operator, request.task_id)
    second_expired = operator.state.expire(request.task_id, "human:controller")
    assert second_expired["revision"] > first_expired["revision"]

    with pytest.raises(ContractError, match="exact expired state"):
        operator.state.claim(request.task_id, "worker-c", human_override=old_approval)

    current_approval = approval_artifact(
        "stale_lock_takeover", second_expired, approved_by="human:maintainer"
    )
    assert operator.state.claim(
        request.task_id, "worker-c", human_override=current_approval
    )["worker_id"] == "worker-c"


def test_expired_task_rejects_previous_worker_heartbeat_without_changing_approval_identity(tmp_path):
    _remote, _live, _policy, operator, request, expired = _setup_expired(tmp_path)
    before_hash = canonical_expired_state_sha256(expired)

    with pytest.raises(ContractError, match="expired task cannot be heartbeated"):
        operator.state.heartbeat(request.task_id, "worker-a")

    unchanged = operator.store.read(request.task_id)
    assert unchanged == expired
    assert canonical_expired_state_sha256(unchanged) == before_hash
    approval = approval_artifact(
        "stale_lock_takeover", unchanged, approved_by="human:maintainer"
    )
    claimed = operator.state.claim(
        request.task_id, "worker-b", human_override=approval
    )
    assert claimed["worker_id"] == "worker-b"
    assert claimed["approvals"][-1] == approval


def test_successful_takeover_consumes_approval_and_blocks_replay(tmp_path):
    _remote, _live, _policy, operator, request, expired = _setup_expired(tmp_path)
    approval = approval_artifact(
        "stale_lock_takeover", expired, approved_by="human:maintainer"
    )

    operator.state.claim(request.task_id, "worker-b", human_override=approval)
    with pytest.raises(ContractError, match="already been consumed"):
        operator.state.claim(request.task_id, "worker-b", human_override=approval)


def test_modified_approval_time_or_human_breaks_the_bound_artifact(tmp_path):
    _remote, _live, _policy, operator, request, expired = _setup_expired(tmp_path)
    approval = approval_artifact(
        "stale_lock_takeover", expired, approved_by="human:maintainer"
    )

    for field, replacement in (
        ("approved_at", "2030-01-01T00:00:00Z"),
        ("approved_by", "human:someone-else"),
    ):
        modified = json.loads(json.dumps(approval))
        modified[field] = replacement
        with pytest.raises(ContractError, match="approval"):
            operator.state.claim(request.task_id, "worker-b", human_override=modified)
