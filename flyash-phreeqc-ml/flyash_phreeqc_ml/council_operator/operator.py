"""Trusted-host orchestration, correction loops, task commits, and push boundary."""

from __future__ import annotations

from dataclasses import replace
import contextlib
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Mapping

from .backends import (
    BackendError,
    COUNCIL_REQUIRED_PROFILES,
    ConfigurableCommandBackend,
    CouncilCompatibilityAdapter,
    DeterministicFakeBackend,
    ExecutionBackend,
    HermesBackend,
    file_sha256,
)
from .contracts import (
    ContractError,
    OPERATOR_VERSION,
    OperatorConfig,
    ProjectPolicy,
    TaskRequest,
    approval_artifact,
    canonical_json,
    digest_json,
    load_request_file,
    scan_public_text,
    utc_now,
    validate_relative_path,
    validate_state_record,
)
from .obsidian import append_handoff
from .remote_preflight import RemotePreflightError, probe_operator_remotes
from .runtime import (
    RuntimeIdentityError,
    current_python_identity,
    effective_test_command,
    missing_compileall_targets,
)
from .sandbox import SandboxError, SandboxManager, bounded_process, run_git, safe_git_environment, tree_hash
from .store import GitRefStateStore, StateStoreError, TaskStateController


class OperatorError(RuntimeError):
    """The trusted operator stopped safely."""


