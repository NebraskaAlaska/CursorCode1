"""Closed, versioned contracts for managed scientific resources.

The contracts in this module deliberately contain no Streamlit, network, subprocess, or
scientific-calculation code.  They are the durable identity boundary shared by the catalog,
database registry, update steward, run provenance adapters, and offline knowledge pack.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

RESOURCE_MANIFEST_SCHEMA = "wpi.virtual-lab.scientific-resource"
RESOURCE_MANIFEST_VERSION = 1
CATALOG_SCHEMA = "wpi.virtual-lab.resource-catalog"
CATALOG_VERSION = 1
KNOWLEDGE_CATALOG_PROJECTION_SCHEMA = (
    "wpi.virtual-lab.knowledge-catalog-projection"
)
KNOWLEDGE_CATALOG_PROJECTION_VERSION = 1
UPDATE_PROPOSAL_SCHEMA = "wpi.virtual-lab.resource-update-proposal"
UPDATE_PROPOSAL_VERSION = 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RESOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,126}[a-z0-9]$")
_INSTALLATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,190}$")


class ResourceContractError(ValueError):
    """A resource document is malformed, incomplete, or uses an open-ended field."""


class ResourceKind(str, Enum):
    PHREEQC_RUNTIME = "phreeqc_runtime"
    PHREEQC_OFFICIAL_DATABASE = "phreeqc_official_database"
    EXTERNAL_THERMODYNAMIC_DATABASE = "external_thermodynamic_database"
    DATABASE_EXTENSION = "database_extension"
    DOCUMENTATION_KNOWLEDGE_PACK = "documentation_knowledge_pack"
    AI_PROVIDER_MODEL = "ai_provider_model"


class RedistributionState(str, Enum):
    PERMITTED = "permitted"
    EXTERNAL_ONLY = "external_only"
    USER_SUPPLIED = "user_supplied"
    UNKNOWN = "unknown"
    PROHIBITED = "prohibited"


class CompatibilityStatus(str, Enum):
    UNKNOWN = "unknown"
    COMPATIBLE = "compatible"
    PARTIAL = "partial"
    INCOMPATIBLE = "incompatible"
    REVIEW_REQUIRED = "review_required"


class TestStatus(str, Enum):
    NOT_TESTED = "not_tested"
    NOT_APPLICABLE_REQUIRES_BASE = "not_applicable_requires_base"
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"


class RollbackState(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ROLLBACK_AVAILABLE = "rollback_available"
    ROLLED_BACK = "rolled_back"


class ProposalStatus(str, Enum):
    PROPOSED = "proposed"
    DOWNLOADED = "downloaded"
    VERIFIED = "verified"
    BUILT = "built"
    TESTED = "tested"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"


class EvidenceStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class FindingSeverity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


def utc_now() -> str:
    """Return a timezone-explicit, seconds-resolution UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ResourceContractError(f"value is not canonical JSON: {exc}") from exc


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _closed(document: Mapping[str, Any], allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise ResourceContractError(f"{label} must be an object")
    unknown = set(document) - allowed
    if unknown:
        raise ResourceContractError(f"{label} contains unsupported fields: {sorted(unknown)}")
    return dict(document)


def _enum(enum_type, value, label: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ResourceContractError(f"{label} must be one of: {allowed}") from exc


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ResourceContractError(f"{label} must be a list of strings")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise ResourceContractError(f"{label} must not contain duplicates")
    if result != tuple(sorted(result)):
        raise ResourceContractError(f"{label} must be sorted for deterministic identity")
    return result


def _validate_sha256(value: str, label: str, *, optional: bool = True) -> None:
    if not value and optional:
        return
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ResourceContractError(f"{label} must be a lower-case SHA-256 hex digest")


def _validate_time(value: str, label: str) -> None:
    if not value:
        return
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ResourceContractError(f"{label} must be an ISO-8601 timestamp") from exc


def _validate_https_url(value: str, label: str) -> None:
    if not value:
        return
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ResourceContractError(f"{label} must be an HTTPS URL without credentials")


@dataclass(frozen=True)
class Citation:
    title: str = ""
    authors: tuple[str, ...] = ()
    year: int | None = None
    doi: str = ""
    url: str = ""
    source_location: str = ""
    extraction_confidence: float | None = None

    def __post_init__(self) -> None:
        _strings(self.authors, "citation.authors")
        if self.year is not None and (isinstance(self.year, bool) or not isinstance(self.year, int)):
            raise ResourceContractError("citation.year must be an integer or null")
        _validate_https_url(self.url, "citation.url")
        if self.extraction_confidence is not None:
            value = self.extraction_confidence
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)) \
                    or not 0.0 <= float(value) <= 1.0:
                raise ResourceContractError("citation.extraction_confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "doi": self.doi,
            "url": self.url,
            "source_location": self.source_location,
            "extraction_confidence": self.extraction_confidence,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any] | None) -> "Citation":
        data = _closed(document or {}, {
            "title", "authors", "year", "doi", "url", "source_location",
            "extraction_confidence",
        }, "citation")
        data["authors"] = _strings(data.get("authors", ()), "citation.authors")
        return cls(**data)


