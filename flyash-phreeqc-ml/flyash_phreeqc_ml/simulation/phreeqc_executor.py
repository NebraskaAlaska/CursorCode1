"""Deterministic PHREEQC **execution** layer for confirmed Simulate-tab input previews.

This is the first layer that actually *runs* PHREEQC, deliberately separated from the
planning layer and from the scientific result path:

* It runs **only when called explicitly** (a user clicks "Run" after reviewing the input).
  Nothing here runs automatically after AI parsing or preview generation.
* **AI does not write or modify PHREEQC input** — it executes the *exact* reviewed text from
  :class:`simulation.phreeqc_input_builder.PhreeqcInputPreview`; this module never edits it.
* It writes only to a **safe simulation workspace** (``outputs/simulations/`` by default, or a
  caller-supplied run dir) — never ``data/raw``, never the source tree, never the processed
  pipeline CSVs. Generated files are gitignored.
* It is **off the scientific result path**: it imports no AI module, no comparison/residual/
  mapping module, and it writes nothing to the comparison CSVs or measured data. Its outputs
  are **simulation results, not validated predictions**.
* It **never crashes the app**: a missing binary, a failed run, or a timeout returns a typed,
  structured :class:`ExecutionResult` (status ``phreeqc_missing`` / ``failed`` / ``timeout``)
  rather than raising.

Parsing reuses the existing :mod:`parsers.pqo_parser` (the reliable ``.pqo`` output) plus the
optional :mod:`parsers.selected_output_parser` (a ``SELECTED_OUTPUT`` table when produced).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..parsers.pqo_parser import parse_pqo_file, records_to_frames
from ..parsers.selected_output_parser import parse_selected_output
from ..resources.models import (
    RESOURCE_MANIFEST_VERSION,
    CatalogState,
    ResourceContractError,
    ResourceKind,
    ResourceManifest,
)
from . import phreeqc_run_contract as _run_contract

# --------------------------------------------------------------------------- #
# Status vocabularies
# --------------------------------------------------------------------------- #
STATUS_NOT_RUN = "not_run"
STATUS_BLOCKED = "not_runnable"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_MISSING = "phreeqc_missing"
STATUS_TIMEOUT = "timeout"

PARSE_PARSED = "parsed"
PARSE_NO_SELECTED_OUTPUT = "no_selected_output"
PARSE_PARTIAL = "partial"
PARSE_FAILED = "parse_failed"

# Standing wording (single source).
NOT_CONFIGURED_MESSAGE = ("PHREEQC execution is not configured. You can still review and "
                          "download the input preview.")
SIM_OUTPUT_LABEL = ("Generated from PHREEQC execution of the reviewed simulation input. "
                    "Not validated against measured data.")

# A minimal, harmless smoke job — equilibrate pure water (no database-specific phases).
SMOKE_INPUT = "TITLE smoke test\nSOLUTION 1\n    pH 7\n    temp 25\n    units mol/kgw\nEND\n"

_TAIL_LINES = 40             # how many trailing stdout/stderr lines to keep
_MOLALITY_TO_MM = config.PHREEQC_MOLALITY_TO_MM


def _bounded_positive_int_env(name: str, default: int, *, maximum: int) -> int:
    """Return a safe cap; invalid environment text can never disable limits or break import."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw, 10)
    except (TypeError, ValueError):
        return default
    return value if 0 < value <= maximum else default


_MAX_CONFIGURABLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_INPUT_BYTES = _bounded_positive_int_env(
    "PHREEQC_MAX_INPUT_BYTES", 2 * 1024 * 1024, maximum=_MAX_CONFIGURABLE_BYTES)
MAX_OUTPUT_BYTES = _bounded_positive_int_env(
    "PHREEQC_MAX_OUTPUT_BYTES", 50 * 1024 * 1024, maximum=_MAX_CONFIGURABLE_BYTES)
MAX_SELECTED_OUTPUT_BYTES = _bounded_positive_int_env(
    "PHREEQC_MAX_SELECTED_OUTPUT_BYTES", 50 * 1024 * 1024,
    maximum=_MAX_CONFIGURABLE_BYTES)
MAX_STDOUT_BYTES = _bounded_positive_int_env(
    "PHREEQC_MAX_STDOUT_BYTES", 1024 * 1024, maximum=_MAX_CONFIGURABLE_BYTES)
MAX_STDERR_BYTES = _bounded_positive_int_env(
    "PHREEQC_MAX_STDERR_BYTES", 1024 * 1024, maximum=_MAX_CONFIGURABLE_BYTES)
MAX_WORKSPACE_BYTES = _bounded_positive_int_env(
    "PHREEQC_MAX_WORKSPACE_BYTES", 128 * 1024 * 1024,
    maximum=_MAX_CONFIGURABLE_BYTES)
MAX_TIMEOUT_SECONDS = 24 * 60 * 60
_JOB_MARKER = ".phreeqc-job.json"
_SNAPSHOT_EXECUTABLE = ".verified-phreeqc-runtime"
_SNAPSHOT_DATABASE = ".verified-phreeqc-database.dat"
_MAX_IDENTITY_DOCUMENT_BYTES = 16 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OCI_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _snapshot_executable_name(executable_path: str) -> str:
    """Keep the Windows PE suffix because CreateProcess may require it."""
    return (_SNAPSHOT_EXECUTABLE + ".exe"
            if Path(executable_path).suffix.lower() == ".exe" else _SNAPSHOT_EXECUTABLE)


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #
@dataclass
class PhreeqcAvailability:
    """What is configured / present for PHREEQC execution (no run unless ``run_smoke``)."""

    executable_configured: bool
    database_configured: bool
    executable_found: bool
    database_found: bool
    executable_path: str | None
    database_path: str | None
    message: str
    smoke_ok: bool | None = None        # None = smoke not attempted
    environment_identity: _run_contract.ExecutionEnvironmentIdentity | None = None
    release_provenance: dict = field(default_factory=dict)

    @property
    def can_run(self) -> bool:
        return (self.executable_found and self.database_found
                and self.environment_identity is not None)


@dataclass
class ExecutionResult:
    """Structured outcome of one PHREEQC execution attempt (never an exception)."""

    scenario_id: str
    status: str
    input_path: str | None = None
    output_path: str | None = None
    selected_output_path: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error_message: str | None = None
    runtime_seconds: float | None = None
    timestamp: str | None = None
    phreeqc_executable: str | None = None
    database_path: str | None = None
    input_hash: str | None = None
    job_id: str | None = None
    workspace_path: str | None = None
    phreeqc_version: str | None = None
    executable_sha256: str | None = None
    database_resource_id: str | None = None
    database_version: str | None = None
    database_sha256: str | None = None
    environment_identity_hash: str | None = None
    container_image_digest: str | None = None
    resource_manifest_version: str | None = None
    runtime_resource_id: str | None = None
    runtime_installation_id: str | None = None
    database_installation_id: str | None = None
    runtime_manifest_sha256: str | None = None
    resource_catalog_hash: str | None = None
    resource_catalog_generation: int | None = None
    resource_bootstrap_result_sha256: str | None = None
    knowledge_pack_hash: str | None = None
    source_manifest_sha256: str | None = None
    app_version: str | None = None
    app_vcs_ref: str | None = None
    resource_identity_status: str = "file_hashes_only"
    cleanup_token: str | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id, "status": self.status,
            "input_path": self.input_path, "output_path": self.output_path,
            "selected_output_path": self.selected_output_path,
            "error_message": self.error_message, "runtime_seconds": self.runtime_seconds,
            "timestamp": self.timestamp, "phreeqc_executable": self.phreeqc_executable,
            "database_path": self.database_path, "input_hash": self.input_hash,
            "job_id": self.job_id, "workspace_path": self.workspace_path,
            "phreeqc_version": self.phreeqc_version,
            "executable_sha256": self.executable_sha256,
            "database_resource_id": self.database_resource_id,
            "database_version": self.database_version,
            "database_sha256": self.database_sha256,
            "environment_identity_hash": self.environment_identity_hash,
            "container_image_digest": self.container_image_digest,
            "resource_manifest_version": self.resource_manifest_version,
            "runtime_resource_id": self.runtime_resource_id,
            "runtime_installation_id": self.runtime_installation_id,
            "database_installation_id": self.database_installation_id,
            "runtime_manifest_sha256": self.runtime_manifest_sha256,
            "resource_catalog_hash": self.resource_catalog_hash,
            "resource_catalog_generation": self.resource_catalog_generation,
            "resource_bootstrap_result_sha256": self.resource_bootstrap_result_sha256,
            "knowledge_pack_hash": self.knowledge_pack_hash,
            "source_manifest_sha256": self.source_manifest_sha256,
            "app_version": self.app_version,
            "app_vcs_ref": self.app_vcs_ref,
            "resource_identity_status": self.resource_identity_status,
        }