class CouncilOperator:
    def __init__(self, config: OperatorConfig, policy: ProjectPolicy):
        config.validate()
        policy.validate()
        if config.code_remote != policy.allowed_repository:
            raise OperatorError("configured code remote does not match the tracked repository identity")
        try:
            self.python_runtime = current_python_identity()
        except RuntimeIdentityError as exc:
            raise OperatorError(str(exc)) from exc
        self.config = config
        self.policy = policy
        self.application_subtree = self._resolve_application_subtree()
        self.store = GitRefStateStore(config.control_remote, config.state_cache_root, policy.project_slug)
        self.state = TaskStateController(self.store, policy)
        self.sandboxes = SandboxManager(config.repository_path, config.sandbox_root, config.code_remote)

    def _resolve_application_subtree(self) -> Path:
        """Bind the loaded package layout to one Git-root-relative subtree."""

        declared = Path(self.policy.application_subtree)
        package_root = Path(__file__).resolve().parents[2]
        try:
            observed = package_root.relative_to(self.config.repository_path.resolve())
        except ValueError:
            # Tests and immutable images may import the package outside the
            # synthetic configured checkout. The tracked policy remains the
            # explicit layout authority in that packaging shape.
            return declared
        if observed.as_posix() != self.policy.application_subtree:
            raise OperatorError("loaded operator package does not match the configured Git-root application subtree")
        return observed

    @classmethod
    def load(cls, config_path: Path, policy_path: Path) -> "CouncilOperator":
        return cls(OperatorConfig.load(config_path), ProjectPolicy.load(policy_path))

    def doctor(self, *, worker: bool = False) -> dict[str, Any]:
        self.config.validate()
        self.policy.validate()
        try:
            self.python_runtime.verify_pinned_environment()
        except RuntimeIdentityError as exc:
            raise OperatorError(str(exc)) from exc
        live = self.sandboxes.verify_live_checkout()
        origin = run_git(self.config.repository_path, "remote", "get-url", "origin").stdout.decode().strip()
        permissions = self.remote_permission_preflight()
        self._require_remote_write_permissions(permissions)
        council_hashes: dict[str, str] = {}
        profile_hashes: dict[str, dict[str, str]] = {}
        if worker:
            if self.config.council_root is None:
                raise OperatorError("worker configuration requires a Council root")
            adapter = CouncilCompatibilityAdapter(
                self.config.council_root,
                self.policy,
                hermes_executable=self.config.hermes_executable,
            )
            council_hashes = adapter.verify()
            profile_hashes = adapter.verify_worker_profiles()
            if shutil.which("docker") is None:
                raise OperatorError("worker requires Docker")
            if self.config.hermes_executable is None or not self.config.hermes_executable.is_file():
                raise OperatorError("worker requires an explicitly configured Hermes executable")
        return {
            "operator_version": OPERATOR_VERSION,
            "project_slug": self.policy.project_slug,
            "repository_public": self.policy.public_repository,
            "public_repository_warning": "Every code branch is publicly readable" if self.policy.public_repository else "",
            "private_control_remote_configured": self.config.has_private_control_remote,
            "live_checkout": live,
            "configured_origin_matches": origin == self.config.code_remote,
            "code_remote_readable": permissions["code"]["readable"],
            "code_remote_push_authorized": permissions["code"]["push_authorized"],
            "code_remote_permission_failure": permissions["code"]["failure_kind"],
            "control_remote_readable": permissions["control"]["readable"],
            "control_remote_push_authorized": permissions["control"]["push_authorized"],
            "control_remote_permission_failure": permissions["control"]["failure_kind"],
            "remote_permission_preflight_non_mutating": all(
                result["non_mutating"] for result in permissions.values()
            ),
            "operator_python": self.python_runtime.to_dict(),
            "worker_ready": worker,
            "council_contract_sha256": council_hashes,
            "required_hermes_profiles": list(COUNCIL_REQUIRED_PROFILES),
            "hermes_profile_file_sha256": profile_hashes,
        }

    def remote_permission_preflight(self) -> dict[str, dict[str, Any]]:
        """Check both remote namespaces without creating or updating a ref."""

        try:
            return probe_operator_remotes(
                self.config.repository_path,
                self.config.code_remote,
                self.config.control_remote,
                project_slug=self.policy.project_slug,
            )
        except RemotePreflightError as exc:
            raise OperatorError(str(exc)) from exc

    @staticmethod
    def _require_remote_write_permissions(permissions: Mapping[str, Mapping[str, Any]]) -> None:
        for label in ("code", "control"):
            result = permissions.get(label)
            if not isinstance(result, Mapping):
                raise OperatorError(f"{label} remote permission preflight is missing")
            if result.get("non_mutating") is not True:
                raise OperatorError(f"{label} remote permission preflight was not non-mutating")
            failure = str(result.get("failure_kind") or "permission_not_established")
            if result.get("readable") is not True:
                raise OperatorError(f"{label} remote read permission preflight failed ({failure})")
            if result.get("push_authorized") is not True:
                raise OperatorError(f"{label} remote push permission preflight failed ({failure})")

    def submit(self, request_path: Path) -> dict[str, Any]:
        request = load_request_file(request_path)
        return self.state.submit(
            request,
            self.config.worker_id,
            has_private_control_remote=self.config.has_private_control_remote,
        )

    def status(self, task_id: str) -> dict[str, Any]:
        record = self.store.read(task_id)
        record = json.loads(json.dumps(record))
        record["lease_stale"] = self.state.lease_is_stale(record)
        record["public_repository_warning"] = (
            "Every task branch is publicly readable" if self.policy.public_repository else ""
        )
        return record

    def _backend(self, name: str, *, fake_scenario: str = "valid") -> ExecutionBackend:
        if name == "fake":
            return DeterministicFakeBackend(self.policy, fake_scenario)
        if self.config.council_root is None:
            raise OperatorError(f"{name} backend requires a configured Council root")
        adapter = CouncilCompatibilityAdapter(
            self.config.council_root,
            self.policy,
            hermes_executable=self.config.hermes_executable,
        )
        if name == "hermes":
            return HermesBackend(adapter, self.policy)
        if name == "command":
            return ConfigurableCommandBackend(
                adapter,
                self.policy,
                self.config.command_profiles,
                self.config.command_executable_sha256,
            )
        raise OperatorError("unknown execution backend")

    def claim(self, task_id: str, *, human_override: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.state.claim(task_id, self.config.worker_id, human_override=human_override)

    def heartbeat(self, task_id: str) -> dict[str, Any]:
        return self.state.heartbeat(task_id, self.config.worker_id)

    def release(self, task_id: str) -> dict[str, Any]:
        return self.state.release(task_id, self.config.worker_id)

    def expire(self, task_id: str, actor: str) -> dict[str, Any]:
        return self.state.expire(task_id, actor)

    def create_stale_takeover_approval(self, task_id: str, approved_by: str, output: Path | None = None) -> dict[str, Any]:
        record = self.store.read(task_id)
        if record["state"] != "expired":
            raise OperatorError("stale-lock approval requires an explicitly expired task")
        artifact = approval_artifact("stale_lock_takeover", record, approved_by=approved_by)
        if output:
            if output.exists():
                raise OperatorError("approval output already exists")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(canonical_json(artifact))
        return artifact

    def _record_orphaned_attempts(self, task_id: str, record: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        task_root = self.sandboxes.task_root(task_id)
        attempts_root = task_root / "attempts"
        observed: list[int] = []
        if attempts_root.is_dir() and not attempts_root.is_symlink():
            for path in attempts_root.iterdir():
                match = re.fullmatch(r"attempt-(\d{2})", path.name)
                if match and path.is_dir() and not path.is_symlink():
                    observed.append(int(match.group(1)))
                else:
                    raise OperatorError("sandbox contains an unsafe attempt entry")
        recorded = {str(item.get("attempt_id")) for item in record["evidence"]["attempts"]}
        recorded_indexes = [
            int(match.group(1))
            for item in recorded
            if (match := re.fullmatch(r"attempt-(\d{2})", item))
        ]
        for index in sorted(observed):
            attempt_id = f"attempt-{index:02d}"
            if attempt_id in recorded:
                continue
            record = self.state.attach_attempt(
                task_id,
                self.config.worker_id,
                {
                    "attempt_id": attempt_id,
                    "passed": False,
                    "correction_kind": "interrupted",
                    "correction_detail": "worker stopped before publishing bounded attempt evidence",
                    "route": {},
                    "changed_paths": [],
                    "patch_hash": "",
                    "test_evidence_hash": "",
                    "reviewer_report_hash": "",
                    "reviewer_verdict": "BLOCKED",
                    "evidence": {"raw_role_transcripts_retained": False, "interrupted_attempt_retained": True},
                },
                failed=True,
            )
        all_indexes = observed + recorded_indexes
        return dict(record), (max(all_indexes) + 1 if all_indexes else 0)

    def _transition_chain(self, task_id: str, targets: tuple[str, ...]) -> dict[str, Any]:
        record: dict[str, Any] = {}
        for target in targets:
            record = self.state.transition(task_id, self.config.worker_id, target, event=f"enter_{target}")
        return record

    def _fail(self, task_id: str, reason: str) -> dict[str, Any]:
        def mutation(record: dict[str, Any]) -> None:
            record["failure"] = reason[:512]
        return self.state.transition(
            task_id,
            self.config.worker_id,
            "failed",
            event="bounded_stop",
            detail=reason,
            mutation=mutation,
        )

    def _run_required_tests(self, request: TaskRequest, result: Any, started: float) -> Any:
        """Run every immutable request command after the external tester gate."""

        try:
            self.python_runtime.verify_pinned_environment()
        except RuntimeIdentityError as exc:
            raise OperatorError(str(exc)) from exc
        records: list[dict[str, Any]] = []
        passed = True
        for command in request.required_test_commands:
            try:
                effective = effective_test_command(command, self.python_runtime)
            except RuntimeIdentityError as exc:
                raise OperatorError(str(exc)) from exc
            remaining = int(request.max_duration_seconds - (time.monotonic() - started))
            missing_targets = missing_compileall_targets(command, result.workspace)
            if missing_targets:
                record = {
                    "arguments": list(command),
                    "requested_arguments": list(command),
                    "effective_arguments": list(effective),
                    "exit_code": 2,
                    "timed_out": False,
                    "truncated": False,
                    "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                    "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                    "preflight_error": "compileall target contract is missing or unsafe",
                }
            elif remaining < 1:
                record = {
                    "arguments": list(command),
                    "requested_arguments": list(command),
                    "effective_arguments": list(effective),
                    "exit_code": 124,
                    "timed_out": True,
                    "truncated": False,
                    "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                    "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                }
            else:
                execution = bounded_process(
                    effective,
                    cwd=result.workspace,
                    timeout_seconds=remaining,
                    max_output_bytes=self.policy.max_log_bytes,
                    environment=safe_git_environment(roles=True),
                )
                record = {
                    key: execution[key]
                    for key in (
                        "exit_code", "timed_out", "truncated",
                        "stdout_sha256", "stderr_sha256",
                    )
                }
                record.update(
                    {
                        "arguments": list(command),
                        "requested_arguments": list(command),
                        "effective_arguments": list(effective),
                    }
                )
            records.append(record)
            if record["exit_code"] != 0 or record["timed_out"]:
                passed = False
                break
        trusted_hash = digest_json(
            {
                "commands": records,
                "trusted_test_python": self.python_runtime.to_dict(),
                "credential_environment_forwarded": False,
            }
        )
        evidence = dict(result.evidence)
        evidence["trusted_required_tests"] = records
        evidence["trusted_test_python"] = self.python_runtime.to_dict()
        evidence["trusted_required_tests_sha256"] = trusted_hash
        combined_hash = digest_json(
            {
                "council_tester_gate_sha256": result.test_evidence_hash,
                "trusted_required_tests_sha256": trusted_hash,
            }
        )
        if passed:
            return replace(result, test_evidence_hash=combined_hash, evidence=evidence)
        return replace(
            result,
            passed=False,
            correction_kind="tester_gate",
            correction_detail="an immutable trusted-host test command failed",
            test_evidence_hash=combined_hash,
            reviewer_verdict="BLOCKED",
            evidence=evidence,
        )

    @contextlib.contextmanager
    def _maintain_lease(self, task_id: str):
        """Keep a long role/test invocation leased without exposing role credentials."""

        self.state.heartbeat(task_id, self.config.worker_id)
        stop = threading.Event()
        failures: list[Exception] = []
        interval = max(1.0, min(60.0, self.policy.lease_seconds / 3))

        def pulse() -> None:
            while not stop.wait(interval):
                try:
                    self.state.heartbeat(task_id, self.config.worker_id)
                except Exception as exc:  # surfaced on the trusted orchestration thread
                    failures.append(exc)
                    return

        thread = threading.Thread(target=pulse, name=f"wpi-council-heartbeat-{task_id}", daemon=True)
        thread.start()
        active_error = False
        try:
            yield
        except BaseException:
            active_error = True
            raise
        finally:
            stop.set()
            thread.join(timeout=5)
            if not active_error:
                if failures:
                    raise OperatorError("remote lease heartbeat failed during an invocation") from failures[0]
                self.state.heartbeat(task_id, self.config.worker_id)

    def run(
        self,
        task_id: str,
        *,
        backend_name: str | None = None,
        overnight: bool = False,
        fake_scenario: str = "valid",
        push_task_branch: bool = True,
    ) -> dict[str, Any]:
        record = self.store.read(task_id)
        validate_state_record(record)
        if record["state"] not in {"claimed", "correction_required"} or record["worker_id"] != self.config.worker_id:
            raise OperatorError("task must be owned by this worker at an attempt boundary before run")
        request = TaskRequest.from_dict(record["request"])
        selected_backend = backend_name or request.requested_backend
        if selected_backend != request.requested_backend:
            raise OperatorError("runtime backend does not match the immutable request")
        if overnight:
            supervised_instance = os.environ.get("WPI_COUNCIL_SUPERVISED_INSTANCE", "")
            if not re.fullmatch(r"[0-9a-f]{32}", supervised_instance):
                raise OperatorError("overnight execution requires the safe worker supervisor")
            if sys.platform == "darwin" and os.environ.get("WPI_COUNCIL_SLEEP_ASSERTION") != "caffeinate":
                raise OperatorError("macOS overnight execution requires the supervisor caffeinate assertion")
        try:
            self.python_runtime.verify_pinned_environment()
        except RuntimeIdentityError as exc:
            raise OperatorError(str(exc)) from exc
        permissions = self.remote_permission_preflight()
        self._require_remote_write_permissions(permissions)
        backend = self._backend(selected_backend, fake_scenario=fake_scenario)
        workspace, journal = self.sandboxes.create_trusted_workspace(request)
        record, start_index = self._record_orphaned_attempts(task_id, record)
        if start_index > request.max_correction_rounds:
            return self._fail(task_id, "interrupted attempts exhausted the correction limit")
        live_before = self.sandboxes.verify_live_checkout()
        started = time.monotonic()
        previous_failure: str | None = None
        correction: dict[str, Any] | None = None
        result = None

        if record["state"] == "correction_required" and record["evidence"]["attempts"]:
            previous = record["evidence"]["attempts"][-1]
            correction = {
                "version": 1,
                "round": start_index,
                "kind": previous.get("correction_kind") or "interrupted",
                "detail_hash": hashlib.sha256(str(previous.get("correction_detail", "")).encode()).hexdigest(),
                "patch_hash": str(previous.get("patch_hash", "")),
                "test_evidence_hash": str(previous.get("test_evidence_hash", "")),
                "reviewer_report_hash": str(previous.get("reviewer_report_hash", "")),
            }
            previous_failure = digest_json(
                {"kind": previous.get("correction_kind"), "detail": previous.get("correction_detail", "")}
            )

        for round_index in range(start_index, request.max_correction_rounds + 1):
            if time.monotonic() - started > request.max_duration_seconds:
                return self._fail(task_id, "maximum task duration reached")
            current = self.store.read(task_id)
            if current["state"] not in {"claimed", "correction_required"}:
                raise OperatorError("task state is not resumable at an attempt boundary")
            self._transition_chain(task_id, ("routing", "planning", "coding"))
            attempt_id = f"attempt-{round_index:02d}"
            attempt_root = self.sandboxes.create_attempt_root(task_id, attempt_id)
            try:
                with self._maintain_lease(task_id):
                    result = backend.execute_attempt(
                        request,
                        workspace,
                        attempt_id,
                        attempt_root,
                        correction=correction,
                    )
                    if result.passed:
                        result = self._run_required_tests(request, result, started)
            except (BackendError, SandboxError, ContractError) as exc:
                detail = f"{type(exc).__name__}: execution backend stopped safely"
                self._transition_chain(task_id, ("coder_gate",))
                self.state.attach_attempt(
                    task_id,
                    self.config.worker_id,
                    {
                        "attempt_id": attempt_id,
                        "passed": False,
                        "correction_kind": "backend_blocked",
                        "correction_detail": detail,
                        "route": {},
                        "changed_paths": [],
                        "patch_hash": "",
                        "test_evidence_hash": "",
                        "reviewer_report_hash": "",
                        "reviewer_verdict": "BLOCKED",
                        "evidence": {"raw_role_transcripts_retained": False},
                    },
                    failed=True,
                )
                return self._fail(task_id, detail)

            self.state.transition(task_id, self.config.worker_id, "coder_gate", event="coder_gate_recorded")
            failure_stage = result.correction_kind
            if failure_stage != "coder_gate" and failure_stage not in {"planner_blocked", "malformed_role_output", "backend_blocked"}:
                self._transition_chain(task_id, ("testing", "tester_gate"))
                if failure_stage != "tester_gate":
                    self.state.transition(task_id, self.config.worker_id, "reviewing", event="review_recorded")
            self.state.attach_attempt(task_id, self.config.worker_id, result.public_record(), failed=not result.passed)
            journal["attempts"].append(
                {
                    "attempt_id": attempt_id,
                    "status": "passed" if result.passed else "failed",
                    "evidence_hash": digest_json(result.public_record()),
                }
            )
            self.sandboxes.write_journal(self.sandboxes.task_root(task_id), journal)
            if result.passed:
                if self.store.read(task_id)["state"] != "reviewing":
                    raise OperatorError("passing attempt did not reach Reviewer state")
                self.state.transition(
                    task_id,
                    self.config.worker_id,
                    "automatic_gates_passed",
                    event="automatic_gates_passed",
                )
                break

            failure_identity = digest_json(
                {"kind": result.correction_kind, "detail": result.correction_detail}
            )
            current_state = self.store.read(task_id)["state"]
            if current_state not in {"coder_gate", "tester_gate", "reviewing"}:
                return self._fail(task_id, "malformed attempt state")
            if previous_failure == failure_identity:
                return self._fail(task_id, "the same automatic failure repeated")
            if round_index >= request.max_correction_rounds:
                return self._fail(task_id, "maximum correction rounds reached")
            previous_failure = failure_identity
            correction = {
                "version": 1,
                "round": round_index + 1,
                "kind": result.correction_kind,
                "detail_hash": hashlib.sha256(result.correction_detail.encode()).hexdigest(),
                "patch_hash": result.patch_hash,
                "test_evidence_hash": result.test_evidence_hash,
                "reviewer_report_hash": result.reviewer_report_hash,
            }
            def increment(updated: dict[str, Any]) -> None:
                updated["correction_round"] = round_index + 1
            self.state.transition(
                task_id,
                self.config.worker_id,
                "correction_required",
                event="structured_correction",
                detail=str(result.correction_kind),
                mutation=increment,
            )
        else:
            return self._fail(task_id, "bounded correction loop exhausted")

        if result is None or not result.passed:
            return self._fail(task_id, "no passing attempt exists")
        if not push_task_branch:
            return self.store.read(task_id)
        summary = self._commit_and_push(request, result.workspace, result, selected_backend)
        self.state.set_summary(task_id, self.config.worker_id, summary)

        def bind_push(updated: dict[str, Any]) -> None:
            updated["task_branch"] = summary["task_branch"]
            updated["final_task_commit"] = summary["final_task_commit"]

        self.state.transition(
            task_id,
            self.config.worker_id,
            "task_branch_pushed",
            event="trusted_operator_push",
            mutation=bind_push,
        )
        final = self.state.transition(
            task_id,
            self.config.worker_id,
            "awaiting_human_review",
            event="await_human_review",
        )
        if self.sandboxes.verify_live_checkout() != live_before:
            raise OperatorError("live checkout changed during task execution")
        return final

    def resume(self, task_id: str, *, fake_scenario: str = "valid") -> dict[str, Any]:
        record = self.store.read(task_id)
        if record["state"] not in {"claimed", "correction_required"}:
            raise OperatorError("resume requires an owned attempt boundary; expire and explicitly reclaim a stale active task")
        return self.run(task_id, fake_scenario=fake_scenario)

    def _run_required_release_scan(self, request: TaskRequest, workspace: Path) -> dict[str, Any]:
        relative = self.application_subtree / "scripts" / "release_scan.py"
        relative_text = relative.as_posix()
        scan_script = workspace / relative
        try:
            expected = workspace.resolve() / relative
            resolved = scan_script.resolve(strict=True)
            info = scan_script.lstat()
        except OSError as exc:
            raise OperatorError("required public-release scanner is missing or unsafe") from exc
        if resolved != expected or scan_script.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise OperatorError("required public-release scanner is missing or unsafe")
        base_entry = run_git(
            workspace,
            "cat-file",
            "-e",
            f"{request.base_commit}:{relative_text}",
            check=False,
        )
        if base_entry.returncode:
            raise OperatorError("required public-release scanner is not tracked at the request base")
        scan = bounded_process(
            [self.python_runtime.executable, str(scan_script), "--working-tree"],
            cwd=workspace,
            timeout_seconds=120,
            max_output_bytes=self.policy.max_log_bytes,
            environment=safe_git_environment(),
        )
        if scan["exit_code"] != 0 or scan["timed_out"]:
            raise OperatorError("public-release secret/data scan rejected the task branch")
        return {
            key: scan[key]
            for key in (
                "arguments", "exit_code", "timed_out", "truncated",
                "stdout_sha256", "stderr_sha256",
            )
        }

    def _commit_and_push(self, request: TaskRequest, workspace: Path, result: Any, backend_name: str) -> dict[str, Any]:
        branch = f"{self.policy.task_branch_prefix}{request.task_id}"
        if branch in self.policy.forbidden_branches or branch.startswith(("main", "master")):
            raise OperatorError("derived task branch is forbidden")
        manifest = {
            "schema_version": "wpi-council-task-result/v1",
            "task_id": request.task_id,
            "request_hash": request.canonical_request_hash,
            "base_commit": request.base_commit,
            "task_branch": branch,
            "backend": backend_name,
            "synthetic_test_only": backend_name == "fake",
            "route": dict(result.route),
            "changed_paths": list(result.changed_paths),
            "patch_hash": result.patch_hash,
            "test_evidence_hash": result.test_evidence_hash,
            "reviewer_report_hash": result.reviewer_report_hash,
            "reviewer_verdict": result.reviewer_verdict,
            "automatic_gates_passed": True,
            "human_review_required": True,
            "created_at": utc_now(),
        }
        scan_public_text(manifest, allow_research_data=False)
        result_path = workspace / "council-results" / f"{request.task_id}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_bytes(canonical_json(manifest))
        run_git(workspace, "add", "--all")
        names_raw = run_git(
            workspace,
            "diff", "--cached", "--name-only", "-z", "--no-renames", request.base_commit, "--",
        ).stdout
        try:
            changed = tuple(sorted(item for item in names_raw.decode("utf-8").split("\0") if item))
        except UnicodeError as exc:
            raise OperatorError("final workspace contains a non-UTF-8 path") from exc
        allowed = (*request.allowed_paths, "council-results/**")
        for path in changed:
            validate_relative_path(path, allow_glob=False)
            if (workspace / path).is_symlink():
                raise OperatorError("final workspace contains a prohibited symbolic link")
            if not any(
                path == pattern
                or fnmatch.fnmatchcase(path, pattern)
                or (pattern.endswith("/**") and path.startswith(pattern[:-2]))
                for pattern in allowed
            ):
                raise OperatorError("final workspace contains a path outside the immutable request")
        if len(changed) > request.max_changed_files + 1:
            raise OperatorError("final workspace exceeds the changed-file limit")
        binary_numstat = run_git(
            workspace, "diff", "--cached", "--numstat", "-z", "--no-renames", request.base_commit, "--",
        ).stdout.split(b"\0")
        if any(line.startswith(b"-\t-\t") for line in binary_numstat if line):
            raise OperatorError("final workspace contains an unapproved binary change")
        diff = run_git(workspace, "diff", "--cached", "--binary", "--no-renames", request.base_commit, "--").stdout
        if len(diff) > request.max_patch_bytes + len(canonical_json(manifest)):
            raise OperatorError("final workspace exceeds the patch-size limit")
        self._run_required_release_scan(request, workspace)
        run_git(workspace, "diff", "--cached", "--check")
        run_git(workspace, "commit", "-m", f"Council task {request.task_id}: {request.title[:60]}")
        final_commit = run_git(workspace, "rev-parse", "HEAD").stdout.decode().strip()
        if run_git(workspace, "rev-list", "--count", f"{request.base_commit}..HEAD").stdout.decode().strip() != "1":
            raise OperatorError("task branch must contain exactly one trusted operator commit")
        existing = subprocess.run(
            ["git", "ls-remote", "--heads", self.config.code_remote, f"refs/heads/{branch}"],
            capture_output=True,
            shell=False,
            timeout=60,
            env=safe_git_environment(),
        )
        if existing.returncode or existing.stdout.strip():
            raise OperatorError("task branch collision failed closed")
        pushed = subprocess.run(
            ["git", "-C", str(workspace), "push", "--porcelain", self.config.code_remote, f"HEAD:refs/heads/{branch}"],
            capture_output=True,
            shell=False,
            timeout=120,
            env=safe_git_environment(),
        )
        if pushed.returncode:
            raise OperatorError("normal task-branch push failed")
        remote = subprocess.run(
            ["git", "ls-remote", "--heads", self.config.code_remote, f"refs/heads/{branch}"],
            capture_output=True,
            shell=False,
            timeout=60,
            env=safe_git_environment(),
        )
        if remote.returncode or remote.stdout.decode().split("\t", 1)[0] != final_commit:
            raise OperatorError("remote task branch does not match the trusted commit")
        return {
            "worker_id": self.config.worker_id,
            "task_branch": branch,
            "final_task_commit": final_commit,
            "route": result.route.get("selected_level", "unknown"),
            "role_profiles": result.evidence.get("invocation_evidence_sha256", {}),
            "patch_hash": result.patch_hash,
            "test_evidence_hash": result.test_evidence_hash,
            "reviewer_report_hash": result.reviewer_report_hash,
            "reviewer_verdict": result.reviewer_verdict,
            "tests": [list(command) for command in request.required_test_commands],
            "next_action": "human review of the task branch; no automatic merge or deployment",
            "public_repository_warning": "Every task branch is publicly readable" if self.policy.public_repository else "",
        }

    def approve(self, task_id: str, kind: str, approved_by: str, *, bindings: Mapping[str, str] | None = None) -> dict[str, Any]:
        return self.state.approve(task_id, kind, approved_by, extra_bindings=bindings)

    def reject(self, task_id: str, actor: str, reason: str) -> dict[str, Any]:
        return self.state.reject(task_id, actor, reason)

    def cancel(self, task_id: str, actor: str) -> dict[str, Any]:
        return self.state.cancel(task_id, actor)

    def review_manifest(self, task_id: str, output: Path | None = None) -> dict[str, Any]:
        record = self.store.read(task_id)
        manifest = {
            "task_id": task_id,
            "request_hash": record["request_hash"],
            "base_commit": record["base_commit"],
            "state": record["state"],
            "task_branch": record["task_branch"],
            "final_task_commit": record["final_task_commit"],
            "evidence": record["evidence"],
            "approvals": record["approvals"],
            "raw_role_transcripts_included": False,
        }
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(canonical_json(manifest))
        return manifest

    def sync_handoff(self, task_id: str, *, expected_before_hash: str | None = None) -> dict[str, str]:
        record = self.store.read(task_id)
        return append_handoff(
            record,
            vault=self.config.obsidian_vault,
            note_relative=self.policy.obsidian_handoff_note,
            expected_before_hash=expected_before_hash,
        )