@dataclass(frozen=True)
class TemperatureRange:
    minimum_celsius: float | None = None
    maximum_celsius: float | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        for label, value in (("minimum_celsius", self.minimum_celsius),
                             ("maximum_celsius", self.maximum_celsius)):
            if value is not None and (not isinstance(value, (int, float))
                                      or not math.isfinite(float(value))):
                raise ResourceContractError(f"temperature_range.{label} must be finite or null")
        if self.minimum_celsius is not None and self.maximum_celsius is not None \
                and float(self.minimum_celsius) > float(self.maximum_celsius):
            raise ResourceContractError("temperature range minimum exceeds maximum")

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimum_celsius": self.minimum_celsius,
            "maximum_celsius": self.maximum_celsius,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any] | None) -> "TemperatureRange":
        return cls(**_closed(document or {}, {
            "minimum_celsius", "maximum_celsius", "notes",
        }, "temperature_range"))


@dataclass(frozen=True)
class SupportedSpeciesPhaseSummary:
    master_species: tuple[str, ...] = ()
    solution_species: tuple[str, ...] = ()
    phases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _strings(self.master_species, "supported_summary.master_species")
        _strings(self.solution_species, "supported_summary.solution_species")
        _strings(self.phases, "supported_summary.phases")

    @property
    def master_species_count(self) -> int:
        return len(self.master_species)

    @property
    def solution_species_count(self) -> int:
        return len(self.solution_species)

    @property
    def phase_count(self) -> int:
        return len(self.phases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "master_species": list(self.master_species),
            "solution_species": list(self.solution_species),
            "phases": list(self.phases),
            "master_species_count": self.master_species_count,
            "solution_species_count": self.solution_species_count,
            "phase_count": self.phase_count,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any] | None) -> "SupportedSpeciesPhaseSummary":
        data = _closed(document or {}, {
            "master_species", "solution_species", "phases", "master_species_count",
            "solution_species_count", "phase_count",
        }, "supported_summary")
        supplied_counts = {
            key: data.pop(key, None) for key in
            ("master_species_count", "solution_species_count", "phase_count")
        }
        result = cls(
            master_species=_strings(data.get("master_species", ()),
                                    "supported_summary.master_species"),
            solution_species=_strings(data.get("solution_species", ()),
                                      "supported_summary.solution_species"),
            phases=_strings(data.get("phases", ()), "supported_summary.phases"),
        )
        actual = {
            "master_species_count": result.master_species_count,
            "solution_species_count": result.solution_species_count,
            "phase_count": result.phase_count,
        }
        for key, supplied in supplied_counts.items():
            if supplied is not None and supplied != actual[key]:
                raise ResourceContractError(f"supported_summary.{key} does not match its list")
        return result


