from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from flyash_phreeqc_ml.council_operator.backends import BackendError, CouncilCompatibilityAdapter
from flyash_phreeqc_ml.council_operator.contracts import (
    OperatorConfig,
    approval_artifact,
    new_state_record,
)
from flyash_phreeqc_ml.council_operator.obsidian import (
    ObsidianSyncError,
    append_handoff,
    resolve_wpi_vault,
)
from flyash_phreeqc_ml.council_operator.operator import CouncilOperator, OperatorError
from flyash_phreeqc_ml.council_operator.resource_boundary import (
    ResourceBoundaryError,
    ResourceStewardAdapter,
)
from flyash_phreeqc_ml.council_operator.sandbox import (
    SandboxError,
    SandboxManager,
    bounded_process,
    safe_git_environment,
)
from council_operator_helpers import create_code_remote, make_policy, make_request


def test_disposable_clone_starts_at_exact_base_and_preserves_live_checkout(tmp_path):
    remote, live, head = create_code_remote(tmp_path)
    request = make_request(make_policy(remote), head)
    manager = SandboxManager(live, tmp_path / "sandboxes", str(remote))
    before = manager.verify_live_checkout()
    workspace, journal = manager.create_trusted_workspace(request)
    after = manager.verify_live_checkout()
    assert before == after
    assert subprocess.check_output(["git", "-C", workspace, "rev-parse", "HEAD"], text=True).strip() == head
    assert subprocess.check_output(["git", "-C", workspace, "remote"], text=True).strip() == ""
    assert journal["request_hash"] == request.canonical_request_hash


def test_dirty_live_checkout_refused_without_stash_reset_or_switch(tmp_path):
    remote, live, head = create_code_remote(tmp_path)
    request = make_request(make_policy(remote), head)
    (live / "README.md").write_text("dirty\n", encoding="utf-8")
    branch = subprocess.check_output(["git", "-C", live, "branch", "--show-current"], text=True).strip()
    with pytest.raises(SandboxError, match="clean"):
        SandboxManager(live, tmp_path / "sandboxes", str(remote)).create_trusted_workspace(request)
    assert subprocess.check_output(["git", "-C", live, "branch", "--show-current"], text=True).strip() == branch
    assert (live / "README.md").read_text(encoding="utf-8") == "dirty\n"


def test_stale_remote_base_refused(tmp_path):
    remote, live, head = create_code_remote(tmp_path)
    request = make_request(make_policy(remote), head)
    (live / "README.md").write_text("# advanced\n", encoding="utf-8")
    subprocess.run(["git", "-C", live, "add", "README.md"], check=True)
    subprocess.run(["git", "-C", live, "commit", "-m", "advance"], check=True, capture_output=True)
    subprocess.run(["git", "-C", live, "push", "origin", "base"], check=True, capture_output=True)
    with pytest.raises(SandboxError, match="stale"):
        SandboxManager(live, tmp_path / "sandboxes", str(remote)).create_trusted_workspace(request)


def test_symlink_sandbox_root_and_role_copy_rejected(tmp_path):
    remote, live, head = create_code_remote(tmp_path)
    real = tmp_path / "real-sandboxes"
    real.mkdir()
    symlink = tmp_path / "sandboxes"
    symlink.symlink_to(real, target_is_directory=True)
    with pytest.raises(SandboxError, match="unsafe"):
        SandboxManager(live, symlink, str(remote)).create_trusted_workspace(make_request(make_policy(remote), head))


def test_tracked_repository_symlink_is_rejected_before_role_copy(tmp_path):
    remote, live, _head = create_code_remote(tmp_path)
    (live / "README.md").unlink()
    (live / "README.md").symlink_to("/tmp/prohibited-target")
    subprocess.run(["git", "-C", live, "add", "README.md"], check=True)
    subprocess.run(["git", "-C", live, "commit", "-m", "symlink fixture"], check=True, capture_output=True)
    subprocess.run(["git", "-C", live, "push", "origin", "base"], check=True, capture_output=True)
    head = subprocess.check_output(["git", "-C", live, "rev-parse", "HEAD"], text=True).strip()
    with pytest.raises(SandboxError, match="symbolic link"):
        SandboxManager(live, tmp_path / "sandboxes", str(remote)).create_trusted_workspace(
            make_request(make_policy(remote), head)
        )


def test_role_environment_contains_no_credentials_or_agent_socket(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-forwarded")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/socket")
    monkeypatch.setenv("GITHUB_TOKEN", "not-forwarded")
    environment = safe_git_environment(roles=True)
    assert "OPENAI_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert environment["HOME"] == "/nonexistent"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


def test_bounded_process_timeout_kills_process_group(tmp_path):
    result = bounded_process(
        ["python3", "-c", "import time; time.sleep(20)"],
        cwd=tmp_path,
        timeout_seconds=1,
        max_output_bytes=1024,
    )
    assert result["exit_code"] == 124
    assert result["timed_out"] is True


def test_bounded_process_truncates_output_and_never_shell_interpolates(tmp_path):
    marker = tmp_path / "injected"
    payload = f";touch {marker}"
    result = bounded_process(
        ["python3", "-c", "import sys; print('x'*5000); print(sys.argv[1])", payload],
        cwd=tmp_path,
        timeout_seconds=5,
        max_output_bytes=128,
    )
    assert result["exit_code"] == 0
    assert result["truncated"] is True
    assert not marker.exists()