@dataclass
class ParsedSimulation:
    """Basic parsed outputs from a successful run (pH / pe / totals / SI / selected output)."""

    scenario_id: str
    parse_status: str
    pH: float | None = None
    pe: float | None = None
    element_totals_mM: dict = field(default_factory=dict)      # {element: mM}
    saturation_indices: list = field(default_factory=list)     # [{phase, SI}]
    selected_output: object = None                             # DataFrame or None
    warnings: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    n_states: int = 0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _safe_stem(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", str(text)).strip("_") or "scenario"


def _tail(text: str | None, n: int = _TAIL_LINES) -> str:
    if not text:
        return ""
    lines = str(text).splitlines()
    return "\n".join(lines[-n:])


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None       # drop NaN


def default_workspace() -> Path:
    """The default safe execution workspace (``outputs/simulations/``)."""
    return config.SIMULATIONS_DIR


# Directories the executor must never write into.
def _forbidden_roots() -> tuple[Path, ...]:
    return (config.RAW_DIR, config.PROCESSED_DIR, config.PACKAGE_DIR)


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def assert_safe_workspace(path) -> Path:
    """Return the resolved workspace path, or raise ``ValueError`` if it is unsafe.

    Refuses any directory inside ``data/raw``, ``data/processed``, or the package source
    tree — generated simulation files must never land there.
    """
    rp = Path(path).resolve()
    for root in _forbidden_roots():
        if _is_within(rp, root):
            raise ValueError(
                f"refusing to use {rp} as a simulation workspace — it is inside the "
                f"protected directory {root}.")
    return rp


class _ResourceIdentityError(ValueError):
    """Configured release metadata does not identify the executable/database exactly."""


def _identity_file_bytes(path_value: str, label: str, *, maximum: int) -> tuple[bytes, Path]:
    """Read a bounded, stable, non-symlink identity document."""
    location = Path(path_value).expanduser().absolute()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(location, flags)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 \
                    or before.st_size > maximum:
                raise OSError(f"size must be between 1 and {maximum} bytes")
            payload = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
        if len(payload) > maximum:
            raise OSError(f"size exceeds {maximum} bytes")
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")
        if any(getattr(before, key) != getattr(after, key) for key in stable) \
                or len(payload) != before.st_size:
            raise OSError("file changed while it was being read")
        return payload, location.resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise _ResourceIdentityError(f"cannot verify {label}: {exc}") from exc


def _identity_json(path_value: str, label: str) -> tuple[dict, str, Path]:
    payload, resolved = _identity_file_bytes(
        path_value, label, maximum=_MAX_IDENTITY_DOCUMENT_BYTES)
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ResourceIdentityError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise _ResourceIdentityError(f"{label} must be a JSON object")
    return document, hashlib.sha256(payload).hexdigest(), resolved


def _declared_sha256(name: str) -> str | None:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        return None
    if not _SHA256_RE.fullmatch(value):
        raise _ResourceIdentityError(f"{name} must be an exact lower-case SHA-256")
    return value


def _path_is_exact(value: str, expected: str) -> bool:
    try:
        return Path(value).resolve(strict=True) == Path(expected).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return False


def _release_provenance(environment: _run_contract.ExecutionEnvironmentIdentity) -> dict:
    """Validate configured release metadata and return only evidence-backed identities.

    A local user-supplied executable/database remains usable with exact file hashes.  Once a
    deployment declares a manifest, catalog, bootstrap result, source manifest, or image digest,
    malformed or inconsistent declarations block execution instead of being copied into run
    provenance as if verified.
    """
    declared_version = str(os.environ.get("PHREEQC_VERSION", "")).strip()
    declared_database_id = str(os.environ.get("PHREEQC_DATABASE_ID", "")).strip()
    declared_database_version = str(
        os.environ.get("PHREEQC_DATABASE_VERSION", "")).strip()
    declared_database_sha = _declared_sha256("PHREEQC_DATABASE_SHA256")
    if declared_database_sha and declared_database_sha != environment.database.sha256:
        raise _ResourceIdentityError(
            "PHREEQC_DATABASE_SHA256 does not match the configured database bytes")

    image_digest = str(os.environ.get("PHREEQC_CONTAINER_IMAGE_DIGEST", "")).strip()
    if image_digest and not _OCI_DIGEST_RE.fullmatch(image_digest):
        raise _ResourceIdentityError(
            "PHREEQC_CONTAINER_IMAGE_DIGEST must be an exact lower-case OCI sha256 digest")

    provenance = {
        "phreeqc_version": declared_version or None,
        "database_resource_id": declared_database_id or None,
        "database_version": declared_database_version or None,
        "container_image_digest": image_digest or None,
        "app_version": str(os.environ.get("APP_VERSION", "")).strip() or None,
        "app_vcs_ref": str(os.environ.get("APP_VCS_REF", "")).strip() or None,
        "resource_identity_status": "file_hashes_only",
    }

    source_path = str(os.environ.get("PHREEQC_SOURCE_MANIFEST", "")).strip()
    declared_source_sha = _declared_sha256("PHREEQC_SOURCE_MANIFEST_SHA256")
    if bool(source_path) != bool(declared_source_sha):
        raise _ResourceIdentityError(
            "PHREEQC source manifest path and SHA-256 must be configured together")
    if source_path and declared_source_sha:
        payload, _ = _identity_file_bytes(
            source_path, "PHREEQC source manifest", maximum=_MAX_IDENTITY_DOCUMENT_BYTES)
        observed = hashlib.sha256(payload).hexdigest()
        if observed != declared_source_sha:
            raise _ResourceIdentityError(
                "PHREEQC source manifest bytes do not match the declared SHA-256")
        provenance["source_manifest_sha256"] = observed

    runtime_manifest_path = str(os.environ.get("PHREEQC_RUNTIME_MANIFEST", "")).strip()
    runtime_manifest = None
    if runtime_manifest_path:
        document, manifest_sha, _ = _identity_json(
            runtime_manifest_path, "PHREEQC runtime manifest")
        try:
            runtime_manifest = ResourceManifest.from_dict(document)
        except ResourceContractError as exc:
            raise _ResourceIdentityError(f"PHREEQC runtime manifest is invalid: {exc}") from exc
        if runtime_manifest.resource_kind != ResourceKind.PHREEQC_RUNTIME:
            raise _ResourceIdentityError("PHREEQC runtime manifest has the wrong resource kind")
        if runtime_manifest.executable_sha256 != environment.executable.sha256 \
                or not _path_is_exact(
                    runtime_manifest.install_path, environment.executable.resolved_path):
            raise _ResourceIdentityError(
                "PHREEQC runtime manifest does not identify the configured executable")
        if not runtime_manifest.installed_version:
            raise _ResourceIdentityError("PHREEQC runtime manifest has no installed version")
        if declared_version and declared_version != runtime_manifest.installed_version:
            raise _ResourceIdentityError(
                "PHREEQC_VERSION does not match the verified runtime manifest")
        declared_runtime_id = str(os.environ.get("PHREEQC_RUNTIME_ID", "")).strip()
        if declared_runtime_id and declared_runtime_id != runtime_manifest.resource_id:
            raise _ResourceIdentityError(
                "PHREEQC_RUNTIME_ID does not match the verified runtime manifest")
        provenance.update({
            "phreeqc_version": runtime_manifest.installed_version,
            "runtime_resource_id": runtime_manifest.resource_id,
            "runtime_installation_id": runtime_manifest.installation_id,
            "runtime_manifest_sha256": manifest_sha,
            "resource_manifest_version": str(runtime_manifest.manifest_version),
            "resource_identity_status": "verified_runtime_manifest",
        })

    catalog_path = str(os.environ.get("VLAB_RESOURCE_CATALOG", "")).strip()
    active_runtime_id = str(
        os.environ.get("VLAB_ACTIVE_RUNTIME_INSTALLATION_ID", "")).strip()
    active_database_id = str(
        os.environ.get("VLAB_ACTIVE_DATABASE_INSTALLATION_ID", "")).strip()
    bootstrap_path = str(os.environ.get("VLAB_RESOURCE_BOOTSTRAP_RESULT", "")).strip()
    declared_knowledge_hash = _declared_sha256("VLAB_KNOWLEDGE_PACK_HASH")
    if not catalog_path and any((active_runtime_id, active_database_id, bootstrap_path,
                                 declared_knowledge_hash)):
        raise _ResourceIdentityError(
            "active resource/bootstrap identities require VLAB_RESOURCE_CATALOG")

    state = None
    catalog_root = None
    active_runtime = None
    active_database = None
    if catalog_path:
        document, _catalog_file_sha, resolved_catalog = _identity_json(
            catalog_path, "active scientific resource catalog")
        try:
            state = CatalogState.from_dict(document)
        except ResourceContractError as exc:
            raise _ResourceIdentityError(f"active resource catalog is invalid: {exc}") from exc
        catalog_root = resolved_catalog.parent
        by_installation = {item.installation_id: item for item in state.resources}
        active_manifests = [by_installation[item] for item in state.active.values()]
        runtime_matches = [
            item for item in active_manifests
            if item.resource_kind == ResourceKind.PHREEQC_RUNTIME
            and item.executable_sha256 == environment.executable.sha256
            and _path_is_exact(item.install_path, environment.executable.resolved_path)
        ]
        database_matches = [
            item for item in active_manifests
            if item.resource_kind in {
                ResourceKind.PHREEQC_OFFICIAL_DATABASE,
                ResourceKind.EXTERNAL_THERMODYNAMIC_DATABASE,
            }
            and item.database_sha256 == environment.database.sha256
            and _path_is_exact(item.install_path, environment.database.resolved_path)
        ]
        if len(runtime_matches) != 1 or len(database_matches) != 1:
            raise _ResourceIdentityError(
                "active resource catalog does not uniquely identify this executable/database")
        active_runtime = runtime_matches[0]
        active_database = database_matches[0]
        if active_runtime_id and active_runtime_id != active_runtime.installation_id:
            raise _ResourceIdentityError(
                "active runtime installation ID does not match the catalog")
        if active_database_id and active_database_id != active_database.installation_id:
            raise _ResourceIdentityError(
                "active database installation ID does not match the catalog")
        if runtime_manifest and (
                runtime_manifest.installation_id != active_runtime.installation_id
                or runtime_manifest.resource_id != active_runtime.resource_id
                or runtime_manifest.installed_version != active_runtime.installed_version
                or runtime_manifest.executable_sha256 != active_runtime.executable_sha256):
            raise _ResourceIdentityError(
                "runtime manifest and active catalog identify different installations")
        if declared_version and declared_version != active_runtime.installed_version:
            raise _ResourceIdentityError(
                "PHREEQC_VERSION does not match the active runtime catalog manifest")
        if declared_database_id and declared_database_id != active_database.resource_id:
            raise _ResourceIdentityError(
                "PHREEQC_DATABASE_ID does not match the active database catalog manifest")
        declared_database_manifest_id = str(
            os.environ.get("PHREEQC_DATABASE_MANIFEST_ID", "")).strip()
        if declared_database_manifest_id \
                and declared_database_manifest_id != active_database.resource_id:
            raise _ResourceIdentityError(
                "PHREEQC_DATABASE_MANIFEST_ID does not match the active database")
        if declared_database_version \
                and declared_database_version != active_database.installed_version:
            raise _ResourceIdentityError(
                "PHREEQC_DATABASE_VERSION does not match the active database manifest")
        provenance.update({
            "phreeqc_version": active_runtime.installed_version,
            "runtime_resource_id": active_runtime.resource_id,
            "runtime_installation_id": active_runtime.installation_id,
            "database_resource_id": active_database.resource_id,
            "database_installation_id": active_database.installation_id,
            "database_version": active_database.installed_version,
            "resource_manifest_version": str(RESOURCE_MANIFEST_VERSION),
            "resource_catalog_hash": state.catalog_hash,
            "resource_catalog_generation": state.generation,
            "resource_identity_status": "verified_active_catalog",
        })

    bootstrap = None
    if bootstrap_path:
        document, bootstrap_sha, _ = _identity_json(
            bootstrap_path, "release resource bootstrap result")
        allowed = {
            "schema", "version", "phreeqc_version", "runtime_installation_id",
            "active_database_installation_id", "registered_database_installation_ids",
            "catalog_generation", "catalog_hash", "registry_file_sha256",
            "knowledge_pack_hash", "knowledge_pack_path",
        }
        if set(document) != allowed:
            raise _ResourceIdentityError(
                "release bootstrap result does not use the closed version-1 contract")
        if document.get("schema") != "wpi.virtual-lab.release-resource-bootstrap-result" \
                or document.get("version") != 1:
            raise _ResourceIdentityError("release bootstrap result schema/version is invalid")
        registered = document.get("registered_database_installation_ids")
        if not isinstance(registered, list) or any(
                not isinstance(item, str) for item in registered):
            raise _ResourceIdentityError(
                "release bootstrap database installation IDs are invalid")
        for key in ("catalog_hash", "registry_file_sha256", "knowledge_pack_hash"):
            if not isinstance(document.get(key), str) \
                    or not _SHA256_RE.fullmatch(document[key]):
                raise _ResourceIdentityError(f"release bootstrap {key} is invalid")
        if state is None or active_runtime is None or active_database is None \
                or document["catalog_hash"] != state.catalog_hash \
                or document["catalog_generation"] != state.generation \
                or document["runtime_installation_id"] != active_runtime.installation_id \
                or document["active_database_installation_id"] \
                != active_database.installation_id \
                or active_database.installation_id not in registered \
                or document["phreeqc_version"] != active_runtime.installed_version:
            raise _ResourceIdentityError(
                "release bootstrap result does not match the active catalog")
        if declared_knowledge_hash \
                and declared_knowledge_hash != document["knowledge_pack_hash"]:
            raise _ResourceIdentityError(
                "VLAB_KNOWLEDGE_PACK_HASH does not match the bootstrap result")
        provenance["resource_bootstrap_result_sha256"] = bootstrap_sha
        bootstrap = document

    knowledge_path = None
    if bootstrap is not None:
        knowledge_path = str(bootstrap.get("knowledge_pack_path", "")).strip()
    elif declared_knowledge_hash and catalog_root is not None:
        knowledge_path = str(catalog_root / "knowledge" / "knowledge_pack.json")
    if knowledge_path:
        document, _knowledge_file_sha, _ = _identity_json(
            knowledge_path, "active scientific knowledge pack")
        try:
            from ..resources.knowledge import KnowledgePack, KnowledgePackError
        except ImportError as exc:
            raise _ResourceIdentityError(
                f"active knowledge-pack contract is unavailable: {exc}") from exc
        try:
            pack = KnowledgePack.from_dict(document)
        except KnowledgePackError as exc:
            raise _ResourceIdentityError(f"active knowledge pack is invalid: {exc}") from exc
        if state is None or pack.catalog_hash != state.knowledge_projection_hash:
            raise _ResourceIdentityError(
                "active knowledge pack does not match the active catalog projection")
        expected_pack_hash = (bootstrap["knowledge_pack_hash"] if bootstrap is not None
                              else declared_knowledge_hash)
        if pack.pack_hash != expected_pack_hash:
            raise _ResourceIdentityError(
                "active knowledge pack does not match its declared identity")
        provenance["knowledge_pack_hash"] = pack.pack_hash

    return provenance


def _environment_provenance(availability: PhreeqcAvailability) -> dict:
    """Return immutable resource identity fields without exposing secret configuration."""
    environment = availability.environment_identity
    if environment is None:
        return {}
    return {
        "executable_sha256": environment.executable.sha256,
        "database_sha256": environment.database.sha256,
        "environment_identity_hash": environment.identity_hash,
        **availability.release_provenance,
    }


def _new_job_workspace(root: Path, stem: str) -> tuple[str, str, Path]:
    """Atomically allocate an isolated directory for one execution attempt."""
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        job_id = f"{stem}-{uuid.uuid4().hex[:16]}"
        cleanup_token = uuid.uuid4().hex
        job_dir = assert_safe_workspace(root / job_id)
        try:
            job_dir.mkdir(mode=0o700)
        except FileExistsError:
            continue
        marker = job_dir / _JOB_MARKER
        try:
            marker.write_text(json.dumps({
                "schema": "wpi.virtual-lab.phreeqc-job-workspace",
                "version": 1,
                "job_id": job_id,
                "cleanup_token": cleanup_token,
            }, sort_keys=True), encoding="utf-8")
        except OSError:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        return job_id, cleanup_token, job_dir
    raise RuntimeError("could not allocate an isolated PHREEQC job workspace")


def cleanup_job_workspace(result: ExecutionResult) -> bool:
    """Delete only the isolated job directory named by a trusted execution result.

    Successful evidence is retained unless this explicit operation is requested.  Broad
    caller-supplied workspaces and results whose files escape the job directory are refused.
    """
    if not result.job_id or not result.workspace_path or not result.cleanup_token:
        return False
    raw_job_dir = Path(result.workspace_path).expanduser().absolute()
    try:
        job_details = raw_job_dir.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(job_details.st_mode) or not stat.S_ISDIR(job_details.st_mode):
        return False
    try:
        job_dir = assert_safe_workspace(raw_job_dir)
        exact_job_dir = raw_job_dir.resolve(strict=True)
    except (OSError, ValueError):
        return False
    if job_dir != exact_job_dir or job_dir.name != result.job_id:
        return False
    marker = job_dir / _JOB_MARKER
    try:
        marker_bytes, _ = _identity_file_bytes(
            str(marker), "PHREEQC job marker", maximum=4096)
        document = json.loads(marker_bytes.decode("utf-8"))
    except (_ResourceIdentityError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if document != {
                "schema": "wpi.virtual-lab.phreeqc-job-workspace",
                "version": 1,
                "job_id": result.job_id,
                "cleanup_token": result.cleanup_token,
            }:
        return False
    for value in (result.input_path, result.output_path, result.selected_output_path):
        if value and not _is_within(Path(value), job_dir):
            return False
    try:
        shutil.rmtree(job_dir)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Configuration / availability (never runs PHREEQC unless run_smoke=True)
# --------------------------------------------------------------------------- #
def _resolve_executable(exe: str | None) -> tuple[str | None, bool]:
    """``(resolved_path_or_None, configured)``. ``configured`` = a non-empty exe was set."""
    name = exe or config.PHREEQC_EXE_PATH
    configured = bool(name)
    if not configured:
        return None, False
    found = shutil.which(name) or (name if Path(name).is_file() else None)
    if not found:
        return None, configured
    try:
        return str(Path(found).expanduser().resolve(strict=True)), configured
    except OSError:
        return None, configured


def _resolve_database(database: str | None) -> tuple[str | None, bool]:
    db = database if database is not None else config.PHREEQC_DATABASE_PATH
    configured = bool(db)
    if not configured:
        return None, False
    path = Path(db).expanduser()
    if not path.is_file():
        return None, configured
    try:
        return str(path.resolve(strict=True)), configured
    except OSError:
        return None, configured


def _is_configured_release_target(executable_path: str, database_path: str) -> bool:
    """Whether deployment-level identity variables describe these exact configured paths."""
    declared_executable = str(os.environ.get("PHREEQC_EXE", "")).strip()
    declared_database = str(os.environ.get("PHREEQC_DATABASE", "")).strip()
    if not declared_executable or not declared_database:
        return False
    configured_executable, _ = _resolve_executable(declared_executable)
    configured_database, _ = _resolve_database(declared_database)
    return (configured_executable == executable_path and configured_database == database_path)


def check_availability(*, run_smoke: bool = False, exe: str | None = None,
                       database: str | None = None) -> PhreeqcAvailability:
    """Report whether PHREEQC can run. Only runs a tiny smoke job when ``run_smoke`` is True
    **and** both the executable and database are present."""
    exe_path, exe_conf = _resolve_executable(exe)
    db_path, db_conf = _resolve_database(database)
    exe_found = exe_path is not None
    db_found = db_path is not None

    if exe_found and db_found:
        message = "PHREEQC is configured and ready."
    elif not (exe_conf or db_conf):
        message = NOT_CONFIGURED_MESSAGE
    else:
        bits = []
        if not exe_found:
            bits.append("executable not found"
                        if exe_conf else "executable not configured (set PHREEQC_EXE)")
        if not db_found:
            bits.append("database not found"
                        if db_conf else "database not configured (set PHREEQC_DATABASE)")
        message = NOT_CONFIGURED_MESSAGE + "  (" + "; ".join(bits) + ")"

    environment = None
    release_provenance = {}
    if exe_found and db_found:
        if not os.access(exe_path, os.X_OK):
            message = NOT_CONFIGURED_MESSAGE + "  (configured executable is not executable)"
        else:
            try:
                environment = _run_contract.build_execution_environment(exe_path, db_path)
                if _is_configured_release_target(exe_path, db_path):
                    release_provenance = _release_provenance(environment)
                else:
                    release_provenance = {"resource_identity_status": "file_hashes_only"}
            except (_run_contract.RunContractError, _ResourceIdentityError) as exc:
                environment = None
                message = NOT_CONFIGURED_MESSAGE + f"  (release identity check failed: {exc})"

    av = PhreeqcAvailability(
        executable_configured=exe_conf, database_configured=db_conf,
        executable_found=exe_found, database_found=db_found,
        executable_path=exe_path, database_path=db_path, message=message,
        environment_identity=environment, release_provenance=release_provenance)

    if run_smoke and av.can_run and av.environment_identity is not None:
        av.smoke_ok = smoke_test(exe=exe, database=database)
    return av


def is_configured() -> bool:
    """True when both the executable and the database resolve (no run attempted)."""
    av = check_availability()
    return av.can_run and av.environment_identity is not None


def running_in_docker() -> bool:
    """Best-effort: are we running inside the app's container? (no run, never raises).

    True when the image marker ``APP_IN_DOCKER`` is set (the Dockerfile sets it) or the
    container sentinel ``/.dockerenv`` exists. Used only to phrase a clearer "local Streamlit
    without PHREEQC" vs "container, but PHREEQC env missing" message — never to gate execution.
    """
    import os
    if str(os.environ.get("APP_IN_DOCKER", "")).strip():
        return True
    try:
        return Path("/.dockerenv").exists()
    except Exception:                                        # noqa: BLE001
        return False


def availability_hint(av: PhreeqcAvailability | None = None) -> str:
    """One human, key-free line both Settings and the Assistant show, so they always agree.

    Distinguishes three cases the demo conflated: PHREEQC ready; running **locally** without
    PHREEQC (point at Docker / env vars); or inside the **container** but the PHREEQC env vars
    don't resolve (a deployment problem to fix in the image).
    """
    av = av if av is not None else check_availability()
    if av.can_run and av.environment_identity is not None:
        return "PHREEQC is configured and ready — simulations can run after you confirm."
    if running_in_docker():
        return ("PHREEQC is not ready in this container — " + av.message
                + " Check the image's PHREEQC_EXE / PHREEQC_DATABASE (see docs/deployment.md).")
    return ("Local Streamlit without PHREEQC — " + av.message
            + " The assistant still plans and builds a reviewable input; to actually run "
            "simulations, use the Docker image (docs/deployment.md) or set PHREEQC_EXE + "
            "PHREEQC_DATABASE to a PHREEQC CLI you supply.")


# --------------------------------------------------------------------------- #
# The execution primitive (shared by execute_preview + smoke_test)
# --------------------------------------------------------------------------- #
@dataclass
class _RunOutcome:
    returncode: int | None
    stdout: str
    stderr: str
    out_path: Path | None
    selected_output_path: Path | None
    runtime_seconds: float
    timed_out: bool
    error: str | None = None


class _BoundedCapture:
    """Drain one pipe fully while retaining at most its configured tail bytes."""

    def __init__(self, limit: int):
        self.limit = limit
        self.total = 0
        self.tail = bytearray()
        self.exceeded = threading.Event()
        self.failed = threading.Event()
        self.failure_message = ""

    def drain(self, stream) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                self.total += len(chunk)
                if self.total > self.limit:
                    self.exceeded.set()
                self.tail.extend(chunk)
                if len(self.tail) > self.limit:
                    del self.tail[:-self.limit]
        except OSError as exc:
            self.failure_message = str(exc)
            self.failed.set()
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def text(self) -> str:
        return bytes(self.tail).decode("utf-8", "replace")


def _validated_timeout(value) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("PHREEQC timeout must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"PHREEQC timeout must be between 0 and {MAX_TIMEOUT_SECONDS} seconds")
    return timeout


def _terminate_process_tree(process: subprocess.Popen) -> None:
    """Terminate the process group and reap the child; never leave a timed-out worker."""
    # A process-group leader may exit while one of its descendants remains alive and keeps
    # stdout/stderr open.  On POSIX, still signal the group in that state; returning merely
    # because the leader was reaped would orphan the descendant.
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        else:
            process.wait()
        return

    if process.poll() is not None:
        process.wait()
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
        else:
            process.terminate()
        process.wait(timeout=0.5)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _workspace_limit_error(workdir: Path, out_path: Path) -> str | None:
    total = 0
    try:
        entries = tuple(workdir.iterdir())
    except OSError as exc:
        return f"PHREEQC workspace could not be inspected safely: {exc}"
    for path in entries:
        try:
            details = path.lstat()
        except OSError as exc:
            return f"PHREEQC generated path could not be inspected safely: {exc}"
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            return f"PHREEQC generated a prohibited link/non-file path: {path.name}"
        total += int(details.st_size)
        if path == out_path and details.st_size > MAX_OUTPUT_BYTES:
            return f"PHREEQC output exceeds the {MAX_OUTPUT_BYTES}-byte limit."
        if path.name not in {
            out_path.name, _JOB_MARKER, _SNAPSHOT_EXECUTABLE,
            _SNAPSHOT_EXECUTABLE + ".exe", _SNAPSHOT_DATABASE,
        } and not path.name.endswith(".pqi") \
                and details.st_size > MAX_SELECTED_OUTPUT_BYTES:
            return (
                "PHREEQC selected/auxiliary output exceeds the "
                f"{MAX_SELECTED_OUTPUT_BYTES}-byte limit.")
    if total > MAX_WORKSPACE_BYTES:
        return f"PHREEQC workspace exceeds the {MAX_WORKSPACE_BYTES}-byte limit."
    return None


def _bounded_subprocess_run(command: list[str], *, cwd: Path, timeout: float,
                            out_path: Path):
    """Run PHREEQC with bounded stream capture, file monitoring, and process-group cleanup."""
    timeout = _validated_timeout(timeout)
    popen_options: dict = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": False,
    }
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(command, **popen_options)
    stdout = _BoundedCapture(MAX_STDOUT_BYTES)
    stderr = _BoundedCapture(MAX_STDERR_BYTES)
    threads = (
        threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True),
    )
    started_threads = []
    try:
        for thread in threads:
            thread.start()
            started_threads.append(thread)
    except RuntimeError as exc:
        _terminate_process_tree(process)
        for thread in started_threads:
            thread.join(timeout=2.0)
        raise OSError("could not start bounded PHREEQC output capture") from exc

    deadline = time.monotonic() + timeout
    timed_out = False
    error = None
    while process.poll() is None:
        if stdout.exceeded.is_set():
            error = f"PHREEQC stdout exceeds the {MAX_STDOUT_BYTES}-byte limit."
        elif stderr.exceeded.is_set():
            error = f"PHREEQC stderr exceeds the {MAX_STDERR_BYTES}-byte limit."
        elif stdout.failed.is_set() or stderr.failed.is_set():
            error = "PHREEQC output stream capture failed safely."
        else:
            error = _workspace_limit_error(cwd, out_path)
        if error:
            _terminate_process_tree(process)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            error = f"PHREEQC timed out after {timeout:g}s."
            _terminate_process_tree(process)
            break
        time.sleep(0.01)

    if process.poll() is None or os.name == "posix":
        # POSIX cleanup is intentionally also invoked after a normal leader exit: a
        # background descendant can otherwise outlive the PHREEQC job and retain its pipes.
        _terminate_process_tree(process)
    else:
        process.wait()
    for thread in threads:
        thread.join(timeout=2.0)
    if any(thread.is_alive() for thread in threads):
        error = error or "PHREEQC output streams did not close after process termination."
    if error is None:
        if stdout.exceeded.is_set():
            error = f"PHREEQC stdout exceeds the {MAX_STDOUT_BYTES}-byte limit."
        elif stderr.exceeded.is_set():
            error = f"PHREEQC stderr exceeds the {MAX_STDERR_BYTES}-byte limit."
        elif stdout.failed.is_set() or stderr.failed.is_set():
            error = "PHREEQC output stream capture failed safely."
        else:
            error = _workspace_limit_error(cwd, out_path)
    return process.returncode, stdout.text(), stderr.text(), timed_out, error


def _output_directive_error(input_text: str) -> str | None:
    """Reject external includes and unsafe PHREEQC ``-file`` destinations."""
    reserved = {
        _JOB_MARKER, _SNAPSHOT_EXECUTABLE, _SNAPSHOT_EXECUTABLE + ".exe",
        _SNAPSHOT_DATABASE,
    }
    safe_filename = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,179}$")
    for line_number, original in enumerate(str(input_text).splitlines(), start=1):
        line = original.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError:
            continue
        if tokens and tokens[0].lower() in {"include", "include$"}:
            return (
                f"PHREEQC external include directive on line {line_number} is not allowed; "
                "review one self-contained input instead.")
        for index, token in enumerate(tokens):
            if token.lower() != "-file":
                continue
            if index + 1 >= len(tokens):
                return f"PHREEQC -file directive on line {line_number} has no destination."
            filename = tokens[index + 1]
            if not filename or filename in reserved or not safe_filename.fullmatch(filename) \
                    or Path(filename).name != filename or "/" in filename \
                    or "\\" in filename or filename in {".", ".."}:
                return (
                    f"PHREEQC -file directive on line {line_number} must use a plain "
                    "workspace filename.")
    return None