@dataclass(frozen=True)
class ResourceManifest:
    resource_id: str
    installation_id: str
    resource_kind: ResourceKind
    display_name: str
    provider: str
    official_source_url: str = ""
    discovered_version: str = ""
    installed_version: str = ""
    active_version: str = ""
    archive_filename: str = ""
    source_sha256: str = ""
    executable_sha256: str = ""
    database_sha256: str = ""
    content_sha256: str = ""
    architectures: tuple[str, ...] = ()
    operating_system_targets: tuple[str, ...] = ()
    build_toolchain: tuple[str, ...] = ()
    install_path: str = ""
    installed_at: str = ""
    verified_at: str = ""
    citation: Citation = field(default_factory=Citation)
    rights_notice: str = ""
    licence_notice: str = ""
    redistribution_state: RedistributionState = RedistributionState.UNKNOWN
    temperature_range: TemperatureRange = field(default_factory=TemperatureRange)
    database_family: str = ""
    dependencies: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    requires_database_family: str = ""
    extension_for_resource_id: str = ""
    duplicate_species_phase_risk: str = ""
    domain_notes: tuple[str, ...] = ()
    supported_summary: SupportedSpeciesPhaseSummary = field(
        default_factory=SupportedSpeciesPhaseSummary)
    compatibility_status: CompatibilityStatus = CompatibilityStatus.UNKNOWN
    test_status: TestStatus = TestStatus.NOT_TESTED
    standalone_test_status: TestStatus = TestStatus.NOT_TESTED
    base_include_test_status: TestStatus = TestStatus.NOT_TESTED
    warnings: tuple[str, ...] = ()
    superseded_by: str = ""
    rollback_state: RollbackState = RollbackState.CANDIDATE
    manifest_schema: str = RESOURCE_MANIFEST_SCHEMA
    manifest_version: int = RESOURCE_MANIFEST_VERSION

    def __post_init__(self) -> None:
        if self.manifest_schema != RESOURCE_MANIFEST_SCHEMA \
                or self.manifest_version != RESOURCE_MANIFEST_VERSION:
            raise ResourceContractError("unsupported scientific-resource manifest schema/version")
        if not _RESOURCE_ID_RE.fullmatch(self.resource_id):
            raise ResourceContractError(f"invalid resource_id: {self.resource_id!r}")
        if not _INSTALLATION_ID_RE.fullmatch(self.installation_id):
            raise ResourceContractError(f"invalid installation_id: {self.installation_id!r}")
        if not self.display_name.strip() or not self.provider.strip():
            raise ResourceContractError("display_name and provider are required")
        _validate_https_url(self.official_source_url, "official_source_url")
        if self.archive_filename and (Path(self.archive_filename).name != self.archive_filename
                                      or self.archive_filename in {".", ".."}):
            raise ResourceContractError("archive_filename must be a plain file name")
        for label, value in (
            ("source_sha256", self.source_sha256),
            ("executable_sha256", self.executable_sha256),
            ("database_sha256", self.database_sha256),
            ("content_sha256", self.content_sha256),
        ):
            _validate_sha256(value, label)
        for label, value in (("installed_at", self.installed_at), ("verified_at", self.verified_at)):
            _validate_time(value, label)
        if self.install_path and not Path(self.install_path).is_absolute():
            raise ResourceContractError("install_path must be absolute when supplied")
        for label, values in (
            ("architectures", self.architectures),
            ("operating_system_targets", self.operating_system_targets),
            ("build_toolchain", self.build_toolchain),
            ("dependencies", self.dependencies),
            ("conflicts", self.conflicts),
            ("domain_notes", self.domain_notes),
            ("warnings", self.warnings),
        ):
            _strings(values, label)
        if self.extension_for_resource_id and not _RESOURCE_ID_RE.fullmatch(
                self.extension_for_resource_id):
            raise ResourceContractError("extension_for_resource_id is invalid")
        if self.superseded_by and not _INSTALLATION_ID_RE.fullmatch(self.superseded_by):
            raise ResourceContractError("superseded_by is invalid")
        if self.resource_kind == ResourceKind.PHREEQC_RUNTIME \
                and not (self.executable_sha256 or self.content_sha256):
            raise ResourceContractError("a PHREEQC runtime requires an executable/content hash")
        if self.resource_kind in {
            ResourceKind.PHREEQC_OFFICIAL_DATABASE,
            ResourceKind.EXTERNAL_THERMODYNAMIC_DATABASE,
            ResourceKind.DATABASE_EXTENSION,
        } and not (self.database_sha256 or self.content_sha256):
            raise ResourceContractError("a database resource requires a database/content hash")
        if self.resource_kind == ResourceKind.DOCUMENTATION_KNOWLEDGE_PACK \
                and not self.content_sha256:
            raise ResourceContractError("a knowledge pack requires a content hash")
        if self.resource_kind == ResourceKind.DATABASE_EXTENSION:
            if not self.requires_database_family:
                raise ResourceContractError("a database extension requires its base family")
            if self.standalone_test_status != TestStatus.NOT_APPLICABLE_REQUIRES_BASE:
                raise ResourceContractError(
                    "a database extension standalone test is not_applicable_requires_base")
        elif self.standalone_test_status == TestStatus.NOT_APPLICABLE_REQUIRES_BASE:
            raise ResourceContractError(
                "only a database extension may require a base for standalone testing")

    @property
    def primary_sha256(self) -> str:
        return (self.content_sha256 or self.executable_sha256 or self.database_sha256
                or self.source_sha256)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_schema": self.manifest_schema,
            "manifest_version": self.manifest_version,
            "resource_id": self.resource_id,
            "installation_id": self.installation_id,
            "resource_kind": self.resource_kind.value,
            "display_name": self.display_name,
            "provider": self.provider,
            "official_source_url": self.official_source_url,
            "discovered_version": self.discovered_version,
            "installed_version": self.installed_version,
            "active_version": self.active_version,
            "archive_filename": self.archive_filename,
            "source_sha256": self.source_sha256,
            "executable_sha256": self.executable_sha256,
            "database_sha256": self.database_sha256,
            "content_sha256": self.content_sha256,
            "architectures": list(self.architectures),
            "operating_system_targets": list(self.operating_system_targets),
            "build_toolchain": list(self.build_toolchain),
            "install_path": self.install_path,
            "installed_at": self.installed_at,
            "verified_at": self.verified_at,
            "citation": self.citation.to_dict(),
            "rights_notice": self.rights_notice,
            "licence_notice": self.licence_notice,
            "redistribution_state": self.redistribution_state.value,
            "temperature_range": self.temperature_range.to_dict(),
            "database_family": self.database_family,
            "dependencies": list(self.dependencies),
            "conflicts": list(self.conflicts),
            "requires_database_family": self.requires_database_family,
            "extension_for_resource_id": self.extension_for_resource_id,
            "duplicate_species_phase_risk": self.duplicate_species_phase_risk,
            "domain_notes": list(self.domain_notes),
            "supported_summary": self.supported_summary.to_dict(),
            "compatibility_status": self.compatibility_status.value,
            "test_status": self.test_status.value,
            "standalone_test_status": self.standalone_test_status.value,
            "base_include_test_status": self.base_include_test_status.value,
            "warnings": list(self.warnings),
            "superseded_by": self.superseded_by,
            "rollback_state": self.rollback_state.value,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "ResourceManifest":
        allowed = {
            "manifest_schema", "manifest_version", "resource_id", "installation_id",
            "resource_kind", "display_name", "provider", "official_source_url",
            "discovered_version", "installed_version", "active_version", "archive_filename",
            "source_sha256", "executable_sha256", "database_sha256", "content_sha256",
            "architectures", "operating_system_targets", "build_toolchain", "install_path",
            "installed_at", "verified_at", "citation", "rights_notice", "licence_notice",
            "redistribution_state", "temperature_range", "database_family", "dependencies",
            "conflicts", "requires_database_family", "extension_for_resource_id",
            "duplicate_species_phase_risk", "domain_notes", "supported_summary",
            "compatibility_status", "test_status", "warnings", "superseded_by",
            "standalone_test_status", "base_include_test_status", "rollback_state",
        }
        data = _closed(document, allowed, "resource manifest")
        for required in ("resource_id", "installation_id", "resource_kind", "display_name",
                         "provider"):
            if required not in data:
                raise ResourceContractError(f"resource manifest is missing {required}")
        data["resource_kind"] = _enum(ResourceKind, data["resource_kind"], "resource_kind")
        data["redistribution_state"] = _enum(
            RedistributionState, data.get("redistribution_state", RedistributionState.UNKNOWN.value),
            "redistribution_state")
        data["compatibility_status"] = _enum(
            CompatibilityStatus, data.get("compatibility_status", CompatibilityStatus.UNKNOWN.value),
            "compatibility_status")
        data["test_status"] = _enum(
            TestStatus, data.get("test_status", TestStatus.NOT_TESTED.value), "test_status")
        data["standalone_test_status"] = _enum(
            TestStatus,
            data.get("standalone_test_status", TestStatus.NOT_TESTED.value),
            "standalone_test_status",
        )
        data["base_include_test_status"] = _enum(
            TestStatus,
            data.get("base_include_test_status", TestStatus.NOT_TESTED.value),
            "base_include_test_status",
        )
        data["rollback_state"] = _enum(
            RollbackState, data.get("rollback_state", RollbackState.CANDIDATE.value),
            "rollback_state")
        for label in ("architectures", "operating_system_targets", "build_toolchain",
                      "dependencies", "conflicts", "domain_notes", "warnings"):
            data[label] = _strings(data.get(label, ()), label)
        data["citation"] = Citation.from_dict(data.get("citation"))
        data["temperature_range"] = TemperatureRange.from_dict(data.get("temperature_range"))
        data["supported_summary"] = SupportedSpeciesPhaseSummary.from_dict(
            data.get("supported_summary"))
        return cls(**data)


