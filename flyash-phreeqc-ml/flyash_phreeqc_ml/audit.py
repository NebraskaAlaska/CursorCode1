"""Append-only audit log — reconstruct how a run's comparison was produced.

Every meaningful action on a run (import, validation, mapping accept/delete, script
run, comparison generation, inclusion, export) appends **one JSON line** to
``experiments/<run>/outputs/audit_log.jsonl``. The log is append-only *by
construction*: this module exposes :func:`log_event` (and typed convenience wrappers)
and :func:`read_audit`, and **no edit or delete API**. The file is the export.

What is logged — and what is NOT
--------------------------------
The log records **actions**, never the data itself. It stores bounded counts, ids,
statuses, and content hashes only — **never measured values, prompts, provider
endpoints, browser filenames, or file contents** (those live in tenant-scoped stores).
For uploaded files, only a validated extension/size/hash provenance envelope is retained.

Robustness
----------
Logging must never break the workflow it observes. Every public function here is
defensive: a logging failure issues a :class:`UserWarning` and returns ``False``
instead of raising. The reader tolerates malformed lines and unknown (future)
``event_type`` values.

Relationship to the comparison stamp
------------------------------------
``comparison_meta.json`` (the Prompt-1 stale-detection stamp) remains the source of
truth for provenance fingerprints; the ``comparison_generated`` event *references* its
hashes rather than duplicating the stamp's role.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import __version__ as APP_VERSION
from . import config, run_manager, units
from .security.identity import ROLE_MEMBER, current_identity
from .storage_scope import StorageScope

try:  # POSIX deployment lock; the in-process lock remains the portable fallback.
    import fcntl
except ImportError:  # pragma: no cover - Windows compatibility
    fcntl = None

AUDIT_LOG_FILENAME = "audit_log.jsonl"
AUDIT_SCHEMA_VERSION = 2
MAX_AUDIT_LINE_BYTES = 64 * 1024
MAX_AUDIT_FILE_BYTES = 16 * 1024 * 1024
MAX_AUDIT_ROWS = 50_000

# Event-type vocabulary (one place, so the UI filter and the writers agree).
EVENT_IMPORT = "import"
EVENT_VALIDATION = "validation"
EVENT_SUGGESTION_TABLE = "suggestion_table"
EVENT_MAPPING_ACCEPTED = "mapping_accepted"
EVENT_MAPPING_DELETED = "mapping_deleted"
EVENT_SCRIPT_RUN = "script_run"
EVENT_COMPARISON_GENERATED = "comparison_generated"
EVENT_INCLUSION = "inclusion"
EVENT_EXPORT = "export"
EVENT_LITERATURE_PROPOSED = "literature_proposed"
EVENT_LITERATURE_CONFIRMED = "literature_confirmed"
EVENT_MODEL_PREDICTIONS_IMPORT = "model_predictions_import"

EVENT_TYPES = (
    EVENT_IMPORT, EVENT_VALIDATION, EVENT_SUGGESTION_TABLE, EVENT_MAPPING_ACCEPTED,
    EVENT_MAPPING_DELETED, EVENT_SCRIPT_RUN, EVENT_COMPARISON_GENERATED,
    EVENT_INCLUSION, EVENT_EXPORT, EVENT_LITERATURE_PROPOSED, EVENT_LITERATURE_CONFIRMED,
    EVENT_MODEL_PREDICTIONS_IMPORT,
)

# The columns read_audit always returns (schema every event carries).
AUDIT_COLUMNS = ["timestamp", "event_type", "app_version", "payload"]

_EVENT_ALLOWED_FIELDS = {
    EVENT_IMPORT: frozenset({"file", "sheet_present", "mode", "n_rows", "columns",
                             "conversions", "column_mapping", "mapping_confirmed"}),
    EVENT_VALIDATION: frozenset({"severity_counts", "source"}),
    EVENT_SUGGESTION_TABLE: frozenset({"status_counts", "n_conditions"}),
    EVENT_MAPPING_ACCEPTED: frozenset({"condition_key", "phreeqc_record_key",
                                       "mapping_status", "override", "has_notes"}),
    EVENT_MAPPING_DELETED: frozenset({"scope", "n_deleted", "keys"}),
    EVENT_SCRIPT_RUN: frozenset({"script", "exit_status", "ok"}),
    EVENT_COMPARISON_GENERATED: frozenset({"meta_file", "sources"}),
    EVENT_INCLUSION: frozenset({"variables"}),
    EVENT_EXPORT: frozenset({"kind", "file", "n_rows"}),
    EVENT_LITERATURE_PROPOSED: frozenset({"n_candidates", "kinds", "materials",
                                          "candidate_ids"}),
    EVENT_LITERATURE_CONFIRMED: frozenset({
        "candidate_id", "quantity", "value", "unit", "element", "kind",
        "citation_link", "doi", "title", "year", "conditions_mismatch",
        "acknowledged_mismatch"}),
    EVENT_MODEL_PREDICTIONS_IMPORT: frozenset({"file", "model_names", "n_rows"}),
}
_SENSITIVE_KEY_RE = re.compile(
    r"(^|_)(api_?key|token|authorization|cookie|password|secret|credential|session|"
    r"prompt|messages?|raw_?response|model_?output|endpoint|base_?url|file_?bytes|content)(_|$)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._~+/=-]+|\bsk-[A-Za-z0-9_-]{8,}|"
    r"\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def audit_log_path(run_name: str) -> Path:
    """Path to a run's append-only audit log (under its outputs dir)."""
    return run_manager._scoped_path(run_name, "outputs", AUDIT_LOG_FILENAME)


