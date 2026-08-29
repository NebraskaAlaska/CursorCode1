"""Section-aware PHREEQC database parsing, registry views, and safe external import."""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .catalog import CatalogError, CatalogStore, atomic_write_bytes, atomic_write_json
from .models import (
    Citation,
    CompatibilityStatus,
    RedistributionState,
    ResourceContractError,
    ResourceKind,
    ResourceManifest,
    SupportedSpeciesPhaseSummary,
    TemperatureRange,
    TestStatus,
    canonical_hash,
    make_installation_id,
    sha256_bytes,
    utc_now,
)
from .sources import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    SourcePolicyError,
    archive_database_members,
    inspect_archive,
    read_archive_member,
)

DATABASE_SUMMARY_SCHEMA = "wpi.virtual-lab.phreeqc-database-summary"
DATABASE_SUMMARY_VERSION = 1

_SECTION_KEYWORDS = {
    "SOLUTION_MASTER_SPECIES",
    "SOLUTION_SPECIES",
    "PHASES",
    "EXCHANGE_MASTER_SPECIES",
    "EXCHANGE_SPECIES",
    "SURFACE_MASTER_SPECIES",
    "SURFACE_SPECIES",
    "RATES",
    "PITZER",
    "SIT",
    "NAMED_EXPRESSIONS",
    "END",
}
_PHASE_PARAMETER_PREFIXES = (
    "log_k", "-log_k", "delta_h", "-delta_h", "-analytical_expression", "-gamma",
    "-vm", "vm", "-add_constant", "-add_logk",
)

# These are PHREEQC-distributed extension files, not complete databases.  The mapping
# records the exact base named by their upstream instructions.  It intentionally does
# not merge either file with its base: callers must construct and review an INCLUDE$
# input explicitly.
_OFFICIAL_EXTENSION_BASES = {
    "concrete_phr.dat": ("phreeqc.dat", "phreeqc"),
    "concrete_pz.dat": ("pitzer.dat", "pitzer"),
}
_INCLUDE_DIRECTIVE_RE = re.compile(
    r"(?im)^\s*INCLUDE\$\s+(?:\"([^\"]+)\"|'([^']+)'|([^\s#;]+))"
)
_FOR_USE_WITH_RE = re.compile(
    r"(?i)\bfor\s+use\s+with\s+(?:the\s+)?([A-Za-z0-9_.-]+\.dat)\b"
)


class DatabaseRegistryError(ValueError):
    """A database cannot be parsed, imported, or registered safely."""


