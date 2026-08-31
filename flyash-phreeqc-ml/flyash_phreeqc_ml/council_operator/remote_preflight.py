"""Read and non-mutating push-permission probes for operator Git remotes."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from .sandbox import safe_git_environment


AUTHENTICATION_MARKERS = (
    "authentication failed",
    "authorization failed",
    "could not read username",
    "permission denied (publickey)",
    "write access to repository not granted",
    "repository not found",
    "http 401",
    "http 403",
    "returned error: 401",
    "returned error: 403",
    "access denied",
)
NETWORK_MARKERS = (
    "could not resolve host",
    "failed to connect",
    "connection timed out",
    "operation timed out",
    "network is unreachable",
    "connection refused",
    "connection reset",
    "connection closed",
    "remote end hung up unexpectedly",
    "no route to host",
    "temporary failure in name resolution",
    "could not resolve proxy",
    "proxy connect",
    "tls handshake",
    "gnutls_handshake",
    "ssl_connect",
    "ssl certificate problem",
    "certificate verify failed",
    "unable to access",
    "rpc failed",
    "early eof",
    "does not appear to be a git repository",
)
POLICY_MARKERS = (
    "protected branch",
    "pre-receive hook declined",
    "hook declined",
    "remote rejected",
    "not allowed to push",
    "prohibited",
    "deny updating",
    "refusing to update",
    "cannot lock ref",
    "non-fast-forward",
)


class RemotePreflightError(RuntimeError):
    """A remote permission probe could not establish its required contract."""


def classify_git_failure(stderr: bytes | str, *, operation: str) -> str:
    """Classify a bounded Git failure without returning credential-bearing text."""

    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr
    lowered = text.lower()
    if any(marker in lowered for marker in AUTHENTICATION_MARKERS):
        return "authentication_failure"
    if any(marker in lowered for marker in NETWORK_MARKERS):
        return "network_failure"
    if any(marker in lowered for marker in POLICY_MARKERS):
        return "branch_ref_policy_refusal"
    return "unclassified_failure"


def _run(arguments: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            arguments,
            capture_output=True,
            shell=False,
            timeout=timeout,
            env=safe_git_environment(),
        )
    except subprocess.TimeoutExpired:
        # Preserve the same structured path as a Git transport failure so the
        # caller can distinguish a network timeout from authentication/policy.
        return subprocess.CompletedProcess(arguments, 124, b"", b"operation timed out")
    except OSError as exc:
        raise RemotePreflightError("Git remote permission probe could not run") from exc


def _probe_token(repository: Path, kind: str) -> str:
    material = f"{os.getpid()}:{time.time_ns()}:{repository}:{kind}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:20]


def _target_ref(project_slug: str, kind: str, token: str) -> str:
    if kind == "code_branch":
        return f"refs/heads/council/wpi/permission-preflight-{token}"
    if kind == "control_state":
        return f"refs/heads/council-state/{project_slug}/permission-preflight-{token}"
    raise RemotePreflightError("unknown remote permission probe kind")


def probe_remote_permission(
    repository: Path,
    remote: str,
    *,
    project_slug: str,
    kind: str,
) -> dict[str, Any]:
    """Prove read and namespace-specific push authorization without updating a ref."""

    repository = Path(repository)
    if not repository.is_absolute() or not (repository / ".git").exists():
        raise RemotePreflightError("permission probe requires an absolute live Git checkout")
    if not remote or re.search(r"[\x00\r\n]", remote):
        raise RemotePreflightError("permission probe remote is invalid")

    result: dict[str, Any] = {
        "probe_kind": kind,
        "readable": False,
        "push_authorized": False,
        "failure_kind": None,
        "non_mutating": True,
        "remote_identity_sha256": hashlib.sha256(remote.encode("utf-8")).hexdigest(),
    }
    readable = _run(["git", "ls-remote", "--heads", remote])
    if readable.returncode:
        result["failure_kind"] = classify_git_failure(readable.stderr, operation="read")
        return result
    result["readable"] = True

    source = _run(["git", "-C", str(repository), "rev-parse", "HEAD"])
    if source.returncode:
        raise RemotePreflightError("permission probe could not resolve the local source commit")
    source_oid = source.stdout.decode("ascii", errors="strict").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", source_oid):
        raise RemotePreflightError("permission probe source commit is malformed")

    target_ref = _target_ref(project_slug, kind, _probe_token(repository, kind))
    before = _run(["git", "ls-remote", "--refs", remote, target_ref])
    if before.returncode:
        result["failure_kind"] = classify_git_failure(before.stderr, operation="read")
        return result
    if before.stdout.strip():
        raise RemotePreflightError("permission probe ref unexpectedly already exists")

    dry_run = _run(
        [
            "git",
            "-C",
            str(repository),
            "push",
            "--dry-run",
            "--porcelain",
            "--no-verify",
            remote,
            f"{source_oid}:{target_ref}",
        ],
        timeout=120,
    )
    after = _run(["git", "ls-remote", "--refs", remote, target_ref])
    if after.returncode:
        result["failure_kind"] = classify_git_failure(after.stderr, operation="read")
        return result
    if after.stdout.strip():
        result["non_mutating"] = False
        raise RemotePreflightError("dry-run permission probe unexpectedly changed the remote")
    if dry_run.returncode:
        result["failure_kind"] = classify_git_failure(dry_run.stderr, operation="push")
        return result

    result["push_authorized"] = True
    return result


def probe_operator_remotes(
    repository: Path,
    code_remote: str,
    control_remote: str,
    *,
    project_slug: str,
) -> dict[str, dict[str, Any]]:
    return {
        "code": probe_remote_permission(
            repository,
            code_remote,
            project_slug=project_slug,
            kind="code_branch",
        ),
        "control": probe_remote_permission(
            repository,
            control_remote,
            project_slug=project_slug,
            kind="control_state",
        ),
    }