# --------------------------------------------------------------------------- #
# JSON sanitising (numpy / NaN / Path -> plain JSON; never raises)
# --------------------------------------------------------------------------- #
def _jsonable(obj, *, _depth: int = 0):
    try:
        import math

        import numpy as np
        if _depth > 8:
            return "[maximum depth omitted]"
        if isinstance(obj, dict):
            out = {}
            for index, (key, value) in enumerate(obj.items()):
                if index >= 256:
                    break
                name = str(key)[:128]
                out[name] = ("[redacted]" if _SENSITIVE_KEY_RE.search(name)
                             else _jsonable(value, _depth=_depth + 1))
            return out
        if isinstance(obj, (list, tuple, set)):
            return [_jsonable(v, _depth=_depth + 1) for v in list(obj)[:256]]
        if isinstance(obj, np.floating):
            obj = float(obj)
        elif isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, float):
            return None if math.isnan(obj) else obj
        if isinstance(obj, str):
            text = obj[:2048]
            return "[redacted]" if _SECRET_VALUE_RE.search(text) else text
        if isinstance(obj, Path):
            return "[path omitted]"
        if obj is None or isinstance(obj, (int, bool)):
            return obj
        text = str(obj)[:2048]
        return "[redacted]" if _SECRET_VALUE_RE.search(text) else text
    except Exception:  # pragma: no cover - sanitiser must never raise
        return "[unserializable value omitted]"


def _file_provenance(file_name: str | None) -> dict | None:
    """Content-free file provenance; browser-supplied names are never persisted."""
    if not file_name:
        return None
    suffix = Path(str(file_name)).suffix.lower()
    return {"extension": suffix[:16]} if re.fullmatch(r"\.[a-z0-9]{1,15}", suffix) else None


def _payload_for_event(event_type: str, payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    allowed = _EVENT_ALLOWED_FIELDS.get(event_type)
    selected = payload if allowed is None else {k: v for k, v in payload.items() if k in allowed}
    cleaned = _jsonable(selected)
    return cleaned if isinstance(cleaned, dict) else {}


def _path_lock(path: Path) -> threading.Lock:
    key = str(path.resolve(strict=False))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


# --------------------------------------------------------------------------- #
# Core writer (the only writer) + reader
# --------------------------------------------------------------------------- #
def log_event(run_name: str, event_type: str, payload: dict | None = None) -> bool:
    """Append one event to the run's audit log. Returns True on success.

    Writes a single JSON line with ``timestamp`` (ISO seconds), ``event_type``,
    ``app_version``, and the (sanitised) ``payload``. Append-only — there is no API
    to edit or remove a line. **Never raises**: on any failure it warns and returns
    ``False`` so the surrounding workflow continues.
    """
    try:
        current_identity().require_role(ROLE_MEMBER)
        kind = str(event_type).strip().lower()
        if not _EVENT_RE.fullmatch(kind):
            raise ValueError("invalid audit event type")
        scope = StorageScope.for_identity(config.EXPERIMENT_RUNS_DIR, current_identity())
        record = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event_type": kind,
            "app_version": APP_VERSION,
            "actor_scope": hashlib.sha256(
                scope.session_namespace.encode("ascii")).hexdigest(),
            "payload": _payload_for_event(kind, payload),
        }
        path = audit_log_path(run_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.is_symlink():
            raise OSError("audit path must not be a symlink")
        line = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
            "utf-8")
        if len(line) > MAX_AUDIT_LINE_BYTES:
            raise ValueError("audit record exceeds the bounded schema")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        with _path_lock(path):
            fd = os.open(path, flags, 0o600)
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                if os.fstat(fd).st_size + len(line) > MAX_AUDIT_FILE_BYTES:
                    raise OSError("audit log has reached its configured size limit")
                remaining = memoryview(line)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("audit append did not make progress")
                    remaining = remaining[written:]
            finally:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        return True
    except Exception:  # logging must never crash the workflow; errors carry no user data
        warnings.warn("audit event could not be recorded", stacklevel=2)
        return False


