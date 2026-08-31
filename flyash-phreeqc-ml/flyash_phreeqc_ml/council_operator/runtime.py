"""Pinned Python identity used by every trusted operator test command."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Iterable


REQUIRED_PYTHON = (3, 12)
DEPENDENCY_CONTRACT_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "constraints-py312.txt",
    "scripts/validate_dependency_lock.py",
)


class RuntimeIdentityError(RuntimeError):
    """The running operator is not using its verified Python 3.12 runtime."""


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeIdentityError("operator Python executable cannot be read") from exc


def _operator_project_root() -> Path:
    root = Path(__file__).resolve().parents[2]
    if not root.is_absolute() or not root.is_dir():
        raise RuntimeIdentityError("operator dependency contract root is unavailable")
    return root


def _dependency_contract_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in DEPENDENCY_CONTRACT_FILES:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeIdentityError("operator dependency contract is incomplete")
        encoded = relative.encode("utf-8")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise RuntimeIdentityError("operator dependency contract cannot be read") from exc
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


@dataclass(frozen=True)
class PythonRuntimeIdentity:
    """Identity of the interpreter that loaded the trusted operator package.

    The executable path deliberately preserves a virtual-environment launcher path
    instead of resolving its symlink to a system interpreter. Executing that exact
    path is what selects the approved virtual environment and its pinned packages.
    """

    executable: str
    version: str
    executable_sha256: str
    environment_prefix: str
    base_prefix: str
    implementation: str
    virtual_environment: bool
    dependency_contract_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "executable": self.executable,
            "version": self.version,
            "executable_sha256": self.executable_sha256,
            "environment_prefix": self.environment_prefix,
            "base_prefix": self.base_prefix,
            "implementation": self.implementation,
            "virtual_environment": self.virtual_environment,
            "dependency_contract_sha256": self.dependency_contract_sha256,
        }

    def verify_unchanged(self) -> None:
        path = Path(self.executable)
        if not path.is_absolute() or not path.exists() or not path.is_file():
            raise RuntimeIdentityError("operator Python executable identity is unavailable")
        if _sha256(path) != self.executable_sha256:
            raise RuntimeIdentityError("operator Python executable identity changed")

    def verify_pinned_environment(self) -> None:
        """Revalidate exact dependency declarations and installed versions."""

        self.verify_unchanged()
        root = _operator_project_root()
        if _dependency_contract_sha256(root) != self.dependency_contract_sha256:
            raise RuntimeIdentityError("operator dependency contract changed")
        validator = root / "scripts" / "validate_dependency_lock.py"
        allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        environment.update(
            {
                "PIP_CONFIG_FILE": os.devnull,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
            }
        )
        try:
            completed = subprocess.run(
                [self.executable, str(validator), "--installed"],
                cwd=root,
                env=environment,
                capture_output=True,
                shell=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeIdentityError("operator dependency validation could not run") from exc
        if completed.returncode:
            raise RuntimeIdentityError("operator pinned dependency environment is incompatible")


def current_python_identity() -> PythonRuntimeIdentity:
    """Return and validate the exact interpreter currently running the operator."""

    if sys.version_info[:2] != REQUIRED_PYTHON:
        raise RuntimeIdentityError("the trusted operator requires Python exactly 3.12")
    executable = Path(os.path.abspath(sys.executable))
    if not executable.is_absolute() or not executable.exists() or not executable.is_file():
        raise RuntimeIdentityError("operator Python executable must be an existing absolute path")
    return PythonRuntimeIdentity(
        executable=str(executable),
        version=platform.python_version(),
        executable_sha256=_sha256(executable),
        environment_prefix=os.path.abspath(sys.prefix),
        base_prefix=os.path.abspath(sys.base_prefix),
        implementation=platform.python_implementation(),
        virtual_environment=sys.prefix != sys.base_prefix,
        dependency_contract_sha256=_dependency_contract_sha256(_operator_project_root()),
    )


def effective_test_command(
    requested: Iterable[str],
    identity: PythonRuntimeIdentity,
) -> tuple[str, ...]:
    """Pin ``python``/``python3`` requests to the operator's exact runtime."""

    arguments = tuple(requested)
    if not arguments:
        raise RuntimeIdentityError("trusted test command is empty")
    identity.verify_unchanged()
    if arguments[0] in {"python", "python3"}:
        return (identity.executable, *arguments[1:])
    return arguments