def _snapshot_file(source: str, destination: Path, expected, *, executable: bool) -> Path:
    """Copy one already-confirmed file and re-verify bytes before PHREEQC can open it."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(descriptor, "rb") as reader, destination.open("xb") as writer:
            before = os.fstat(reader.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise OSError("source is not a regular file")
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected.size_bytes:
                    raise OSError("source grew while its snapshot was being created")
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
            after = os.fstat(reader.fileno())
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")
        if any(getattr(before, key) != getattr(after, key) for key in stable) \
                or total != expected.size_bytes or digest.hexdigest() != expected.sha256:
            raise OSError("source identity changed after confirmation")
        destination.chmod(0o500 if executable else 0o400)
        return destination
    except (OSError, ValueError) as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            "PHREEQC executable/database changed after confirmation while preparing the "
            f"isolated execution snapshot: {exc}") from exc


def _run_phreeqc(input_text: str, workdir: Path, stem: str, exe_path: str, db_path: str,
                 timeout: float) -> _RunOutcome:
    """Write ``<stem>.pqi`` and invoke ``phreeqc <in> <out> <db>``; capture everything.

    Never raises for a PHREEQC-side failure — returns a ``_RunOutcome``. The only way this
    raises is an unexpected internal error, which the caller catches.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    in_path = workdir / f"{stem}.pqi"
    out_path = workdir / f"{stem}.pqo"
    in_path.write_text(input_text, encoding="utf-8")

    t0 = time.monotonic()
    try:
        returncode, stdout, stderr, timed_out, error = _bounded_subprocess_run(
            [exe_path, str(in_path), str(out_path), str(db_path)],
            cwd=workdir, timeout=timeout, out_path=out_path)
    except (OSError, ValueError) as exc:
        return _RunOutcome(None, "", "", None, None, time.monotonic() - t0, False,
                           error=f"PHREEQC process could not start safely: {exc}")
    runtime = time.monotonic() - t0
    sel = _find_selected_output(workdir, stem)
    return _RunOutcome(returncode, stdout, stderr,
                       out_path if out_path.is_file() and not out_path.is_symlink() else None,
                       sel, runtime, timed_out, error=error)


