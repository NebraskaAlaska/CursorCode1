"""Trusted disposable Git sandboxes and credential-free role copies."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Iterable

from .contracts import ContractError, TaskRequest, canonical_json, utc_now


class SandboxError(RuntimeError):
    """A disposable-workspace safety check failed."""


def safe_git_environment(*, roles: bool = False) -> dict[str, str]:
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT", "SSH_AUTH_SOCK"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update({"GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1"})
    if roles:
        environment.update(
            {
                "HOME": "/nonexistent",
                "XDG_CONFIG_HOME": "/nonexistent",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_ASKPASS": "/usr/bin/false",
                "SSH_ASKPASS": "/usr/bin/false",
            }
        )
        environment.pop("SSH_AUTH_SOCK", None)
    return environment


def run_git(repo: Path | None, *arguments: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[bytes]:
    command = ["git"]
    if repo is not None:
        command.extend(["-C", str(repo)])
    command.extend(arguments)
    completed = subprocess.run(
        command,
        capture_output=True,
        shell=False,
        timeout=timeout,
        env=safe_git_environment(),
    )
    if check and completed.returncode:
        raise SandboxError("trusted Git workspace operation failed")
    return completed


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def require_contained_real_path(path: Path, root: Path, *, label: str, must_exist: bool = False) -> Path:
    absolute_root = Path(os.path.abspath(root))
    absolute = Path(os.path.abspath(path))
    if not _inside(absolute, absolute_root):
        raise SandboxError(f"{label} escapes its trusted root")
    current = absolute_root
    if current.is_symlink():
        raise SandboxError(f"{label} root is a symbolic link")
    for part in absolute.relative_to(absolute_root).parts:
        current /= part
        if current.is_symlink():
            raise SandboxError(f"{label} contains a symbolic link")
        if not current.exists():
            break
    if must_exist and not absolute.exists():
        raise SandboxError(f"{label} does not exist")
    return absolute


def tree_hash(root: Path, *, exclude_git: bool = True) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if exclude_git and relative.parts and relative.parts[0] == ".git":
            continue
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        encoded = relative.as_posix().encode("utf-8")
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode("utf-8")
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        elif path.is_dir():
            kind, payload = b"D", b""
        else:
            raise SandboxError("sandbox contains an unsupported filesystem object")
        digest.update(kind + mode.to_bytes(4, "big") + len(encoded).to_bytes(8, "big") + encoded)
        digest.update(len(payload).to_bytes(8, "big") + payload)
    return digest.hexdigest()


def reject_repository_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            raise SandboxError("repository workspace contains a prohibited symbolic link")


class SandboxManager:
    def __init__(self, live_repository: Path, sandbox_root: Path, code_remote: str):
        self.live_repository = live_repository.resolve()
        self.sandbox_root = Path(os.path.abspath(sandbox_root))
        self.code_remote = code_remote

    def _ensure_root(self) -> None:
        self.sandbox_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.sandbox_root.is_symlink() or not self.sandbox_root.is_dir():
            raise SandboxError("sandbox root is unsafe")
        if _inside(self.sandbox_root, self.live_repository) or _inside(self.live_repository, self.sandbox_root):
            raise SandboxError("sandbox root and live checkout must be disjoint")

    def verify_live_checkout(self) -> dict[str, str]:
        if self.live_repository.is_symlink() or not (self.live_repository / ".git").exists():
            raise SandboxError("live WPI checkout is missing or unsafe")
        status = run_git(self.live_repository, "status", "--porcelain=v1", "--untracked-files=all").stdout
        if status:
            raise SandboxError("live WPI checkout must be clean before task execution")
        branch = run_git(self.live_repository, "branch", "--show-current").stdout.decode().strip()
        head = run_git(self.live_repository, "rev-parse", "HEAD").stdout.decode().strip()
        origin = run_git(self.live_repository, "remote", "get-url", "origin").stdout.decode().strip()
        if origin != self.code_remote:
            raise SandboxError("live checkout origin does not match the configured code remote")
        return {"branch": branch, "head": head, "tree_hash": tree_hash(self.live_repository)}

    def verify_remote_base(self, request: TaskRequest) -> str:
        completed = subprocess.run(
            ["git", "ls-remote", "--heads", self.code_remote, f"refs/heads/{request.base_branch}"],
            capture_output=True,
            shell=False,
            timeout=60,
            env=safe_git_environment(),
        )
        if completed.returncode:
            raise SandboxError("cannot verify the code remote base branch")
        lines = [line for line in completed.stdout.decode("utf-8", errors="replace").splitlines() if line]
        if len(lines) != 1:
            raise SandboxError("code remote base branch is missing or ambiguous")
        remote_commit, remote_ref = lines[0].split("\t", 1)
        if remote_ref != f"refs/heads/{request.base_branch}":
            raise SandboxError("code remote returned the wrong base branch")
        if remote_commit != request.base_commit:
            raise SandboxError("requested base commit is stale")
        return remote_commit

    def task_root(self, task_id: str) -> Path:
        self._ensure_root()
        return require_contained_real_path(self.sandbox_root / task_id, self.sandbox_root, label="task sandbox")

    def create_trusted_workspace(self, request: TaskRequest) -> tuple[Path, dict[str, Any]]:
        before = self.verify_live_checkout()
        self.verify_remote_base(request)
        root = self.task_root(request.task_id)
        workspace = root / "trusted"
        journal_path = root / "journal.json"
        if root.exists():
            require_contained_real_path(root, self.sandbox_root, label="existing task sandbox", must_exist=True)
            if not journal_path.is_file() or journal_path.is_symlink() or not workspace.is_dir() or workspace.is_symlink():
                raise SandboxError("existing task sandbox is incomplete or unsafe")
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if journal.get("request_hash") != request.canonical_request_hash or journal.get("base_commit") != request.base_commit:
                raise SandboxError("existing task sandbox belongs to another request")
            if run_git(workspace, "rev-parse", "HEAD").stdout.decode().strip() != request.base_commit:
                raise SandboxError("existing task workspace moved from the requested base")
            return workspace, journal
        root.mkdir(mode=0o700, parents=False)
        try:
            completed = subprocess.run(
                ["git", "clone", "--no-hardlinks", "--no-checkout", str(self.live_repository), str(workspace)],
                capture_output=True,
                shell=False,
                timeout=120,
                env=safe_git_environment(),
            )
            if completed.returncode:
                raise SandboxError("cannot create disposable trusted clone")
            run_git(workspace, "checkout", "--detach", request.base_commit)
            reject_repository_symlinks(workspace)
            for remote in run_git(workspace, "remote").stdout.decode().split():
                run_git(workspace, "remote", "remove", remote)
            run_git(workspace, "config", "user.name", "WPI Council Operator")
            run_git(workspace, "config", "user.email", "wpi-council@invalid.local")
            run_git(workspace, "config", "commit.gpgsign", "false")
            if run_git(workspace, "status", "--porcelain=v1", "--untracked-files=all").stdout:
                raise SandboxError("new trusted workspace is not clean")
            journal = {
                "version": 1,
                "task_id": request.task_id,
                "request_hash": request.canonical_request_hash,
                "base_commit": request.base_commit,
                "created_at": utc_now(),
                "starting_tree_hash": tree_hash(workspace),
                "live_checkout_before": before,
                "attempts": [],
            }
            self.write_journal(root, journal)
            after = self.verify_live_checkout()
            if before != after:
                raise SandboxError("live WPI checkout changed while creating the sandbox")
            return workspace, journal
        except Exception:
            # Material remains for forensic inspection when repository creation began.
            raise

    def write_journal(self, task_root: Path, journal: dict[str, Any]) -> None:
        task_root = require_contained_real_path(task_root, self.sandbox_root, label="task journal root", must_exist=True)
        temporary = task_root / f".journal.{os.getpid()}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_json(journal))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, task_root / "journal.json")
        finally:
            temporary.unlink(missing_ok=True)

    def isolated_role_copy(self, trusted_workspace: Path, attempt_root: Path, role: str) -> Path:
        attempt_root = require_contained_real_path(attempt_root, self.sandbox_root, label="attempt root", must_exist=True)
        destination = attempt_root / f"{role}-repo"
        if destination.exists() or destination.is_symlink():
            raise SandboxError("role workspace already exists")
        shutil.copytree(trusted_workspace, destination, symlinks=True)
        if destination.is_symlink() or not (destination / ".git").exists():
            raise SandboxError("role workspace copy is unsafe")
        reject_repository_symlinks(destination)
        for remote in run_git(destination, "remote").stdout.decode().split():
            run_git(destination, "remote", "remove", remote)
        return destination

    def create_attempt_root(self, task_id: str, attempt_id: str) -> Path:
        root = self.task_root(task_id)
        attempts = root / "attempts"
        attempts.mkdir(mode=0o700, exist_ok=True)
        if attempts.is_symlink():
            raise SandboxError("attempt root is unsafe")
        attempt = attempts / attempt_id
        if attempt.exists() or attempt.is_symlink():
            raise SandboxError("attempt evidence is immutable and already exists")
        attempt.mkdir(mode=0o700)
        return attempt

    def explicit_cleanup(self, task_id: str, *, retain_failed: bool) -> None:
        root = require_contained_real_path(self.task_root(task_id), self.sandbox_root, label="cleanup target", must_exist=True)
        journal = json.loads((root / "journal.json").read_text(encoding="utf-8"))
        if retain_failed and any(item.get("status") != "passed" for item in journal.get("attempts", [])):
            raise SandboxError("failed evidence retention policy blocks cleanup")
        shutil.rmtree(root)


def bounded_process(
    arguments: Iterable[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_bytes: int,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    argv = list(arguments)
    if not argv or any(not isinstance(item, str) or not item or "\x00" in item for item in argv):
        raise SandboxError("process arguments are invalid")
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
        env=environment or safe_git_environment(roles=True),
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, 15)
            stdout, stderr = process.communicate(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
    truncated = len(stdout) > max_output_bytes or len(stderr) > max_output_bytes
    stdout = stdout[:max_output_bytes]
    stderr = stderr[:max_output_bytes]
    return {
        "arguments": argv,
        "exit_code": 124 if timed_out else process.returncode,
        "timed_out": timed_out,
        "truncated": truncated,
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }
