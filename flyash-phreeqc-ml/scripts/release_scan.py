#!/usr/bin/env python3
"""Fail-closed release and staged-content scanner.

The same policy is used by the pre-commit hook and CI. Findings identify only
paths and rule names; file contents and possible secret values are never printed.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = PROJECT_ROOT / "release" / "scan-policy.json"

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private-key-header", re.compile(br"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(br"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("anthropic-api-key", re.compile(br"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai-api-key", re.compile(br"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("github-token", re.compile(br"\bgh[pousr]_[A-Za-z0-9]{20,}")),
)


def git(*args: str, text: bool = False) -> bytes | str:
    return subprocess.check_output(
        ["git", *args], cwd=PROJECT_ROOT, text=text, stderr=subprocess.DEVNULL
    )


def git_root() -> Path:
    return Path(str(git("rev-parse", "--show-toplevel", text=True)).strip()).resolve()


def nul_paths(raw: bytes) -> list[str]:
    return [part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_policy() -> dict[str, object]:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def path_findings(path: str, policy: dict[str, object]) -> list[str]:
    normalized = "/" + path.replace("\\", "/").lstrip("/")
    lower = normalized.lower()
    findings: list[str] = []
    for suffix in policy["forbidden_suffixes"]:  # type: ignore[index]
        if lower.endswith(str(suffix).lower()):
            findings.append(f"forbidden-suffix:{suffix}")
    for fragment in policy["forbidden_path_fragments"]:  # type: ignore[index]
        if str(fragment).lower() in lower:
            findings.append(f"forbidden-path:{fragment}")
    basename = PurePosixPath(normalized).name
    for pattern in policy["forbidden_basename_patterns"]:  # type: ignore[index]
        if fnmatch.fnmatchcase(basename.lower(), str(pattern).lower()):
            findings.append(f"forbidden-name:{pattern}")
    return findings


def is_reviewed_exception(
    path: str, data: bytes, policy: dict[str, object]
) -> bool:
    exceptions = policy["tracked_baseline_exceptions"]  # type: ignore[index]
    expected = exceptions.get(path)  # type: ignore[union-attr]
    if expected == "ALLOW_TEXT_BASELINE":
        return b"\0" not in data
    return isinstance(expected, str) and sha256(data) == expected


def is_content_exception(path: str, data: bytes, policy: dict[str, object]) -> bool:
    exceptions = policy.get("content_baseline_exceptions", {})
    expected = exceptions.get(path)  # type: ignore[union-attr]
    return isinstance(expected, str) and sha256(data) == expected


def content_findings(data: bytes) -> list[str]:
    if b"\0" in data or len(data) > 8 * 1024 * 1024:
        return []
    findings: list[str] = []
    for name, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(data):
            token = match.group(0).lower()
            if any(marker in token for marker in (b"test", b"dummy", b"fake", b"example")):
                continue
            findings.append(name)
            break
    return findings


def docker_pattern_matches(path: str, raw_pattern: str) -> bool:
    pattern = raw_pattern.strip().replace("\\", "/")
    if not pattern or pattern.startswith("#"):
        return False
    if pattern.startswith("!"):
        pattern = pattern[1:]
    pattern = pattern.lstrip("/").rstrip("/")
    path = path.lstrip("/")
    if not pattern:
        return False
    if "/" not in pattern:
        return any(fnmatch.fnmatchcase(part, pattern) for part in path.split("/"))
    if not any(char in pattern for char in "*?["):
        return path == pattern or path.startswith(pattern + "/")
    return fnmatch.fnmatchcase(path, pattern) or PurePosixPath(path).match(pattern)


def docker_ignored(path: str, rules: list[str]) -> bool:
    ignored = False
    for raw in rules:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if docker_pattern_matches(path, stripped):
            ignored = not stripped.startswith("!")
    return ignored


def scan_docker_context(policy: dict[str, object]) -> list[str]:
    rules = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return [
        f"docker-context-canary-not-ignored:{path}"
        for path in policy["required_docker_ignored_canaries"]  # type: ignore[index]
        if not docker_ignored(str(path), rules)
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan release paths and content without displaying secret values."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--staged", action="store_true", help="scan staged added/modified files")
    source.add_argument("--tracked", action="store_true", help="scan every tracked file")
    source.add_argument(
        "--working-tree", action="store_true", help="scan tracked and non-ignored untracked files"
    )
    source.add_argument("--paths", nargs="+", help="scan explicit repo- or project-relative paths")
    parser.add_argument(
        "--docker-context", action="store_true", help="also verify Docker ignore canaries"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    policy = load_policy()
    root = git_root()
    staged = bool(args.staged)

    if args.paths:
        paths = []
        for supplied in args.paths:
            candidate = Path(supplied)
            if not candidate.is_absolute():
                candidate = (Path.cwd() / candidate).resolve()
            paths.append(candidate.relative_to(root).as_posix())
    elif args.tracked:
        paths = nul_paths(git("ls-files", "--full-name", "-z"))  # type: ignore[arg-type]
    elif args.working_tree:
        paths = nul_paths(git("ls-files", "--full-name", "-z", "--cached", "--others", "--exclude-standard"))  # type: ignore[arg-type]
    else:
        staged = True
        paths = nul_paths(git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"))  # type: ignore[arg-type]

    def read_data(path: str) -> bytes:
        if staged:
            return git("show", f":{path}")  # type: ignore[return-value]
        return (root / path).read_bytes()

    failures: list[tuple[str, str]] = []
    for path in sorted(set(paths)):
        try:
            data = read_data(path)
        except (OSError, subprocess.CalledProcessError):
            failures.append((path, "unreadable"))
            continue
        rules = path_findings(path, policy)
        if rules and not is_reviewed_exception(path, data, policy):
            failures.extend((path, rule) for rule in rules)
        if not is_content_exception(path, data, policy):
            failures.extend((path, f"secret-signature:{rule}") for rule in content_findings(data))

    if args.docker_context:
        failures.extend((".dockerignore", finding) for finding in scan_docker_context(policy))

    if failures:
        print("release scan failed; only paths and rule identifiers follow:", file=sys.stderr)
        for path, finding in failures:
            print(f"  {path}: {finding}", file=sys.stderr)
        return 1
    print(f"release scan passed ({len(set(paths))} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