def _as_text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else value.decode("utf-8", "replace")


def _find_selected_output(workdir: Path, stem: str) -> Path | None:
    """Locate a regular SELECTED_OUTPUT file inside this newly-created isolated job."""
    def regular(path: Path) -> bool:
        try:
            details = path.lstat()
        except OSError:
            return False
        return stat.S_ISREG(details.st_mode) and not stat.S_ISLNK(details.st_mode)

    candidates = [workdir / f"{stem}.sel", workdir / "selected.out"]
    for c in candidates:
        if regular(c):
            return c
    hits = [path for path in (
        list(workdir.glob("*.sel")) + list(workdir.glob("selected*.out"))) if regular(path)]
    return sorted(hits, key=lambda path: (path.lstat().st_mtime_ns, path.name),
                  reverse=True)[0] if hits else None


# PHREEQC flags real errors with the uppercase sentinel ``ERROR`` (e.g. "ERROR: ...").
# Match it case-SENSITIVELY so benign lowercase output like "Percent error" in a perfectly
# good .pqo is not mistaken for a failure.
_ERROR_RE = re.compile(r"\bERROR\b")
_END_OF_RUN_RE = re.compile(r"(?m)^\s*End of Run(?:\s+after\b.*)?\s*$")


def _error_lines(*texts: str) -> list[str]:
    return [ln.strip() for blob in texts for ln in str(blob).splitlines()
            if _ERROR_RE.search(ln)]


