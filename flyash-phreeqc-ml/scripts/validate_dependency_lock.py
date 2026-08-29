#!/usr/bin/env python3
"""Validate exact agreement among requirements, pyproject, and constraints."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import platform
import re
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*;\s*(.+))?$")
SIMPLE_MARKER = re.compile(
    r'^(sys_platform|platform_system)\s*(==|!=)\s*["\']([^"\']+)["\']$'
)


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lines(path: Path, *, recurse: bool = True) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ")):
            if not recurse:
                raise ValueError(f"{path.name}:{line_number}: include not allowed")
            included = line.split(maxsplit=1)[1]
            pins.update(parse_lines(path.parent / included))
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise ValueError(f"{path.name}:{line_number}: dependency is not exactly pinned")
        name, version = canonical(match.group(1)), match.group(2)
        if name in pins and pins[name] != version:
            raise ValueError(f"{path.name}:{line_number}: conflicting pin for {name}")
        pins[name] = version
    return pins


def active_constraint_names(path: Path) -> set[str]:
    """Return constraints selected for this host, validating supported markers."""
    active: set[str] = set()
    environment = {"sys_platform": sys.platform, "platform_system": platform.system()}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise ValueError(f"{path.name}:{line_number}: dependency is not exactly pinned")
        name, _version, marker = match.groups()
        if marker is None:
            active.add(canonical(name))
            continue
        marker_match = SIMPLE_MARKER.fullmatch(marker.strip())
        if not marker_match:
            raise ValueError(f"{path.name}:{line_number}: unsupported environment marker")
        variable, operator, expected = marker_match.groups()
        equal = environment[variable] == expected
        if equal if operator == "==" else not equal:
            active.add(canonical(name))
    return active


def parse_pyproject() -> tuple[dict[str, str], dict[str, str], dict[str, str], str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]

    def pins(values: list[str], label: str) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for value in values:
            match = PIN.fullmatch(value)
            if not match:
                raise ValueError(f"pyproject {label}: dependency is not exactly pinned: {value}")
            parsed[canonical(match.group(1))] = match.group(2)
        return parsed

    runtime = pins(project["dependencies"], "runtime")
    dev = pins(project.get("optional-dependencies", {}).get("dev", []), "dev")
    build = pins(data["build-system"]["requires"], "build-system")
    return runtime, dev, build, project["requires-python"]


def compare(label: str, expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for name in sorted(expected.keys() | actual.keys()):
        if expected.get(name) != actual.get(name):
            errors.append(
                f"{label}: {name}: expected {expected.get(name)!r}, found {actual.get(name)!r}"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--installed", action="store_true", help="also compare the active Python environment"
    )
    args = parser.parse_args()
    errors: list[str] = []
    try:
        requirements = parse_lines(ROOT / "requirements.txt")
        dev_requirements = parse_lines(ROOT / "requirements-dev.txt")
        constraints = parse_lines(ROOT / "constraints-py312.txt", recurse=False)
        active_constraints = active_constraint_names(ROOT / "constraints-py312.txt")
        project, project_dev, project_build, python_range = parse_pyproject()
    except (KeyError, OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"dependency lock validation failed: {exc}", file=sys.stderr)
        return 1

    errors.extend(compare("requirements vs pyproject", requirements, project))
    errors.extend(compare("requirements-dev vs pyproject dev", {**requirements, **project_dev}, dev_requirements))
    for name, version in {**dev_requirements, **project_build}.items():
        if constraints.get(name) != version:
            errors.append(f"constraints: {name} must be pinned to {version}")
    if python_range != ">=3.12,<3.13":
        errors.append("pyproject requires-python must be exactly >=3.12,<3.13")

    if args.installed:
        for name in active_constraints:
            version = constraints[name]
            try:
                found = metadata.version(name)
            except metadata.PackageNotFoundError:
                errors.append(f"installed: {name} is missing")
            else:
                if found != version:
                    errors.append(f"installed: {name} is {found}, expected {version}")
        if not errors:
            check = subprocess.run(
                [sys.executable, "-m", "pip", "check"], cwd=ROOT, check=False
            )
            if check.returncode:
                errors.append("installed: pip check failed")

    if errors:
        print("dependency lock validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    print(
        f"dependency lock validation passed ({len(requirements)} runtime, "
        f"{len(dev_requirements) - len(requirements)} development pins)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
