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
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config
from .instruments import virtual_lab_machines

SCHEMA_VERSION = 1
PROJECT_PREFIX = "prj_"
MATERIAL_PREFIX = "mat_"
RUN_PREFIX = "run_"
_ID_RE = re.compile(r"^(prj|mat|run)_[0-9a-f]{32}$")
_SECRET_KEYS = re.compile(
    r"(^|_)(api_?key|token|password|passwd|secret|cookie|authorization|credential)s?($|_)",
    re.IGNORECASE,
)


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


def _assert_no_secrets(value: Any, path: str = "record") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SECRET_KEYS.search(str(key)):
                raise MalformedRecordError(f"secret-like field is not permitted: {path}.{key}")
            _assert_no_secrets(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_no_secrets(child, f"{path}[{index}]")


def _record_from_dict(cls, payload: dict):
    known = {item.name for item in fields(cls)}
    return cls(**{key: value for key, value in payload.items() if key in known})


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

    def to_dict(self) -> dict:
        return asdict(self)


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
                  "runs": RUN_PREFIX}.get(kind)
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
            if create_only and path.exists():
                raise DuplicateRecordError(f"record already exists: {path.stem}")
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
        if not path.exists():
            raise RecordNotFoundError(f"record not found: {path.stem}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MalformedRecordError(f"cannot read {path.name}: malformed JSON") from exc
        if not isinstance(payload, dict):
            raise MalformedRecordError(f"{path.name} must contain a JSON object")
        version = payload.get("schema_version")
        if not isinstance(version, int):
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

    def cleanup_stale_temps(self) -> int:
        """Remove only this store's abandoned atomic-write temp files."""
        count = 0
        if not self.root.exists():
            return count
        for kind in ("projects", "materials", "runs"):
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
        self._atomic_write(self._record_path("materials", record.material_id), record.to_dict(),
                           create_only=True)
        return record

    def get_material(self, material_id: str) -> MaterialRecord:
        return _record_from_dict(MaterialRecord,
                                 self._read(self._record_path("materials", material_id), "material"))

    def list_materials(self, project_id: str | None = None) -> list[MaterialRecord]:
        if project_id is not None:
            self.get_project(project_id)
        records = self._list_records("materials", MaterialRecord, "material")
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
            result_data=dict(result_data or {}), warnings=[str(item) for item in warnings],
            validation_state=str(validation_state or "not_evaluated"),
            model_identity=dict(model_identity or {}),
            environment_identity=dict(environment_identity or {}),
            evidence_identity=[dict(item) for item in evidence_identity],
            result_location=str(result_location) if result_location else None,
            legacy_references=[dict(item) for item in legacy_references],
        )
        self._atomic_write(self._record_path("runs", record.run_id), record.to_dict(),
                           create_only=True)
        if record.run_id not in material.associated_run_ids:
            self.update_material(material_id,
                                 associated_run_ids=[*material.associated_run_ids, record.run_id])
        return record

    def get_run(self, run_id: str) -> RunRecord:
        return _record_from_dict(RunRecord,
                                 self._read(self._record_path("runs", run_id), "run"))

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
        records = self._list_records("runs", RunRecord, "run")
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
            "active_project_id": context.get("active_project_id"),
            "active_material_id": context.get("active_material_id"),
            "active_run_id": context.get("active_run_id"),
        }
