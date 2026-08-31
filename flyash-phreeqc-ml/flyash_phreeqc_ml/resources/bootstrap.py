"""Fail-closed registration of an installed, metadata-pinned USGS PHREEQC release."""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .catalog import CatalogError, CatalogStore
from .database import DatabaseSummary, parse_database_file
from .knowledge import (
    KNOWLEDGE_PACK_RESOURCE_ID,
    KnowledgeArtifactProvenance,
    KnowledgeFact,
    KnowledgeFactType,
    build_knowledge_pack,
    knowledge_pack_installation_id,
    knowledge_pack_manifest,
    make_fact,
    write_knowledge_pack,
)
from .models import (
    Citation,
    CompatibilityStatus,
    RedistributionState,
    ResourceContractError,
    ResourceKind,
    ResourceManifest,
    RollbackState,
    TemperatureRange,
    TestStatus,
    canonical_hash,
    make_installation_id,
    sha256_bytes,
)

OFFICIAL_DATABASE_REGISTRY_SCHEMA = "wpi.virtual-lab.usgs-phreeqc-database-registry"
OFFICIAL_DATABASE_REGISTRY_VERSION = 1
PHREEQC_RELEASE_NOTES_SCHEMA = "wpi.virtual-lab.phreeqc-release-notes"
PHREEQC_RELEASE_NOTES_VERSION = 1
BOOTSTRAP_RESULT_SCHEMA = "wpi.virtual-lab.release-resource-bootstrap-result"
BOOTSTRAP_RESULT_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReleaseBootstrapError(RuntimeError):
    """Installed release bytes or metadata do not match the reviewed registry."""


def _require_bootstrap_activation_safe(
    store: CatalogStore,
    targets: tuple[tuple[str, str, str], ...],
) -> None:
    """Refuse startup-time replacement of any existing active resource pointer."""
    try:
        state = store.load()
    except CatalogError as exc:
        raise ReleaseBootstrapError(
            f"release catalog activation preflight failed: {exc}") from exc
    for resource_id, installation_id, label in targets:
        active_installation_id = state.active.get(resource_id, "")
        if active_installation_id and active_installation_id != installation_id:
            raise ReleaseBootstrapError(
                f"bootstrap refuses to replace active {label} {resource_id!r}; "
                "use a reviewed update proposal and explicit promotion"
            )


def _closed(document: Mapping[str, Any], allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise ReleaseBootstrapError(f"{label} must be an object")
    unknown = set(document) - allowed
    if unknown:
        raise ReleaseBootstrapError(f"{label} contains unsupported fields: {sorted(unknown)}")
    return dict(document)


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ReleaseBootstrapError(f"{label} must be a string list")
    result = tuple(value)
    if result != tuple(sorted(set(result))):
        raise ReleaseBootstrapError(f"{label} must be sorted and unique")
    return result


def _sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ReleaseBootstrapError(f"{label} must be an exact lower-case SHA-256")
    return value


def _https(value: str, label: str) -> str:
    parsed = urlsplit(str(value))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ReleaseBootstrapError(f"{label} must be an HTTPS URL without credentials")
    return str(value)


@dataclass(frozen=True)
class OfficialDatabaseRecord:
    filename: str
    resource_id: str
    resource_kind: ResourceKind
    database_sha256: str
    byte_size: int
    database_family: str
    sections: tuple[str, ...]
    master_species_count: int
    solution_species_count: int
    phase_count: int
    master_species_names_sha256: str
    solution_species_names_sha256: str
    phase_names_sha256: str
    parsed_summary_sha256: str
    compatibility_status: CompatibilityStatus
    test_status: TestStatus
    standalone_test_status: TestStatus
    base_include_test_status: TestStatus
    requires_database_filename: str = ""
    requires_database_family: str = ""
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if Path(self.filename).name != self.filename or not self.filename.lower().endswith(".dat"):
            raise ReleaseBootstrapError("official database filename must be a plain .dat name")
        for label in (
            "database_sha256", "master_species_names_sha256",
            "solution_species_names_sha256", "phase_names_sha256", "parsed_summary_sha256",
        ):
            _sha256(getattr(self, label), f"database record {label}")
        for label in ("byte_size", "master_species_count", "solution_species_count", "phase_count"):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReleaseBootstrapError(f"database record {label} must be non-negative")
        _strings(self.sections, "database record sections")
        _strings(self.limitations, "database record limitations")
        if self.resource_kind == ResourceKind.DATABASE_EXTENSION:
            if not self.requires_database_filename or not self.requires_database_family:
                raise ReleaseBootstrapError("database extension requires its exact base dependency")
            if self.standalone_test_status != TestStatus.NOT_APPLICABLE_REQUIRES_BASE \
                    or self.test_status != TestStatus.NOT_APPLICABLE_REQUIRES_BASE:
                raise ReleaseBootstrapError("database extension cannot claim standalone PASS")
            if self.base_include_test_status != TestStatus.PASSED:
                raise ReleaseBootstrapError("reviewed official extension requires a passing base include test")
        elif self.requires_database_filename or self.requires_database_family:
            raise ReleaseBootstrapError("standalone database cannot declare an add-on base")
        elif self.standalone_test_status != TestStatus.PASSED \
                or self.test_status != TestStatus.PASSED:
            raise ReleaseBootstrapError("reviewed official standalone database must record PASS")

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "resource_id": self.resource_id,
            "resource_kind": self.resource_kind.value,
            "database_sha256": self.database_sha256,
            "byte_size": self.byte_size,
            "database_family": self.database_family,
            "sections": list(self.sections),
            "master_species_count": self.master_species_count,
            "solution_species_count": self.solution_species_count,
            "phase_count": self.phase_count,
            "master_species_names_sha256": self.master_species_names_sha256,
            "solution_species_names_sha256": self.solution_species_names_sha256,
            "phase_names_sha256": self.phase_names_sha256,
            "parsed_summary_sha256": self.parsed_summary_sha256,
            "compatibility_status": self.compatibility_status.value,
            "test_status": self.test_status.value,
            "standalone_test_status": self.standalone_test_status.value,
            "base_include_test_status": self.base_include_test_status.value,
            "requires_database_filename": self.requires_database_filename,
            "requires_database_family": self.requires_database_family,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "OfficialDatabaseRecord":
        data = _closed(document, {
            "filename", "resource_id", "resource_kind", "database_sha256", "byte_size",
            "database_family", "sections", "master_species_count", "solution_species_count",
            "phase_count", "master_species_names_sha256", "solution_species_names_sha256",
            "phase_names_sha256", "parsed_summary_sha256", "compatibility_status",
            "test_status", "standalone_test_status", "base_include_test_status",
            "requires_database_filename", "requires_database_family", "limitations",
        }, "official database record")
        required = {
            "filename", "resource_id", "resource_kind", "database_sha256", "byte_size",
            "database_family", "sections", "master_species_count", "solution_species_count",
            "phase_count", "master_species_names_sha256", "solution_species_names_sha256",
            "phase_names_sha256", "parsed_summary_sha256", "compatibility_status",
            "test_status", "standalone_test_status", "base_include_test_status",
        }
        missing = required - set(data)
        if missing:
            raise ReleaseBootstrapError(
                f"official database record is missing fields: {sorted(missing)}")
        try:
            data["resource_kind"] = ResourceKind(data["resource_kind"])
            data["compatibility_status"] = CompatibilityStatus(data["compatibility_status"])
            for label in ("test_status", "standalone_test_status", "base_include_test_status"):
                data[label] = TestStatus(data[label])
        except (TypeError, ValueError) as exc:
            raise ReleaseBootstrapError("official database record contains an invalid enum") from exc
        data["sections"] = _strings(data["sections"], "database record sections")
        data["limitations"] = _strings(data.get("limitations", []), "database record limitations")
        return cls(**data)


@dataclass(frozen=True)
class OfficialReleaseDatabaseRegistry:
    phreeqc_version: str
    source_archive_url: str
    source_archive_sha256: str
    official_release_page: str
    rights_notice: str
    rights_notice_sha256: str
    databases: tuple[OfficialDatabaseRecord, ...]
    schema: str = OFFICIAL_DATABASE_REGISTRY_SCHEMA
    version: int = OFFICIAL_DATABASE_REGISTRY_VERSION

    def __post_init__(self) -> None:
        if self.schema != OFFICIAL_DATABASE_REGISTRY_SCHEMA \
                or self.version != OFFICIAL_DATABASE_REGISTRY_VERSION:
            raise ReleaseBootstrapError("unsupported official database registry schema/version")
        if not self.phreeqc_version.strip() or not self.rights_notice.strip():
            raise ReleaseBootstrapError("release version and reviewed rights notice are required")
        _https(self.source_archive_url, "source_archive_url")
        _https(self.official_release_page, "official_release_page")
        _sha256(self.source_archive_sha256, "source_archive_sha256")
        _sha256(self.rights_notice_sha256, "rights_notice_sha256")
        names = [item.filename for item in self.databases]
        ids = [item.resource_id for item in self.databases]
        if not self.databases or names != sorted(names) or len(names) != len(set(names)):
            raise ReleaseBootstrapError("official databases must have unique sorted filenames")
        if len(ids) != len(set(ids)):
            raise ReleaseBootstrapError("official databases must have unique resource IDs")
        by_name = {item.filename: item for item in self.databases}
        for item in self.databases:
            if item.requires_database_filename and item.requires_database_filename not in by_name:
                raise ReleaseBootstrapError("database extension base is absent from the registry")

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "OfficialReleaseDatabaseRegistry":
        data = _closed(document, {
            "schema", "version", "phreeqc_version", "source_archive_url",
            "source_archive_sha256", "official_release_page", "rights_notice",
            "rights_notice_sha256", "database_count", "databases",
        }, "official release database registry")
        database_count = data.pop("database_count", None)
        values = data.get("databases")
        if not isinstance(values, list):
            raise ReleaseBootstrapError("official release databases must be a list")
        data["databases"] = tuple(OfficialDatabaseRecord.from_dict(item) for item in values)
        if database_count != len(data["databases"]):
            raise ReleaseBootstrapError("database_count does not match registry entries")
        try:
            return cls(**data)
        except TypeError as exc:
            raise ReleaseBootstrapError(f"official registry is missing a required field: {exc}") from exc


@dataclass(frozen=True)
class PhreeqcReleaseNotesArtifact:
    """Closed, reviewed release-metadata artifact; never inferred changelog prose."""

    artifact_id: str
    artifact_version: str
    repository_path: str
    provider: str
    official_release_page: str
    source_archive_url: str
    archive_filename: str
    reviewed_on: str
    release_metadata: tuple[str, ...]
    limitations: tuple[str, ...]
    citation: Citation
    schema: str = PHREEQC_RELEASE_NOTES_SCHEMA
    version: int = PHREEQC_RELEASE_NOTES_VERSION

    def __post_init__(self) -> None:
        if self.schema != PHREEQC_RELEASE_NOTES_SCHEMA \
                or self.version != PHREEQC_RELEASE_NOTES_VERSION:
            raise ReleaseBootstrapError("unsupported PHREEQC release-notes schema/version")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,126}[a-z0-9]", self.artifact_id):
            raise ReleaseBootstrapError("release-notes artifact_id is invalid")
        if self.artifact_id != "usgs.phreeqc.release-notes":
            raise ReleaseBootstrapError("release-notes artifact_id is not the reviewed USGS ID")
        if not self.artifact_version.strip() or not self.provider.strip():
            raise ReleaseBootstrapError(
                "release-notes artifact version and provider are required")
        logical_path = Path(self.repository_path)
        if not self.repository_path or logical_path.is_absolute() \
                or "\\" in self.repository_path \
                or any(part in {"", ".", ".."} for part in logical_path.parts):
            raise ReleaseBootstrapError(
                "release-notes repository_path must be a safe relative path")
        _https(self.official_release_page, "release-notes official_release_page")
        _https(self.source_archive_url, "release-notes source_archive_url")
        if Path(self.archive_filename).name != self.archive_filename \
                or self.archive_filename in {"", ".", ".."}:
            raise ReleaseBootstrapError(
                "release-notes archive_filename must be a plain filename")
        try:
            date.fromisoformat(self.reviewed_on)
        except (TypeError, ValueError) as exc:
            raise ReleaseBootstrapError(
                "release-notes reviewed_on must be an ISO-8601 date") from exc
        if not self.release_metadata or not self.limitations:
            raise ReleaseBootstrapError(
                "release-notes metadata and explicit limitations are required")
        _strings(self.release_metadata, "release-notes release_metadata")
        _strings(self.limitations, "release-notes limitations")
        if self.citation.url != self.official_release_page:
            raise ReleaseBootstrapError(
                "release-notes citation must identify the exact official release page")
        if not self.citation.title.strip() or not self.citation.authors \
                or not self.citation.source_location.strip() \
                or self.citation.extraction_confidence is None:
            raise ReleaseBootstrapError(
                "release-notes citation provenance is incomplete")

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "PhreeqcReleaseNotesArtifact":
        data = _closed(document, {
            "schema", "version", "artifact_id", "artifact_version", "repository_path",
            "provider", "official_release_page", "source_archive_url", "archive_filename",
            "reviewed_on", "release_metadata", "limitations", "citation",
        }, "PHREEQC release-notes artifact")
        required = {
            "artifact_id", "artifact_version", "repository_path", "provider",
            "official_release_page", "source_archive_url", "archive_filename", "reviewed_on",
            "release_metadata", "limitations", "citation",
        }
        missing = required - set(data)
        if missing:
            raise ReleaseBootstrapError(
                f"PHREEQC release-notes artifact is missing fields: {sorted(missing)}")
        data["release_metadata"] = _strings(
            data["release_metadata"], "release-notes release_metadata")
        data["limitations"] = _strings(data["limitations"], "release-notes limitations")
        try:
            data["citation"] = Citation.from_dict(data["citation"])
            return cls(**data)
        except (ResourceContractError, TypeError) as exc:
            raise ReleaseBootstrapError(
                f"PHREEQC release-notes artifact is invalid: {exc}") from exc

    def to_knowledge_fact(
        self,
        *,
        artifact_sha256: str,
        runtime_installation_id: str,
    ) -> KnowledgeFact:
        _sha256(artifact_sha256, "release-notes artifact SHA-256")
        provenance = KnowledgeArtifactProvenance(
            artifact_id=self.artifact_id,
            artifact_version=self.artifact_version,
            repository_path=self.repository_path,
            sha256=artifact_sha256,
        )
        return make_fact(
            KnowledgeFactType.DOCUMENTATION_STATEMENT,
            f"Versioned release-notes artifact {self.artifact_id} "
            f"{self.artifact_version} (SHA-256 {artifact_sha256}) records reviewed official "
            f"PHREEQC release metadata: {' '.join(self.release_metadata)}",
            source_installation_ids=(runtime_installation_id,),
            source_artifacts=(provenance,),
            citation=self.citation,
            warnings=self.limitations,
        )