def _has_end_of_run(text: str) -> bool:
    """Accept the official PHREEQC terminal banner, with or without timing detail."""
    return bool(_END_OF_RUN_RE.search(str(text)))


# --------------------------------------------------------------------------- #
# Public: execute one confirmed input preview
# --------------------------------------------------------------------------- #
def execute_preview(preview, *, confirmation=None, workdir=None, exe: str | None = None,
                    database: str | None = None, timeout: float | None = None,
                    scenario_id: str | None = None) -> ExecutionResult:
    """Run the exact immutable snapshot linked to ``preview`` after explicit confirmation.

    ``confirmation`` must be a :class:`phreeqc_run_contract.ConfirmedPhreeqcInput` derived from
    this exact preview. Missing scientific inputs, missing confirmation, or a changed preview fail
    closed as ``not_runnable``. A missing binary/database remains ``phreeqc_missing``. The
    confirmed snapshot text, never mutable UI/session state, is executed verbatim.
    """
    sid = scenario_id or getattr(preview, "scenario_id", "SIM")
    ts = _now_iso()

    availability = check_availability(exe=exe, database=database)
    provenance = _environment_provenance(availability)
    readiness = _run_contract.assess_execution(preview, confirmation, availability)
    if not (readiness.scientific_ready and readiness.reviewed and readiness.confirmed
            and readiness.snapshot_matches):
        return ExecutionResult(
            sid, STATUS_BLOCKED, error_message=readiness.message, timestamp=ts,
            phreeqc_executable=availability.executable_path,
            database_path=availability.database_path, input_hash=readiness.input_hash,
            **provenance)
    if not readiness.configuration_ready:
        return ExecutionResult(
            sid, STATUS_MISSING, error_message=availability.message, timestamp=ts,
            phreeqc_executable=availability.executable_path,
            database_path=availability.database_path, input_hash=readiness.input_hash,
            **provenance)
    if not readiness.environment_matches:
        return ExecutionResult(
            sid, STATUS_BLOCKED, error_message=readiness.message, timestamp=ts,
            phreeqc_executable=availability.executable_path,
            database_path=availability.database_path, input_hash=readiness.input_hash,
            **provenance)
    if scenario_id is not None and str(scenario_id) != confirmation.scenario_id:
        return ExecutionResult(
            sid, STATUS_BLOCKED,
            error_message=("The execution scenario identifier differs from the reviewed "
                           "snapshot; review and confirm that target explicitly."),
            timestamp=ts, phreeqc_executable=availability.executable_path,
            database_path=availability.database_path, input_hash=readiness.input_hash,
            **provenance)

    exe_path = availability.executable_path
    db_path = availability.database_path
    input_text = confirmation.phreeqc_input_text
    sid = scenario_id or confirmation.scenario_id

    if len(input_text.encode("utf-8")) > MAX_INPUT_BYTES:
        return ExecutionResult(
            sid, STATUS_BLOCKED,
            error_message=f"Reviewed PHREEQC input exceeds the {MAX_INPUT_BYTES}-byte limit.",
            timestamp=ts, phreeqc_executable=exe_path, database_path=db_path,
            input_hash=confirmation.input_hash, **provenance)

    directive_error = _output_directive_error(input_text)
    if directive_error:
        return ExecutionResult(
            sid, STATUS_BLOCKED, error_message=directive_error, timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path,
            input_hash=confirmation.input_hash, **provenance)

    timeout = config.PHREEQC_RUN_TIMEOUT_S if timeout is None else timeout
    try:
        timeout = _validated_timeout(timeout)
    except ValueError as exc:
        return ExecutionResult(
            sid, STATUS_BLOCKED, error_message=str(exc), timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path,
            input_hash=confirmation.input_hash, **provenance)

    # The availability call above re-resolved and re-hashed both files for this exact execution.
    # Do not create or resolve a writable workspace until that live identity matches confirmation.
    try:
        workspace_root = assert_safe_workspace(
            workdir if workdir is not None else default_workspace())
        stem = _safe_stem(confirmation.execution_basename or sid)
        job_id, cleanup_token, ws = _new_job_workspace(workspace_root, stem)
    except ValueError as exc:
        return ExecutionResult(sid, STATUS_FAILED, error_message=str(exc), timestamp=ts,
                               phreeqc_executable=exe_path, database_path=db_path,
                               input_hash=confirmation.input_hash, **provenance)
    except Exception as exc:                                  # noqa: BLE001
        return ExecutionResult(
            sid, STATUS_FAILED, error_message=f"{type(exc).__name__}: {exc}", timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path,
            input_hash=confirmation.input_hash, **provenance)

    snapshot_executable = ws / _snapshot_executable_name(exe_path)
    snapshot_database = ws / _SNAPSHOT_DATABASE
    try:
        expected_environment = confirmation.execution_environment
        _snapshot_file(
            exe_path, snapshot_executable, expected_environment.executable, executable=True)
        _snapshot_file(
            db_path, snapshot_database, expected_environment.database, executable=False)
        outcome = _run_phreeqc(
            input_text, ws, stem, str(snapshot_executable), str(snapshot_database), timeout)
    except Exception as exc:                                  # noqa: BLE001 — never crash
        return ExecutionResult(
            sid, STATUS_FAILED, input_path=str(ws / f"{stem}.pqi"),
            error_message=f"{type(exc).__name__}: {exc}", timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path,
            input_hash=confirmation.input_hash, job_id=job_id,
            workspace_path=str(ws), cleanup_token=cleanup_token, **provenance)
    finally:
        for snapshot in (snapshot_executable, snapshot_database):
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                pass

    in_path = str(ws / f"{stem}.pqi")
    sel_path = str(outcome.selected_output_path) if outcome.selected_output_path else None
    if outcome.timed_out:
        return ExecutionResult(
            sid, STATUS_TIMEOUT, input_path=in_path, stdout_tail=_tail(outcome.stdout),
            stderr_tail=_tail(outcome.stderr), error_message=outcome.error,
            runtime_seconds=outcome.runtime_seconds, timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path,
            input_hash=confirmation.input_hash, job_id=job_id,
            workspace_path=str(ws), cleanup_token=cleanup_token, **provenance)

    if outcome.error:
        return ExecutionResult(
            sid, STATUS_FAILED, input_path=in_path,
            output_path=str(outcome.out_path) if outcome.out_path else None,
            selected_output_path=sel_path, stdout_tail=_tail(outcome.stdout),
            stderr_tail=_tail(outcome.stderr), error_message=outcome.error,
            runtime_seconds=outcome.runtime_seconds, timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path,
            input_hash=confirmation.input_hash, job_id=job_id,
            workspace_path=str(ws), cleanup_token=cleanup_token, **provenance)

    if outcome.out_path and outcome.out_path.stat().st_size > MAX_OUTPUT_BYTES:
        return ExecutionResult(
            sid, STATUS_FAILED, input_path=in_path, output_path=str(outcome.out_path),
            selected_output_path=sel_path,
            error_message=f"PHREEQC output exceeds the {MAX_OUTPUT_BYTES}-byte limit.",
            runtime_seconds=outcome.runtime_seconds, timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path,
            input_hash=confirmation.input_hash, job_id=job_id,
            workspace_path=str(ws), cleanup_token=cleanup_token, **provenance)
    if outcome.selected_output_path \
            and outcome.selected_output_path.stat().st_size > MAX_SELECTED_OUTPUT_BYTES:
        return ExecutionResult(
            sid, STATUS_FAILED, input_path=in_path, output_path=str(outcome.out_path),
            selected_output_path=sel_path,
            error_message=("PHREEQC selected output exceeds the "
                           f"{MAX_SELECTED_OUTPUT_BYTES}-byte limit."),
            runtime_seconds=outcome.runtime_seconds, timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path,
            input_hash=confirmation.input_hash, job_id=job_id,
            workspace_path=str(ws), cleanup_token=cleanup_token, **provenance)
    try:
        output_bytes, _ = _identity_file_bytes(
            str(outcome.out_path), "PHREEQC output", maximum=MAX_OUTPUT_BYTES)
        out_text = output_bytes.decode("utf-8", "replace")
    except _ResourceIdentityError as exc:
        return ExecutionResult(
            sid, STATUS_FAILED, input_path=in_path,
            output_path=str(outcome.out_path) if outcome.out_path else None,
            selected_output_path=sel_path, stdout_tail=_tail(outcome.stdout),
            stderr_tail=_tail(outcome.stderr), error_message=str(exc),
            runtime_seconds=outcome.runtime_seconds, timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path,
            input_hash=confirmation.input_hash, job_id=job_id,
            workspace_path=str(ws), cleanup_token=cleanup_token, **provenance)
    errs = _error_lines(outcome.stdout, outcome.stderr, out_text)
    if outcome.returncode != 0 or outcome.out_path is None or errs:
        detail = "\n".join(errs[:20]) or (outcome.stderr.strip() or outcome.stdout.strip()
                                          or f"PHREEQC exited with code {outcome.returncode}")
        return ExecutionResult(
            sid, STATUS_FAILED, input_path=in_path,
            output_path=str(outcome.out_path) if outcome.out_path else None,
            selected_output_path=sel_path, stdout_tail=_tail(outcome.stdout),
            stderr_tail=_tail(outcome.stderr), error_message=detail,
            runtime_seconds=outcome.runtime_seconds, timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path,
            input_hash=confirmation.input_hash, job_id=job_id,
            workspace_path=str(ws), cleanup_token=cleanup_token, **provenance)

    if not _has_end_of_run(out_text):
        return ExecutionResult(
            sid, STATUS_FAILED, input_path=in_path, output_path=str(outcome.out_path),
            selected_output_path=sel_path, stdout_tail=_tail(outcome.stdout),
            stderr_tail=_tail(outcome.stderr),
            error_message=("PHREEQC returned zero but its output is incomplete or malformed "
                           "(missing End of Run)."),
            runtime_seconds=outcome.runtime_seconds, timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path,
            input_hash=confirmation.input_hash, job_id=job_id,
            workspace_path=str(ws), cleanup_token=cleanup_token, **provenance)

    return ExecutionResult(
        sid, STATUS_SUCCESS, input_path=in_path, output_path=str(outcome.out_path),
        selected_output_path=sel_path, stdout_tail=_tail(outcome.stdout),
        stderr_tail=_tail(outcome.stderr), runtime_seconds=outcome.runtime_seconds,
        timestamp=ts, phreeqc_executable=exe_path, database_path=db_path,
        input_hash=confirmation.input_hash, job_id=job_id,
        workspace_path=str(ws), cleanup_token=cleanup_token, **provenance)