def _fake_council_root(root: Path, policy):
    for directory in ("runs", "sandboxes", ".role-outbox/planner", ".role-outbox/coder", ".role-outbox/tester", ".role-outbox/reviewer"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    hashes = {}
    for relative in policy.council_required_sha256:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return replace(policy, council_required_sha256=hashes)


def test_council_contract_hash_mismatch_fails_closed(tmp_path):
    remote, _live, _head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    root = tmp_path / "council-runtime"
    bound = _fake_council_root(root, policy)
    (root / "tools" / "council_route.py").write_text("changed", encoding="utf-8")
    with pytest.raises(BackendError, match="hash mismatch"):
        CouncilCompatibilityAdapter(root, bound).verify()


def test_obsidian_export_snapshot_cannot_be_used_as_runtime(tmp_path):
    remote, _live, _head = create_code_remote(tmp_path)
    parent = tmp_path / "vault"
    (parent / ".obsidian").mkdir(parents=True)
    root = parent / "AI Council"
    policy = _fake_council_root(root, make_policy(remote))
    with pytest.raises(BackendError, match="reference"):
        CouncilCompatibilityAdapter(root, policy).verify()


def test_resource_steward_automatic_promotion_and_rollback_blocked(tmp_path):
    remote, _live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    adapter = ResourceStewardAdapter(policy, tmp_path, tmp_path / "resources")
    with pytest.raises(ResourceBoundaryError, match="separate exact human approval"):
        adapter.execute("promote", ["proposal-1"])
    with pytest.raises(ResourceBoundaryError):
        adapter.execute("rollback", ["proposal-1"])


def test_resource_steward_allowed_action_and_exact_approval(monkeypatch, tmp_path):
    remote, _live, head = create_code_remote(tmp_path)
    policy = make_policy(remote)
    request = make_request(policy, head)
    record = new_state_record(request, policy.project_slug, "controller")
    adapter = ResourceStewardAdapter(policy, tmp_path, tmp_path / "resources")

    monkeypatch.setattr(
        "flyash_phreeqc_ml.council_operator.resource_boundary.bounded_process",
        lambda *args, **kwargs: {
            "exit_code": 0, "timed_out": False, "stdout_sha256": "1" * 64, "stderr_sha256": "2" * 64
        },
    )
    evidence = adapter.execute("show", [])
    assert evidence["active_resource_changed_automatically"] is False
    approval = approval_artifact(
        "resource_promotion",
        record,
        approved_by="human:admin",
        extra_bindings={"proposal_id": "proposal-1", "candidate_sha256": "3" * 64},
    )
    assert adapter.execute("promote", ["proposal-1"], approval=approval)["approval_hash"] == approval["approval_hash"]
    forged = json.loads(json.dumps(approval))
    forged["approval_hash"] = "0" * 64
    with pytest.raises(ResourceBoundaryError, match="invalid"):
        adapter.execute("promote", ["proposal-1"], approval=forged)
    forged_time = json.loads(json.dumps(approval))
    forged_time["approved_at"] = "2030-01-01T00:00:00Z"
    with pytest.raises(ResourceBoundaryError, match="invalid"):
        adapter.execute("promote", ["proposal-1"], approval=forged_time)


def test_private_control_remote_requires_explicit_visibility_attestation(tmp_path):
    remote, live, _head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    config = OperatorConfig(
        repository_path=live,
        code_remote=str(remote),
        control_remote=str(tmp_path / "separate-control.git"),
        worker_id="worker-a",
        sandbox_root=tmp_path / "sandboxes",
        state_cache_root=tmp_path / "state",
        council_root=None,
        hermes_executable=None,
        obsidian_vault=None,
    )
    assert config.has_private_control_remote is False
    assert replace(config, control_remote_private=True).has_private_control_remote is True
    with pytest.raises(OperatorError, match="repository identity"):
        CouncilOperator(replace(config, code_remote=str(tmp_path / "other.git")), policy)


def test_git_client_is_present_only_in_the_exact_test_image_stage():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    runtime, test_and_release = dockerfile.split("FROM runtime AS test", 1)
    test_stage, release_stage = test_and_release.split("FROM runtime AS release", 1)
    assert "apt-get install -y --no-install-recommends git" in test_stage
    assert "apt-get install -y --no-install-recommends git" not in runtime
    assert "apt-get install -y --no-install-recommends git" not in release_stage
    assert "COPY --chown=root:root config ./config" in runtime
    installer = (Path(__file__).resolve().parents[1] / "scripts" / "install-council-operator.sh").read_text(encoding="utf-8")
    assert "pip install --user" not in installer
    assert '"$operator_python" -m pip install' in installer
    assert '--no-build-isolation --no-deps --editable "$project_dir"' in installer


def _vault(tmp_path):
    vault = tmp_path / "Obsidian Test"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "AI Council").mkdir()
    (vault / "WPI Project").mkdir()
    note = vault / "WPI Project" / "99 - Handoff Log.md"
    note.write_text("# Handoff\n", encoding="utf-8")
    return vault, note


def test_obsidian_append_only_hash_and_concurrent_modification_detection(tmp_path):
    remote, _live, head = create_code_remote(tmp_path / "repo")
    policy = make_policy(remote)
    record = new_state_record(make_request(policy, head), policy.project_slug, "controller")
    vault, note = _vault(tmp_path)
    before = hashlib.sha256(note.read_bytes()).hexdigest()
    result = append_handoff(record, vault=vault, expected_before_hash=before)
    assert result["sha256_before"] == before
    assert note.read_text(encoding="utf-8").startswith("# Handoff\n")
    with pytest.raises(ObsidianSyncError, match="concurrently"):
        append_handoff(record, vault=vault, expected_before_hash=before)


def test_obsidian_resolution_uses_configured_path_or_parent(tmp_path):
    vault, _note = _vault(tmp_path)
    nested = vault / "Nested Vault"
    nested.mkdir()
    config = tmp_path / "obsidian.json"
    config.write_text(json.dumps({"vaults": {"one": {"path": str(nested), "open": True}}}), encoding="utf-8")
    assert resolve_wpi_vault(config) == vault