@dataclass(frozen=True)
class ReleaseBootstrapResult:
    phreeqc_version: str
    runtime_installation_id: str
    active_database_installation_id: str
    registered_database_installation_ids: tuple[str, ...]
    catalog_generation: int
    catalog_hash: str
    registry_file_sha256: str
    knowledge_pack_hash: str
    knowledge_pack_path: str
    schema: str = BOOTSTRAP_RESULT_SCHEMA
    version: int = BOOTSTRAP_RESULT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "phreeqc_version": self.phreeqc_version,
            "runtime_installation_id": self.runtime_installation_id,
            "active_database_installation_id": self.active_database_installation_id,
            "registered_database_installation_ids": list(
                self.registered_database_installation_ids),
            "catalog_generation": self.catalog_generation,
            "catalog_hash": self.catalog_hash,
            "registry_file_sha256": self.registry_file_sha256,
            "knowledge_pack_hash": self.knowledge_pack_hash,
            "knowledge_pack_path": self.knowledge_pack_path,
        }


def default_official_database_registry_path() -> Path:
    return Path(__file__).resolve().parents[2] / "resources" / "official_usgs_3.8.6-17100_databases.json"


def default_phreeqc_release_notes_path() -> Path:
    return (Path(__file__).resolve().parents[2]
            / "release" / "PHREEQC_RELEASE_NOTES_3.8.6-17100.json")


