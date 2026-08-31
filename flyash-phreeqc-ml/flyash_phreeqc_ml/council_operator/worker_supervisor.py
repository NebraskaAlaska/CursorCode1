"""Fail-closed lifecycle supervision for one background Hermes worker.

The shell entry points deliberately do very little: they resolve the pinned
operator Python and invoke this module.  This module owns the background
process, its dedicated process group, the macOS sleep assertion, and strict
local metadata.  The remote task record remains the authority for leases and
task evidence; local metadata is only a safe process-control capability.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import datetime as dt
from dataclasses import asdict, dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import select
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Iterator, Mapping, Sequence

from .contracts import TASK_ID_RE


METADATA_SCHEMA = "wpi-council-worker-metadata/v1"
CONTROL_SCHEMA = "wpi-council-worker-control/v1"
RECEIPT_SCHEMA = "wpi-council-worker-receipt/v1"
MODULE = "flyash_phreeqc_ml.council_operator.worker_supervisor"
MAX_METADATA_BYTES = 64 * 1024
DEFAULT_TERM_TIMEOUT_SECONDS = 10.0
READY_TIMEOUT_SECONDS = 15.0
INSTANCE_RE = re.compile(r"[0-9a-f]{32}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class SupervisorError(RuntimeError):
    """The worker lifecycle could not be completed safely."""


class MetadataError(SupervisorError):
    """Local worker metadata is malformed or unsafe."""


class OwnershipError(SupervisorError):
    """A PID or process group is not demonstrably owned by this instance."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    process_start_identity: str
    executable_path: str
    executable_sha256: str
    process_group_id: int
    session_id: int


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_timestamp(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise MetadataError(f"{label} is not a timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MetadataError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise MetadataError(f"{label} does not include a timezone")


def _validate_argv(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise MetadataError(f"{label} is not an argument array")
    if any(not isinstance(item, str) or not item or any(char in item for char in "\x00\n\r") for item in value):
        raise MetadataError(f"{label} contains an unsafe argument")
    return value


def _validate_process_identity(value: Any, label: str) -> dict[str, Any]:
    expected = {
        "pid",
        "process_start_identity",
        "executable_path",
        "executable_sha256",
        "process_group_id",
        "session_id",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise MetadataError(f"{label} process identity has unknown or missing fields")
    if any(type(value[key]) is not int or value[key] <= 1 for key in ("pid", "process_group_id", "session_id")):
        raise MetadataError(f"{label} process identity has an unsafe PID")
    if not isinstance(value["process_start_identity"], str) or not value["process_start_identity"]:
        raise MetadataError(f"{label} process start identity is missing")
    path = Path(value["executable_path"])
    if not path.is_absolute() or "\x00" in str(path):
        raise MetadataError(f"{label} executable path is unsafe")
    if not isinstance(value["executable_sha256"], str) or not SHA256_RE.fullmatch(value["executable_sha256"]):
        raise MetadataError(f"{label} executable identity is invalid")
    return value


def validate_metadata(value: Any, *, state_dir: Path) -> dict[str, Any]:
    expected = {
        "schema_version",
        "task_id",
        "instance_id",
        "created_at",
        "operator_python",
        "command",
        "supervisor",
        "worker",
        "control_request",
        "term_timeout_seconds",
        "caffeinate",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise MetadataError("worker metadata has unknown or missing fields")
    if value["schema_version"] != METADATA_SCHEMA:
        raise MetadataError("worker metadata schema is incompatible")
    if not isinstance(value["task_id"], str) or not TASK_ID_RE.fullmatch(value["task_id"]):
        raise MetadataError("worker metadata has an unsafe task ID")
    if not isinstance(value["instance_id"], str) or not INSTANCE_RE.fullmatch(value["instance_id"]):
        raise MetadataError("worker metadata has an invalid instance ID")
    _validate_timestamp(value["created_at"], "worker creation time")

    python_value = value["operator_python"]
    if not isinstance(python_value, dict) or set(python_value) != {"path", "version", "sha256"}:
        raise MetadataError("operator Python identity is malformed")
    if not Path(str(python_value["path"])).is_absolute():
        raise MetadataError("operator Python path is not absolute")
    if not isinstance(python_value["version"], str) or not python_value["version"].startswith("3.12."):
        raise MetadataError("operator Python version is not 3.12")
    if not isinstance(python_value["sha256"], str) or not SHA256_RE.fullmatch(python_value["sha256"]):
        raise MetadataError("operator Python SHA-256 is invalid")

    command = value["command"]
    command_fields = {"requested_argv", "effective_argv", "requested_sha256", "effective_sha256"}
    if not isinstance(command, dict) or set(command) != command_fields:
        raise MetadataError("worker command identity is malformed")
    requested = _validate_argv(command["requested_argv"], "requested worker command")
    effective = _validate_argv(command["effective_argv"], "effective worker command")
    if command["requested_sha256"] != _digest_json(requested):
        raise MetadataError("requested worker command hash does not match")
    if command["effective_sha256"] != _digest_json(effective):
        raise MetadataError("effective worker command hash does not match")

    _validate_process_identity(value["supervisor"], "supervisor")
    worker = _validate_process_identity(value["worker"], "worker")
    if worker["process_group_id"] != worker["pid"] or worker["session_id"] != worker["pid"]:
        raise MetadataError("worker does not identify a dedicated process group and session")

    expected_request = state_dir / f"hermes-worker-{value['instance_id'][:12]}.stop.json"
    if value["control_request"] != str(expected_request):
        raise MetadataError("worker control request is outside the owned state path")
    timeout = value["term_timeout_seconds"]
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not (0.1 <= float(timeout) <= 120.0):
        raise MetadataError("worker termination timeout is invalid")

    caffeinate = value["caffeinate"]
    if not isinstance(caffeinate, dict) or set(caffeinate) != {"enabled", "executable", "sha256"}:
        raise MetadataError("caffeinate identity is malformed")
    if type(caffeinate["enabled"]) is not bool:
        raise MetadataError("caffeinate enabled flag is invalid")
    if caffeinate["enabled"]:
        if not Path(str(caffeinate["executable"])).is_absolute() or not SHA256_RE.fullmatch(str(caffeinate["sha256"])):
            raise MetadataError("caffeinate executable identity is invalid")
    elif caffeinate != {"enabled": False, "executable": "", "sha256": ""}:
        raise MetadataError("disabled caffeinate metadata is invalid")
    return value


def _strict_json(data: bytes, label: str) -> dict[str, Any]:
    if not data or len(data) > MAX_METADATA_BYTES:
        raise MetadataError(f"{label} is empty or too large")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise MetadataError(f"{label} has a duplicate field")
            result[key] = item
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MetadataError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise MetadataError(f"{label} is not a JSON object")
    return value


def _safe_state_dir(path: Path) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise MetadataError("worker state directory must be absolute")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise MetadataError("worker state directory is not a real directory")
    if info.st_uid != os.getuid():
        raise MetadataError("worker state directory is not owned by the current user")
    os.chmod(path, 0o700)
    return path


def _read_private_file(path: Path, label: str) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise MetadataError(f"{label} is not a regular file")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise MetadataError(f"{label} must be owned by the current user with mode 0600")
    if info.st_size > MAX_METADATA_BYTES:
        raise MetadataError(f"{label} is too large")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size > MAX_METADATA_BYTES
        ):
            raise MetadataError(f"{label} changed while it was being opened")
        return os.read(descriptor, MAX_METADATA_BYTES + 1)
    finally:
        os.close(descriptor)


def read_metadata(path: Path, *, state_dir: Path) -> dict[str, Any]:
    return validate_metadata(_strict_json(_read_private_file(path, "worker metadata"), "worker metadata"), state_dir=state_dir)


def _write_new_atomic(path: Path, value: Mapping[str, Any]) -> None:
    payload = _canonical_json(value)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise MetadataError("worker metadata already exists") from exc
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _replace_atomic(path: Path, value: Mapping[str, Any]) -> None:
    payload = _canonical_json(value)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _linux_process_identity(pid: int) -> ProcessIdentity | None:
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        raw = stat_path.read_text(encoding="ascii")
        closing = raw.rfind(")")
        if closing < 0:
            return None
        fields = raw[closing + 2 :].split()
        # fields begins with proc(5) field 3 (state); pgrp/session/starttime are
        # original fields 5, 6, and 22 respectively.
        process_group_id = int(fields[2])
        session_id = int(fields[3])
        start_ticks = fields[19]
        boot_id = (Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip())
        executable = (Path("/proc") / str(pid) / "exe").resolve(strict=True)
        executable_hash = _sha256_file(executable)
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError, IndexError):
        return None
    return ProcessIdentity(
        pid=pid,
        process_start_identity=f"linux:{boot_id}:{start_ticks}",
        executable_path=str(executable),
        executable_sha256=executable_hash,
        process_group_id=process_group_id,
        session_id=session_id,
    )


class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _darwin_process_identity(pid: int) -> ProcessIdentity | None:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        info = _ProcBSDInfo()
        result = libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if result != ctypes.sizeof(info) or info.pbi_pid != pid:
            return None
        buffer = ctypes.create_string_buffer(4096)
        length = libproc.proc_pidpath(pid, buffer, len(buffer))
        if length <= 0:
            return None
        executable = Path(os.fsdecode(buffer.value)).resolve(strict=True)
        return ProcessIdentity(
            pid=pid,
            process_start_identity=f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}",
            executable_path=str(executable),
            executable_sha256=_sha256_file(executable),
            process_group_id=os.getpgid(pid),
            session_id=os.getsid(pid),
        )
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError):
        return None


def inspect_process(pid: int) -> ProcessIdentity | None:
    if type(pid) is not int or pid <= 1:
        return None
    system = platform.system()
    if system == "Linux":
        return _linux_process_identity(pid)
    if system == "Darwin":
        return _darwin_process_identity(pid)
    raise SupervisorError(f"kernel process-start identity is unsupported on {system}")


def _process_matches(expected: Mapping[str, Any]) -> bool:
    observed = inspect_process(int(expected["pid"]))
    if observed is None:
        return False
    # PID reuse is established with the kernel-provided process start identity,
    # not with an executable pathname.  Script interpreters and the macOS Python
    # launcher can legitimately exec after process creation while retaining the
    # same owned process instance, group, and session.  The exact executable
    # observed at start remains recorded as command evidence, but treating an
    # in-place exec as PID reuse would prevent the supervisor from safely
    # stopping its own process group.
    return (
        observed.pid == expected["pid"]
        and observed.process_start_identity == expected["process_start_identity"]
        and observed.process_group_id == expected["process_group_id"]
        and observed.session_id == expected["session_id"]
    )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _list_pids() -> list[int]:
    if platform.system() == "Linux":
        return [int(item.name) for item in Path("/proc").iterdir() if item.name.isdigit()]
    if platform.system() == "Darwin":
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        count = max(1, int(libproc.proc_listallpids(None, 0)))
        array = (ctypes.c_int * (count * 2))()
        found = int(libproc.proc_listallpids(array, ctypes.sizeof(array)))
        return [int(array[index]) for index in range(max(0, found)) if int(array[index]) > 1]
    raise SupervisorError("cannot enumerate a process group without a kernel process API")


def _group_members(process_group_id: int, session_id: int) -> dict[tuple[int, str], ProcessIdentity]:
    members: dict[tuple[int, str], ProcessIdentity] = {}
    for pid in _list_pids():
        try:
            if os.getpgid(pid) != process_group_id or os.getsid(pid) != session_id:
                continue
        except (ProcessLookupError, PermissionError):
            continue
        identity = inspect_process(pid)
        if identity and identity.process_group_id == process_group_id and identity.session_id == session_id:
            members[(identity.pid, identity.process_start_identity)] = identity
    return members


def _group_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_for_group_exit(process_group_id: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if not _group_alive(process_group_id):
            return True
        time.sleep(0.02)
    return not _group_alive(process_group_id)


def _owned_group_or_raise(
    worker: Mapping[str, Any],
    known_members: dict[tuple[int, str], ProcessIdentity],
) -> dict[tuple[int, str], ProcessIdentity]:
    process_group_id = int(worker["process_group_id"])
    session_id = int(worker["session_id"])
    current = _group_members(process_group_id, session_id)
    if not current:
        raise OwnershipError("worker process group is no longer present")
    if not (set(current) & set(known_members)):
        raise OwnershipError("worker process group no longer contains a process with the recorded start identity")
    return current


def terminate_owned_process_group(
    process: subprocess.Popen[Any] | None,
    worker: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """TERM then, if needed, KILL one reverified dedicated worker group."""

    process_group_id = int(worker["process_group_id"])
    session_id = int(worker["session_id"])
    known_members = _group_members(process_group_id, session_id)
    if not known_members:
        if process is not None:
            with contextlib.suppress(Exception):
                process.wait(timeout=0)
        return {"already_finished": True, "sigterm_sent": False, "sigkill_sent": False}
    if not _process_matches(worker):
        # A completed group leader is acceptable only when the supervising
        # Popen has already reaped that exact child and a recorded descendant
        # still anchors the otherwise unjoinable session.
        if process is None or process.pid != int(worker["pid"]) or process.poll() is None:
            raise OwnershipError("worker PID was reused or belongs to an unrelated process")

    os.killpg(process_group_id, signal.SIGTERM)
    sigkill_sent = False
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and _group_alive(process_group_id):
        # Retain identities of descendants observed inside the owned session.
        current = _group_members(process_group_id, session_id)
        if set(current) & set(known_members):
            known_members.update(current)
        if process is not None:
            process.poll()
        time.sleep(0.02)

    if _group_alive(process_group_id):
        current = _owned_group_or_raise(worker, known_members)
        known_members.update(current)
        os.killpg(process_group_id, signal.SIGKILL)
        sigkill_sent = True
        if process is not None:
            try:
                process.wait(timeout=max(2.0, timeout_seconds))
            except subprocess.TimeoutExpired as exc:
                raise SupervisorError("worker child could not be reaped after SIGKILL") from exc
        if not _wait_for_group_exit(process_group_id, time.monotonic() + max(2.0, timeout_seconds)):
            raise SupervisorError("owned worker process group did not exit after SIGKILL")

    if process is not None:
        try:
            process.wait(timeout=max(2.0, timeout_seconds))
        except subprocess.TimeoutExpired as exc:
            raise SupervisorError("worker child could not be reaped") from exc
    return {"already_finished": False, "sigterm_sent": True, "sigkill_sent": sigkill_sent}


@contextlib.contextmanager
def _lifecycle_lock(state_dir: Path) -> Iterator[None]:
    lock_path = state_dir / "hermes-worker.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise MetadataError("worker lifecycle lock is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _default_state_dir() -> Path:
    explicit = os.environ.get("WPI_COUNCIL_WORKER_STATE_DIR")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "wpi-council"
    return Path.home() / ".local" / "state" / "wpi-council"


def _python_identity() -> dict[str, str]:
    if sys.version_info[:2] != (3, 12):
        raise SupervisorError("Hermes worker supervision requires the pinned operator Python 3.12")
    # Preserve the venv entry point exactly.  Resolving its symlink to the base
    # framework binary would discard the pinned environment when launching the
    # worker in a new terminal/session.
    executable = Path(sys.executable)
    if not executable.is_absolute() or not executable.is_file():
        raise SupervisorError("operator Python executable is not an absolute usable file")
    return {
        "path": str(executable),
        "version": platform.python_version(),
        "sha256": _sha256_file(executable),
    }


def _encode_argv(arguments: Sequence[str]) -> str:
    value = _validate_argv(list(arguments), "worker command")
    return base64.urlsafe_b64encode(_canonical_json(value)).decode("ascii")


def _decode_argv(value: str) -> list[str]:
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise SupervisorError("internal worker command encoding is invalid") from exc
    try:
        return _validate_argv(parsed, "worker command")
    except MetadataError as exc:
        raise SupervisorError(str(exc)) from exc


def _open_log(path: Path) -> int:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            raise MetadataError("worker log path is unsafe")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    return descriptor


class WorkerSupervisor:
    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        project_root: Path | None = None,
        term_timeout_seconds: float = DEFAULT_TERM_TIMEOUT_SECONDS,
        caffeinate_mode: str = "auto",
        caffeinate_path: Path | None = None,
    ):
        self.state_dir = _safe_state_dir(state_dir or _default_state_dir())
        self.project_root = (project_root or Path(__file__).resolve().parents[2]).resolve(strict=True)
        if not (0.1 <= float(term_timeout_seconds) <= 120.0):
            raise SupervisorError("termination timeout must be between 0.1 and 120 seconds")
        if caffeinate_mode not in {"auto", "on", "off"}:
            raise SupervisorError("caffeinate mode is invalid")
        self.term_timeout_seconds = float(term_timeout_seconds)
        self.caffeinate_mode = caffeinate_mode
        self.caffeinate_path = caffeinate_path
        self.metadata_path = self.state_dir / "hermes-worker.json"
        self.receipt_path = self.state_dir / "hermes-worker-last.json"
        self.legacy_pid_path = self.state_dir / "hermes-worker.pid"

    def _existing(self) -> dict[str, Any] | None:
        try:
            return read_metadata(self.metadata_path, state_dir=self.state_dir)
        except FileNotFoundError:
            return None

    def _remove_stale(self, metadata: Mapping[str, Any]) -> None:
        request_path = Path(str(metadata["control_request"]))
        with contextlib.suppress(FileNotFoundError):
            request_path.unlink()
        current = self._existing()
        if current and current["instance_id"] == metadata["instance_id"]:
            self.metadata_path.unlink()

    def _classify_existing(self, metadata: Mapping[str, Any]) -> str:
        supervisor = metadata["supervisor"]
        worker = metadata["worker"]
        if _process_matches(supervisor):
            if _process_matches(worker):
                return "active"
            if _pid_exists(int(worker["pid"])) or _group_alive(int(worker["process_group_id"])):
                return "worker_pid_reused"
            # The direct worker can finish just before its owner writes the
            # receipt and removes metadata.  The still-exact supervisor remains
            # the only process allowed to act during that bounded cleanup race.
            return "active"
        if _pid_exists(int(supervisor["pid"])):
            return "supervisor_pid_reused"
        if _process_matches(worker) or _group_alive(int(worker["process_group_id"])):
            return "orphaned_worker"
        if _pid_exists(int(worker["pid"])):
            return "worker_pid_reused"
        return "finished"

    def _receipt(self) -> dict[str, Any] | None:
        if not self.receipt_path.exists():
            return None
        receipt = _strict_json(_read_private_file(self.receipt_path, "worker receipt"), "worker receipt")
        if set(receipt) != {
            "schema_version", "task_id", "instance_id", "finished_at", "exit_code", "reason",
            "sigterm_sent", "sigkill_sent", "child_reaped",
        }:
            raise MetadataError("worker receipt is malformed")
        if receipt["schema_version"] != RECEIPT_SCHEMA:
            raise MetadataError("worker receipt schema is incompatible")
        if not isinstance(receipt["instance_id"], str) or not INSTANCE_RE.fullmatch(receipt["instance_id"]):
            raise MetadataError("worker receipt instance is invalid")
        if not isinstance(receipt["task_id"], str) or not TASK_ID_RE.fullmatch(receipt["task_id"]):
            raise MetadataError("worker receipt task is invalid")
        _validate_timestamp(receipt["finished_at"], "worker finish time")
        if receipt["exit_code"] is not None and type(receipt["exit_code"]) is not int:
            raise MetadataError("worker receipt exit code is invalid")
        if not isinstance(receipt["reason"], str) or not receipt["reason"]:
            raise MetadataError("worker receipt reason is invalid")
        if any(type(receipt[key]) is not bool for key in ("sigterm_sent", "sigkill_sent", "child_reaped")):
            raise MetadataError("worker receipt shutdown flags are invalid")
        return receipt

    def start(self, task_id: str, *, worker_argv: Sequence[str] | None = None) -> dict[str, Any]:
        if not TASK_ID_RE.fullmatch(task_id):
            raise SupervisorError("unsafe task ID")
        python_identity = _python_identity()
        logical = list(worker_argv or [
            python_identity["path"],
            "-m",
            MODULE,
            "_worker",
            task_id,
        ])
        _validate_argv(logical, "worker command")
        if self.legacy_pid_path.exists() or self.legacy_pid_path.is_symlink():
            raise MetadataError("legacy PID-only worker metadata exists; audit it before starting the safe supervisor")

        with _lifecycle_lock(self.state_dir):
            existing = self._existing()
            previous_finished = False
            if existing:
                receipt = self._receipt()
                classification = (
                    "finished"
                    if receipt is not None and receipt["instance_id"] == existing["instance_id"]
                    else self._classify_existing(existing)
                )
                if classification == "active":
                    raise SupervisorError(
                        f"Hermes worker instance {existing['instance_id']} is already running task {existing['task_id']}"
                    )
                if classification in {"supervisor_pid_reused", "worker_pid_reused"}:
                    raise OwnershipError("recorded PID was reused by an unrelated process; metadata was retained")
                if classification == "orphaned_worker":
                    raise OwnershipError("an owned worker group remains without its supervisor; stop it before restart")
                self._remove_stale(existing)
                previous_finished = True

            instance_id = secrets.token_hex(16)
            read_fd, write_fd = os.pipe()
            os.set_inheritable(write_fd, True)
            log_descriptor = _open_log(self.state_dir / "hermes-worker.log")
            command = [
                python_identity["path"],
                "-m",
                MODULE,
                "_serve",
                "--task-id",
                task_id,
                "--instance-id",
                instance_id,
                "--state-dir",
                str(self.state_dir),
                "--project-root",
                str(self.project_root),
                "--term-timeout",
                str(self.term_timeout_seconds),
                "--caffeinate-mode",
                self.caffeinate_mode,
                "--ready-fd",
                str(write_fd),
                "--worker-argv",
                _encode_argv(logical),
            ]
            if self.caffeinate_path is not None:
                command.extend(["--caffeinate-path", str(self.caffeinate_path)])
            environment = dict(os.environ)
            environment["PYTHONUNBUFFERED"] = "1"
            try:
                process = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    stdin=subprocess.DEVNULL,
                    stdout=log_descriptor,
                    stderr=log_descriptor,
                    env=environment,
                    shell=False,
                    close_fds=True,
                    pass_fds=(write_fd,),
                    start_new_session=True,
                )
            finally:
                os.close(write_fd)
                os.close(log_descriptor)

            try:
                ready, _, _ = select.select([read_fd], [], [], READY_TIMEOUT_SECONDS)
                if not ready:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(process.pid, signal.SIGTERM)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=2)
                    raise SupervisorError("background worker did not publish safe metadata before the startup deadline")
                response = os.read(read_fd, MAX_METADATA_BYTES).decode("utf-8", errors="replace").strip()
            finally:
                os.close(read_fd)
            if not response.startswith("READY "):
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)
                raise SupervisorError(response.removeprefix("ERROR ") or "background worker startup failed")
            metadata = self._existing()
            if metadata is None or metadata["instance_id"] != instance_id:
                raise SupervisorError("background worker readiness did not match its atomic metadata")
            return {
                "status": "started",
                "task_id": task_id,
                "instance_id": instance_id,
                "supervisor_pid": metadata["supervisor"]["pid"],
                "worker_pid": metadata["worker"]["pid"],
                "process_group_id": metadata["worker"]["process_group_id"],
                "operator_python": metadata["operator_python"],
                "caffeinate": metadata["caffeinate"],
                "previous_worker_already_finished": previous_finished,
                "log_path": str(self.state_dir / "hermes-worker.log"),
            }

    def _last_status(self) -> dict[str, Any]:
        receipt = self._receipt()
        if receipt is None:
            return {"status": "not_running", "lease_recovery": "remote task state remains authoritative"}
        return {
            "status": "already_finished",
            "task_id": receipt["task_id"],
            "instance_id": receipt["instance_id"],
            "exit_code": receipt["exit_code"],
            "reason": receipt["reason"],
            "lease_recovery": "remote task state remains authoritative",
        }

    def stop(self) -> dict[str, Any]:
        if self.legacy_pid_path.exists() or self.legacy_pid_path.is_symlink():
            raise MetadataError("legacy PID-only metadata is unsafe and will not be signalled")
        with _lifecycle_lock(self.state_dir):
            metadata = self._existing()
            if metadata is None:
                return self._last_status()
            receipt = self._receipt()
            if receipt is not None and receipt["instance_id"] == metadata["instance_id"]:
                self._remove_stale(metadata)
                return {
                    "status": "already_finished",
                    "task_id": metadata["task_id"],
                    "instance_id": metadata["instance_id"],
                    "exit_code": receipt["exit_code"],
                    "reason": receipt["reason"],
                    "lease_recovery": "remote task state remains authoritative",
                }
            classification = self._classify_existing(metadata)
            if classification in {"supervisor_pid_reused", "worker_pid_reused"}:
                raise OwnershipError("recorded PID was reused by an unrelated process; refusing to send a signal")
            if classification == "finished":
                self._remove_stale(metadata)
                return {
                    "status": "already_finished",
                    "task_id": metadata["task_id"],
                    "instance_id": metadata["instance_id"],
                    "lease_recovery": "remote task state remains authoritative",
                }
            if classification == "orphaned_worker":
                result = terminate_owned_process_group(
                    None,
                    metadata["worker"],
                    timeout_seconds=float(metadata["term_timeout_seconds"]),
                )
                if _group_alive(int(metadata["worker"]["process_group_id"])):
                    raise SupervisorError("orphaned worker group did not shut down")
                self._remove_stale(metadata)
                return {
                    "status": "stopped_orphaned_worker",
                    "task_id": metadata["task_id"],
                    **result,
                    "lease_recovery": "remote task state remains authoritative",
                }

            request_path = Path(str(metadata["control_request"]))
            request = _canonical_json(
                {
                    "schema_version": CONTROL_SCHEMA,
                    "action": "stop",
                    "instance_id": metadata["instance_id"],
                }
            )
            try:
                _write_new_atomic(request_path, _strict_json(request, "worker stop request"))
            except MetadataError:
                # An exact duplicate request is idempotent; any other file is
                # retained and rejected instead of being overwritten.
                existing_request = _strict_json(
                    _read_private_file(request_path, "worker stop request"), "worker stop request"
                )
                if existing_request != _strict_json(request, "worker stop request"):
                    raise OwnershipError("worker stop request path contains an unrelated capability")

            deadline = time.monotonic() + float(metadata["term_timeout_seconds"]) + 5.0
            while self.metadata_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            if self.metadata_path.exists():
                current = self._existing()
                if current is None or current["instance_id"] != metadata["instance_id"]:
                    raise OwnershipError("worker metadata changed during shutdown")
                raise SupervisorError("owning supervisor did not finish verified shutdown")
            receipt = _strict_json(_read_private_file(self.receipt_path, "worker receipt"), "worker receipt")
            if receipt.get("schema_version") != RECEIPT_SCHEMA or receipt.get("instance_id") != metadata["instance_id"]:
                raise SupervisorError("worker shutdown receipt did not match the owned instance")
            with contextlib.suppress(FileNotFoundError):
                request_path.unlink()
            return {
                "status": "stopped",
                "task_id": metadata["task_id"],
                "instance_id": metadata["instance_id"],
                "sigterm_sent": bool(receipt.get("sigterm_sent")),
                "sigkill_sent": bool(receipt.get("sigkill_sent")),
                "child_reaped": bool(receipt.get("child_reaped")),
                "lease_recovery": "remote task state remains authoritative",
            }