def smoke_test(*, exe: str | None = None, database: str | None = None,
               timeout: float = 30.0) -> bool:
    """Run a tiny harmless PHREEQC job in a throwaway temp dir. ``True`` iff it succeeds.

    Used only by :func:`check_availability(run_smoke=True)`. Never raises.
    """
    exe_path, _ = _resolve_executable(exe)
    db_path, _ = _resolve_database(database)
    if exe_path is None or db_path is None:
        return False
    try:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            expected = _run_contract.build_execution_environment(exe_path, db_path)
            _release_provenance(expected)
            snapshot_executable = _snapshot_file(
                exe_path, directory / _snapshot_executable_name(exe_path),
                expected.executable, executable=True)
            snapshot_database = _snapshot_file(
                db_path, directory / _SNAPSHOT_DATABASE, expected.database, executable=False)
            outcome = _run_phreeqc(
                SMOKE_INPUT, directory, "smoke", str(snapshot_executable),
                str(snapshot_database), timeout)
            if outcome.timed_out or outcome.error or outcome.out_path is None \
                    or outcome.returncode != 0:
                return False
            output_bytes, _ = _identity_file_bytes(
                str(outcome.out_path), "PHREEQC smoke output", maximum=MAX_OUTPUT_BYTES)
            out_text = output_bytes.decode("utf-8", "replace")
            return not _error_lines(outcome.stdout, outcome.stderr, out_text) \
                and _has_end_of_run(out_text)
    except Exception:                                        # noqa: BLE001 — smoke never crashes
        return False