def make_installation_id(resource_id: str, version: str, primary_sha256: str) -> str:
    if not _RESOURCE_ID_RE.fullmatch(resource_id):
        raise ResourceContractError(f"invalid resource_id: {resource_id!r}")
    _validate_sha256(primary_sha256, "primary_sha256", optional=False)
    version_slug = re.sub(r"[^a-z0-9._-]+", "-", str(version).lower()).strip("-._") or "unknown"
    value = f"{resource_id}.{version_slug}.{primary_sha256[:16]}"
    if not _INSTALLATION_ID_RE.fullmatch(value):
        raise ResourceContractError("generated installation identity is invalid")
    return value


@dataclass(frozen=True)
class ActivationRecord:
    action: str
    resource_id: str
    installation_id: str
    previous_installation_id: str = ""
    proposal_id: str = ""
    catalog_generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "resource_id": self.resource_id,
            "installation_id": self.installation_id,
            "previous_installation_id": self.previous_installation_id,
            "proposal_id": self.proposal_id,
            "catalog_generation": self.catalog_generation,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "ActivationRecord":
        data = _closed(document, {
            "action", "resource_id", "installation_id", "previous_installation_id",
            "proposal_id", "catalog_generation",
        }, "activation record")
        return cls(**data)


@dataclass(frozen=True)
class CatalogState:
    generation: int = 0
    resources: tuple[ResourceManifest, ...] = ()
    active: Mapping[str, str] = field(default_factory=dict)
    references: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    activation_history: tuple[ActivationRecord, ...] = ()
    schema: str = CATALOG_SCHEMA
    version: int = CATALOG_VERSION

    def __post_init__(self) -> None:
        if self.schema != CATALOG_SCHEMA or self.version != CATALOG_VERSION:
            raise ResourceContractError("unsupported resource catalog schema/version")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) \
                or self.generation < 0:
            raise ResourceContractError("catalog generation must be a non-negative integer")
        ids = [item.installation_id for item in self.resources]
        if len(ids) != len(set(ids)):
            raise ResourceContractError("catalog contains duplicate installation identities")
        by_id = {item.installation_id: item for item in self.resources}
        for resource_id, installation_id in self.active.items():
            if not _RESOURCE_ID_RE.fullmatch(resource_id):
                raise ResourceContractError(f"invalid active resource id: {resource_id!r}")
            if installation_id not in by_id or by_id[installation_id].resource_id != resource_id:
                raise ResourceContractError("active resource points to an incompatible installation")
        for installation_id, run_ids in self.references.items():
            if installation_id not in by_id:
                raise ResourceContractError("resource reference points to an unknown installation")
            _strings(run_ids, f"references[{installation_id}]")

    @property
    def catalog_hash(self) -> str:
        return canonical_hash(self.to_dict())

    def knowledge_projection_dict(self) -> dict[str, Any]:
        """Return the deterministic catalog view that a knowledge pack describes.

        A knowledge-pack manifest contains the hash of the pack bytes. Including that
        self-manifest in the pack's own catalog hash would create an unsatisfiable hash
        cycle. The projection therefore excludes only
        ``DOCUMENTATION_KNOWLEDGE_PACK`` manifests and their catalog-owned pointers,
        references, activation events, and generation effects. Every other candidate,
        active resource, reference, and activation event remains identity-bearing.

        Activation-record generations are replaced by a stable projection sequence so
        knowledge-manifest catalog commits cannot perturb later scientific-resource
        history. This projection is never used as the full catalog identity.
        """
        excluded_installations = {
            item.installation_id for item in self.resources
            if item.resource_kind == ResourceKind.DOCUMENTATION_KNOWLEDGE_PACK
        }
        excluded_resource_ids = {
            item.resource_id for item in self.resources
            if item.resource_kind == ResourceKind.DOCUMENTATION_KNOWLEDGE_PACK
        }
        resources = tuple(
            item for item in self.resources
            if item.installation_id not in excluded_installations
        )
        projected_history = []
        for record in self.activation_history:
            if record.resource_id in excluded_resource_ids:
                continue
            projected_history.append({
                "projection_sequence": len(projected_history) + 1,
                "action": record.action,
                "resource_id": record.resource_id,
                "installation_id": record.installation_id,
                "previous_installation_id": record.previous_installation_id,
                "proposal_id": record.proposal_id,
            })
        return {
            "schema": KNOWLEDGE_CATALOG_PROJECTION_SCHEMA,
            "version": KNOWLEDGE_CATALOG_PROJECTION_VERSION,
            "resource_catalog_schema": self.schema,
            "resource_catalog_version": self.version,
            "resources": [item.to_dict() for item in
                          sorted(resources, key=lambda item: item.installation_id)],
            "active": {
                key: value for key, value in sorted(self.active.items())
                if value not in excluded_installations
            },
            "references": {
                key: list(value) for key, value in sorted(self.references.items())
                if key not in excluded_installations
            },
            "activation_history": projected_history,
        }

    @property
    def knowledge_projection_hash(self) -> str:
        """SHA-256 of the non-self-referential knowledge catalog projection."""
        return canonical_hash(self.knowledge_projection_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "generation": self.generation,
            "resources": [item.to_dict() for item in
                          sorted(self.resources, key=lambda item: item.installation_id)],
            "active": dict(sorted(self.active.items())),
            "references": {key: list(value) for key, value in sorted(self.references.items())},
            "activation_history": [item.to_dict() for item in self.activation_history],
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "CatalogState":
        data = _closed(document, {
            "schema", "version", "generation", "resources", "active", "references",
            "activation_history",
        }, "resource catalog")
        resources = data.get("resources", [])
        if not isinstance(resources, list):
            raise ResourceContractError("catalog resources must be a list")
        active = data.get("active", {})
        references = data.get("references", {})
        if not isinstance(active, Mapping) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in active.items()):
            raise ResourceContractError("catalog active must be a string mapping")
        if not isinstance(references, Mapping):
            raise ResourceContractError("catalog references must be an object")
        parsed_references = {
            str(key): _strings(value, f"references[{key}]") for key, value in references.items()
        }
        history = data.get("activation_history", [])
        if not isinstance(history, list):
            raise ResourceContractError("activation_history must be a list")
        return cls(
            schema=data.get("schema", CATALOG_SCHEMA),
            version=data.get("version", CATALOG_VERSION),
            generation=data.get("generation", 0),
            resources=tuple(ResourceManifest.from_dict(item) for item in resources),
            active=dict(active),
            references=parsed_references,
            activation_history=tuple(ActivationRecord.from_dict(item) for item in history),
        )


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    status: EvidenceStatus
    details: str = ""
    artifact_sha256: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,127}", self.evidence_id):
            raise ResourceContractError("invalid evidence_id")
        _validate_sha256(self.artifact_sha256, "evidence.artifact_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "status": self.status.value,
            "details": self.details,
            "artifact_sha256": self.artifact_sha256,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "EvidenceRecord":
        data = _closed(document, {
            "evidence_id", "status", "details", "artifact_sha256",
        }, "test/build evidence")
        if "evidence_id" not in data or "status" not in data:
            raise ResourceContractError("evidence_id and status are required")
        data["status"] = _enum(EvidenceStatus, data["status"], "evidence.status")
        return cls(**data)


@dataclass(frozen=True)
class Finding:
    finding_id: str
    severity: FindingSeverity
    summary: str
    resolved: bool = False
    resolution: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,127}", self.finding_id):
            raise ResourceContractError("invalid finding_id")
        if not self.summary.strip():
            raise ResourceContractError("finding summary is required")
        if self.resolved and not self.resolution.strip():
            raise ResourceContractError("a resolved finding requires a resolution")

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity.value,
            "summary": self.summary,
            "resolved": self.resolved,
            "resolution": self.resolution,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "Finding":
        data = _closed(document, {
            "finding_id", "severity", "summary", "resolved", "resolution",
        }, "finding")
        for required in ("finding_id", "severity", "summary"):
            if required not in data:
                raise ResourceContractError(f"finding is missing {required}")
        data["severity"] = _enum(FindingSeverity, data["severity"], "finding.severity")
        return cls(**data)


