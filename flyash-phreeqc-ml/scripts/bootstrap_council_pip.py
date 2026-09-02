"""Verify or bootstrap interpreter-local pip for the Council operator."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


REQUIRED_PYTHON = (3, 12)
BOOTSTRAP_FAILURE = (
    "The selected Python 3.12 environment has no usable local pip and its "
    "standard-library ensurepip bootstrap failed. Repair ensurepip for this exact "
    "interpreter, then rerun the installer."
)
INVALID_PIP = (
    "The selected Python 3.12 environment resolves an unusable or non-local pip. "
    "Repair pip inside this exact environment without using system or user site, "
    "then rerun the installer."
)


def _subprocess_environment() -> dict[str, str]:
    """Exclude ambient Python, user-site, and pip configuration authority."""

    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("PIP_") or name in {
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
        }:
            environment.pop(name, None)
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return environment


def _run_interpreter(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", *arguments],
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )


def _pip_identity() -> tuple[str, dict[str, Any] | None]:
    version_check = _run_interpreter("-m", "pip", "--version")
    if version_check.returncode:
        presence_check = _run_interpreter(
            "-c",
            "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('pip') is None else 1)",
        )
        return ("missing", None) if presence_check.returncode == 0 else ("invalid", None)

    identity_check = _run_interpreter(
        "-c",
        """from importlib import metadata
import json
from pathlib import Path
import pip
import site
import sys

prefix = Path(sys.prefix).resolve()
module_path = Path(pip.__file__).resolve()
distribution_root = Path(metadata.distribution("pip").locate_file("")).resolve()
user_site = Path(site.getusersitepackages()).resolve()

def beneath(path, root):
    return path == root or root in path.parents

if sys.version_info[:2] != (3, 12) or sys.prefix == sys.base_prefix:
    raise SystemExit(1)
if not beneath(module_path, prefix) or not beneath(distribution_root, prefix):
    raise SystemExit(1)
if beneath(module_path, user_site) or beneath(distribution_root, user_site):
    raise SystemExit(1)

print(json.dumps({
    "environment_prefix": str(prefix),
    "pip_distribution_root": str(distribution_root),
    "pip_module": str(module_path),
    "pip_version": metadata.version("pip"),
    "python_executable": str(Path(sys.executable).absolute()),
}, sort_keys=True, separators=(",", ":")))
""",
    )
    if identity_check.returncode:
        return "invalid", None
    try:
        identity = json.loads(identity_check.stdout)
    except (json.JSONDecodeError, TypeError):
        return "invalid", None
    if not isinstance(identity, dict):
        return "invalid", None
    return "valid", identity


def _ensurepip_identity() -> dict[str, str] | None:
    identity_check = _run_interpreter(
        "-c",
        """import ensurepip
import json
from pathlib import Path
import sysconfig

stdlib_root = Path(sysconfig.get_path("stdlib")).resolve()
module_path = Path(ensurepip.__file__).resolve()
if module_path != stdlib_root and stdlib_root not in module_path.parents:
    raise SystemExit(1)
print(json.dumps({
    "ensurepip_module": str(module_path),
    "stdlib_root": str(stdlib_root),
}, sort_keys=True, separators=(",", ":")))
""",
    )
    if identity_check.returncode:
        return None
    try:
        identity = json.loads(identity_check.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(identity, dict) or not all(
        isinstance(identity.get(name), str) for name in ("ensurepip_module", "stdlib_root")
    ):
        return None
    return identity


def main() -> int:
    arguments = sys.argv[1:]
    if arguments not in ([], ["--verify-only"]):
        print("usage: bootstrap_council_pip.py [--verify-only]", file=sys.stderr)
        return 2
    verify_only = arguments == ["--verify-only"]

    if sys.version_info[:2] != REQUIRED_PYTHON:
        print("The Council operator requires Python exactly 3.12.", file=sys.stderr)
        return 1
    if sys.prefix == sys.base_prefix:
        print("The selected Council operator Python must be a virtual environment.", file=sys.stderr)
        return 1

    pip_state, identity = _pip_identity()
    bootstrapped = False
    if pip_state == "invalid":
        print(INVALID_PIP, file=sys.stderr)
        return 1
    if pip_state == "missing":
        if verify_only:
            print(INVALID_PIP, file=sys.stderr)
            return 1
        ensurepip_identity = _ensurepip_identity()
        if ensurepip_identity is None:
            print(BOOTSTRAP_FAILURE, file=sys.stderr)
            return 1
        ensured = _run_interpreter("-m", "ensurepip", "--default-pip")
        if ensured.returncode:
            print(BOOTSTRAP_FAILURE, file=sys.stderr)
            return 1
        bootstrapped = True
        pip_state, identity = _pip_identity()
        if pip_state != "valid" or identity is None:
            print(BOOTSTRAP_FAILURE, file=sys.stderr)
            return 1
        identity.update(ensurepip_identity)

    if identity is None:
        print(INVALID_PIP, file=sys.stderr)
        return 1
    identity["bootstrapped"] = bootstrapped
    print(json.dumps(identity, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