@dataclass(frozen=True)
class DatabaseSummary:
    source_sha256: str
    byte_size: int
    sections: tuple[str, ...]
    master_species: tuple[str, ...]
    solution_species: tuple[str, ...]
    phases: tuple[str, ...]
    detected_family: str = "unknown"
    detected_version: str = ""
    include_files: tuple[str, ...] = ()
    is_database_extension: bool = False
    requires_database_filename: str = ""
    requires_database_family: str = ""
    standalone_test_status: TestStatus = TestStatus.NOT_TESTED
    compatibility_warning: str = ""
    warnings: tuple[str, ...] = ()
    schema: str = DATABASE_SUMMARY_SCHEMA
    version: int = DATABASE_SUMMARY_VERSION

    def __post_init__(self) -> None:
        if self.schema != DATABASE_SUMMARY_SCHEMA or self.version != DATABASE_SUMMARY_VERSION:
            raise DatabaseRegistryError("unsupported database-summary schema/version")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise DatabaseRegistryError("database summary requires an exact SHA-256")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int) \
                or self.byte_size < 0:
            raise DatabaseRegistryError("database byte_size must be a non-negative integer")
        for label in (
            "sections", "master_species", "solution_species", "phases", "include_files",
            "warnings",
        ):
            value = getattr(self, label)
            if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
                raise DatabaseRegistryError(f"database {label} must be a tuple of strings")
            if value != tuple(sorted(set(value))):
                raise DatabaseRegistryError(f"database {label} must be sorted and unique")
        if not isinstance(self.is_database_extension, bool):
            raise DatabaseRegistryError("is_database_extension must be boolean")
        try:
            TestStatus(self.standalone_test_status)
        except (TypeError, ValueError) as exc:
            raise DatabaseRegistryError("invalid standalone_test_status") from exc
        if self.is_database_extension:
            if not self.requires_database_filename or not self.requires_database_family:
                raise DatabaseRegistryError(
                    "a database extension requires an exact base filename and family")
            if self.standalone_test_status != TestStatus.NOT_APPLICABLE_REQUIRES_BASE:
                raise DatabaseRegistryError(
                    "a database extension standalone test must be not_applicable_requires_base")
            if not self.compatibility_warning:
                raise DatabaseRegistryError("a database extension requires a compatibility warning")
        elif self.requires_database_filename or self.requires_database_family:
            raise DatabaseRegistryError(
                "a standalone database cannot declare an extension base dependency")

    @property
    def supported_summary(self) -> SupportedSpeciesPhaseSummary:
        return SupportedSpeciesPhaseSummary(
            master_species=self.master_species,
            solution_species=self.solution_species,
            phases=self.phases,
        )

    def has_phase(self, phase: str) -> bool:
        return str(phase) in self.phases

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "source_sha256": self.source_sha256,
            "byte_size": self.byte_size,
            "sections": list(self.sections),
            "master_species": list(self.master_species),
            "solution_species": list(self.solution_species),
            "phases": list(self.phases),
            "detected_family": self.detected_family,
            "detected_version": self.detected_version,
            "include_files": list(self.include_files),
            "is_database_extension": self.is_database_extension,
            "requires_database_filename": self.requires_database_filename,
            "requires_database_family": self.requires_database_family,
            "standalone_test_status": self.standalone_test_status.value,
            "compatibility_warning": self.compatibility_warning,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "DatabaseSummary":
        allowed = {
            "schema", "version", "source_sha256", "byte_size", "sections", "master_species",
            "solution_species", "phases", "detected_family", "detected_version", "warnings",
            "include_files", "is_database_extension", "requires_database_filename",
            "requires_database_family", "standalone_test_status", "compatibility_warning",
        }
        if not isinstance(document, Mapping):
            raise DatabaseRegistryError("database summary must be an object")
        unknown = set(document) - allowed
        if unknown:
            raise DatabaseRegistryError(
                f"database summary contains unsupported fields: {sorted(unknown)}")
        data = dict(document)
        for required in ("source_sha256", "byte_size"):
            if required not in data:
                raise DatabaseRegistryError(f"database summary is missing {required}")
        for label in (
            "sections", "master_species", "solution_species", "phases", "include_files",
            "warnings",
        ):
            value = data.get(label, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise DatabaseRegistryError(f"database summary {label} must be a list of strings")
            data[label] = tuple(value)
        try:
            data["standalone_test_status"] = TestStatus(
                data.get("standalone_test_status", TestStatus.NOT_TESTED.value))
        except (TypeError, ValueError) as exc:
            raise DatabaseRegistryError("invalid standalone_test_status") from exc
        return cls(**data)


def _strip_comment(line: str) -> str:
    return line.split("#", 1)[0].rstrip()


def _section_heading(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    first = stripped.split()[0].upper()
    return first if first in _SECTION_KEYWORDS else None


def _detect_family(filename: str, text: str) -> str:
    lower_name = Path(filename).name.lower()
    exact_names = {
        "phreeqc.dat": "phreeqc",
        "phreeqc_rates.dat": "phreeqc",
        "pitzer.dat": "pitzer",
        "sit.dat": "sit",
        "llnl.dat": "llnl",
        "minteq.dat": "minteq",
        "minteq.v4.dat": "minteq",
        "wateq4f.dat": "wateq",
    }
    if lower_name in exact_names:
        return exact_names[lower_name]
    sample = (filename + "\n" + text[:16_384]).lower()
    markers = (
        ("cemdata", "cemdata"),
        ("pitzer", "pitzer"),
        ("minteq", "minteq"),
        ("wateq", "wateq"),
        ("llnl", "llnl"),
        ("sit.dat", "sit"),
        ("phreeqc.dat", "phreeqc"),
    )
    return next((family for marker, family in markers if marker in sample), "unknown")


def _detect_version(text: str) -> str:
    sample = text[:16_384]
    patterns = (
        r"\bPHREEQC(?:\s+version)?\s+([0-9]+(?:\.[0-9]+){1,3}(?:-[0-9]+)?)\b",
        r"\bCEMDATA(?:18)?(?:[._-][0-9]+)+(?:[._-][0-9]+)*\b",
    )
    for pattern in patterns:
        match = re.search(pattern, sample, flags=re.IGNORECASE)
        if match:
            return match.group(1) if match.lastindex else match.group(0)
    return ""


def _detect_extension_dependency(filename: str, text: str) -> tuple[tuple[str, ...], str, str]:
    """Return mentioned INCLUDE$ files plus an exact required base filename/family.

    INCLUDE$ directives in the file are reported for audit.  A base is inferred only
    from an explicit upstream phrase or the two known PHREEQC-distributed Concrete
    extension filenames; arbitrary incomplete databases are never silently reclassified.
    """
    include_files: set[str] = set()
    for match in _INCLUDE_DIRECTIVE_RE.finditer(text):
        value = next((part for part in match.groups() if part), "").replace("\\", "/")
        if value:
            include_files.add(Path(value).name)

    known = _OFFICIAL_EXTENSION_BASES.get(Path(filename).name.lower())
    required_filename = known[0] if known else ""
    required_family = known[1] if known else ""
    explicit = _FOR_USE_WITH_RE.search(text[:32_768])
    if explicit:
        mentioned = Path(explicit.group(1)).name
        lower = mentioned.lower()
        if lower in {"phreeqc.dat", "pitzer.dat", "sit.dat", "llnl.dat"}:
            required_filename = mentioned
            required_family = Path(lower).stem
        elif "minteq" in lower:
            required_filename, required_family = mentioned, "minteq"
        elif "wateq" in lower:
            required_filename, required_family = mentioned, "wateq"
        elif "cemdata" in lower:
            required_filename, required_family = mentioned, "cemdata"
    return tuple(sorted(include_files)), required_filename, required_family


def parse_database_bytes(raw: bytes, *, filename: str = "database.dat",
                         max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES) -> DatabaseSummary:
    """Parse only named PHREEQC database sections; no token is searched globally."""
    if not isinstance(raw, bytes):
        raise DatabaseRegistryError("database contents must be bytes")
    if not raw or len(raw) > max_bytes:
        raise DatabaseRegistryError("database is empty or exceeds the configured size cap")
    if b"\x00" in raw:
        raise DatabaseRegistryError("database contains NUL bytes and is not accepted as text")
    text = raw.decode("utf-8", errors="replace")
    sections: set[str] = set()
    master_species: set[str] = set()
    solution_species: set[str] = set()
    phases: set[str] = set()
    active_section = ""

    for original in text.splitlines():
        line = _strip_comment(original)
        if not line.strip():
            continue
        heading = _section_heading(line)
        if heading:
            active_section = heading
            sections.add(heading)
            continue
        stripped = line.strip()
        if stripped.startswith("-"):
            continue
        if active_section == "SOLUTION_MASTER_SPECIES":
            token = stripped.split()[0]
            if token and token.upper() not in _SECTION_KEYWORDS:
                master_species.add(token)
        elif active_section == "SOLUTION_SPECIES" and "=" in stripped:
            left = stripped.split("=", 1)[0].strip()
            if left:
                solution_species.add(left)
        elif active_section == "PHASES":
            # PHREEQC phase headings occur at column zero and are followed by an equation.
            # Thermodynamic parameter lines are indented or use a known option prefix.
            lower = stripped.lower()
            if not original[:1].isspace() and "=" not in stripped \
                    and not lower.startswith(_PHASE_PARAMETER_PREFIXES):
                phases.add(stripped)

    warnings: set[str] = set()
    for required in ("SOLUTION_MASTER_SPECIES", "SOLUTION_SPECIES", "PHASES"):
        if required not in sections:
            warnings.add(f"missing required PHREEQC database section: {required}")
    if not master_species:
        warnings.add("no master species were parsed")
    if not solution_species:
        warnings.add("no solution species were parsed")
    if not phases:
        warnings.add("no phases were parsed")
    if "\ufffd" in text:
        warnings.add("database contained undecodable bytes replaced during text inspection")

    include_files, required_filename, required_family = _detect_extension_dependency(
        filename, text)
    is_extension = bool(required_filename)
    compatibility_warning = ""
    standalone_status = TestStatus.NOT_TESTED
    if is_extension:
        standalone_status = TestStatus.NOT_APPLICABLE_REQUIRES_BASE
        compatibility_warning = (
            f"database extension/add-on requires explicit reviewed INCLUDE$ use with "
            f"{required_filename}; standalone loading is not applicable"
        )
        warnings.add(compatibility_warning)

    return DatabaseSummary(
        source_sha256=sha256_bytes(raw),
        byte_size=len(raw),
        sections=tuple(sorted(sections)),
        master_species=tuple(sorted(master_species)),
        solution_species=tuple(sorted(solution_species)),
        phases=tuple(sorted(phases)),
        detected_family=_detect_family(filename, text),
        detected_version=_detect_version(text),
        include_files=include_files,
        is_database_extension=is_extension,
        requires_database_filename=required_filename,
        requires_database_family=required_family,
        standalone_test_status=standalone_status,
        compatibility_warning=compatibility_warning,
        warnings=tuple(sorted(warnings)),
    )


def parse_database_file(path: str | Path, *,
                        max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES) -> DatabaseSummary:
    source = Path(path).expanduser()
    try:
        details = source.lstat()
    except OSError as exc:
        raise DatabaseRegistryError(f"cannot inspect database: {exc}") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise DatabaseRegistryError("database must be a regular non-symlink file")
    if details.st_size > max_bytes:
        raise DatabaseRegistryError("database exceeds the configured size cap")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise DatabaseRegistryError(f"cannot read database: {exc}") from exc
    return parse_database_bytes(raw, filename=source.name, max_bytes=max_bytes)


class DatabaseRegistry:
    """Database-specific read view over the authoritative resource catalog."""

    DATABASE_KINDS = {
        ResourceKind.PHREEQC_OFFICIAL_DATABASE,
        ResourceKind.EXTERNAL_THERMODYNAMIC_DATABASE,
        ResourceKind.DATABASE_EXTENSION,
    }

    def __init__(self, store: CatalogStore):
        self.store = store

    def list(self) -> tuple[ResourceManifest, ...]:
        return tuple(item for item in self.store.load().resources
                     if item.resource_kind in self.DATABASE_KINDS)

    def available_phases(self, installation_id: str) -> tuple[str, ...]:
        manifest = self.store.get_installation(installation_id)
        if manifest.resource_kind not in self.DATABASE_KINDS:
            raise DatabaseRegistryError("resource is not a thermodynamic database")
        return manifest.supported_summary.phases

    def audit_phases(self, installation_id: str, requested_phases) -> dict[str, Any]:
        available = set(self.available_phases(installation_id))
        requested = tuple(sorted({str(item) for item in requested_phases}))
        return {
            "installation_id": installation_id,
            "available": [item for item in requested if item in available],
            "missing": [item for item in requested if item not in available],
        }


class ExternalDatabaseImporter:
    """Install exactly one reviewed database file; archives are never concatenated."""

    def __init__(self, store: CatalogStore, *, max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES):
        self.store = store
        self.max_bytes = max_bytes

    def _source_bytes(self, source: Path) -> bytes:
        try:
            details = source.lstat()
        except OSError as exc:
            raise DatabaseRegistryError(f"cannot inspect import source: {exc}") from exc
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise DatabaseRegistryError("database import source must be a regular non-symlink file")
        if details.st_size > self.max_bytes:
            raise DatabaseRegistryError("database import source exceeds the configured size cap")
        try:
            return source.read_bytes()
        except OSError as exc:
            raise DatabaseRegistryError(f"cannot read database import source: {exc}") from exc

    def import_database(
        self,
        source: str | Path,
        *,
        resource_id: str,
        display_name: str,
        provider: str,
        installed_version: str,
        rights_confirmed: bool,
        rights_notice: str,
        redistribution_state: RedistributionState,
        actor_role: str,
        actor_type: str = "human",
        deployment_scope: str = "local",
        archive_member: str | None = None,
        official_source_url: str = "",
        licence_notice: str = "",
        citation: Citation | None = None,
        temperature_range: TemperatureRange | None = None,
        database_family: str = "",
        dependencies: tuple[str, ...] = (),
        conflicts: tuple[str, ...] = (),
        domain_notes: tuple[str, ...] = (),
        resource_kind: ResourceKind | None = None,
    ) -> ResourceManifest:
        if actor_type != "human" or actor_role not in {"user", "admin"}:
            raise DatabaseRegistryError("database rights must be confirmed by a human user/admin")
        if deployment_scope not in {"local", "hosted"}:
            raise DatabaseRegistryError("deployment_scope must be local or hosted")
        if deployment_scope == "hosted" and actor_role != "admin":
            raise DatabaseRegistryError("hosted database installation is administrator-only")
        if rights_confirmed is not True or not str(rights_notice).strip():
            raise DatabaseRegistryError("explicit rights confirmation and notice are required")
        try:
            redistribution_state = RedistributionState(redistribution_state)
        except (TypeError, ValueError) as exc:
            raise DatabaseRegistryError("invalid redistribution state") from exc
        if redistribution_state == RedistributionState.PROHIBITED:
            raise DatabaseRegistryError("a prohibited database cannot be installed")

        source_path = Path(source).expanduser()
        source_raw = self._source_bytes(source_path)
        source_sha256 = sha256_bytes(source_raw)
        selected_member = ""
        if source_path.suffix.lower() == ".dat":
            if archive_member:
                raise DatabaseRegistryError("archive_member is invalid for a plain .dat file")
            database_raw = source_raw
            database_filename = source_path.name
        else:
            try:
                inspect_archive(source_path)
                candidates = archive_database_members(source_path)
            except SourcePolicyError as exc:
                raise DatabaseRegistryError(str(exc)) from exc
            if archive_member is None:
                if len(candidates) != 1:
                    raise DatabaseRegistryError(
                        "archive must contain exactly one .dat file or an explicit member selection")
                selected_member = candidates[0]
            else:
                selected_member = str(archive_member).replace("\\", "/")
                if selected_member not in candidates:
                    raise DatabaseRegistryError("selected database member is absent from the archive")
            try:
                database_raw = read_archive_member(
                    source_path, selected_member, max_bytes=self.max_bytes)
            except SourcePolicyError as exc:
                raise DatabaseRegistryError(str(exc)) from exc
            database_filename = Path(selected_member).name

        summary = parse_database_bytes(
            database_raw, filename=database_filename, max_bytes=self.max_bytes)
        installation_id = make_installation_id(
            resource_id, installed_version, summary.source_sha256)
        try:
            return self.store.get_installation(installation_id)
        except CatalogError:
            pass

        install_dir = self.store.installation_dir(installation_id)
        install_dir.mkdir(parents=True, exist_ok=True)
        if install_dir.is_symlink():
            raise DatabaseRegistryError("installation directory must not be a symlink")
        installed_database = install_dir / "database.dat"
        if installed_database.exists():
            if installed_database.is_symlink() \
                    or sha256_bytes(installed_database.read_bytes()) != summary.source_sha256:
                raise DatabaseRegistryError("side-by-side installation path contains other bytes")
        else:
            atomic_write_bytes(installed_database, database_raw, create_only=True)

        notes = set(domain_notes)
        if selected_member:
            notes.add(f"selected archive member: {selected_member}")
        warnings = set(summary.warnings)
        requested_kind = (ResourceKind(resource_kind) if resource_kind is not None
                          else ResourceKind.EXTERNAL_THERMODYNAMIC_DATABASE)
        if requested_kind not in {
            ResourceKind.PHREEQC_OFFICIAL_DATABASE,
            ResourceKind.EXTERNAL_THERMODYNAMIC_DATABASE,
            ResourceKind.DATABASE_EXTENSION,
        }:
            raise DatabaseRegistryError("database import received a non-database resource kind")
        imported_kind = requested_kind
        compatibility_status = CompatibilityStatus.REVIEW_REQUIRED
        test_status = TestStatus.NOT_TESTED
        requires_database_family = ""
        manifest_dependencies = set(dependencies)
        if summary.is_database_extension:
            if requested_kind not in {
                ResourceKind.EXTERNAL_THERMODYNAMIC_DATABASE,
                ResourceKind.DATABASE_EXTENSION,
            }:
                raise DatabaseRegistryError(
                    "parsed database extension does not match the requested resource kind")
            imported_kind = ResourceKind.DATABASE_EXTENSION
            test_status = TestStatus.NOT_APPLICABLE_REQUIRES_BASE
            requires_database_family = summary.requires_database_family
            manifest_dependencies.add(summary.requires_database_filename)
            warnings.add(
                "extension compatibility requires an explicit administrator-reviewed INCLUDE$ "
                "test with the declared base; no files were concatenated"
            )
        else:
            if requested_kind == ResourceKind.DATABASE_EXTENSION:
                raise DatabaseRegistryError(
                    "requested database extension has no explicit base dependency")
            warnings.add("database load/syntax smoke test has not yet run")
        manifest = ResourceManifest(
            resource_id=resource_id,
            installation_id=installation_id,
            resource_kind=imported_kind,
            display_name=display_name,
            provider=provider,
            official_source_url=official_source_url,
            discovered_version=summary.detected_version,
            installed_version=installed_version,
            archive_filename=source_path.name,
            source_sha256=source_sha256,
            database_sha256=summary.source_sha256,
            content_sha256=summary.source_sha256,
            install_path=str(installed_database.resolve()),
            installed_at=utc_now(),
            verified_at=utc_now(),
            citation=citation or Citation(),
            rights_notice=str(rights_notice).strip(),
            licence_notice=str(licence_notice).strip(),
            redistribution_state=redistribution_state,
            temperature_range=temperature_range or TemperatureRange(),
            database_family=database_family or summary.detected_family,
            dependencies=tuple(sorted(manifest_dependencies)),
            conflicts=tuple(sorted(set(conflicts))),
            requires_database_family=requires_database_family,
            domain_notes=tuple(sorted(notes)),
            supported_summary=summary.supported_summary,
            compatibility_status=compatibility_status,
            test_status=test_status,
            standalone_test_status=summary.standalone_test_status,
            warnings=tuple(sorted(warnings)),
        )
        atomic_write_json(install_dir / "resource_manifest.json", manifest.to_dict(),
                          create_only=True)
        self.store.register(manifest)
        return manifest