@dataclass(frozen=True)
class ResourceComparison:
    current_installation_id: str = ""
    candidate_installation_id: str = ""
    current_version: str = ""
    candidate_version: str = ""
    current_sha256: str = ""
    candidate_sha256: str = ""
    added_master_species: tuple[str, ...] = ()
    removed_master_species: tuple[str, ...] = ()
    added_solution_species: tuple[str, ...] = ()
    removed_solution_species: tuple[str, ...] = ()
    added_phases: tuple[str, ...] = ()
    removed_phases: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_sha256(self.current_sha256, "comparison.current_sha256")
        _validate_sha256(self.candidate_sha256, "comparison.candidate_sha256")
        for label in ("added_master_species", "removed_master_species",
                      "added_solution_species", "removed_solution_species", "added_phases",
                      "removed_phases", "warnings"):
            _strings(getattr(self, label), f"comparison.{label}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_installation_id": self.current_installation_id,
            "candidate_installation_id": self.candidate_installation_id,
            "current_version": self.current_version,
            "candidate_version": self.candidate_version,
            "current_sha256": self.current_sha256,
            "candidate_sha256": self.candidate_sha256,
            "added_master_species": list(self.added_master_species),
            "removed_master_species": list(self.removed_master_species),
            "added_solution_species": list(self.added_solution_species),
            "removed_solution_species": list(self.removed_solution_species),
            "added_phases": list(self.added_phases),
            "removed_phases": list(self.removed_phases),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "ResourceComparison":
        allowed = {
            "current_installation_id", "candidate_installation_id", "current_version",
            "candidate_version", "current_sha256", "candidate_sha256",
            "added_master_species", "removed_master_species", "added_solution_species",
            "removed_solution_species", "added_phases", "removed_phases", "warnings",
        }
        data = _closed(document, allowed, "resource comparison")
        for label in ("added_master_species", "removed_master_species",
                      "added_solution_species", "removed_solution_species", "added_phases",
                      "removed_phases", "warnings"):
            data[label] = _strings(data.get(label, ()), f"comparison.{label}")
        return cls(**data)


@dataclass(frozen=True)
class UpdateProposal:
    proposal_id: str
    resource_id: str
    resource_kind: ResourceKind
    candidate_version: str
    source_url: str
    archive_filename: str
    status: ProposalStatus = ProposalStatus.PROPOSED
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    current_installation_id: str = ""
    previous_active_installation_id: str = ""
    candidate_installation_id: str = ""
    expected_source_sha256: str = ""
    observed_source_sha256: str = ""
    candidate_sha256: str = ""
    quarantine_path: str = ""
    candidate_file_hashes: Mapping[str, str] = field(default_factory=dict)
    rights_notice: str = ""
    redistribution_state: RedistributionState = RedistributionState.UNKNOWN
    build_evidence: tuple[EvidenceRecord, ...] = ()
    test_evidence: tuple[EvidenceRecord, ...] = ()
    findings: tuple[Finding, ...] = ()
    comparison: ResourceComparison | None = None
    schema: str = UPDATE_PROPOSAL_SCHEMA
    version: int = UPDATE_PROPOSAL_VERSION

    def __post_init__(self) -> None:
        if self.schema != UPDATE_PROPOSAL_SCHEMA or self.version != UPDATE_PROPOSAL_VERSION:
            raise ResourceContractError("unsupported update proposal schema/version")
        if not re.fullmatch(r"proposal-[0-9a-f]{24}", self.proposal_id):
            raise ResourceContractError("proposal_id must be a deterministic proposal digest")
        if not _RESOURCE_ID_RE.fullmatch(self.resource_id):
            raise ResourceContractError("proposal resource_id is invalid")
        if not self.candidate_version.strip():
            raise ResourceContractError("candidate_version is required")
        _validate_https_url(self.source_url, "proposal.source_url")
        if Path(self.archive_filename).name != self.archive_filename \
                or self.archive_filename in {"", ".", ".."}:
            raise ResourceContractError("proposal archive_filename must be a plain file name")
        for label in ("created_at", "updated_at"):
            _validate_time(getattr(self, label), f"proposal.{label}")
        for label in ("expected_source_sha256", "observed_source_sha256", "candidate_sha256"):
            _validate_sha256(getattr(self, label), f"proposal.{label}")
        if self.quarantine_path and not Path(self.quarantine_path).is_absolute():
            raise ResourceContractError("proposal quarantine_path must be absolute")
        if not isinstance(self.candidate_file_hashes, Mapping) or any(
                not isinstance(name, str) or not name
                or not isinstance(digest, str)
                for name, digest in self.candidate_file_hashes.items()):
            raise ResourceContractError("candidate_file_hashes must be a string mapping")
        for name, digest in self.candidate_file_hashes.items():
            if name.startswith("/") or "\\" in name or any(
                    part in {"", ".", ".."} for part in name.split("/")):
                raise ResourceContractError("candidate_file_hashes contains an unsafe path")
            _validate_sha256(digest, f"candidate_file_hashes[{name}]", optional=False)

    @property
    def unresolved_blockers(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if not item.resolved and item.severity in {
            FindingSeverity.HIGH, FindingSeverity.MEDIUM,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "proposal_id": self.proposal_id,
            "resource_id": self.resource_id,
            "resource_kind": self.resource_kind.value,
            "candidate_version": self.candidate_version,
            "source_url": self.source_url,
            "archive_filename": self.archive_filename,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_installation_id": self.current_installation_id,
            "previous_active_installation_id": self.previous_active_installation_id,
            "candidate_installation_id": self.candidate_installation_id,
            "expected_source_sha256": self.expected_source_sha256,
            "observed_source_sha256": self.observed_source_sha256,
            "candidate_sha256": self.candidate_sha256,
            "quarantine_path": self.quarantine_path,
            "candidate_file_hashes": dict(sorted(self.candidate_file_hashes.items())),
            "rights_notice": self.rights_notice,
            "redistribution_state": self.redistribution_state.value,
            "build_evidence": [item.to_dict() for item in self.build_evidence],
            "test_evidence": [item.to_dict() for item in self.test_evidence],
            "findings": [item.to_dict() for item in self.findings],
            "comparison": self.comparison.to_dict() if self.comparison else None,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "UpdateProposal":
        allowed = {
            "schema", "version", "proposal_id", "resource_id", "resource_kind",
            "candidate_version", "source_url", "archive_filename", "status", "created_at",
            "updated_at", "current_installation_id", "previous_active_installation_id",
            "candidate_installation_id", "expected_source_sha256", "observed_source_sha256",
            "candidate_sha256", "quarantine_path", "rights_notice", "redistribution_state",
            "candidate_file_hashes", "build_evidence", "test_evidence", "findings",
            "comparison",
        }
        data = _closed(document, allowed, "update proposal")
        for required in ("proposal_id", "resource_id", "resource_kind", "candidate_version",
                         "source_url", "archive_filename"):
            if required not in data:
                raise ResourceContractError(f"update proposal is missing {required}")
        data["resource_kind"] = _enum(ResourceKind, data["resource_kind"], "resource_kind")
        data["status"] = _enum(
            ProposalStatus, data.get("status", ProposalStatus.PROPOSED.value), "proposal.status")
        data["redistribution_state"] = _enum(
            RedistributionState, data.get("redistribution_state", RedistributionState.UNKNOWN.value),
            "proposal.redistribution_state")
        hashes = data.get("candidate_file_hashes", {})
        if not isinstance(hashes, Mapping):
            raise ResourceContractError("proposal.candidate_file_hashes must be an object")
        data["candidate_file_hashes"] = dict(hashes)
        for label, cls_type in (("build_evidence", EvidenceRecord),
                                ("test_evidence", EvidenceRecord), ("findings", Finding)):
            value = data.get(label, [])
            if not isinstance(value, list):
                raise ResourceContractError(f"proposal.{label} must be a list")
            data[label] = tuple(cls_type.from_dict(item) for item in value)
        comparison = data.get("comparison")
        data["comparison"] = ResourceComparison.from_dict(comparison) if comparison else None
        return cls(**data)


def proposal_id_for(resource_id: str, candidate_version: str, source_url: str,
                    archive_filename: str) -> str:
    payload = {
        "resource_id": resource_id,
        "candidate_version": candidate_version,
        "source_url": source_url,
        "archive_filename": archive_filename,
    }
    return "proposal-" + canonical_hash(payload)[:24]