def read_audit(run_name: str) -> pd.DataFrame:
    """Read a run's audit log into a DataFrame (oldest first), tolerating bad lines.

    Returns :data:`AUDIT_COLUMNS`. Malformed JSON lines are skipped with a warning;
    a valid line missing a field is filled with a default (so a future, richer event
    schema or an unknown ``event_type`` still reads cleanly). Empty / absent log → an
    empty frame with the right columns.
    """
    path = audit_log_path(run_name)
    if not path.exists():
        return pd.DataFrame(columns=AUDIT_COLUMNS)
    if path.is_symlink() or path.stat().st_size > MAX_AUDIT_FILE_BYTES:
        warnings.warn("audit log could not be read safely", stacklevel=2)
        return pd.DataFrame(columns=AUDIT_COLUMNS)
    rows: list[dict] = []
    bad = 0
    with path.open("rb") as fh:
        for index, raw_line in enumerate(fh):
            if index >= MAX_AUDIT_ROWS:
                bad += 1
                break
            if len(raw_line) > MAX_AUDIT_LINE_BYTES:
                bad += 1
                continue
            try:
                obj = json.loads(raw_line.decode("utf-8"))
            except Exception:
                bad += 1
                continue
            if not isinstance(obj, dict):
                bad += 1
                continue
            rows.append({
                "timestamp": obj.get("timestamp", ""),
                "event_type": obj.get("event_type", "unknown"),
                "app_version": obj.get("app_version", ""),
                "payload": obj.get("payload", {}),
            })
    if bad:
        warnings.warn(f"audit log skipped {bad} malformed or excessive line(s)", stacklevel=2)
    return pd.DataFrame(rows, columns=AUDIT_COLUMNS)


# --------------------------------------------------------------------------- #
# Helpers shared by the convenience loggers
# --------------------------------------------------------------------------- #
def conversions_from_frame(df) -> dict:
    """``{converted_column: conversion_id}`` read from a frame's provenance companions.

    Records only ids (e.g. ``mgL_to_mM`` / ``identity``) — never any value.
    """
    out: dict[str, str] = {}
    if df is None or getattr(df, "empty", True):
        return out
    for col in df.columns:
        if str(col).endswith(units.CONVERSION_ID_SUFFIX):
            base = str(col)[: -len(units.CONVERSION_ID_SUFFIX)]
            series = df[col].dropna()
            if not series.empty:
                out[base] = str(series.iloc[0])
    return out


# --------------------------------------------------------------------------- #
# Typed convenience loggers (consistent payload schemas; all defensive)
# --------------------------------------------------------------------------- #
def log_import(run_name: str, *, n_rows: int, mode: str, columns=None,
               conversions: dict | None = None, file_name: str | None = None,
               sheet: str | None = None, column_mapping: dict | None = None,
               mapping_confirmed: bool | None = None) -> bool:
    """An import action: file *name* only, sheet, column mapping, conversions, rows, mode."""
    payload = {
        "file": _file_provenance(file_name), "sheet_present": sheet is not None, "mode": mode,
        "n_rows": int(n_rows) if n_rows is not None else None,
        "columns": [str(c) for c in (columns or [])],
        "conversions": dict(conversions or {}),
        "column_mapping": {str(k): (None if v is None else str(v))
                           for k, v in (column_mapping or {}).items()},
        "mapping_confirmed": mapping_confirmed,
    }
    return log_event(run_name, EVENT_IMPORT, payload)


def log_validation(run_name: str, *, severity_counts: dict, source: str | None = None) -> bool:
    """A validation pass: issue counts by severity (e.g. {'error': 0, 'warning': 3})."""
    return log_event(run_name, EVENT_VALIDATION, {
        "severity_counts": {str(k): int(v) for k, v in (severity_counts or {}).items()},
        "source": source,
    })


