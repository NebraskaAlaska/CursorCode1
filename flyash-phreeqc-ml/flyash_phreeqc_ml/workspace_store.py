"""Durable, local project/material/run records for the Virtual LAB product shell.

This module is deliberately UI-free and contains no scientific calculations.  It
stores small JSON records under the gitignored ``outputs/`` tree and references
legacy run/evidence/model artifacts without copying them.  Writes are deterministic
and atomic; generated IDs are never derived from display names.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config
from .instruments import virtual_lab_machines

# Schema 2 adds full-envelope integrity digests to newly written ArtifactRecord
# and RunRecord documents. Schema-1 files remain readable without rewriting.
SCHEMA_VERSION = 2
PROJECT_PREFIX = "prj_"
MATERIAL_PREFIX = "mat_"
RUN_PREFIX = "run_"
ARTIFACT_PREFIX = "art_"
_ID_RE = re.compile(r"^(prj|mat|run|art)_[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_LOCKS_GUARD = threading.Lock()
_REVISION_THREAD_LOCKS: dict[str, threading.Lock] = {}

ARTIFACT_ICP_REVIEW = "icp_review"
ARTIFACT_XRD_PATTERN = "xrd_measured_pattern"
ARTIFACT_XRD_REFERENCE = "xrd_reference"
ARTIFACT_EVIDENCE = "evidence"
ARTIFACT_EXPERIMENT_PLAN = "experimental_plan"
ARTIFACT_SUSTAINABILITY_SCREEN = "sustainability_screen"
ARTIFACT_TYPES = (
    ARTIFACT_ICP_REVIEW,
    ARTIFACT_XRD_PATTERN,
    ARTIFACT_XRD_REFERENCE,
    ARTIFACT_EVIDENCE,
    ARTIFACT_EXPERIMENT_PLAN,
    ARTIFACT_SUSTAINABILITY_SCREEN,
)
ARTIFACT_STATUSES = (
    "draft", "needs_review", "finalized", "reviewed", "rejected", "superseded",
)
IMMUTABLE_ARTIFACT_STATUSES = frozenset({"finalized", "reviewed", "rejected", "superseded"})
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_KEY_SEPARATOR = re.compile(r"[^A-Za-z0-9]+")
_SECRET_KEY_SEGMENTS = frozenset({
    "token", "tokens", "password", "passwords", "passwd", "passwds",
    "secret", "secrets", "cookie", "cookies", "authorization", "authorizations",
    "credential", "credentials",
})
_SECRET_COMPACT_KEYS = frozenset({"apikey", "apikeys"})


class WorkspaceStoreError(RuntimeError):
    """Base class for controlled durable-store failures."""


class UnsafePathError(WorkspaceStoreError):
    """A path or identifier could escape the configured workspace root."""


class RecordNotFoundError(WorkspaceStoreError):
    """A requested project, material, or run does not exist."""


class DuplicateRecordError(WorkspaceStoreError):
    """A create operation would overwrite a different record."""


class MalformedRecordError(WorkspaceStoreError):
    """A stored JSON document is malformed or has the wrong shape."""


class UnsupportedSchemaError(WorkspaceStoreError):
    """A record was written by a newer, unsupported schema."""


class ConfirmationRequiredError(WorkspaceStoreError):
    """A destructive operation did not carry an exact target confirmation."""


class ImmutableRecordError(WorkspaceStoreError):
    """A caller attempted to change an immutable finalized/reviewed artifact."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MalformedRecordError(f"record is not finite JSON-safe data: {exc}") from exc


def identity_hash(value: Any) -> str:
    """SHA-256 over deterministic JSON (also rejects NaN/Infinity)."""
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise MalformedRecordError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _is_secret_key(value: Any) -> bool:
    """Recognize secret-bearing field aliases without substring false positives.

    Keys are normalized across camelCase, acronymCase, snake_case, kebab-case,
    spaces, and punctuation. Exact segments such as ``token`` and ``secret`` are
    refused, while scientific words such as ``secretion`` or ``tokenization`` are
    not treated as credentials merely because they contain those letters.
    """
    text = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1_\2", str(value))
    text = _CAMEL_WORD_BOUNDARY.sub(r"\1_\2", text)
    parts = tuple(part.lower() for part in _KEY_SEPARATOR.split(text) if part)
    if any(part in _SECRET_KEY_SEGMENTS for part in parts):
        return True
    if any(left in {"api", "license"} and right in {"key", "keys"}
           for left, right in zip(parts, parts[1:])):
        return True
    compact = "".join(parts)
    return compact in _SECRET_COMPACT_KEYS


