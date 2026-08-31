"""Append-only Git-ref state and atomic cross-computer task locks."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable, Iterator, Mapping

from .contracts import (
    ContractError,
    OPERATOR_VERSION,
    ProjectPolicy,
    TaskRequest,
    approval_artifact,
    canonical_json,
    new_state_record,
    parse_time,
    transition_record,
    utc_now,
    validate_stale_takeover_approval,
    validate_state_record,
)


class StateStoreError(RuntimeError):
    """Remote state could not be read or written."""


class StateConflict(StateStoreError):
    """The remote ref moved during a compare-and-swap update."""


class TaskNotFound(StateStoreError):
    """No remote state ref exists for the task."""


def _safe_environment() -> dict[str, str]:
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT", "SSH_AUTH_SOCK"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "WPI Council Operator",
            "GIT_AUTHOR_EMAIL": "wpi-council@invalid.local",
            "GIT_COMMITTER_NAME": "WPI Council Operator",
            "GIT_COMMITTER_EMAIL": "wpi-council@invalid.local",
        }
    )
    return environment


class GitRefStateStore:
    """Store one append-only state history per task on a remote Git ref.

    Every update commit has the previously observed remote commit as its parent.
    A normal (non-force) push is therefore an atomic compare-and-swap: only one
    claimant can advance a task ref from a given state.
    """

    def __init__(self, remote: str, cache_root: Path, project_slug: str):
        self.remote = remote
        self.cache_root = Path(cache_root)
        self.project_slug = project_slug
        self.repository = self.cache_root / f"{project_slug}.git"
        self.lock_path = self.cache_root / f"{project_slug}.lock"

    def _run(
        self,
        *arguments: str,
        input_data: bytes | None = None,
        check: bool = True,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[bytes]:
        completed = subprocess.run(
            ["git", "--git-dir", str(self.repository), *arguments],
            input=input_data,
            capture_output=True,
            shell=False,
            timeout=timeout,
            env=_safe_environment(),
        )
        if check and completed.returncode:
            raise StateStoreError("Git state operation failed")
        return completed

    def initialize(self) -> None:
        self.cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.cache_root.is_symlink() or not self.cache_root.is_dir():
            raise StateStoreError("state cache root is unsafe")
        if not self.repository.exists():
            subprocess.run(
                ["git", "init", "--bare", str(self.repository)],
                check=True,
                capture_output=True,
                timeout=15,
                env=_safe_environment(),
            )
        if self.repository.is_symlink() or not self.repository.is_dir():
            raise StateStoreError("state cache repository is unsafe")
        existing = self._run("remote", "get-url", "origin", check=False)
        if existing.returncode:
            self._run("remote", "add", "origin", self.remote)
        elif existing.stdout.decode("utf-8", errors="replace").strip() != self.remote:
            raise StateStoreError("state cache remote does not match configuration")

    @contextlib.contextmanager
    def _local_lock(self) -> Iterator[None]:
        self.initialize()
        descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def ref(self, task_id: str) -> str:
        return f"refs/heads/council-state/{self.project_slug}/{task_id}"

    def _remote_oid(self, task_id: str) -> str | None:
        ref = self.ref(task_id)
        completed = self._run("ls-remote", "--refs", "origin", ref, check=False, timeout=60)
        if completed.returncode:
            raise StateStoreError("remote state lookup failed")
        lines = [line for line in completed.stdout.decode("utf-8", errors="replace").splitlines() if line]
        if not lines:
            return None
        if len(lines) != 1:
            raise StateStoreError("remote state ref is ambiguous")
        oid, observed_ref = lines[0].split("\t", 1)
        if observed_ref != ref or not all(character in "0123456789abcdef" for character in oid):
            raise StateStoreError("remote state ref is malformed")
        return oid

    def _fetch_oid(self, oid: str) -> None:
        completed = self._run("fetch", "--no-tags", "origin", oid, check=False, timeout=60)
        if completed.returncode:
            raise StateStoreError("remote state fetch failed")

    def _read_oid(self, oid: str) -> dict[str, Any]:
        self._fetch_oid(oid)
        size_result = self._run("cat-file", "-s", f"{oid}:state.json")
        try:
            size = int(size_result.stdout.decode("ascii").strip())
        except (UnicodeError, ValueError) as exc:
            raise StateStoreError("remote state record size is invalid") from exc
        if size < 1 or size > 2 * 1024 * 1024:
            raise StateStoreError("remote state record exceeds the bounded size")
        completed = self._run("show", f"{oid}:state.json")
        try:
            record = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise StateStoreError("remote state record is invalid") from exc
        try:
            validate_state_record(record)
        except ContractError as exc:
            raise StateStoreError("remote state record failed validation") from exc
        return record

    def read(self, task_id: str) -> dict[str, Any]:
        with self._local_lock():
            oid = self._remote_oid(task_id)
            if oid is None:
                raise TaskNotFound(task_id)
            return self._read_oid(oid)

    def _commit(self, record: Mapping[str, Any], parent: str | None) -> str:
        blob = self._run("hash-object", "-w", "--stdin", input_data=canonical_json(record)).stdout.decode().strip()
        tree_input = f"100644 blob {blob}\tstate.json\n".encode()
        tree = self._run("mktree", input_data=tree_input).stdout.decode().strip()
        arguments = ["commit-tree", tree]
        if parent:
            arguments.extend(["-p", parent])
        message = f"state {record['task_id']} r{record['revision']} {record['state']}\n".encode()
        return self._run(*arguments, input_data=message).stdout.decode().strip()

    def create(self, record: Mapping[str, Any]) -> dict[str, Any]:
        validate_state_record(record)
        task_id = str(record["task_id"])
        with self._local_lock():
            if self._remote_oid(task_id) is not None:
                raise StateConflict("task ID already exists")
            commit = self._commit(record, None)
            pushed = self._run("push", "--porcelain", "origin", f"{commit}:{self.ref(task_id)}", check=False, timeout=60)
            if pushed.returncode:
                raise StateConflict("task creation lost a concurrent compare-and-swap")
        return json.loads(json.dumps(record))

    def mutate(self, task_id: str, callback: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        with self._local_lock():
            old_oid = self._remote_oid(task_id)
            if old_oid is None:
                raise TaskNotFound(task_id)
            current = self._read_oid(old_oid)
            updated = callback(json.loads(json.dumps(current)))
            validate_state_record(updated)
            if canonical_json(updated) == canonical_json(current):
                return updated
            commit = self._commit(updated, old_oid)
            pushed = self._run("push", "--porcelain", "origin", f"{commit}:{self.ref(task_id)}", check=False, timeout=60)
            if pushed.returncode:
                raise StateConflict("remote state changed during compare-and-swap")
            return updated


class TaskStateController:
    """High-level lock, lease, transition, evidence, and approval operations."""

    def __init__(self, store: GitRefStateStore, policy: ProjectPolicy):
        self.store = store
        self.policy = policy

    def submit(self, request: TaskRequest, actor: str, *, has_private_control_remote: bool) -> dict[str, Any]:
        self.policy.validate_request(request, has_private_control_remote=has_private_control_remote)
        try:
            current = self.store.read(request.task_id)
        except TaskNotFound:
            return self.store.create(new_state_record(request, self.policy.project_slug, actor))
        if current["state"] != "submitted" or current["worker_id"] is not None:
            raise ContractError("claimed or completed requests are immutable")
        old_request = TaskRequest.from_dict(current["request"])
        if request.canonical_request_hash == old_request.canonical_request_hash:
            return current
        if request.revision != old_request.revision + 1:
            raise ContractError("a revised request must increment revision by one")

        def revise(record: dict[str, Any]) -> dict[str, Any]:
            if record["state"] != "submitted" or record["request_hash"] != old_request.canonical_request_hash:
                raise StateConflict("request changed before revision")
            record["request"] = request.to_dict()
            record["request_hash"] = request.canonical_request_hash
            record["base_commit"] = request.base_commit
            record["revision"] += 1
            record["updated_at"] = utc_now()
            record["evidence"]["summary"] = {
                "request_revision": request.revision,
                "previous_request_hash": old_request.canonical_request_hash,
                "revised_by": actor,
            }
            return record

        return self.store.mutate(request.task_id, revise)

    def claim(self, task_id: str, worker_id: str, *, human_override: Mapping[str, Any] | None = None) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            if record["state"] == "claimed" and record["worker_id"] == worker_id:
                if human_override is not None:
                    validate_stale_takeover_approval(human_override, record)
                return record
            if record["state"] not in {"submitted", "expired"}:
                raise ContractError("task is not claimable")
            takeover_approval: Mapping[str, Any] | None = None
            if record["state"] == "expired":
                if human_override is not None:
                    validate_stale_takeover_approval(human_override, record)
                    takeover_approval = human_override
                elif not self.policy.allow_policy_expiry_takeover:
                    raise ContractError("stale-lock takeover requires exact human approval")
            elif human_override is not None:
                raise ContractError("stale-lock approval applies only to its exact expired state")
            lease_expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=self.policy.lease_seconds)

            def bind(updated: dict[str, Any]) -> None:
                updated["worker_id"] = worker_id
                updated["lock"] = {
                    "task_id": updated["task_id"],
                    "request_hash": updated["request_hash"],
                    "base_commit": updated["base_commit"],
                    "worker_id": worker_id,
                    "acquired_at": updated["updated_at"],
                    "heartbeat_at": updated["updated_at"],
                    "lease_expires_at": lease_expires.isoformat().replace("+00:00", "Z"),
                    "operator_version": OPERATOR_VERSION,
                    "state_revision": updated["revision"],
                }
                if takeover_approval is not None:
                    updated["approvals"].append(json.loads(json.dumps(takeover_approval)))

            return transition_record(record, "claimed", actor=worker_id, event="claim", mutate=bind)

        return self.store.mutate(task_id, mutate)

    def heartbeat(self, task_id: str, worker_id: str) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            if record["worker_id"] != worker_id or record["lock"] is None:
                raise ContractError("worker does not hold the task lock")
            if record["state"] == "expired":
                raise ContractError("an explicitly expired task cannot be heartbeated")
            before_request = record["request_hash"]
            now = dt.datetime.now(dt.timezone.utc)
            record["revision"] += 1
            record["updated_at"] = now.isoformat().replace("+00:00", "Z")
            record["lock"]["heartbeat_at"] = record["updated_at"]
            record["lock"]["lease_expires_at"] = (now + dt.timedelta(seconds=self.policy.lease_seconds)).isoformat().replace("+00:00", "Z")
            record["lock"]["state_revision"] = record["revision"]
            if record["request_hash"] != before_request:
                raise ContractError("heartbeat attempted to modify the request")
            return record

        return self.store.mutate(task_id, mutate)

    def lease_is_stale(self, record: Mapping[str, Any], *, now: dt.datetime | None = None) -> bool:
        validate_state_record(record)
        if record["lock"] is None:
            return False
        current = now or dt.datetime.now(dt.timezone.utc)
        return parse_time(record["lock"]["lease_expires_at"], "lease_expires_at") <= current

    def expire(self, task_id: str, actor: str) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            if not self.lease_is_stale(record):
                raise ContractError("task lease has not expired")
            return transition_record(record, "expired", actor=actor, event="lease_expired", detail="policy-bound lease expiry")

        return self.store.mutate(task_id, mutate)

    def release(self, task_id: str, worker_id: str) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            if record["lock"] is None:
                return record
            if record["worker_id"] != worker_id:
                raise ContractError("worker does not hold the task lock")
            if record["state"] == "claimed":
                def clear(updated: dict[str, Any]) -> None:
                    updated["worker_id"] = None
                    updated["lock"] = None
                return transition_record(record, "submitted", actor=worker_id, event="release", mutate=clear)
            if record["state"] not in {"awaiting_human_review", "approved", "rejected", "failed", "cancelled"}:
                raise ContractError("an active task may be released only before routing or after completion")
            record["revision"] += 1
            record["updated_at"] = utc_now()
            record["worker_id"] = None
            record["lock"] = None
            return record

        return self.store.mutate(task_id, mutate)

    def transition(self, task_id: str, worker_id: str, target: str, *, event: str, detail: str = "", mutation: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            if record["worker_id"] != worker_id or record["lock"] is None:
                raise ContractError("worker does not hold the task lock")
            return transition_record(record, target, actor=worker_id, event=event, detail=detail, mutate=mutation)

        return self.store.mutate(task_id, mutate)

    def attach_attempt(self, task_id: str, worker_id: str, attempt: Mapping[str, Any], *, failed: bool = False) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            if record["worker_id"] != worker_id:
                raise ContractError("worker does not own this task")
            record["revision"] += 1
            record["updated_at"] = utc_now()
            record["evidence"]["attempts"].append(json.loads(json.dumps(attempt)))
            if failed:
                record["evidence"]["retained_failed_attempts"].append(attempt["attempt_id"])
            return record

        return self.store.mutate(task_id, mutate)

    def set_summary(self, task_id: str, worker_id: str, summary: Mapping[str, Any]) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            if record["worker_id"] != worker_id:
                raise ContractError("worker does not own this task")
            record["revision"] += 1
            record["updated_at"] = utc_now()
            record["evidence"]["summary"] = json.loads(json.dumps(summary))
            return record

        return self.store.mutate(task_id, mutate)

    def approve(self, task_id: str, kind: str, approved_by: str, *, extra_bindings: Mapping[str, str] | None = None) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            artifact = approval_artifact(kind, record, approved_by=approved_by, extra_bindings=extra_bindings)
            if any(item.get("approval_hash") == artifact["approval_hash"] for item in record["approvals"]):
                return record
            if kind == "task_branch":
                if record["state"] != "awaiting_human_review":
                    raise ContractError("task branch is not awaiting human review")
                def append(updated: dict[str, Any]) -> None:
                    updated["approvals"].append(artifact)
                return transition_record(record, "approved", actor=approved_by, event="approve_task_branch", mutate=append)
            if record["state"] != "approved":
                raise ContractError("later approvals require an approved task branch")
            record["revision"] += 1
            record["updated_at"] = utc_now()
            record["approvals"].append(artifact)
            return record

        return self.store.mutate(task_id, mutate)

    def reject(self, task_id: str, actor: str, reason: str) -> dict[str, Any]:
        return self.store.mutate(
            task_id,
            lambda record: transition_record(record, "rejected", actor=actor, event="reject", detail=reason),
        )

    def cancel(self, task_id: str, actor: str) -> dict[str, Any]:
        return self.store.mutate(
            task_id,
            lambda record: transition_record(record, "cancelled", actor=actor, event="cancel"),
        )
