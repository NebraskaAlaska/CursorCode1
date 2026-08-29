#!/usr/bin/env python3
"""Run the pinned Phase 4 dependency-vulnerability audit without auto-fixing.

The audit tool is deliberately separate from the application dependency set.
CI installs the exact version below in an isolated job.  A missing or different
tool version is reported as unavailable and is never treated as a clean scan.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PIP_AUDIT_VERSION = "2.10.1"


def main() -> int:
    try:
        installed = metadata.version("pip-audit")
    except metadata.PackageNotFoundError:
        print(
            "dependency vulnerability audit unavailable: "
            f"pip-audit=={PIP_AUDIT_VERSION} is not installed",
            file=sys.stderr,
        )
        return 2
    if installed != PIP_AUDIT_VERSION:
        print(
            "dependency vulnerability audit unavailable: "
            f"pip-audit version {installed} does not match pinned {PIP_AUDIT_VERSION}",
            file=sys.stderr,
        )
        return 2

    # Audit every cross-platform pin, including dependencies whose environment
    # markers are inactive on the CI runner. The temporary file contains package
    # names and versions only; it contains no credentials or application data.
    locked = []
    for raw in (ROOT / "constraints-py312.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            locked.append(line.split(";", 1)[0].strip())
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt") as audit_lock:
        audit_lock.write("\n".join(locked) + "\n")
        audit_lock.flush()
        command = [
            sys.executable,
            "-m",
            "pip_audit",
            "--requirement",
            audit_lock.name,
            "--no-deps",
            "--disable-pip",
            "--strict",
            "--progress-spinner",
            "off",
            "--desc",
            "off",
        ]
        completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode:
        print(
            "dependency vulnerability audit did not pass; "
            "the pip-audit result above is authoritative",
            file=sys.stderr,
        )
        return completed.returncode
    print(
        f"dependency vulnerability audit passed with pip-audit=={PIP_AUDIT_VERSION}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