def _assert_no_secrets(value: Any, path: str = "record") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _is_secret_key(key):
                raise MalformedRecordError(f"secret-like field is not permitted: {path}.{key}")
            _assert_no_secrets(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_no_secrets(child, f"{path}[{index}]")


def _record_from_dict(cls, payload: dict):
    known = {item.name for item in fields(cls)}
    unknown = set(payload) - known
    # Current-schema documents must be closed shapes: otherwise an unverified
    # field could remain on disk while projection into the dataclass makes it
    # invisible to the full-envelope digest. Schema 1 retains its historical
    # tolerant-reader behavior for compatibility and is never rewritten on read.
    if payload.get("schema_version", 1) >= 2 and unknown:
        raise MalformedRecordError(
            f"record contains unsupported schema-{payload.get('schema_version')} fields for "
            f"{cls.__name__}: {sorted(unknown)}")
    try:
        return cls(**{key: value for key, value in payload.items() if key in known})
    except (TypeError, ValueError) as exc:
        raise MalformedRecordError(
            f"record fields are malformed for {cls.__name__}: {exc}") from exc


def _artifact_type(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized not in ARTIFACT_TYPES:
        raise MalformedRecordError(f"unsupported artifact record_type: {value!r}")
    return normalized


def _artifact_status(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized not in ARTIFACT_STATUSES:
        raise MalformedRecordError(f"unsupported artifact status: {value!r}")
    return normalized


@dataclass
class ProjectRecord:
    project_id: str
    name: str
    description: str = ""
    status: str = "active"
    archived: bool = False
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MaterialRecord:
    material_id: str
    project_id: str
    name: str
    description: str = ""
    material_type: str = ""
    composition: list = field(default_factory=list)
    composition_provenance: dict = field(default_factory=dict)
    verification_status: str = "unverified"
    assumptions: list = field(default_factory=list)
    process_conditions: dict = field(default_factory=dict)
    measurement_references: list = field(default_factory=list)
    evidence_references: list = field(default_factory=list)
    associated_model_ids: list = field(default_factory=list)
    associated_run_ids: list = field(default_factory=list)
    unresolved_issues: list = field(default_factory=list)
    revision: int = 1
    composition_revision: int = 1
    assumption_revision: int = 1
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunRecord:
    run_id: str
    project_id: str
    material_id: str
    machine_id: str
    input_snapshot: dict
    input_hash: str
    material_revision: int
    composition_revision: int
    assumption_revision: int
    material_snapshot_hash: str
    status: str
    output_type: str
    epistemic_type: str
    result_data: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    validation_state: str = "not_evaluated"
    model_identity: dict = field(default_factory=dict)
    environment_identity: dict = field(default_factory=dict)
    evidence_identity: list = field(default_factory=list)
    result_location: str | None = None
    legacy_references: list = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    schema_version: int = SCHEMA_VERSION
    # Optional only so genuine schema-v1 records created before Phase 3 remain readable.
    # Every run created by the current store persists and verifies this digest.
    result_hash: str | None = None
    envelope_hash: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        # Preserve the exact historical schema-1 shape when the optional
        # integrity fields did not exist. Reads remain non-migrating and any
        # identity derived by legacy consumers stays stable.
        if self.schema_version == 1 and self.result_hash is None:
            data.pop("result_hash", None)
        if self.schema_version == 1 and self.envelope_hash is None:
            data.pop("envelope_hash", None)
        return data


@dataclass
class ArtifactRecord:
    """Typed, revision-linked Phase 3 record stored by the existing workspace authority.

    The common envelope owns durable identity and lifecycle only. Domain modules validate the
    scientific payload, so this record cannot become a second ICP/XRD/evidence/planning engine.
    """

    artifact_id: str
    logical_id: str
    project_id: str
    material_id: str | None
    record_type: str
    revision: int
    status: str
    input_snapshot: dict
    input_hash: str
    payload: dict
    payload_hash: str
    source_identity: dict = field(default_factory=dict)
    creator: str = ""
    reviewer: str = ""
    reason: str = ""
    previous_artifact_id: str | None = None
    previous_artifact_hash: str | None = None
    related_run_id: str | None = None
    provenance: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    reviewed_at: str | None = None
    schema_version: int = SCHEMA_VERSION
    envelope_hash: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        # Schema-1 lineage hashes were calculated before ``envelope_hash`` was a
        # field. Omitting the absent optional field is required to validate those
        # immutable predecessor identities without rewriting legacy files.
        if self.schema_version == 1 and self.envelope_hash is None:
            data.pop("envelope_hash", None)
        return data


def material_identity_payload(material: MaterialRecord | dict) -> dict:
    """Only scientifically meaningful material state used to bind run freshness."""
    data = material.to_dict() if isinstance(material, MaterialRecord) else dict(material)
    return {
        "material_id": data.get("material_id"),
        "project_id": data.get("project_id"),
        "material_type": data.get("material_type"),
        "composition": data.get("composition", []),
        "composition_provenance": data.get("composition_provenance", {}),
        "verification_status": data.get("verification_status"),
        "assumptions": data.get("assumptions", []),
        "process_conditions": data.get("process_conditions", {}),
        "measurement_references": data.get("measurement_references", []),
        "evidence_references": data.get("evidence_references", []),
        "associated_model_ids": data.get("associated_model_ids", []),
        "revision": data.get("revision"),
        "composition_revision": data.get("composition_revision"),
        "assumption_revision": data.get("assumption_revision"),
    }


def material_identity_hash(material: MaterialRecord | dict) -> str:
    return identity_hash(material_identity_payload(material))


def artifact_envelope_hash(record: ArtifactRecord | dict) -> str:
    """Digest every durable artifact field except the digest itself."""
    data = record.to_dict() if isinstance(record, ArtifactRecord) else dict(record)
    data.pop("envelope_hash", None)
    return identity_hash(data)


def run_envelope_hash(record: RunRecord | dict) -> str:
    """Digest every durable run field except the digest itself."""
    data = record.to_dict() if isinstance(record, RunRecord) else dict(record)
    data.pop("envelope_hash", None)
    return identity_hash(data)


class WorkspaceStore:
    """One durable authority for Phase 2 project, material, run, and active context state."""

    def __init__(self, root: Path | str | None = None):
        raw_root = Path(root or config.VIRTUAL_LAB_WORKSPACE_DIR).expanduser()
        if any(part == ".." for part in raw_root.parts):
            raise UnsafePathError("workspace root must not contain '..'")
        self.root = raw_root.absolute()
        if self.root.exists() and self.root.is_symlink():
            raise UnsafePathError("workspace root must not be a symlink")

    @staticmethod
    def _validate_id(record_id: str, prefix: str | None = None) -> str:
        value = str(record_id or "")
        if not _ID_RE.fullmatch(value) or (prefix and not value.startswith(prefix)):
            raise UnsafePathError(f"invalid durable record id: {record_id!r}")
        return value

    @staticmethod
    def _validate_part(part: str) -> str:
        value = str(part)
        if value in {"", ".", ".."} or Path(value).name != value or "/" in value or "\\" in value:
            raise UnsafePathError(f"unsafe path component: {part!r}")
        return value

    def _path(self, *parts: str) -> Path:
        if self.root.exists() and self.root.is_symlink():
            raise UnsafePathError("workspace root must not be a symlink")
        safe_parts = [self._validate_part(part) for part in parts]
        candidate = self.root.joinpath(*safe_parts)
        root_resolved = self.root.resolve(strict=False)
        candidate_resolved = candidate.resolve(strict=False)
        try:
            candidate_resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise UnsafePathError(f"path escapes workspace root: {candidate}") from exc
        current = self.root
        for part in safe_parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise UnsafePathError(f"symlink path component is not permitted: {current}")
        return candidate

    def _record_path(self, kind: str, record_id: str) -> Path:
        prefix = {"projects": PROJECT_PREFIX, "materials": MATERIAL_PREFIX,
                  "runs": RUN_PREFIX, "artifacts": ARTIFACT_PREFIX}.get(kind)
        if prefix is None:
            raise UnsafePathError(f"unknown record kind: {kind!r}")
        return self._path(kind, f"{self._validate_id(record_id, prefix)}.json")

    def _atomic_write(self, path: Path, payload: dict, *, create_only: bool = False) -> None:
        _assert_no_secrets(payload)
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Re-check after mkdir so a concurrently introduced symlink cannot redirect the write.
        try:
            relative = path.absolute().relative_to(self.root)
        except ValueError as exc:
            raise UnsafePathError(f"write path escapes workspace root: {path}") from exc
        self._path(*relative.parts)
        if create_only and path.exists():
            raise DuplicateRecordError(f"record already exists: {path.stem}")
        descriptor, temp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            if create_only:
                # ``link`` supplies the atomic create-if-absent guarantee that an
                # exists-check followed by ``replace`` cannot provide to competing writers.
                try:
                    os.link(temp_name, path)
                except FileExistsError as exc:
                    raise DuplicateRecordError(f"record already exists: {path.stem}") from exc
                os.unlink(temp_name)
            else:
                os.replace(temp_name, path)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _read(self, path: Path, expected_kind: str | None = None) -> dict:
        # Test the directory entry itself before ``exists``/``read_text`` can
        # dereference either a live or broken symlink outside the workspace.
        if path.is_symlink():
            raise UnsafePathError(f"record file must not be a symlink: {path.name}")
        if not path.exists():
            raise RecordNotFoundError(f"record not found: {path.stem}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MalformedRecordError(f"cannot read {path.name}: malformed JSON") from exc
        if not isinstance(payload, dict):
            raise MalformedRecordError(f"{path.name} must contain a JSON object")
        version = payload.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise MalformedRecordError(f"{path.name} has no integer schema_version")
        if version > SCHEMA_VERSION:
            raise UnsupportedSchemaError(
                f"{path.name} uses schema {version}; supported maximum is {SCHEMA_VERSION}")
        if version < 1:
            raise UnsupportedSchemaError(f"{path.name} uses unsupported schema {version}")
        if expected_kind and payload.get(f"{expected_kind}_id") != path.stem:
            raise MalformedRecordError(f"{path.name} identity does not match its filename")
        _canonical_json(payload)
        _assert_no_secrets(payload)
        return payload

    @contextmanager
    def _artifact_revision_lock(self, logical_id: str):
        """Serialize appends to one artifact lineage across threads/processes.

        The lock contains no scientific or user data and remains as an empty local
        coordination file. ``O_NOFOLLOW`` prevents a planted lock-file symlink from
        redirecting the open.
        """
        logical = self._validate_id(logical_id, ARTIFACT_PREFIX)
        lock_key = f"{self.root}:{logical}"
        with _REVISION_LOCKS_GUARD:
            thread_lock = _REVISION_THREAD_LOCKS.setdefault(lock_key, threading.Lock())
        thread_lock.acquire()
        try:
            lock_dir = self._path("locks")
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_path = self._path("locks", f"{logical}.lock")
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise UnsafePathError(
                    f"cannot safely open artifact revision lock: {logical}") from exc
        except Exception:
            thread_lock.release()
            raise
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
                thread_lock.release()

    def cleanup_stale_temps(self) -> int:
        """Remove only this store's abandoned atomic-write temp files."""
        count = 0
        if not self.root.exists():
            return count
        for kind in ("projects", "materials", "runs", "artifacts"):
            directory = self._path(kind)
            if not directory.exists() or directory.is_symlink():
                continue
            for path in directory.glob(".tmp-*.json"):
                if path.is_file() and not path.is_symlink():
                    path.unlink()
                    count += 1
        return count

    def create_project(self, name: str, *, description: str = "", metadata: dict | None = None,
                       project_id: str | None = None) -> ProjectRecord:
        if not str(name or "").strip():
            raise MalformedRecordError("project name is required")
        record = ProjectRecord(
            project_id=self._validate_id(project_id, PROJECT_PREFIX) if project_id
            else _new_id(PROJECT_PREFIX),
            name=str(name).strip(), description=str(description or "").strip(),
            metadata=dict(metadata or {}),
        )
        self._atomic_write(self._record_path("projects", record.project_id), record.to_dict(),
                           create_only=True)
        return record

    def get_project(self, project_id: str) -> ProjectRecord:
        return _record_from_dict(ProjectRecord,
                                 self._read(self._record_path("projects", project_id), "project"))

    def list_projects(self, *, include_archived: bool = False) -> list[ProjectRecord]:
        records = self._list_records("projects", ProjectRecord, "project")
        return [record for record in records if include_archived or not record.archived]

    def update_project(self, project_id: str, **changes) -> ProjectRecord:
        record = self.get_project(project_id)
        before = record.to_dict()
        allowed = {"name", "description", "status", "metadata"}
        unknown = set(changes) - allowed
        if unknown:
            raise MalformedRecordError(f"unsupported project fields: {sorted(unknown)}")
        for key, value in changes.items():
            if key == "name" and not str(value or "").strip():
                raise MalformedRecordError("project name is required")
            setattr(record, key, str(value).strip() if key in {"name", "description", "status"}
                    else dict(value or {}))
        if any(before.get(key) != getattr(record, key) for key in changes):
            record.updated_at = _now()
            self._atomic_write(self._record_path("projects", project_id), record.to_dict())
        return record

    def archive_project(self, project_id: str, *, confirmation: str) -> ProjectRecord:
        if confirmation != project_id:
            raise ConfirmationRequiredError("archive confirmation must exactly match the project ID")
        record = self.get_project(project_id)
        record.archived = True
        record.status = "archived"
        record.updated_at = _now()
        self._atomic_write(self._record_path("projects", project_id), record.to_dict())
        context = self.get_active_context()
        if context.get("active_project_id") == project_id:
            self.set_active_context(None, None, None)
        return record

    def create_material(self, project_id: str, name: str, *, material_id: str | None = None,
                        **values) -> MaterialRecord:
        project = self.get_project(project_id)
        if project.archived:
            raise WorkspaceStoreError("cannot add a material to an archived project")
        if not str(name or "").strip():
            raise MalformedRecordError("material name is required")
        allowed = {item.name for item in fields(MaterialRecord)} - {
            "material_id", "project_id", "name", "created_at", "updated_at", "schema_version",
            "revision", "composition_revision", "assumption_revision",
        }
        unknown = set(values) - allowed
        if unknown:
            raise MalformedRecordError(f"unsupported material fields: {sorted(unknown)}")
        record = MaterialRecord(
            material_id=self._validate_id(material_id, MATERIAL_PREFIX) if material_id
            else _new_id(MATERIAL_PREFIX), project_id=project_id, name=str(name).strip(), **values)
        self._validate_loaded_material(record)
        self._atomic_write(self._record_path("materials", record.material_id), record.to_dict(),
                           create_only=True)
        return record

    def _read_material_record(self, material_id: str) -> MaterialRecord:
        return _record_from_dict(
            MaterialRecord,
            self._read(self._record_path("materials", material_id), "material"),
        )

    def _validate_material_reference_list(
        self,
        record: MaterialRecord,
        field_name: str,
        values: Any,
    ) -> None:
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise MalformedRecordError(f"material {field_name} must be a list of strings")
        if len(values) != len(set(values)):
            raise MalformedRecordError(f"material {field_name} must not contain duplicates")
        for reference in values:
            # Opaque legacy source labels remain readable. A value that presents
            # itself as a durable ID must, however, resolve to the right typed
            # record in this exact project/material context.
            if field_name in {"measurement_references", "evidence_references"} \
                    and reference.startswith(ARTIFACT_PREFIX):
                self._validate_id(reference, ARTIFACT_PREFIX)
                artifact = self.get_artifact(reference)
                if artifact.project_id != record.project_id \
                        or artifact.material_id != record.material_id:
                    raise WorkspaceStoreError(
                        f"material {field_name} contains a cross-context artifact")
                permitted = ({ARTIFACT_EVIDENCE} if field_name == "evidence_references" else {
                    ARTIFACT_ICP_REVIEW, ARTIFACT_XRD_PATTERN, ARTIFACT_XRD_REFERENCE,
                })
                if artifact.record_type not in permitted:
                    raise WorkspaceStoreError(
                        f"material {field_name} contains an incompatible artifact type")
            elif field_name == "associated_run_ids" and reference.startswith(RUN_PREFIX):
                self._validate_id(reference, RUN_PREFIX)
                run = self.get_run(reference)
                if run.project_id != record.project_id or run.material_id != record.material_id:
                    raise WorkspaceStoreError(
                        "material associated_run_ids contains a cross-context run")

    def _validate_loaded_material(self, record: MaterialRecord) -> MaterialRecord:
        self._validate_id(record.material_id, MATERIAL_PREFIX)
        self._validate_id(record.project_id, PROJECT_PREFIX)
        project = self.get_project(record.project_id)
        if not isinstance(record.name, str) or not record.name.strip():
            raise MalformedRecordError("material name is required")
        for name in ("revision", "composition_revision", "assumption_revision"):
            value = getattr(record, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise MalformedRecordError(f"material {name} must be a positive integer")
        for name in ("measurement_references", "evidence_references", "associated_run_ids"):
            self._validate_material_reference_list(record, name, getattr(record, name))
        if project.project_id != record.project_id:  # pragma: no cover - defensive clarity
            raise WorkspaceStoreError("material project binding is invalid")
        return record

    def get_material(self, material_id: str) -> MaterialRecord:
        return self._validate_loaded_material(self._read_material_record(material_id))

    def list_materials(self, project_id: str | None = None) -> list[MaterialRecord]:
        if project_id is not None:
            self.get_project(project_id)
        records = [self._validate_loaded_material(record) for record in
                   self._list_records("materials", MaterialRecord, "material")]
        return [record for record in records if project_id is None or record.project_id == project_id]

    def update_material(self, material_id: str, **changes) -> MaterialRecord:
        record = self.get_material(material_id)
        immutable = {"material_id", "project_id", "created_at", "schema_version", "revision",
                     "composition_revision", "assumption_revision"}
        allowed = {item.name for item in fields(MaterialRecord)} - immutable
        unknown = set(changes) - allowed
        if unknown:
            raise MalformedRecordError(f"unsupported material fields: {sorted(unknown)}")
        before = record.to_dict()
        for key, value in changes.items():
            if key == "name" and not str(value or "").strip():
                raise MalformedRecordError("material name is required")
            setattr(record, key, value)
        self._validate_loaded_material(record)
        after = record.to_dict()
        changed = {key for key in changes if before.get(key) != after.get(key)}
        meaningful_fields = {
            "material_type", "composition", "composition_provenance", "verification_status",
            "assumptions", "process_conditions", "measurement_references",
            "evidence_references", "associated_model_ids",
        }
        if changed:
            meaningful = bool(changed & meaningful_fields)
            if meaningful:
                record.revision += 1
            if any(key in changes for key in ("composition", "composition_provenance")):
                record.composition_revision += 1
            if any(key in changes for key in ("assumptions", "process_conditions")):
                record.assumption_revision += 1
            record.updated_at = _now()
            self._atomic_write(self._record_path("materials", material_id), record.to_dict())
        return record

    def create_artifact(
        self,
        project_id: str,
        material_id: str | None,
        record_type: str,
        input_snapshot: dict | None = None,
        *,
        status: str = "draft",
        payload: dict | None = None,
        source_identity: dict | None = None,
        creator: str = "",
        reviewer: str = "",
        reason: str = "",
        provenance: dict | None = None,
        revision: int = 1,
        previous_artifact_id: str | None = None,
        related_run_id: str | None = None,
        artifact_id: str | None = None,
        logical_id: str | None = None,
        reviewed_at: str | None = None,
    ) -> ArtifactRecord:
        """Create one typed Phase 3 artifact without changing existing record schemas."""
        self.get_project(project_id)
        if material_id is not None:
            material = self.get_material(material_id)
            if material.project_id != project_id:
                raise WorkspaceStoreError("material does not belong to the selected project")
        kind = _artifact_type(record_type)
        lifecycle = _artifact_status(status)
        normalized_reviewer = str(reviewer or "").strip()
        normalized_reason = str(reason or "").strip()
        if lifecycle in {"finalized", "reviewed", "rejected"}:
            if not normalized_reviewer or not normalized_reason:
                raise MalformedRecordError(
                    f"{lifecycle} artifact requires reviewer/resolver and reason")
            reviewed_at = reviewed_at or _now()
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise MalformedRecordError("artifact revision must be a positive integer")

        if previous_artifact_id is None and revision != 1:
            raise MalformedRecordError("an artifact revision above 1 requires a previous artifact")

        def persist(previous: ArtifactRecord | None) -> ArtifactRecord:
            if previous is not None:
                if (previous.project_id != project_id or previous.material_id != material_id
                        or previous.record_type != kind):
                    raise WorkspaceStoreError(
                        "previous artifact does not share project/material/type identity")
                if revision != previous.revision + 1:
                    raise MalformedRecordError(
                        "artifact revision must follow the previous revision")
            if related_run_id is not None:
                run = self.get_run(related_run_id)
                if run.project_id != project_id or run.material_id != material_id:
                    raise WorkspaceStoreError("related run does not share artifact context")

            snapshot = dict(input_snapshot or {})
            artifact_payload = dict(payload or {})
            _canonical_json(snapshot)
            _canonical_json(artifact_payload)
            new_id = self._validate_id(artifact_id, ARTIFACT_PREFIX) if artifact_id \
                else _new_id(ARTIFACT_PREFIX)
            if logical_id is None:
                logical = previous.logical_id if previous else new_id
            else:
                logical = self._validate_id(logical_id, ARTIFACT_PREFIX)
                if previous and logical != previous.logical_id:
                    raise WorkspaceStoreError("artifact revision cannot change logical identity")
                if previous is None and logical != new_id:
                    raise MalformedRecordError(
                        "a root artifact logical ID must equal its artifact ID")
            record = ArtifactRecord(
                artifact_id=new_id,
                logical_id=logical,
                project_id=project_id,
                material_id=material_id,
                record_type=kind,
                revision=revision,
                status=lifecycle,
                input_snapshot=snapshot,
                input_hash=identity_hash(snapshot),
                payload=artifact_payload,
                payload_hash=identity_hash(artifact_payload),
                source_identity=dict(source_identity or {}),
                creator=str(creator or "").strip(),
                reviewer=normalized_reviewer,
                reason=normalized_reason,
                previous_artifact_id=previous.artifact_id if previous else None,
                previous_artifact_hash=identity_hash(previous.to_dict()) if previous else None,
                related_run_id=related_run_id,
                provenance=dict(provenance or {}),
                reviewed_at=reviewed_at,
            )
            record.envelope_hash = artifact_envelope_hash(record)
            self._atomic_write(
                self._record_path("artifacts", record.artifact_id),
                record.to_dict(),
                create_only=True,
            )
            return record

        if previous_artifact_id is None:
            return persist(None)

        initially_loaded = self.get_artifact(previous_artifact_id)
        with self._artifact_revision_lock(initially_loaded.logical_id):
            # Re-read after acquiring the cross-process lock. Another writer may
            # have appended while this caller waited.
            previous = self.get_artifact(previous_artifact_id)
            inventory = self._loaded_artifact_inventory()
            self._validate_artifact_lineage(previous, inventory)
            family = [item for item in inventory if item.logical_id == previous.logical_id]
            latest = max(family, key=lambda item: item.revision)
            if latest.artifact_id != previous.artifact_id:
                raise DuplicateRecordError(
                    "artifact revision must extend the latest logical revision")
            return persist(previous)

    def _validate_loaded_artifact(self, record: ArtifactRecord) -> ArtifactRecord:
        self._validate_id(record.artifact_id, ARTIFACT_PREFIX)
        self._validate_id(record.logical_id, ARTIFACT_PREFIX)
        self._validate_id(record.project_id, PROJECT_PREFIX)
        if record.material_id is not None:
            self._validate_id(record.material_id, MATERIAL_PREFIX)
        if record.previous_artifact_id is not None:
            self._validate_id(record.previous_artifact_id, ARTIFACT_PREFIX)
        if record.related_run_id is not None:
            self._validate_id(record.related_run_id, RUN_PREFIX)
        _artifact_type(record.record_type)
        _artifact_status(record.status)
        if isinstance(record.revision, bool) or not isinstance(record.revision, int) \
                or record.revision < 1:
            raise MalformedRecordError("artifact revision must be a positive integer")
        if any(not isinstance(value, dict) for value in (
                record.input_snapshot, record.payload, record.source_identity, record.provenance)):
            raise MalformedRecordError(
                "artifact input, payload, source identity, and provenance must be objects")
        if identity_hash(record.input_snapshot) != record.input_hash:
            raise MalformedRecordError("artifact input hash does not match its stored snapshot")
        if identity_hash(record.payload) != record.payload_hash:
            raise MalformedRecordError("artifact payload hash does not match its stored payload")
        if record.schema_version >= 2:
            _require_sha256(record.envelope_hash, "artifact envelope_hash")
            if artifact_envelope_hash(record) != record.envelope_hash:
                raise MalformedRecordError(
                    "artifact envelope hash does not match its stored record")
        elif record.envelope_hash is not None:
            _require_sha256(record.envelope_hash, "artifact envelope_hash")
            if artifact_envelope_hash(record) != record.envelope_hash:
                raise MalformedRecordError(
                    "schema-1 artifact envelope hash does not match its stored record")
        if record.status in {"finalized", "reviewed", "rejected"} \
                and (not record.reviewer or not record.reason or not record.reviewed_at):
            raise MalformedRecordError(
                f"stored {record.status} artifact lacks required review metadata")
        return record

    def _validate_artifact_context(self, record: ArtifactRecord) -> None:
        self.get_project(record.project_id)
        if record.material_id is not None:
            # Use the basic read here, not ``get_material``: a material can link
            # back to this artifact and full reference validation would recurse.
            material = self._read_material_record(record.material_id)
            if material.project_id != record.project_id:
                raise WorkspaceStoreError("artifact material belongs to another project")
        if record.related_run_id is not None:
            run = self.get_run(record.related_run_id)
            if run.project_id != record.project_id or run.material_id != record.material_id:
                raise WorkspaceStoreError("artifact related run belongs to another context")

    def _validate_artifact_lineage(
        self,
        record: ArtifactRecord,
        records: Iterable[ArtifactRecord],
    ) -> None:
        lineage = sorted(
            (item for item in records if item.logical_id == record.logical_id),
            key=lambda item: item.revision,
        )
        if not lineage:
            raise MalformedRecordError("artifact lineage is missing")
        revisions = [item.revision for item in lineage]
        if revisions != list(range(1, len(lineage) + 1)):
            raise MalformedRecordError(
                "artifact lineage has duplicate, missing, or branched revision numbers")
        root = lineage[0]
        if root.artifact_id != root.logical_id \
                or root.previous_artifact_id is not None \
                or root.previous_artifact_hash is not None:
            raise MalformedRecordError("artifact lineage root identity is invalid")
        for previous, current in zip(lineage, lineage[1:]):
            if (current.project_id != root.project_id
                    or current.material_id != root.material_id
                    or current.record_type != root.record_type):
                raise MalformedRecordError("artifact lineage changes durable context or type")
            if current.previous_artifact_id != previous.artifact_id:
                raise MalformedRecordError("artifact lineage branches or skips its previous revision")
            if current.previous_artifact_hash != identity_hash(previous.to_dict()):
                raise MalformedRecordError("artifact previous-revision hash does not match")

    def _loaded_artifact_inventory(self) -> list[ArtifactRecord]:
        records = [self._validate_loaded_artifact(record) for record in
                   self._list_records("artifacts", ArtifactRecord, "artifact")]
        for record in records:
            self._validate_artifact_context(record)
        return records

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        record = _record_from_dict(
            ArtifactRecord,
            self._read(self._record_path("artifacts", artifact_id), "artifact"),
        )
        record = self._validate_loaded_artifact(record)
        self._validate_artifact_context(record)
        # Loading the complete logical family detects a manually planted sibling
        # revision or a broken intermediate link even when reopening history.
        lineage = [self._validate_loaded_artifact(item) for item in
                   self._list_records("artifacts", ArtifactRecord, "artifact")
                   if item.logical_id == record.logical_id]
        for item in lineage:
            self._validate_artifact_context(item)
        self._validate_artifact_lineage(record, lineage)
        return record

    def list_artifacts(
        self,
        *,
        project_id: str | None = None,
        material_id: str | None = None,
        record_type: str | None = None,
        status: str | None = None,
        logical_id: str | None = None,
    ) -> list[ArtifactRecord]:
        if project_id is not None:
            self.get_project(project_id)
        if material_id is not None:
            material = self.get_material(material_id)
            if project_id is not None and material.project_id != project_id:
                raise WorkspaceStoreError("material does not belong to the selected project")
        kind = _artifact_type(record_type) if record_type is not None else None
        lifecycle = _artifact_status(status) if status is not None else None
        logical = self._validate_id(logical_id, ARTIFACT_PREFIX) if logical_id else None
        records = self._loaded_artifact_inventory()
        for record in records:
            self._validate_artifact_lineage(record, records)
        visible = [
            record for record in records
            if (project_id is None or record.project_id == project_id)
            and (material_id is None or record.material_id == material_id)
            and (kind is None or record.record_type == kind)
            and (lifecycle is None or record.status == lifecycle)
            and (logical is None or record.logical_id == logical)
        ]
        # Artifact history has a stronger ordering contract than generic record
        # enumeration: every logical lineage is returned root-to-head. Timestamps
        # have only one-second resolution and generated artifact IDs are random (or
        # content-derived), so neither can safely break same-second revision ties.
        return sorted(
            visible,
            key=lambda record: (record.logical_id, record.revision, record.artifact_id),
        )

    def update_artifact(
        self,
        artifact_id: str,
        *,
        expected_payload_hash: str | None = None,
        expected_input_hash: str | None = None,
        expected_status: str | None = None,
        **changes,
    ) -> ArtifactRecord:
        """Update only an editable draft/review artifact; terminal states are immutable."""
        record = self.get_artifact(artifact_id)
        if record.status in IMMUTABLE_ARTIFACT_STATUSES:
            raise ImmutableRecordError(
                f"artifact {artifact_id} is {record.status}; create a new revision instead")
        family = [item for item in self._loaded_artifact_inventory()
                  if item.logical_id == record.logical_id]
        latest = max(family, key=lambda item: item.revision)
        if latest.artifact_id != record.artifact_id:
            raise ImmutableRecordError(
                "a historical artifact revision cannot be updated")
        if expected_payload_hash is not None and record.payload_hash != expected_payload_hash:
            raise WorkspaceStoreError("artifact payload changed since it was loaded")
        if expected_input_hash is not None and record.input_hash != expected_input_hash:
            raise WorkspaceStoreError("artifact input changed since it was loaded")
        if expected_status is not None and record.status != expected_status:
            raise WorkspaceStoreError("artifact lifecycle state changed since it was loaded")

        allowed = {
            "status", "input_snapshot", "payload", "source_identity", "reviewer", "reason",
            "related_run_id", "provenance", "reviewed_at",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise MalformedRecordError(f"unsupported artifact fields: {sorted(unknown)}")
        before = record.to_dict()
        if "status" in changes:
            record.status = _artifact_status(changes["status"])
        if "input_snapshot" in changes:
            record.input_snapshot = dict(changes["input_snapshot"] or {})
            record.input_hash = identity_hash(record.input_snapshot)
        if "payload" in changes:
            record.payload = dict(changes["payload"] or {})
            record.payload_hash = identity_hash(record.payload)
        if "source_identity" in changes:
            record.source_identity = dict(changes["source_identity"] or {})
        if "reviewer" in changes:
            record.reviewer = str(changes["reviewer"] or "").strip()
        if "reason" in changes:
            record.reason = str(changes["reason"] or "").strip()
        if "provenance" in changes:
            record.provenance = dict(changes["provenance"] or {})
        if "reviewed_at" in changes:
            record.reviewed_at = changes["reviewed_at"]
        if "related_run_id" in changes:
            related_run_id = changes["related_run_id"]
            if related_run_id is not None:
                run = self.get_run(related_run_id)
                if run.project_id != record.project_id or run.material_id != record.material_id:
                    raise WorkspaceStoreError("related run does not share artifact context")
            record.related_run_id = related_run_id

        if record.status in {"finalized", "reviewed", "rejected"}:
            if not record.reviewer or not record.reason:
                raise MalformedRecordError(
                    f"{record.status} artifact requires reviewer/resolver and reason")
            if not record.reviewed_at:
                record.reviewed_at = _now()
        if before != record.to_dict():
            record.updated_at = _now()
            # Reads never rewrite legacy records. A caller-authorized mutation,
            # however, is a new persistence event and is written with the current
            # integrity-bearing schema instead of extending schema 1 silently.
            record.schema_version = SCHEMA_VERSION
            record.envelope_hash = artifact_envelope_hash(record)
            self._atomic_write(
                self._record_path("artifacts", artifact_id), record.to_dict())
        return record

    def create_artifact_revision(
        self,
        artifact_id: str,
        *,
        status: str = "draft",
        payload: dict | None = None,
        input_snapshot: dict | None = None,
        source_identity: dict | None = None,
        creator: str = "",
        reason: str = "",
        provenance: dict | None = None,
    ) -> ArtifactRecord:
        """Create a linked revision; the previous immutable record is never rewritten."""
        previous = self.get_artifact(artifact_id)
        return self.create_artifact(
            previous.project_id,
            previous.material_id,
            previous.record_type,
            previous.input_snapshot if input_snapshot is None else input_snapshot,
            status=status,
            payload=previous.payload if payload is None else payload,
            source_identity=previous.source_identity if source_identity is None else source_identity,
            creator=creator,
            reason=reason,
            provenance=previous.provenance if provenance is None else provenance,
            revision=previous.revision + 1,
            previous_artifact_id=previous.artifact_id,
            logical_id=previous.logical_id,
        )

    def link_artifact_to_material(self, artifact_id: str, *, evidence: bool = False) -> MaterialRecord:
        record = self.get_artifact(artifact_id)
        if record.material_id is None:
            raise WorkspaceStoreError("project-scoped artifact has no material link")
        material = self.get_material(record.material_id)
        if material.project_id != record.project_id:
            raise WorkspaceStoreError("artifact/material project binding is invalid")
        field_name = "evidence_references" if evidence else "measurement_references"
        references = list(getattr(material, field_name))
        if artifact_id not in references:
            references.append(artifact_id)
            material = self.update_material(material.material_id, **{field_name: references})
        return material

    def unlink_artifact_from_material(self, artifact_id: str, *, evidence: bool = False) -> MaterialRecord:
        record = self.get_artifact(artifact_id)
        if record.material_id is None:
            raise WorkspaceStoreError("project-scoped artifact has no material link")
        material = self.get_material(record.material_id)
        field_name = "evidence_references" if evidence else "measurement_references"
        references = [item for item in getattr(material, field_name) if item != artifact_id]
        return self.update_material(material.material_id, **{field_name: references})

    def create_run(self, project_id: str, material_id: str, machine_id: str,
                   input_snapshot: dict, *, status: str, output_type: str,
                   epistemic_type: str, result_data: dict | None = None,
                   warnings: Iterable[str] = (), validation_state: str = "not_evaluated",
                   model_identity: dict | None = None, environment_identity: dict | None = None,
                   evidence_identity: Iterable[dict] = (), result_location: str | None = None,
                   legacy_references: Iterable[dict] = (), run_id: str | None = None) -> RunRecord:
        self.get_project(project_id)
        material = self.get_material(material_id)
        if material.project_id != project_id:
            raise WorkspaceStoreError("material does not belong to the selected project")
        canonical_machine = virtual_lab_machines.canonical_machine_id(machine_id)
        if canonical_machine is None:
            raise MalformedRecordError(f"unknown machine id: {machine_id!r}")
        snapshot = dict(input_snapshot or {})
        _canonical_json(snapshot)
        output = dict(result_data or {})
        result_hash = identity_hash(output)
        record = RunRecord(
            run_id=self._validate_id(run_id, RUN_PREFIX) if run_id else _new_id(RUN_PREFIX),
            project_id=project_id, material_id=material_id, machine_id=canonical_machine,
            input_snapshot=snapshot, input_hash=identity_hash(snapshot),
            material_revision=material.revision,
            composition_revision=material.composition_revision,
            assumption_revision=material.assumption_revision,
            material_snapshot_hash=material_identity_hash(material),
            status=str(status or "unknown"), output_type=str(output_type or "unspecified"),
            epistemic_type=str(epistemic_type or "unspecified"),
            result_data=output, warnings=[str(item) for item in warnings],
            validation_state=str(validation_state or "not_evaluated"),
            model_identity=dict(model_identity or {}),
            environment_identity=dict(environment_identity or {}),
            evidence_identity=[dict(item) for item in evidence_identity],
            result_location=str(result_location) if result_location else None,
            legacy_references=[dict(item) for item in legacy_references],
            result_hash=result_hash,
        )
        record.envelope_hash = run_envelope_hash(record)
        self._validate_loaded_run(record)
        self._atomic_write(self._record_path("runs", record.run_id), record.to_dict(),
                           create_only=True)
        if record.run_id not in material.associated_run_ids:
            self.update_material(material_id,
                                 associated_run_ids=[*material.associated_run_ids, record.run_id])
        return record

    def _validate_artifact_identity_shape(
        self,
        value: Any,
        label: str,
        *,
        require_exact: bool = False,
    ) -> None:
        if not isinstance(value, dict):
            raise MalformedRecordError(f"{label} must be an object")
        if "artifact_id" in value:
            self._validate_id(value["artifact_id"], ARTIFACT_PREFIX)
            if "logical_id" in value:
                self._validate_id(value["logical_id"], ARTIFACT_PREFIX)
            if "record_type" in value:
                _artifact_type(value["record_type"])
            revision = value.get("revision")
            if require_exact or revision is not None:
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                    raise MalformedRecordError(f"{label}.revision must be a positive integer")
            for hash_name in ("input_hash", "payload_hash"):
                if require_exact or hash_name in value:
                    _require_sha256(value.get(hash_name), f"{label}.{hash_name}")

    def _validate_loaded_run(self, record: RunRecord) -> RunRecord:
        self._validate_id(record.run_id, RUN_PREFIX)
        self._validate_id(record.project_id, PROJECT_PREFIX)
        self._validate_id(record.material_id, MATERIAL_PREFIX)
        self.get_project(record.project_id)
        material = self._read_material_record(record.material_id)
        if material.project_id != record.project_id:
            raise WorkspaceStoreError("run material belongs to another project")
        canonical_machine = virtual_lab_machines.canonical_machine_id(record.machine_id)
        if canonical_machine is None or canonical_machine != record.machine_id:
            raise MalformedRecordError("run machine_id is not a canonical machine identity")
        for name in ("material_revision", "composition_revision", "assumption_revision"):
            value = getattr(record, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise MalformedRecordError(f"run {name} must be a positive integer")
        if not isinstance(record.input_snapshot, dict):
            raise MalformedRecordError("run input_snapshot must be an object")
        _require_sha256(record.input_hash, "run input_hash")
        if identity_hash(record.input_snapshot) != record.input_hash:
            raise MalformedRecordError("run input hash does not match its stored snapshot")
        _require_sha256(record.material_snapshot_hash, "run material_snapshot_hash")
        if not isinstance(record.result_data, dict):
            raise MalformedRecordError("run result_data must be an object")
        if record.result_hash is not None:
            _require_sha256(record.result_hash, "run result_hash")
            if identity_hash(record.result_data) != record.result_hash:
                raise MalformedRecordError(
                    "run result payload hash changed from its stored integrity digest")
        if record.schema_version >= 2:
            if record.result_hash is None:
                raise MalformedRecordError(
                    "Phase 3 run is missing its required result integrity hash")
            _require_sha256(record.result_hash, "run result_hash")
            _require_sha256(record.envelope_hash, "run envelope_hash")
            if run_envelope_hash(record) != record.envelope_hash:
                raise MalformedRecordError(
                    "run envelope hash does not match its stored record")
        elif record.envelope_hash is not None:
            _require_sha256(record.envelope_hash, "run envelope_hash")
            if run_envelope_hash(record) != record.envelope_hash:
                raise MalformedRecordError(
                    "schema-1 run envelope hash does not match its stored record")
        for name in ("model_identity", "environment_identity"):
            if not isinstance(getattr(record, name), dict):
                raise MalformedRecordError(f"run {name} must be an object")
        for name in ("evidence_identity", "legacy_references"):
            values = getattr(record, name)
            if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
                raise MalformedRecordError(f"run {name} must be a list of objects")
            for index, item in enumerate(values):
                self._validate_artifact_identity_shape(item, f"run {name}[{index}]")
        if not isinstance(record.warnings, list) \
                or any(not isinstance(item, str) for item in record.warnings):
            raise MalformedRecordError("run warnings must be a list of strings")
        if record.result_location is not None and not isinstance(record.result_location, str):
            raise MalformedRecordError("run result_location must be a string or null")
        for name in ("status", "output_type", "epistemic_type", "validation_state", "created_at"):
            if not isinstance(getattr(record, name), str) or not getattr(record, name):
                raise MalformedRecordError(f"run {name} must be a non-empty string")
        dependencies = record.input_snapshot.get("artifact_identities", [])
        if not isinstance(dependencies, list):
            raise MalformedRecordError("run artifact_identities must be a list")
        if dependencies and record.result_hash is None:
            raise MalformedRecordError(
                "Phase 3 run is missing its required result integrity hash")
        for index, dependency in enumerate(dependencies):
            self._validate_artifact_identity_shape(
                dependency, f"run input artifact_identities[{index}]", require_exact=True)
        return record

    def get_run(self, run_id: str) -> RunRecord:
        record = _record_from_dict(
            RunRecord,
            self._read(self._record_path("runs", run_id), "run"),
        )
        return self._validate_loaded_run(record)

    def list_runs(self, *, project_id: str | None = None, material_id: str | None = None,
                  machine_id: str | None = None) -> list[RunRecord]:
        if project_id is not None:
            self.get_project(project_id)
        if material_id is not None:
            self.get_material(material_id)
        canonical_machine = None
        if machine_id is not None:
            canonical_machine = virtual_lab_machines.canonical_machine_id(machine_id)
            if canonical_machine is None:
                raise MalformedRecordError(f"unknown machine id: {machine_id!r}")
        records = [self._validate_loaded_run(record) for record in
                   self._list_records("runs", RunRecord, "run")]
        return [record for record in records
                if (project_id is None or record.project_id == project_id)
                and (material_id is None or record.material_id == material_id)
                and (canonical_machine is None or record.machine_id == canonical_machine)]

    def run_staleness(self, run: RunRecord | str) -> tuple[bool, list[str]]:
        record = self.get_run(run) if isinstance(run, str) else run
        try:
            material = self.get_material(record.material_id)
        except RecordNotFoundError:
            return True, ["material record is missing"]
        reasons = []
        if material.project_id != record.project_id:
            reasons.append("project/material binding changed")
        if material.revision != record.material_revision:
            reasons.append("material revision changed")
        if material.composition_revision != record.composition_revision:
            reasons.append("composition revision changed")
        if material.assumption_revision != record.assumption_revision:
            reasons.append("assumption/process revision changed")
        if material_identity_hash(material) != record.material_snapshot_hash:
            reasons.append("material identity hash changed")
        artifact_identities = record.input_snapshot.get("artifact_identities", []) \
            if isinstance(record.input_snapshot, dict) else []
        if artifact_identities and not isinstance(artifact_identities, list):
            reasons.append("artifact dependency identity is malformed")
            artifact_identities = []
        for dependency in artifact_identities:
            if not isinstance(dependency, dict):
                reasons.append("artifact dependency identity is malformed")
                continue
            artifact_id = dependency.get("artifact_id")
            try:
                artifact = self.get_artifact(artifact_id)
            except WorkspaceStoreError:
                reasons.append(f"artifact dependency is missing: {artifact_id}")
                continue
            if artifact.project_id != record.project_id or artifact.material_id != record.material_id:
                reasons.append(f"artifact dependency context changed: {artifact_id}")
            if dependency.get("revision") is not None \
                    and dependency.get("revision") != artifact.revision:
                reasons.append(f"artifact revision changed: {artifact_id}")
            if dependency.get("payload_hash") \
                    and dependency.get("payload_hash") != artifact.payload_hash:
                reasons.append(f"artifact payload changed: {artifact_id}")
            if dependency.get("input_hash") \
                    and dependency.get("input_hash") != artifact.input_hash:
                reasons.append(f"artifact input changed: {artifact_id}")
        return bool(reasons), reasons

    def current_runs(self, *, project_id: str, material_id: str) -> list[RunRecord]:
        return [record for record in self.list_runs(project_id=project_id, material_id=material_id)
                if not self.run_staleness(record)[0]]

    def get_active_context(self) -> dict:
        path = self._path("active_context.json")
        if not path.exists():
            return {"schema_version": SCHEMA_VERSION, "active_project_id": None,
                    "active_material_id": None, "active_run_id": None, "updated_at": None}
        payload = self._read(path)
        return {
            "schema_version": SCHEMA_VERSION,
            "active_project_id": payload.get("active_project_id"),
            "active_material_id": payload.get("active_material_id"),
            "active_run_id": payload.get("active_run_id"),
            "updated_at": payload.get("updated_at"),
        }

    def set_active_context(self, project_id: str | None, material_id: str | None,
                           run_id: str | None) -> dict:
        if project_id is None and (material_id is not None or run_id is not None):
            raise WorkspaceStoreError("material/run context requires a project")
        if material_id is None and run_id is not None:
            raise WorkspaceStoreError("run context requires a material")
        if project_id is not None:
            project = self.get_project(project_id)
            if project.archived:
                raise WorkspaceStoreError("an archived project cannot be active")
        if material_id is not None:
            material = self.get_material(material_id)
            if material.project_id != project_id:
                raise WorkspaceStoreError("material does not belong to active project")
        if run_id is not None:
            run = self.get_run(run_id)
            if run.project_id != project_id or run.material_id != material_id:
                raise WorkspaceStoreError("run does not belong to active project/material")
        payload = {"schema_version": SCHEMA_VERSION, "active_project_id": project_id,
                   "active_material_id": material_id, "active_run_id": run_id,
                   "updated_at": _now()}
        self._atomic_write(self._path("active_context.json"), payload)
        return payload

    def _list_records(self, kind: str, cls, expected_kind: str):
        directory = self._path(kind)
        if not directory.exists():
            return []
        if directory.is_symlink():
            raise UnsafePathError(f"record directory is a symlink: {directory}")
        records = []
        for path in sorted(directory.glob("*.json")):
            if path.name.startswith(".tmp-"):
                continue
            records.append(_record_from_dict(cls, self._read(path, expected_kind)))
        return sorted(records, key=lambda item: (item.created_at, getattr(item, f"{expected_kind}_id")))

    def diagnostics(self) -> dict:
        context = self.get_active_context()
        return {
            "schema_version": SCHEMA_VERSION,
            "storage_root": str(self.root),
            "projects": len(self.list_projects(include_archived=True)),
            "materials": len(self.list_materials()),
            "runs": len(self.list_runs()),
            "artifacts": len(self.list_artifacts()),
            "active_project_id": context.get("active_project_id"),
            "active_material_id": context.get("active_material_id"),
            "active_run_id": context.get("active_run_id"),
        }
