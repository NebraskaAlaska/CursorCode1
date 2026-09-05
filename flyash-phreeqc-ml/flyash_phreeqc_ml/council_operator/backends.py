"""Execution backends and the thin compatibility layer over AI Council controls."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any, Mapping, Protocol

from .contracts import (
    ContractError,
    OPERATOR_VERSION,
    ProjectPolicy,
    TaskRequest,
    canonical_json,
    command_text,
    digest_json,
    render_request_markdown,
    scan_public_text,
    utc_now,
    validate_relative_path,
)
from .sandbox import (
    SandboxError,
    SandboxManager,
    bounded_process,
    require_contained_real_path,
    reject_repository_symlinks,
    run_git,
    safe_git_environment,
    tree_hash,
)


class BackendError(RuntimeError):
    """A role backend or Council compatibility operation failed safely."""


@dataclass(frozen=True)
class AttemptResult:
    attempt_id: str
    passed: bool
    correction_kind: str | None
    correction_detail: str
    workspace: Path
    route: Mapping[str, Any]
    changed_paths: tuple[str, ...]
    patch_hash: str
    test_evidence_hash: str
    reviewer_report_hash: str
    reviewer_verdict: str
    evidence: Mapping[str, Any]

    def public_record(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "passed": self.passed,
            "correction_kind": self.correction_kind,
            "correction_detail": self.correction_detail[:512],
            "route": dict(self.route),
            "changed_paths": list(self.changed_paths),
            "patch_hash": self.patch_hash,
            "test_evidence_hash": self.test_evidence_hash,
            "reviewer_report_hash": self.reviewer_report_hash,
            "reviewer_verdict": self.reviewer_verdict,
            "evidence": dict(self.evidence),
        }


class ExecutionBackend(Protocol):
    name: str

    def execute_attempt(
        self,
        request: TaskRequest,
        trusted_workspace: Path,
        attempt_id: str,
        attempt_root: Path,
        *,
        correction: Mapping[str, Any] | None = None,
    ) -> AttemptResult: ...


COUNCIL_FILES = {
    "AGENTS.md",
    "START_HERE.md",
    "prompts/START_PLANNER.md",
    "prompts/START_CODER.md",
    "prompts/START_TESTER.md",
    "prompts/START_REVIEWER.md",
    "tools/council_route.py",
    "tools/council_stage_launcher.py",
    "tools/council_patch_gate.py",
    "tools/coder_export_bundle.py",
}

COUNCIL_PROFILE_LEVELS = ("routine", "standard", "complex", "critical")
COUNCIL_REQUIRED_PROFILES = tuple(
    [f"council-planner-{level}" for level in COUNCIL_PROFILE_LEVELS]
    + [f"council-coder-{level}" for level in COUNCIL_PROFILE_LEVELS]
    + ["council-tester", "council-reviewer"]
)


def estimate_changed_files(request: TaskRequest) -> int:
    """Estimate routing size without conflating prediction and hard cap."""

    if any(any(character in path for character in "*?[") for path in request.allowed_paths):
        return request.max_changed_files
    return min(request.max_changed_files, max(1, len(set(request.allowed_paths))))


def file_sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise BackendError("Council contract file is missing or unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _real_directory(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise BackendError(f"missing {label}") from exc
    if not stat.S_ISDIR(mode):
        raise BackendError(f"{label} must be a real directory")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise BackendError(f"{label} is missing, unsafe, or too large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackendError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise BackendError(f"{label} must be a JSON object")
    return value


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise BackendError("artifact directory is unsafe")
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CouncilCompatibilityAdapter:
    """Invoke, never reimplement, the audited external Council controls."""

    def __init__(self, council_root: Path, policy: ProjectPolicy, *, hermes_executable: Path | None = None):
        self.root = Path(council_root)
        self.policy = policy
        self.hermes_executable = hermes_executable

    def verify(self) -> dict[str, str]:
        _real_directory(self.root, "Council root")
        # The exported Obsidian snapshot is reference-only, never a runtime.
        if (self.root.parent / ".obsidian").exists():
            raise BackendError("the Obsidian AI Council reference snapshot cannot be used as a live runtime")
        observed: dict[str, str] = {}
        if set(self.policy.council_required_sha256) != COUNCIL_FILES:
            raise BackendError("tracked policy does not bind the complete Council contract")
        for relative, expected in self.policy.council_required_sha256.items():
            path = self.root / relative
            observed[relative] = file_sha256(path)
            if observed[relative] != expected:
                raise BackendError("configured Council contract hash mismatch")
        for directory in (self.root / "runs", self.root / "sandboxes", self.root / ".role-outbox"):
            _real_directory(directory, directory.name)
        for stage in ("planner", "coder", "tester", "reviewer"):
            _real_directory(self.root / ".role-outbox" / stage, f"{stage} outbox")
        return observed

    def verify_worker_profiles(self, profiles_root: Path | None = None) -> dict[str, dict[str, str]]:
        """Verify the complete launcher-selected profile set without reading secrets."""

        root = profiles_root or (Path.home() / ".hermes" / "profiles")
        _real_directory(root, "Hermes profiles root")
        observed: dict[str, dict[str, str]] = {}
        for profile in COUNCIL_REQUIRED_PROFILES:
            profile_root = root / profile
            _real_directory(profile_root, f"Hermes profile {profile}")
            files: dict[str, str] = {}
            for name in ("config.yaml", "SOUL.md", "state.db"):
                path = profile_root / name
                if path.is_symlink() or not path.is_file():
                    raise BackendError(f"Hermes profile {profile} is incomplete or unsafe")
                files[name] = file_sha256(path)
            observed[profile] = files
        return observed

    def _host_environment(self) -> dict[str, str]:
        allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        environment["GIT_TERMINAL_PROMPT"] = "0"
        if self.hermes_executable:
            environment["PATH"] = f"{self.hermes_executable.parent}{os.pathsep}{environment.get('PATH', os.defpath)}"
        return environment

    def render_council_request(
        self,
        request: TaskRequest,
        run_id: str,
        sandbox: Path,
        correction: Mapping[str, Any] | None,
    ) -> str:
        correction_text = "None"
        if correction:
            correction_text = json.dumps(correction, sort_keys=True, separators=(",", ":"))
        tests = [command_text(command) for command in request.required_test_commands]
        return "\n".join(
            [
                "# Council Request",
                "",
                "> Public/sanitized task metadata only. Do not include credentials or research data.",
                "",
                "## Run identity",
                "",
                f"- **Run ID:** `{run_id}`",
                f"- **Project name:** `WPI Virtual LAB`",
                f"- **Isolated sandbox path:** `{sandbox}`",
                "- **Sandbox root contract:** This path is the Git repository root.",
                f"- **Application subtree:** `{self.policy.application_subtree}/` relative to that Git root.",
                f"- **Starting branch:** `{request.base_branch}`",
                f"- **Starting commit:** `{request.base_commit}`",
                "",
                "## Change request",
                "",
                f"- **Requested change:** {request.goal}",
                f"- **Reason for the change:** {request.background}",
                f"- **Current behaviour:** See exact request hash `{request.canonical_request_hash}`.",
                f"- **Desired behaviour:** {request.title}",
                "",
                "## Boundaries",
                "",
                f"- **Constraints:** Git-root-relative allowed paths: {', '.join(request.allowed_paths)}. Correction binding: {correction_text}",
                f"- **Files that must not change:** Git-root-relative forbidden paths: {', '.join(request.forbidden_paths) or 'all paths outside the allowlist'}",
                "- **Patch root:** Every role patch is interpreted against the Git-root workspace.",
                "- **Role write boundary:** Coder and Tester changes must both remain inside the immutable request allowed paths.",
                f"- **Trusted result ownership:** `council-results/{request.task_id}.json` is generated by the trusted host only after automatic gates; Planner, Coder, Tester, and Reviewer must not write `council-results/**`.",
                "- **Public interfaces that must remain compatible:** Existing scientific and operator contracts.",
                "- **Out-of-scope work:** merge, deploy, main modification, resource promotion, real research data.",
                "",
                "## Risk and edge cases",
                "",
                f"- **Known edge cases:** {'; '.join(request.acceptance_criteria)}",
                f"- **Known risks:** scientific={request.scientific_risk}; privacy={request.security_privacy}",
                "",
                "## Existing commands",
                "",
                "- **Install:** N/A",
                "- **Trusted test cwd:** Git repository root.",
                *[f"- **Immutable test {index}:** `{test}`" for index, test in enumerate(tests, start=1)],
                "- **Lint:** `git diff --check`",
                "- **Type-check:** N/A",
                "- **Build:** N/A",
                "- **Run:** N/A",
                "",
                "## Completion",
                "",
                f"- **Measurable completion conditions:** {'; '.join(request.acceptance_criteria)}",
                "",
            ]
        )

    def prepare_attempt(
        self,
        request: TaskRequest,
        trusted_workspace: Path,
        run_id: str,
        correction: Mapping[str, Any] | None,
    ) -> tuple[Path, Path]:
        self.verify()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise BackendError("unsafe Council run ID")
        run_root = self.root / "runs" / run_id
        sandbox = self.root / "sandboxes" / run_id
        if run_root.exists() or run_root.is_symlink() or sandbox.exists() or sandbox.is_symlink():
            raise BackendError("Council attempt IDs are immutable and may not be reused")
        for role in ("planner", "coder", "tester", "reviewer"):
            if any((self.root / ".role-outbox" / role).iterdir()):
                raise BackendError("Council role outbox is not empty")
        shutil.copytree(trusted_workspace, sandbox, symlinks=True)
        try:
            if sandbox.is_symlink() or not (sandbox / ".git").exists():
                raise BackendError("Council sandbox copy is unsafe")
            reject_repository_symlinks(sandbox)
            for remote in run_git(sandbox, "remote").stdout.decode().split():
                run_git(sandbox, "remote", "remove", remote)
            if run_git(sandbox, "rev-parse", "HEAD").stdout.decode().strip() != request.base_commit:
                raise BackendError("Council sandbox does not start at the exact base")
            if run_git(sandbox, "status", "--porcelain=v1", "--untracked-files=all").stdout:
                raise BackendError("Council sandbox does not start clean")
            run_root.mkdir(mode=0o700)
            _write_atomic(
                run_root / "00_REQUEST.md",
                self.render_council_request(request, run_id, sandbox, correction).encode("utf-8"),
            )
            return run_root, sandbox
        except Exception:
            # Preserve partial runtime evidence; never silently repair or reuse it.
            raise

    def _tool(self, relative: str, *arguments: str, timeout: int) -> dict[str, Any]:
        result = bounded_process(
            [sys.executable, str(self.root / relative), *arguments],
            cwd=self.root,
            timeout_seconds=timeout,
            max_output_bytes=self.policy.max_log_bytes,
            environment=self._host_environment(),
        )
        return result

    def route(self, run_id: str, estimated_files: int) -> dict[str, Any]:
        result = self._tool(
            "tools/council_route.py",
            "--run", run_id,
            "--requested-mode", "AUTO",
            "--estimated-files", str(estimated_files),
            timeout=60,
        )
        if result["exit_code"] != 0:
            raise BackendError("authoritative Council router blocked the task")
        return _load_json(self.root / "runs" / run_id / "05_route" / "ROUTE.json", "Council route")

    def apply_recommendation(self, run_id: str) -> dict[str, Any]:
        result = self._tool("tools/council_route.py", "--run", run_id, "--apply-recommendation", timeout=60)
        if result["exit_code"] != 0:
            raise BackendError("Council route recommendation could not be applied")
        return _load_json(self.root / "runs" / run_id / "05_route" / "ROUTE.json", "Council route")

    def launch(self, run_id: str, stage: str, timeout: int) -> dict[str, Any]:
        result = self._tool(
            "tools/council_stage_launcher.py", "--run", run_id, "--stage", stage,
            timeout=timeout,
        )
        if result["exit_code"] != 0:
            raise BackendError(f"Council {stage} stage was blocked")
        return result

    def gate(self, run_id: str, mode: str, timeout: int) -> dict[str, Any]:
        self._tool("tools/council_patch_gate.py", mode, "--run", run_id, timeout=timeout)
        name = "CODER_GATE_STATUS.json" if mode == "coder" else "TESTER_GATE_STATUS.json"
        directory = "20_code" if mode == "coder" else "30_tests"
        return _load_json(self.root / "runs" / run_id / directory / name, f"Council {mode} gate")

    def ingest_external_stage(self, run_id: str, stage: str) -> None:
        if stage not in {"planner", "reviewer"}:
            raise BackendError("only Planner and Reviewer use launcher ingestion")
        tools_dir = self.root / "tools"
        old_path = list(sys.path)
        try:
            sys.path.insert(0, str(tools_dir))
            route_spec = importlib.util.spec_from_file_location("council_route", tools_dir / "council_route.py")
            if route_spec is None or route_spec.loader is None:
                raise BackendError("cannot load the verified Council router")
            route_module = importlib.util.module_from_spec(route_spec)
            sys.modules["council_route"] = route_module
            route_spec.loader.exec_module(route_module)
            launcher_spec = importlib.util.spec_from_file_location("council_stage_launcher", tools_dir / "council_stage_launcher.py")
            if launcher_spec is None or launcher_spec.loader is None:
                raise BackendError("cannot load the verified Council launcher")
            launcher = importlib.util.module_from_spec(launcher_spec)
            launcher_spec.loader.exec_module(launcher)
            route, _route_path, run_root = route_module.load_route(self.root, run_id)
            launcher._ingest_stage_output(self.root, run_root, stage, route)
        except Exception as exc:
            raise BackendError("verified Council stage ingestion rejected the command backend output") from exc
        finally:
            sys.path[:] = old_path


class _CouncilBackendBase:
    name = "council"

    def __init__(self, adapter: CouncilCompatibilityAdapter, policy: ProjectPolicy):
        self.adapter = adapter
        self.policy = policy

    def invoke_stage(self, run_id: str, stage: str, request: TaskRequest) -> None:
        raise NotImplementedError

    def execute_attempt(
        self,
        request: TaskRequest,
        trusted_workspace: Path,
        attempt_id: str,
        attempt_root: Path,
        *,
        correction: Mapping[str, Any] | None = None,
    ) -> AttemptResult:
        run_id = f"wpi-{request.task_id}-{attempt_id}"
        run_root, workspace = self.adapter.prepare_attempt(request, trusted_workspace, run_id, correction)
        estimated_files = estimate_changed_files(request)
        route = self.adapter.route(run_id, estimated_files)
        self.invoke_stage(run_id, "planner", request)
        plan = _load_json(run_root / "10_plan" / "PLAN_STATUS.json", "Planner status")
        if plan != {"status": "READY"}:
            return self._blocked_result(
                attempt_id, workspace, route, "planner_blocked",
                "Planner did not produce a READY plan", estimated_files,
            )
        route = self.adapter.apply_recommendation(run_id)
        self.invoke_stage(run_id, "coder", request)
        coder_gate = self.adapter.gate(run_id, "coder", request.max_duration_seconds)
        if coder_gate.get("status") != "APPLIED":
            return self._gate_result(
                attempt_id, workspace, route, run_root, coder_gate, "coder_gate", estimated_files
            )
        self.invoke_stage(run_id, "tester", request)
        tester_gate = self.adapter.gate(run_id, "tester", request.max_duration_seconds)
        if tester_gate.get("status") != "APPLIED_PASS":
            return self._gate_result(
                attempt_id, workspace, route, run_root, tester_gate, "tester_gate", estimated_files
            )
        self.invoke_stage(run_id, "reviewer", request)
        verdict = _load_json(run_root / "40_review" / "REVIEW_VERDICT.json", "Reviewer verdict").get("verdict", "BLOCKED")
        report_hash = file_sha256(run_root / "40_review" / "REVIEW_REPORT.md")
        changed = tuple(coder_gate.get("changed_paths", ())) + tuple(tester_gate.get("changed_paths", ()))
        evidence = self._evidence(run_root, route, coder_gate, tester_gate, verdict)
        evidence["router_estimated_files"] = estimated_files
        passed = verdict == "APPROVE"
        return AttemptResult(
            attempt_id=attempt_id,
            passed=passed,
            correction_kind=None if passed else "reviewer",
            correction_detail="" if passed else f"Reviewer verdict: {verdict}",
            workspace=workspace,
            route=route,
            changed_paths=tuple(sorted(set(changed))),
            patch_hash=str(coder_gate.get("patch_sha256", "")),
            test_evidence_hash=digest_json(tester_gate),
            reviewer_report_hash=report_hash,
            reviewer_verdict=str(verdict),
            evidence=evidence,
        )

    def _blocked_result(
        self, attempt_id: str, workspace: Path, route: Mapping[str, Any],
        kind: str, detail: str, estimated_files: int,
    ) -> AttemptResult:
        return AttemptResult(
            attempt_id=attempt_id,
            passed=False,
            correction_kind=kind,
            correction_detail=detail,
            workspace=workspace,
            route=route,
            changed_paths=(),
            patch_hash="",
            test_evidence_hash="",
            reviewer_report_hash="",
            reviewer_verdict="BLOCKED",
            evidence={
                "backend": self.name,
                "operator_version": OPERATOR_VERSION,
                "router_estimated_files": estimated_files,
                "raw_role_transcripts_retained": False,
            },
        )

    def _gate_result(
        self, attempt_id: str, workspace: Path, route: Mapping[str, Any],
        run_root: Path, gate: Mapping[str, Any], kind: str, estimated_files: int,
    ) -> AttemptResult:
        evidence = {
            "backend": self.name,
            "operator_version": OPERATOR_VERSION,
            "gate_status": gate.get("status"),
            "gate_hash": digest_json(gate),
            "router_estimated_files": estimated_files,
            "raw_role_transcripts_retained": False,
        }
        return AttemptResult(
            attempt_id=attempt_id,
            passed=False,
            correction_kind=kind,
            correction_detail=str(gate.get("reason") or gate.get("status") or "gate blocked"),
            workspace=workspace,
            route=route,
            changed_paths=tuple(gate.get("changed_paths", ())),
            patch_hash=str(gate.get("patch_sha256", "")),
            test_evidence_hash=digest_json(gate) if kind == "tester_gate" else "",
            reviewer_report_hash="",
            reviewer_verdict="BLOCKED",
            evidence=evidence,
        )

    def _evidence(self, run_root: Path, route: Mapping[str, Any], coder: Mapping[str, Any], tester: Mapping[str, Any], verdict: str) -> dict[str, Any]:
        invocations: dict[str, str] = {}
        for stage in ("planner", "coder", "tester", "reviewer"):
            path = run_root / "05_route" / "invocations" / f"{stage}.json"
            if path.is_file() and not path.is_symlink():
                invocations[stage] = file_sha256(path)
        return {
            "backend": self.name,
            "operator_version": OPERATOR_VERSION,
            "route_hash": digest_json(route),
            "coder_gate_hash": digest_json(coder),
            "tester_gate_hash": digest_json(tester),
            "reviewer_verdict": verdict,
            "invocation_evidence_sha256": invocations,
            "raw_role_transcripts_retained": False,
        }


class HermesBackend(_CouncilBackendBase):
    name = "hermes"

    def invoke_stage(self, run_id: str, stage: str, request: TaskRequest) -> None:
        self.adapter.launch(run_id, stage, request.max_duration_seconds)


class ConfigurableCommandBackend(_CouncilBackendBase):
    name = "command"

    def __init__(
        self,
        adapter: CouncilCompatibilityAdapter,
        policy: ProjectPolicy,
        profiles: Mapping[str, tuple[str, ...]],
        executable_sha256: Mapping[str, str],
    ):
        super().__init__(adapter, policy)
        self.profiles = dict(profiles)
        self.executable_sha256 = dict(executable_sha256)

    def _resolved_command(self, stage: str) -> list[str]:
        arguments = list(self.profiles.get(stage, ()))
        if not arguments:
            raise BackendError(f"command backend has no {stage} profile")
        executable = Path(arguments[0])
        if not executable.is_absolute():
            found = shutil.which(arguments[0])
            if not found:
                raise BackendError("configured command executable is unavailable")
            executable = Path(found)
        executable = executable.resolve(strict=True)
        if executable.is_symlink() or not executable.is_file():
            raise BackendError("configured command executable is unsafe")
        expected = self.executable_sha256.get(stage)
        if not expected or file_sha256(executable) != expected:
            raise BackendError("configured command executable identity does not match")
        arguments[0] = str(executable)
        return arguments

    def invoke_stage(self, run_id: str, stage: str, request: TaskRequest) -> None:
        run_root = self.adapter.root / "runs" / run_id
        outbox = self.adapter.root / ".role-outbox" / stage
        environment = safe_git_environment(roles=True)
        environment.update(
            {
                "WPI_COUNCIL_RUN_ID": run_id,
                "WPI_COUNCIL_STAGE": stage,
                "WPI_COUNCIL_ROOT": str(self.adapter.root),
                "WPI_COUNCIL_RUN_ROOT": str(run_root),
                "WPI_COUNCIL_SANDBOX": str(self.adapter.root / "sandboxes" / run_id),
                "WPI_COUNCIL_OUTBOX": str(outbox),
            }
        )
        result = bounded_process(
            self._resolved_command(stage),
            cwd=self.adapter.root,
            timeout_seconds=request.max_duration_seconds,
            max_output_bytes=self.policy.max_log_bytes,
            environment=environment,
        )
        invocation = {
            "version": 1,
            "run_id": run_id,
            "stage": stage,
            "backend": "command",
            "executable_sha256": self.executable_sha256[stage],
            "arguments_hash": digest_json(self.profiles[stage]),
            "exit_code": result["exit_code"],
            "timed_out": result["timed_out"],
            "stdout_sha256": result["stdout_sha256"],
            "stderr_sha256": result["stderr_sha256"],
            "completed_at": utc_now(),
        }
        _write_atomic(run_root / "05_route" / "invocations" / f"{stage}.json", canonical_json(invocation))
        if result["exit_code"] != 0:
            raise BackendError(f"configured {stage} command failed")
        if stage in {"planner", "reviewer"}:
            self.adapter.ingest_external_stage(run_id, stage)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(
        path == pattern
        or fnmatch.fnmatchcase(path, pattern)
        or (pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])))
        for pattern in patterns
    )


class DeterministicFakeBackend:
    """Synthetic-only backend for deterministic tests and dual-clone simulations."""

    name = "fake"

    def __init__(self, policy: ProjectPolicy, scenario: str = "valid"):
        if scenario not in {
            "valid", "coder_fail_once", "tester_fail_once", "reviewer_reject_once",
            "malformed", "timeout", "adversarial", "repeat_failure",
        }:
            raise BackendError("unknown deterministic fake scenario")
        self.policy = policy
        self.scenario = scenario
        self.invocations = 0

    def _copy_host(self, trusted_workspace: Path, attempt_root: Path) -> Path:
        host = attempt_root / "synthetic-trusted-host"
        shutil.copytree(trusted_workspace, host, symlinks=True)
        for remote in run_git(host, "remote").stdout.decode().split():
            run_git(host, "remote", "remove", remote)
        return host

    def _candidate_path(self, request: TaskRequest) -> str:
        first = request.allowed_paths[0]
        if not any(character in first for character in "*?["):
            return first
        prefix = first.split("*", 1)[0].rstrip("/")
        return f"{prefix}/council-{request.task_id}.md" if prefix else f"docs/council-{request.task_id}.md"

    def _coder_patch(self, request: TaskRequest, host: Path, role_copy: Path, first_attempt: bool) -> tuple[bytes, tuple[str, ...]]:
        path = self._candidate_path(request)
        if self.scenario == "adversarial":
            path = ".github/workflows/adversarial.yml"
        validate_relative_path(path, allow_glob=False)
        target = role_copy / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.write_text(target.read_text(encoding="utf-8") + f"\n<!-- synthetic Council fixture {request.task_id} -->\n", encoding="utf-8")
        else:
            target.write_text(
                f"# Synthetic Council fixture\n\nTask `{request.task_id}` test-only change.\n",
                encoding="utf-8",
            )
        patch = run_git(role_copy, "diff", "--binary", "--full-index", "--no-ext-diff", request.base_commit, "--").stdout
        if target.exists() and path not in run_git(role_copy, "diff", "--name-only", request.base_commit, "--").stdout.decode().splitlines():
            patch = run_git(role_copy, "diff", "--no-index", "--binary", "/dev/null", f"./{path}", check=False).stdout
        changed = tuple(
            item for item in run_git(role_copy, "status", "--porcelain=v1", "--untracked-files=all").stdout.decode().splitlines()
        )
        changed_paths = tuple(sorted({line[3:] for line in changed if len(line) > 3}))
        if self.scenario in {"coder_fail_once", "repeat_failure"} and first_attempt:
            return b"malformed synthetic patch\n", changed_paths
        if self.scenario == "repeat_failure":
            return b"malformed synthetic patch\n", changed_paths
        return patch, changed_paths

    def _apply_synthetic_gate(self, request: TaskRequest, host: Path, patch: bytes, changed: tuple[str, ...]) -> tuple[bool, str]:
        if not patch.strip() or len(patch) > request.max_patch_bytes:
            return False, "synthetic coder gate rejected patch size or shape"
        if len(changed) > request.max_changed_files:
            return False, "synthetic coder gate rejected changed-file limit"
        for path in changed:
            if not _matches(path, request.allowed_paths) or _matches(path, (*request.forbidden_paths, *self.policy.forbidden_paths)):
                return False, "synthetic coder gate rejected path policy"
        patch_path = host.parent / "synthetic-code.patch"
        patch_path.write_bytes(patch)
        checked = run_git(host, "apply", "--check", "--whitespace=error-all", str(patch_path), check=False)
        if checked.returncode:
            return False, "synthetic coder gate rejected malformed patch"
        run_git(host, "apply", "--whitespace=nowarn", str(patch_path))
        return True, ""

    def _run_tests(self, request: TaskRequest, host: Path, first_attempt: bool) -> tuple[bool, list[dict[str, Any]]]:
        records: list[dict[str, Any]] = []
        if self.scenario == "timeout":
            return False, [{"status": "timeout", "synthetic": True}]
        for arguments in request.required_test_commands:
            result = bounded_process(
                arguments,
                cwd=host,
                timeout_seconds=min(120, request.max_duration_seconds),
                max_output_bytes=self.policy.max_log_bytes,
            )
            records.append(
                {
                    "arguments": list(arguments),
                    "exit_code": result["exit_code"],
                    "timed_out": result["timed_out"],
                    "stdout_sha256": result["stdout_sha256"],
                    "stderr_sha256": result["stderr_sha256"],
                    "synthetic": True,
                }
            )
            if result["exit_code"] != 0:
                return False, records
        if self.scenario == "tester_fail_once" and first_attempt:
            return False, records
        return True, records

    def execute_attempt(
        self,
        request: TaskRequest,
        trusted_workspace: Path,
        attempt_id: str,
        attempt_root: Path,
        *,
        correction: Mapping[str, Any] | None = None,
    ) -> AttemptResult:
        self.invocations += 4
        first_attempt = attempt_id.endswith("00")
        if self.invocations > self.policy.max_role_invocations:
            raise BackendError("synthetic role invocation limit exceeded")
        host = self._copy_host(trusted_workspace, attempt_root)
        route = {
            "selected_level": "CRITICAL" if request.scientific_risk == "critical" else "COMPLEX",
            "router": "deterministic_fake_fixture",
            "synthetic": True,
        }
        if self.scenario == "malformed":
            return self._result(attempt_id, host, route, False, "malformed_role_output", "synthetic Planner output malformed")
        coder = attempt_root / "synthetic-coder-repo"
        shutil.copytree(host, coder, symlinks=True)
        patch, changed = self._coder_patch(request, host, coder, first_attempt)
        gate_passed, detail = self._apply_synthetic_gate(request, host, patch, changed)
        patch_hash = hashlib.sha256(patch).hexdigest()
        if not gate_passed:
            return self._result(attempt_id, host, route, False, "coder_gate", detail, changed, patch_hash)
        tests_passed, test_records = self._run_tests(request, host, first_attempt)
        test_hash = digest_json(test_records)
        if not tests_passed:
            return self._result(attempt_id, host, route, False, "tester_gate", "synthetic targeted test failure", changed, patch_hash, test_hash)
        if self.scenario == "reviewer_reject_once" and first_attempt:
            return self._result(attempt_id, host, route, False, "reviewer", "synthetic Reviewer safety defect", changed, patch_hash, test_hash, "NEEDS_CHANGES")
        report = {
            "verdict": "APPROVE",
            "correctness": "synthetic fixture only",
            "scientific_boundary": "not scientific evidence",
        }
        return AttemptResult(
            attempt_id=attempt_id,
            passed=True,
            correction_kind=None,
            correction_detail="",
            workspace=host,
            route=route,
            changed_paths=changed,
            patch_hash=patch_hash,
            test_evidence_hash=test_hash,
            reviewer_report_hash=digest_json(report),
            reviewer_verdict="APPROVE",
            evidence={
                "backend": "deterministic_fake",
                "synthetic_test_only": True,
                "real_council_evidence": False,
                "failed_attempts_preserved": True,
                "raw_role_transcripts_retained": False,
            },
        )

    def _result(
        self,
        attempt_id: str,
        host: Path,
        route: Mapping[str, Any],
        passed: bool,
        kind: str,
        detail: str,
        changed: tuple[str, ...] = (),
        patch_hash: str = "",
        test_hash: str = "",
        verdict: str = "BLOCKED",
    ) -> AttemptResult:
        return AttemptResult(
            attempt_id=attempt_id,
            passed=passed,
            correction_kind=kind,
            correction_detail=detail,
            workspace=host,
            route=route,
            changed_paths=changed,
            patch_hash=patch_hash,
            test_evidence_hash=test_hash,
            reviewer_report_hash=digest_json({"verdict": verdict, "synthetic": True}),
            reviewer_verdict=verdict,
            evidence={
                "backend": "deterministic_fake",
                "synthetic_test_only": True,
                "real_council_evidence": False,
                "raw_role_transcripts_retained": False,
            },
        )
