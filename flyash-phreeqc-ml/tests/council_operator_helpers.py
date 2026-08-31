from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess

from flyash_phreeqc_ml.council_operator.contracts import (
    OperatorConfig,
    ProjectPolicy,
    REQUEST_SCHEMA,
    TaskRequest,
    utc_now,
)
from flyash_phreeqc_ml.council_operator.operator import CouncilOperator


PROJECT = Path(__file__).resolve().parents[1]


def command(*arguments: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(list(arguments), cwd=cwd, text=True).strip()


def create_code_remote(root: Path, *, branch: str = "base") -> tuple[Path, Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    remote = root / "code.git"
    live = root / "live"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", branch, str(live)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(live), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(live), "config", "user.email", "test@invalid.local"], check=True)
    (live / "README.md").write_text("# Synthetic test repository\n", encoding="utf-8")
    (live / "docs").mkdir()
    (live / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(live), "add", "."], check=True)
    subprocess.run(["git", "-C", str(live), "commit", "-m", "base"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(live), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(live), "push", "-u", "origin", branch], check=True, capture_output=True)
    head = command("git", "-C", str(live), "rev-parse", "HEAD")
    return remote, live, head


def make_policy(remote: Path, *, branch: str = "base", allow_expiry: bool = False) -> ProjectPolicy:
    base = ProjectPolicy.load(PROJECT / "config" / "council_operator_policy.toml")
    return replace(
        base,
        allowed_repository=str(remote),
        allowed_base_branches=(branch,),
        forbidden_branches=("main", "master"),
        default_test_commands=(("python3", "-m", "compileall", "-q", "."),),
        allowed_test_command_prefixes=(("python3", "-m", "compileall"),),
        forbidden_paths=(".git/**", ".env", "private/**", "data/raw/**"),
        max_correction_rounds=2,
        max_task_duration_seconds=600,
        max_patch_bytes=200000,
        max_changed_files=8,
        max_role_invocations=16,
        max_log_bytes=65536,
        lease_seconds=60,
        allow_policy_expiry_takeover=allow_expiry,
    )


def make_request(policy: ProjectPolicy, head: str, *, task_id: str = "test-task", **overrides) -> TaskRequest:
    value = {
        "schema_version": REQUEST_SCHEMA,
        "revision": 1,
        "task_id": task_id,
        "title": "Update public documentation",
        "goal": "Update the public documentation fixture",
        "background": "Synthetic public test fixture with no research information",
        "repository": policy.allowed_repository,
        "base_branch": policy.allowed_base_branches[0],
        "base_commit": head,
        "allowed_paths": ["README.md"],
        "forbidden_paths": list(policy.forbidden_paths),
        "acceptance_criteria": ["The documentation change is present"],
        "required_test_commands": [["python3", "-m", "compileall", "-q", "."]],
        "scientific_risk": "moderate",
        "security_privacy": "sanitized",
        "permitted_external_resources": [],
        "max_correction_rounds": 2,
        "max_duration_seconds": 300,
        "max_changed_files": 4,
        "max_patch_bytes": 100000,
        "requested_backend": "fake",
        "requester_identity": "test:operator",
        "created_at": utc_now(),
        "canonical_request_hash": "",
        "issue_references": [],
        "review_notes": "",
        "relevant_documentation": [],
        "resource_update_request_reference": "",
    }
    value.update(overrides)
    return TaskRequest.seal(value)


def make_operator(root: Path, remote: Path, live: Path, policy: ProjectPolicy, *, worker: str = "worker-a", control_remote: Path | None = None) -> CouncilOperator:
    control = control_remote or remote
    config = OperatorConfig(
        repository_path=live,
        code_remote=str(remote),
        control_remote=str(control),
        worker_id=worker,
        sandbox_root=root / f"sandboxes-{worker}",
        state_cache_root=root / f"state-{worker}",
        council_root=None,
        hermes_executable=None,
        obsidian_vault=None,
    )
    return CouncilOperator(config, policy)