def _caffeinate_contract(mode: str, configured: str | None) -> tuple[list[str], dict[str, Any]]:
    enabled = mode == "on" or (mode == "auto" and platform.system() == "Darwin")
    if not enabled:
        return [], {"enabled": False, "executable": "", "sha256": ""}
    path = Path(configured or "/usr/bin/caffeinate")
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise SupervisorError("macOS overnight work requires the absolute executable /usr/bin/caffeinate")
    resolved = path.resolve(strict=True)
    return [str(resolved), "-dimsu"], {
        "enabled": True,
        "executable": str(resolved),
        "sha256": _sha256_file(resolved),
    }


def _send_ready(descriptor: int, message: str) -> None:
    with contextlib.suppress(OSError):
        os.write(descriptor, (message + "\n").encode("utf-8", errors="replace"))
    with contextlib.suppress(OSError):
        os.close(descriptor)


def _serve(arguments: argparse.Namespace) -> int:
    state_dir = _safe_state_dir(Path(arguments.state_dir))
    metadata_path = state_dir / "hermes-worker.json"
    receipt_path = state_dir / "hermes-worker-last.json"
    request_path = state_dir / f"hermes-worker-{arguments.instance_id[:12]}.stop.json"
    process: subprocess.Popen[Any] | None = None
    metadata_written = False
    stop_requested = False
    stop_reason = "normal_completion"
    termination = {"sigterm_sent": False, "sigkill_sent": False}
    child_reaped = False
    exit_code: int | None = None

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested, stop_reason
        stop_requested = True
        stop_reason = "supervisor_signal"

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    previous_umask = os.umask(0o077)
    try:
        if not INSTANCE_RE.fullmatch(arguments.instance_id):
            raise SupervisorError("internal worker instance identity is invalid")
        if not TASK_ID_RE.fullmatch(arguments.task_id):
            raise SupervisorError("internal worker task ID is invalid")
        python_identity = _python_identity()
        logical = _decode_argv(arguments.worker_argv)
        prefix, caffeinate = _caffeinate_contract(arguments.caffeinate_mode, arguments.caffeinate_path)
        effective = prefix + logical

        with contextlib.suppress(FileNotFoundError):
            request_path.unlink()

        process = subprocess.Popen(
            effective,
            cwd=Path(arguments.project_root),
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            env={
                **os.environ,
                "WPI_COUNCIL_SUPERVISED_INSTANCE": arguments.instance_id,
                "WPI_COUNCIL_SLEEP_ASSERTION": "caffeinate" if caffeinate["enabled"] else "supervised",
            },
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
        # macOS may briefly report the framework launcher before Python's
        # in-process app executable is established.  Recording that transient
        # path would create a false PID-reuse alarm later.
        time.sleep(0.1)
        worker_identity = None
        stable_observations = 0
        previous_identity: ProcessIdentity | None = None
        for _ in range(100):
            observed_identity = inspect_process(process.pid)
            if observed_identity is not None and observed_identity == previous_identity:
                stable_observations += 1
            else:
                stable_observations = 1 if observed_identity is not None else 0
            previous_identity = observed_identity
            if stable_observations >= 3:
                worker_identity = observed_identity
                break
            if process.poll() is not None:
                break
            time.sleep(0.02)
        if worker_identity is None:
            raise SupervisorError("worker exited before its kernel start identity could be recorded")
        if worker_identity.process_group_id != process.pid or worker_identity.session_id != process.pid:
            raise SupervisorError("worker did not start in its own process group and session")
        supervisor_identity = inspect_process(os.getpid())
        if supervisor_identity is None:
            raise SupervisorError("supervisor kernel start identity is unavailable")
        metadata = {
            "schema_version": METADATA_SCHEMA,
            "task_id": arguments.task_id,
            "instance_id": arguments.instance_id,
            "created_at": _utc_now(),
            "operator_python": python_identity,
            "command": {
                "requested_argv": logical,
                "effective_argv": effective,
                "requested_sha256": _digest_json(logical),
                "effective_sha256": _digest_json(effective),
            },
            "supervisor": asdict(supervisor_identity),
            "worker": asdict(worker_identity),
            "control_request": str(request_path),
            "term_timeout_seconds": float(arguments.term_timeout),
            "caffeinate": caffeinate,
        }
        validate_metadata(metadata, state_dir=state_dir)
        _write_new_atomic(metadata_path, metadata)
        metadata_written = True
        _send_ready(arguments.ready_fd, f"READY {arguments.instance_id}")
        arguments.ready_fd = -1

        while True:
            if process.poll() is not None:
                exit_code = process.returncode
                child_reaped = True
                if _group_alive(worker_identity.process_group_id):
                    termination = terminate_owned_process_group(
                        process,
                        metadata["worker"],
                        timeout_seconds=float(arguments.term_timeout),
                    )
                break
            if stop_requested:
                termination = terminate_owned_process_group(
                    process,
                    metadata["worker"],
                    timeout_seconds=float(arguments.term_timeout),
                )
                exit_code = process.returncode
                child_reaped = process.poll() is not None
                break
            if not request_path.exists():
                time.sleep(0.05)
                continue
            request = _strict_json(
                _read_private_file(request_path, "worker control request"), "worker control request"
            )
            if set(request) != {"schema_version", "action", "instance_id"}:
                raise OwnershipError("worker control request is malformed")
            if request != {
                "schema_version": CONTROL_SCHEMA,
                "action": "stop",
                "instance_id": arguments.instance_id,
            }:
                raise OwnershipError("worker control request does not own this instance")
            stop_reason = "requested_stop"
            termination = terminate_owned_process_group(
                process,
                metadata["worker"],
                timeout_seconds=float(arguments.term_timeout),
            )
            exit_code = process.returncode
            child_reaped = process.poll() is not None
            break
    except Exception as exc:
        if arguments.ready_fd >= 0:
            _send_ready(arguments.ready_fd, f"ERROR {exc}")
            arguments.ready_fd = -1
        stop_reason = f"supervisor_error:{type(exc).__name__}"
        if process is not None and process.poll() is None:
            identity = inspect_process(process.pid)
            if identity is not None and identity.process_group_id == process.pid and identity.session_id == process.pid:
                with contextlib.suppress(Exception):
                    termination = terminate_owned_process_group(
                        process,
                        asdict(identity),
                        timeout_seconds=float(arguments.term_timeout),
                    )
        if process is not None:
            exit_code = process.poll()
            child_reaped = exit_code is not None
        print(f"Hermes worker supervisor blocked: {exc}", file=sys.stderr)
        return_code = 2
    else:
        return_code = 0 if stop_reason == "requested_stop" else int(exit_code or 0)
    finally:
        os.umask(previous_umask)
        if arguments.ready_fd >= 0:
            _send_ready(arguments.ready_fd, "ERROR worker supervisor exited before readiness")
        with contextlib.suppress(FileNotFoundError):
            request_path.unlink()
        safe_shutdown = process is None or (
            process.poll() is not None and not _group_alive(process.pid)
        )
        if metadata_written and safe_shutdown:
            try:
                current = read_metadata(metadata_path, state_dir=state_dir)
            except (FileNotFoundError, MetadataError):
                current = None
            receipt = {
                "schema_version": RECEIPT_SCHEMA,
                "task_id": arguments.task_id,
                "instance_id": arguments.instance_id,
                "finished_at": _utc_now(),
                "exit_code": exit_code,
                "reason": stop_reason,
                "sigterm_sent": bool(termination.get("sigterm_sent")),
                "sigkill_sent": bool(termination.get("sigkill_sent")),
                "child_reaped": child_reaped,
            }
            _replace_atomic(receipt_path, receipt)
            if current is not None and current["instance_id"] == arguments.instance_id:
                metadata_path.unlink()
    return return_code


def _worker(task_id: str) -> int:
    """Run doctor, claim, and the Hermes attempt in the same pinned runtime."""

    _python_identity()
    from .cli import main as council_main

    for command in (
        ["doctor", "--worker"],
        ["claim", task_id],
        ["run", task_id, "--backend", "hermes", "--overnight"],
    ):
        result = council_main(command)
        if result:
            return result
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wpi-council-worker")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("task_id")
    start.add_argument("--state-dir")
    start.add_argument("--term-timeout", type=float, default=DEFAULT_TERM_TIMEOUT_SECONDS)
    stop = commands.add_parser("stop")
    stop.add_argument("--state-dir")

    serve = commands.add_parser("_serve")
    serve.add_argument("--task-id", required=True)
    serve.add_argument("--instance-id", required=True)
    serve.add_argument("--state-dir", required=True)
    serve.add_argument("--project-root", required=True)
    serve.add_argument("--term-timeout", required=True, type=float)
    serve.add_argument("--caffeinate-mode", choices=("auto", "on", "off"), required=True)
    serve.add_argument("--caffeinate-path")
    serve.add_argument("--ready-fd", required=True, type=int)
    serve.add_argument("--worker-argv", required=True)

    worker = commands.add_parser("_worker")
    worker.add_argument("task_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "start":
            result = WorkerSupervisor(
                state_dir=Path(arguments.state_dir) if arguments.state_dir else None,
                term_timeout_seconds=arguments.term_timeout,
            ).start(arguments.task_id)
        elif arguments.command == "stop":
            result = WorkerSupervisor(
                state_dir=Path(arguments.state_dir) if arguments.state_dir else None,
            ).stop()
        elif arguments.command == "_serve":
            return _serve(arguments)
        elif arguments.command == "_worker":
            return _worker(arguments.task_id)
        else:
            raise SupervisorError("unknown worker supervisor command")
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except (MetadataError, OwnershipError, SupervisorError, OSError, ValueError) as exc:
        print(f"Hermes worker blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
