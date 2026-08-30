"""Trusted-host, append-only Obsidian handoff synchronization."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping

from .contracts import ContractError, scan_public_text, utc_now, validate_state_record


class ObsidianSyncError(RuntimeError):
    """The trusted append-only handoff could not be synchronized safely."""


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ObsidianSyncError("handoff note is missing or unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_wpi_vault(config_path: Path | None = None) -> Path:
    config = config_path or (Path.home() / "Library/Application Support/obsidian/obsidian.json")
    try:
        value = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ObsidianSyncError("Obsidian configuration is unreadable") from exc
    candidates: list[Path] = []
    for vault in value.get("vaults", {}).values():
        configured = Path(vault.get("path", "")).expanduser()
        for candidate in (configured, *configured.parents):
            if candidate in candidates:
                continue
            if all((candidate / name).exists() for name in (".obsidian", "AI Council", "WPI Project")):
                candidates.append(candidate)
                break
    if len(candidates) != 1:
        raise ObsidianSyncError("could not resolve exactly one WPI Obsidian vault")
    vault = candidates[0]
    if vault.is_symlink() or not vault.is_dir():
        raise ObsidianSyncError("resolved Obsidian vault is unsafe")
    return vault


def render_handoff(record: Mapping[str, Any]) -> str:
    validate_state_record(record)
    request = record["request"]
    summary = record["evidence"]["summary"]
    route = summary.get("route", "not recorded")
    profiles = summary.get("role_profiles", {})
    tests = summary.get("tests", [])
    decision = record["state"] if record["state"] in {"approved", "rejected", "cancelled"} else "pending"
    text = "\n".join(
        [
            f"## {utc_now()} — Council task {record['task_id']}",
            "",
            f"- Request hash: `{record['request_hash']}`",
            f"- Base commit: `{record['base_commit']}`",
            f"- Task branch: `{record['task_branch'] or 'not pushed'}`",
            f"- Final task commit: `{record['final_task_commit'] or 'not created'}`",
            f"- Route: `{route}`",
            f"- Worker computer: `{summary.get('worker_id', record.get('worker_id') or 'released')}`",
            f"- Role profiles: `{json.dumps(profiles, sort_keys=True, separators=(',', ':'))}`",
            f"- Patch hash: `{summary.get('patch_hash', '')}`",
            f"- Tests: `{json.dumps(tests, sort_keys=True, separators=(',', ':'))}`",
            f"- Reviewer result: `{summary.get('reviewer_verdict', 'not recorded')}`",
            f"- Human decision: `{decision}`",
            f"- Next action: `{summary.get('next_action', 'human review')}`",
            "",
        ]
    )
    scan_public_text(text, allow_research_data=False)
    return text


def append_handoff(
    record: Mapping[str, Any],
    *,
    vault: Path | None = None,
    note_relative: str = "WPI Project/99 - Handoff Log.md",
    expected_before_hash: str | None = None,
) -> dict[str, str]:
    resolved = vault or resolve_wpi_vault()
    note = Path(os.path.abspath(resolved / note_relative))
    wpi_root = Path(os.path.abspath(resolved / "WPI Project"))
    try:
        note.relative_to(wpi_root)
    except ValueError as exc:
        raise ObsidianSyncError("handoff note escapes WPI Project") from exc
    if note.is_symlink() or not note.is_file():
        raise ObsidianSyncError("handoff target must be an existing regular note")
    payload = render_handoff(record).encode("utf-8")
    descriptor = os.open(note, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with os.fdopen(descriptor, "r+b", closefd=False) as handle:
            before_bytes = handle.read()
            before = hashlib.sha256(before_bytes).hexdigest()
            if expected_before_hash is not None and before != expected_before_hash:
                raise ObsidianSyncError("handoff note changed concurrently")
            handle.seek(0, os.SEEK_END)
            if before_bytes and not before_bytes.endswith(b"\n"):
                handle.write(b"\n")
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        after = _sha256(note)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)
    return {"note": str(note), "sha256_before": before, "sha256_after": after}