# --------------------------------------------------------------------------- #
# Public: parse the basic outputs of a successful run
# --------------------------------------------------------------------------- #
def parse_outputs(result: ExecutionResult) -> ParsedSimulation:
    """Extract pH / pe / element totals (mM) / saturation indices from a run's ``.pqo``
    (plus the optional SELECTED_OUTPUT table). Never raises — returns ``parse_failed`` /
    ``no_selected_output`` / ``partial`` / ``parsed`` with explicit ``warnings``/``missing``.
    """
    sid = getattr(result, "scenario_id", "SIM")
    if result is None or result.status != STATUS_SUCCESS or not result.output_path:
        return ParsedSimulation(sid, PARSE_FAILED,
                                warnings=["No successful run output to parse."])

    output_path = Path(result.output_path)
    try:
        output_details = output_path.lstat()
    except OSError as exc:
        return ParsedSimulation(
            sid, PARSE_FAILED, warnings=[f"Could not inspect PHREEQC output: {exc}"])
    if stat.S_ISLNK(output_details.st_mode) or not stat.S_ISREG(output_details.st_mode) \
            or output_details.st_size > MAX_OUTPUT_BYTES:
        return ParsedSimulation(
            sid, PARSE_FAILED,
            warnings=["PHREEQC output is not a bounded regular file and was not parsed."])

    warnings: list[str] = []
    missing: list[str] = []
    try:
        records = parse_pqo_file(result.output_path)
        results, saturation, _assemblage = records_to_frames(records)
    except Exception as exc:                                  # noqa: BLE001
        return ParsedSimulation(sid, PARSE_FAILED,
                                warnings=[f"Could not parse PHREEQC output: "
                                          f"{type(exc).__name__}: {exc}"])
    if results is None or results.empty:
        return ParsedSimulation(sid, PARSE_FAILED,
                                warnings=["PHREEQC output parsed but contained no solution "
                                          "states."])

    # Prefer the post-equilibration ("batch") state — that is what an experiment measures.
    if "state" in results.columns and (results["state"] == "batch").any():
        row = results[results["state"] == "batch"].iloc[-1].to_dict()
    else:
        row = results.iloc[-1].to_dict()
        warnings.append("No post-reaction (batch) state found — using the last solution state.")

    pH = _num(row.get("pH"))
    pe = _num(row.get("pe"))
    totals: dict[str, float] = {}
    for col, val in row.items():
        if str(col).startswith("mol_"):
            v = _num(val)
            if v is not None:
                totals[str(col)[4:]] = v * _MOLALITY_TO_MM       # molality → mM

    sis: list[dict] = []
    if saturation is not None and not saturation.empty:
        sat = saturation
        if "state" in sat.columns and (sat["state"] == "batch").any():
            sat = sat[sat["state"] == "batch"]
        for _, r in sat.iterrows():
            si = _num(r.get("SI"))
            if si is not None and r.get("phase"):
                sis.append({"phase": str(r.get("phase")), "SI": si})

    selected_df = None
    if result.selected_output_path:
        try:
            selected_path = Path(result.selected_output_path)
            selected_details = selected_path.lstat()
            if stat.S_ISLNK(selected_details.st_mode) \
                    or not stat.S_ISREG(selected_details.st_mode) \
                    or selected_details.st_size > MAX_SELECTED_OUTPUT_BYTES:
                raise ValueError("selected output is not a bounded regular file")
            selected_df = parse_selected_output(result.selected_output_path)
        except Exception as exc:                              # noqa: BLE001
            warnings.append(f"A SELECTED_OUTPUT file was produced but could not be parsed "
                            f"({type(exc).__name__}).")
    else:
        warnings.append("No SELECTED_OUTPUT file was produced — values were read from the "
                        "main .pqo output instead.")

    if pH is None:
        missing.append("pH")
    if not totals:
        missing.append("element totals")
    if not sis:
        missing.append("saturation indices")

    if pH is None and not totals:
        status = PARSE_NO_SELECTED_OUTPUT if selected_df is None else PARSE_PARTIAL
    elif pH is not None and totals:
        status = PARSE_PARSED
    else:
        status = PARSE_PARTIAL

    return ParsedSimulation(
        sid, status, pH=pH, pe=pe, element_totals_mM=totals, saturation_indices=sis,
        selected_output=selected_df, warnings=warnings, missing=missing, n_states=len(results))


def run_and_parse(preview, **kwargs) -> tuple[ExecutionResult, ParsedSimulation | None]:
    """Convenience: execute then parse. Returns ``(result, parsed-or-None)``."""
    result = execute_preview(preview, **kwargs)
    parsed = parse_outputs(result) if result.status == STATUS_SUCCESS else None
    return result, parsed