def log_suggestion_table(run_name: str, *, status_counts: dict, n_conditions: int) -> bool:
    """A suggestion table was generated: counts per mapping status."""
    return log_event(run_name, EVENT_SUGGESTION_TABLE, {
        "status_counts": {str(k): int(v) for k, v in (status_counts or {}).items()},
        "n_conditions": int(n_conditions),
    })


def log_mapping_accepted(run_name: str, *, condition_key: str, phreeqc_record_key: str,
                         mapping_status: str | None = None, override: bool = False,
                         has_notes: bool = False) -> bool:
    """A mapping was accepted: condition_key + record_key + status + override flag."""
    return log_event(run_name, EVENT_MAPPING_ACCEPTED, {
        "condition_key": str(condition_key),
        "phreeqc_record_key": str(phreeqc_record_key),
        "mapping_status": None if mapping_status is None else str(mapping_status),
        "override": bool(override),
        "has_notes": bool(has_notes),
    })


def log_mapping_deleted(run_name: str, *, scope: str, n_deleted: int,
                        keys: list | None = None) -> bool:
    """Mappings were deleted (scope = 'condition' or 'sample'): count + ids removed."""
    return log_event(run_name, EVENT_MAPPING_DELETED, {
        "scope": str(scope),
        "n_deleted": int(n_deleted),
        "keys": [str(k) for k in (keys or [])],
    })


def log_script_run(run_name: str, *, script: str, exit_status: int) -> bool:
    """A pipeline script was run: its name + exit status (no stdout/stderr content)."""
    return log_event(run_name, EVENT_SCRIPT_RUN, {
        "script": Path(str(script)).name, "exit_status": int(exit_status),
        "ok": int(exit_status) == 0,
    })


def log_comparison_generated(run_name: str, *, meta_file: str | None = None,
                             sources: dict | None = None) -> bool:
    """A comparison was generated: reference the Prompt-1 meta + its provenance hashes."""
    return log_event(run_name, EVENT_COMPARISON_GENERATED, {
        "meta_file": meta_file or run_manager.COMPARISON_META_FILENAME,
        "sources": _jsonable(sources or {}),
    })


def log_inclusion(run_name: str, *, variables: list) -> bool:
    """The inclusion partition (Prompt 4): per-variable plotted/excluded/validity counts."""
    return log_event(run_name, EVENT_INCLUSION, {"variables": _jsonable(variables or [])})


def log_export(run_name: str, *, kind: str, file_name: str | None = None,
               n_rows: int | None = None) -> bool:
    """A report/data export: kind + file *name* + row count (never the rows)."""
    return log_event(run_name, EVENT_EXPORT, {
        "kind": str(kind), "file": _file_provenance(file_name),
        "n_rows": None if n_rows is None else int(n_rows),
    })


def log_literature_proposed(run_name: str, *, n_candidates: int, kinds=None,
                            materials=None, candidate_ids=None) -> bool:
    """AI proposed sourced literature values (quarantined): counts + ids, never numbers."""
    return log_event(run_name, EVENT_LITERATURE_PROPOSED, {
        "n_candidates": int(n_candidates),
        "kinds": [str(k) for k in (kinds or [])],
        "materials": [str(m) for m in (materials or [])],
        "candidate_ids": [str(c) for c in (candidate_ids or [])],
    })


def log_literature_confirmed(run_name: str, *, candidate_id: str, quantity: str,
                             value=None, unit: str | None = None, element=None,
                             kind: str | None = None, citation_link: str | None = None,
                             doi: str | None = None, title: str | None = None,
                             year=None, conditions_mismatch: bool = False,
                             acknowledged_mismatch: bool = False) -> bool:
    """A literature value was confirmed for use — the permanent, traceable record.

    Carries the resolvable **DOI/link** + title + year so any downstream number this
    value influences can be traced back to the exact paper. This is the one place a
    confirmed literature value's source is preserved in the append-only trail.
    """
    return log_event(run_name, EVENT_LITERATURE_CONFIRMED, {
        "candidate_id": str(candidate_id),
        "quantity": str(quantity),
        "value": value,
        "unit": None if unit is None else str(unit),
        "element": None if element is None else str(element),
        "kind": None if kind is None else str(kind),
        "citation_link": citation_link,
        "doi": doi,
        "title": None if title is None else str(title),
        "year": None if year is None else int(year),
        "conditions_mismatch": bool(conditions_mismatch),
        "acknowledged_mismatch": bool(acknowledged_mismatch),
    })