def load_official_database_registry(
    path: str | Path | None = None,
) -> tuple[OfficialReleaseDatabaseRegistry, str]:
    location = Path(path) if path is not None else default_official_database_registry_path()
    try:
        details = location.lstat()
    except OSError as exc:
        raise ReleaseBootstrapError(f"cannot inspect official database registry: {exc}") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ReleaseBootstrapError("official database registry must be a regular non-symlink file")
    raw = location.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBootstrapError(f"official database registry is invalid JSON: {exc}") from exc
    return OfficialReleaseDatabaseRegistry.from_dict(document), sha256_bytes(raw)


def _runtime_manifest(value: ResourceManifest | Mapping[str, Any] | str | Path) -> ResourceManifest:
    if isinstance(value, ResourceManifest):
        return value
    if isinstance(value, Mapping):
        document = value
    else:
        location = Path(value)
        try:
            details = location.lstat()
        except OSError as exc:
            raise ReleaseBootstrapError(f"cannot inspect runtime manifest: {exc}") from exc
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ReleaseBootstrapError("runtime manifest must be a regular non-symlink file")
        try:
            document = json.loads(location.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseBootstrapError(f"runtime manifest is invalid JSON: {exc}") from exc
    try:
        return ResourceManifest.from_dict(document)
    except ResourceContractError as exc:
        raise ReleaseBootstrapError(f"runtime manifest is invalid: {exc}") from exc


def _regular_file(path: str | Path, label: str) -> Path:
    location = Path(path)
    try:
        details = location.lstat()
    except OSError as exc:
        raise ReleaseBootstrapError(f"cannot inspect {label}: {exc}") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ReleaseBootstrapError(f"{label} must be a regular non-symlink file")
    return location.resolve(strict=True)


def load_phreeqc_release_notes(
    path: str | Path,
) -> tuple[PhreeqcReleaseNotesArtifact, str]:
    """Load and hash the exact reviewed release-notes artifact without network access."""
    location = _regular_file(path, "PHREEQC release-notes artifact")
    raw = location.read_bytes()
    if not raw or len(raw) > 256 * 1024:
        raise ReleaseBootstrapError(
            "PHREEQC release-notes artifact has an invalid size")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBootstrapError(
            f"PHREEQC release-notes artifact is invalid JSON: {exc}") from exc
    return PhreeqcReleaseNotesArtifact.from_dict(document), sha256_bytes(raw)


def _release_notes_path_for_registry(
    registry: OfficialReleaseDatabaseRegistry,
    registry_path: str | Path | None,
    release_notes_path: str | Path | None,
) -> Path:
    if release_notes_path is not None:
        return Path(release_notes_path)
    if registry_path is not None:
        sibling = Path(registry_path).parent / (
            f"PHREEQC_RELEASE_NOTES_{registry.phreeqc_version}.json")
        if sibling.exists() or sibling.is_symlink():
            return sibling
    return default_phreeqc_release_notes_path()


def _verify_release_notes_registry_binding(
    artifact: PhreeqcReleaseNotesArtifact,
    registry: OfficialReleaseDatabaseRegistry,
) -> None:
    expected = {
        "artifact_version": registry.phreeqc_version,
        "provider": "U.S. Geological Survey",
        "official_release_page": registry.official_release_page,
        "source_archive_url": registry.source_archive_url,
        "archive_filename": Path(urlsplit(registry.source_archive_url).path).name,
    }
    for label, value in expected.items():
        if getattr(artifact, label) != value:
            raise ReleaseBootstrapError(
                f"PHREEQC release-notes artifact does not match registry {label}")


def _verify_summary(record: OfficialDatabaseRecord, summary: DatabaseSummary) -> None:
    observed = {
        "database_sha256": summary.source_sha256,
        "byte_size": summary.byte_size,
        "sections": summary.sections,
        "master_species_count": len(summary.master_species),
        "solution_species_count": len(summary.solution_species),
        "phase_count": len(summary.phases),
        "master_species_names_sha256": canonical_hash(list(summary.master_species)),
        "solution_species_names_sha256": canonical_hash(list(summary.solution_species)),
        "phase_names_sha256": canonical_hash(list(summary.phases)),
        "parsed_summary_sha256": canonical_hash(summary.to_dict()),
    }
    for label, value in observed.items():
        if value != getattr(record, label):
            raise ReleaseBootstrapError(
                f"official database metadata mismatch for {record.filename}: {label}")
    if record.resource_kind == ResourceKind.DATABASE_EXTENSION:
        if not summary.is_database_extension \
                or summary.requires_database_filename != record.requires_database_filename \
                or summary.requires_database_family != record.requires_database_family:
            raise ReleaseBootstrapError(
                f"official extension dependency mismatch for {record.filename}")
    elif summary.is_database_extension:
        raise ReleaseBootstrapError(
            f"standalone database was unexpectedly parsed as an extension: {record.filename}")


def _database_manifest(
    registry: OfficialReleaseDatabaseRegistry,
    record: OfficialDatabaseRecord,
    summary: DatabaseSummary,
    path: Path,
    resource_ids_by_filename: Mapping[str, str],
    runtime: ResourceManifest,
) -> ResourceManifest:
    dependencies: tuple[str, ...] = ()
    extension_for = ""
    duplicate_risk = ""
    extension_for_installation_id = ""
    extension_base_version = ""
    extension_base_sha256 = ""
    combined_identity_sha256 = ""
    if record.requires_database_filename:
        extension_for = resource_ids_by_filename[record.requires_database_filename]
        dependencies = (extension_for,)
        base_records = [
            item for item in registry.databases
            if item.filename == record.requires_database_filename
        ]
        if len(base_records) != 1:
            raise ReleaseBootstrapError(
                f"official extension base is ambiguous for {record.filename}")
        base_record = base_records[0]
        extension_for_installation_id = make_installation_id(
            base_record.resource_id, registry.phreeqc_version,
            base_record.database_sha256)
        extension_base_version = registry.phreeqc_version
        extension_base_sha256 = base_record.database_sha256
        combined_identity_sha256 = canonical_hash({
            "schema": "wpi.virtual-lab.database-extension-binding",
            "version": 1,
            "base": {
                "resource_id": base_record.resource_id,
                "installation_id": extension_for_installation_id,
                "version": extension_base_version,
                "filename": base_record.filename,
                "family": base_record.database_family,
                "sha256": extension_base_sha256,
            },
            "extension": {
                "resource_id": record.resource_id,
                "installation_id": make_installation_id(
                    record.resource_id, registry.phreeqc_version,
                    record.database_sha256),
                "version": registry.phreeqc_version,
                "filename": record.filename,
                "sha256": record.database_sha256,
            },
        })
        duplicate_risk = (
            "Extension/add-on only: use an explicit administrator-reviewed INCLUDE$ with the "
            "declared base; never concatenate database text automatically."
        )
    return ResourceManifest(
        resource_id=record.resource_id,
        installation_id=make_installation_id(
            record.resource_id, registry.phreeqc_version, record.database_sha256),
        resource_kind=record.resource_kind,
        display_name=f"USGS PHREEQC {record.filename}",
        provider="U.S. Geological Survey",
        official_source_url=registry.source_archive_url,
        discovered_version=registry.phreeqc_version,
        installed_version=registry.phreeqc_version,
        archive_filename=record.filename,
        source_sha256=registry.source_archive_sha256,
        database_sha256=record.database_sha256,
        content_sha256=record.database_sha256,
        install_path=str(path),
        installed_at=runtime.installed_at,
        verified_at=runtime.verified_at,
        citation=Citation(
            title=f"USGS PHREEQC {registry.phreeqc_version} distribution",
            authors=("U.S. Geological Survey",),
            url=registry.official_release_page,
            source_location=registry.source_archive_url,
            extraction_confidence=1.0,
        ),
        rights_notice=registry.rights_notice,
        licence_notice=(
            f"Reviewed upstream NOTICE SHA-256: {registry.rights_notice_sha256}"),
        redistribution_state=RedistributionState.PERMITTED,
        temperature_range=TemperatureRange(
            notes="Database-specific valid temperature range was not inferred by the registry."),
        database_filename=record.filename,
        database_family=record.database_family,
        dependencies=dependencies,
        requires_database_filename=record.requires_database_filename,
        requires_database_family=record.requires_database_family,
        extension_for_resource_id=extension_for,
        extension_for_installation_id=extension_for_installation_id,
        extension_base_version=extension_base_version,
        extension_base_sha256=extension_base_sha256,
        combined_identity_sha256=combined_identity_sha256,
        duplicate_species_phase_risk=duplicate_risk,
        domain_notes=record.limitations,
        supported_summary=summary.supported_summary,
        compatibility_status=record.compatibility_status,
        test_status=record.test_status,
        standalone_test_status=record.standalone_test_status,
        base_include_test_status=record.base_include_test_status,
        warnings=tuple(sorted(set((*summary.warnings, *record.limitations)))),
        rollback_state=RollbackState.CANDIDATE,
    )


def bootstrap_release_resources(
    store: CatalogStore,
    *,
    runtime_manifest: ResourceManifest | Mapping[str, Any] | str | Path,
    executable_path: str | Path,
    database_directory: str | Path,
    registry_path: str | Path | None = None,
    release_notes_path: str | Path | None = None,
    knowledge_output_directory: str | Path | None = None,
) -> ReleaseBootstrapResult:
    """Verify a complete installed release before performing any catalog mutation."""
    if not isinstance(store, CatalogStore):
        raise ReleaseBootstrapError("bootstrap requires a CatalogStore")
    registry, registry_file_sha256 = load_official_database_registry(registry_path)
    notes_location = _release_notes_path_for_registry(
        registry, registry_path, release_notes_path)
    release_notes, release_notes_sha256 = load_phreeqc_release_notes(notes_location)
    _verify_release_notes_registry_binding(release_notes, registry)
    runtime = _runtime_manifest(runtime_manifest)
    executable = _regular_file(executable_path, "PHREEQC executable")
    if runtime.resource_kind != ResourceKind.PHREEQC_RUNTIME:
        raise ReleaseBootstrapError("installed runtime manifest is not a PHREEQC runtime")
    if runtime.installed_version != registry.phreeqc_version:
        raise ReleaseBootstrapError("runtime version does not match the official database registry")
    if runtime.source_sha256 != registry.source_archive_sha256:
        raise ReleaseBootstrapError("runtime source hash does not match the reviewed release archive")
    if not runtime.executable_sha256 \
            or sha256_bytes(executable.read_bytes()) != runtime.executable_sha256:
        raise ReleaseBootstrapError("PHREEQC executable hash does not match its runtime manifest")
    if not os.access(executable, os.X_OK):
        raise ReleaseBootstrapError("PHREEQC executable is not executable")
    if not runtime.install_path \
            or Path(runtime.install_path).resolve(strict=True) != executable:
        raise ReleaseBootstrapError("runtime manifest install_path does not identify the executable")
    if runtime.test_status != TestStatus.PASSED:
        raise ReleaseBootstrapError("runtime manifest does not record passing release tests")
    release_notes_fact = release_notes.to_knowledge_fact(
        artifact_sha256=release_notes_sha256,
        runtime_installation_id=runtime.installation_id,
    )

    database_root = Path(database_directory)
    try:
        details = database_root.lstat()
    except OSError as exc:
        raise ReleaseBootstrapError(f"cannot inspect PHREEQC database directory: {exc}") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise ReleaseBootstrapError(
            "PHREEQC database directory must be a regular non-symlink directory")
    database_root = database_root.resolve(strict=True)
    expected_names = {record.filename for record in registry.databases}
    actual_names = {path.name for path in database_root.iterdir()
                    if path.name.lower().endswith(".dat")}
    missing = sorted(expected_names - actual_names)
    unexpected = sorted(actual_names - expected_names)
    if missing or unexpected:
        raise ReleaseBootstrapError(
            f"official database inventory mismatch; missing={missing}, unexpected={unexpected}")

    summaries: dict[str, DatabaseSummary] = {}
    paths: dict[str, Path] = {}
    for record in registry.databases:
        path = _regular_file(database_root / record.filename, record.filename)
        summary = parse_database_file(path)
        _verify_summary(record, summary)
        paths[record.filename] = path
        summaries[record.filename] = summary

    resource_ids = {record.filename: record.resource_id for record in registry.databases}
    manifests = tuple(_database_manifest(
        registry, record, summaries[record.filename], paths[record.filename], resource_ids,
        runtime,
    ) for record in registry.databases)
    phreeqc_record = next(
        (item for item in registry.databases if item.filename == "phreeqc.dat"), None)
    if phreeqc_record is None:
        raise ReleaseBootstrapError("official registry does not contain phreeqc.dat")
    phreeqc_manifest = next(
        item for item in manifests if item.resource_id == phreeqc_record.resource_id)

    _require_bootstrap_activation_safe(store, (
        (runtime.resource_id, runtime.installation_id, "PHREEQC runtime"),
        (
            phreeqc_manifest.resource_id,
            phreeqc_manifest.installation_id,
            "default PHREEQC database",
        ),
    ))
    try:
        store.register_many((runtime, *manifests))
        store.bootstrap_activate(runtime.resource_id, runtime.installation_id)
        store.bootstrap_activate(phreeqc_manifest.resource_id, phreeqc_manifest.installation_id)
    except CatalogError as exc:
        raise ReleaseBootstrapError(f"release catalog registration failed: {exc}") from exc

    pack = build_knowledge_pack(store, documentation_facts=(release_notes_fact,))
    _require_bootstrap_activation_safe(store, ((
        KNOWLEDGE_PACK_RESOURCE_ID,
        knowledge_pack_installation_id(pack),
        "scientific knowledge pack",
    ),))
    knowledge_path = write_knowledge_pack(pack, store.knowledge_dir)
    if knowledge_output_directory is not None:
        requested_root = Path(knowledge_output_directory).expanduser().absolute()
        if requested_root != store.knowledge_dir.expanduser().absolute():
            write_knowledge_pack(pack, requested_root)
    pack_manifest = knowledge_pack_manifest(
        pack,
        knowledge_path,
        installed_at=runtime.installed_at,
        verified_at=runtime.verified_at,
    )
    try:
        store.register_many((pack_manifest,))
        store.bootstrap_activate(pack_manifest.resource_id, pack_manifest.installation_id)
    except CatalogError as exc:
        raise ReleaseBootstrapError(
            f"knowledge-pack catalog registration failed: {exc}") from exc
    state = store.load()
    return ReleaseBootstrapResult(
        phreeqc_version=registry.phreeqc_version,
        runtime_installation_id=runtime.installation_id,
        active_database_installation_id=phreeqc_manifest.installation_id,
        registered_database_installation_ids=tuple(sorted(
            item.installation_id for item in manifests)),
        catalog_generation=state.generation,
        catalog_hash=state.catalog_hash,
        registry_file_sha256=registry_file_sha256,
        knowledge_pack_hash=pack.pack_hash,
        knowledge_pack_path=str(knowledge_path.resolve()),
    )
