"""Deterministic, offline knowledge packs derived from the active resource catalog."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from .catalog import CatalogError, CatalogStore, atomic_write_bytes
from .models import (
    KNOWLEDGE_CATALOG_PROJECTION_SCHEMA,
    KNOWLEDGE_CATALOG_PROJECTION_VERSION,
    RESOURCE_MANIFEST_VERSION,
    Citation,
    RedistributionState,
    ResourceContractError,
    ResourceKind,
    ResourceManifest,
    RollbackState,
    TestStatus,
    canonical_hash,
    make_installation_id,
    sha256_bytes,
)

KNOWLEDGE_PACK_SCHEMA = "wpi.virtual-lab.scientific-knowledge-pack"
KNOWLEDGE_PACK_VERSION = 2
KNOWLEDGE_PACK_RESOURCE_ID = "wpi.phreeqc.knowledge-pack"
KNOWLEDGE_CATALOG_PROJECTION = (
    f"{KNOWLEDGE_CATALOG_PROJECTION_SCHEMA}/v{KNOWLEDGE_CATALOG_PROJECTION_VERSION}"
)
MAX_ACTIVE_KNOWLEDGE_BYTES = 2 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,126}[a-z0-9]$")
DEFAULT_PROJECT_POLICIES = (
    "AI may explain only deterministic scientific results and versioned resource facts; it may "
    "not calculate PHREEQC chemistry, author reviewed input, invent measurements, or weaken "
    "scientific warnings.",
    "A scientific resource candidate is installed side by side and requires an exact proposal, "
    "complete tests, and explicit human or administrator confirmation before promotion.",
    "Simulation output is neither a measurement nor experimental validation.",
    "Thermodynamic databases and extensions must not be concatenated or switched silently.",
)


class KnowledgePackError(ValueError):
    """A knowledge fact or pack is malformed or no longer matches its digest."""


@dataclass(frozen=True)
class ActiveKnowledgeContext:
    """Validated AI-facing facts for the exact active scientific-resource catalog.

    Only deterministic active-runtime and active-database facts are exposed. Documentation,
    project-policy, and model-inference statements remain distinguishable inside the pack but
    are deliberately not promoted into current installation facts.
    """

    catalog_hash: str
    catalog_generation: int
    pack_hash: str
    facts: tuple["KnowledgeFact", ...]
    catalog_projection: str = KNOWLEDGE_CATALOG_PROJECTION

    def to_provenance_dict(self) -> dict[str, Any]:
        return {
            "available": True,
            "catalog_hash": self.catalog_hash,
            "catalog_generation": self.catalog_generation,
            "catalog_projection": self.catalog_projection,
            "pack_hash": self.pack_hash,
            "facts": [
                {
                    "fact_id": fact.fact_id,
                    "fact_type": fact.fact_type.value,
                    "source_installation_ids": list(fact.source_installation_ids),
                    "source_artifacts": [
                        item.to_dict() for item in fact.source_artifacts
                    ],
                    "citation": fact.citation.to_dict(),
                }
                for fact in self.facts
            ],
        }

    def to_ai_dict(self) -> dict[str, Any]:
        document = self.to_provenance_dict()
        for target, fact in zip(document["facts"], self.facts, strict=True):
            target["statement"] = fact.statement
            target["warnings"] = list(fact.warnings)
        return document


class KnowledgeFactType(str, Enum):
    ACTIVE_RUNTIME_FACT = "active_runtime_fact"
    ACTIVE_DATABASE_FACT = "active_database_fact"
    DOCUMENTATION_STATEMENT = "documentation_statement"
    PROJECT_POLICY = "project_policy"
    MODEL_INFERENCE = "model_inference"


@dataclass(frozen=True, order=True)
class KnowledgeArtifactProvenance:
    """Exact identity of a versioned local artifact supporting a knowledge fact."""

    artifact_id: str
    artifact_version: str
    repository_path: str
    sha256: str

    def __post_init__(self) -> None:
        if not _ARTIFACT_ID_RE.fullmatch(self.artifact_id):
            raise KnowledgePackError("knowledge artifact_id is invalid")
        if not self.artifact_version.strip():
            raise KnowledgePackError("knowledge artifact_version is required")
        path = Path(self.repository_path)
        if not self.repository_path or path.is_absolute() or "\\" in self.repository_path \
                or any(part in {"", ".", ".."} for part in path.parts):
            raise KnowledgePackError(
                "knowledge artifact repository_path must be a safe relative path")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise KnowledgePackError("knowledge artifact sha256 is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_version": self.artifact_version,
            "repository_path": self.repository_path,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "KnowledgeArtifactProvenance":
        allowed = {"artifact_id", "artifact_version", "repository_path", "sha256"}
        if not isinstance(document, Mapping):
            raise KnowledgePackError("knowledge artifact provenance must be an object")
        unknown = set(document) - allowed
        if unknown:
            raise KnowledgePackError(
                f"knowledge artifact provenance contains unsupported fields: {sorted(unknown)}")
        missing = allowed - set(document)
        if missing:
            raise KnowledgePackError(
                f"knowledge artifact provenance is missing fields: {sorted(missing)}")
        return cls(**dict(document))


@dataclass(frozen=True)
class KnowledgeFact:
    fact_id: str
    fact_type: KnowledgeFactType
    statement: str
    source_installation_ids: tuple[str, ...] = ()
    source_artifacts: tuple[KnowledgeArtifactProvenance, ...] = ()
    citation: Citation = Citation()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.fact_id.startswith("fact-") or len(self.fact_id) != 29:
            raise KnowledgePackError("fact_id must be a deterministic fact digest")
        if not self.statement.strip():
            raise KnowledgePackError("knowledge fact statement is required")
        for label, values in (("source_installation_ids", self.source_installation_ids),
                              ("warnings", self.warnings)):
            if not isinstance(values, tuple) or any(not isinstance(item, str) for item in values):
                raise KnowledgePackError(f"knowledge fact {label} must be a tuple of strings")
            if values != tuple(sorted(set(values))):
                raise KnowledgePackError(f"knowledge fact {label} must be sorted and unique")
        if not isinstance(self.source_artifacts, tuple) or any(
                not isinstance(item, KnowledgeArtifactProvenance)
                for item in self.source_artifacts):
            raise KnowledgePackError(
                "knowledge fact source_artifacts must be a tuple of artifact provenance")
        if self.source_artifacts != tuple(sorted(set(self.source_artifacts))):
            raise KnowledgePackError(
                "knowledge fact source_artifacts must be sorted and unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "fact_type": self.fact_type.value,
            "statement": self.statement,
            "source_installation_ids": list(self.source_installation_ids),
            "source_artifacts": [item.to_dict() for item in self.source_artifacts],
            "citation": self.citation.to_dict(),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "KnowledgeFact":
        allowed = {
            "fact_id", "fact_type", "statement", "source_installation_ids", "citation",
            "source_artifacts", "warnings",
        }
        if not isinstance(document, Mapping):
            raise KnowledgePackError("knowledge fact must be an object")
        unknown = set(document) - allowed
        if unknown:
            raise KnowledgePackError(f"knowledge fact contains unsupported fields: {sorted(unknown)}")
        data = dict(document)
        for required in ("fact_id", "fact_type", "statement"):
            if required not in data:
                raise KnowledgePackError(f"knowledge fact is missing {required}")
        try:
            data["fact_type"] = KnowledgeFactType(data["fact_type"])
        except (TypeError, ValueError) as exc:
            raise KnowledgePackError("unsupported knowledge fact type") from exc
        for label in ("source_installation_ids", "warnings"):
            value = data.get(label, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise KnowledgePackError(f"knowledge fact {label} must be a list of strings")
            data[label] = tuple(value)
        artifacts = data.get("source_artifacts", [])
        if not isinstance(artifacts, list):
            raise KnowledgePackError("knowledge fact source_artifacts must be a list")
        data["source_artifacts"] = tuple(
            KnowledgeArtifactProvenance.from_dict(item) for item in artifacts)
        try:
            data["citation"] = Citation.from_dict(data.get("citation"))
        except ResourceContractError as exc:
            raise KnowledgePackError(str(exc)) from exc
        return cls(**data)


def make_fact(fact_type: KnowledgeFactType, statement: str, *,
              source_installation_ids: Iterable[str] = (), citation: Citation | None = None,
              source_artifacts: Iterable[KnowledgeArtifactProvenance] = (),
              warnings: Iterable[str] = ()) -> KnowledgeFact:
    sources = tuple(sorted(set(str(item) for item in source_installation_ids)))
    artifact_values = tuple(source_artifacts)
    if any(not isinstance(item, KnowledgeArtifactProvenance) for item in artifact_values):
        raise KnowledgePackError(
            "source_artifacts must contain KnowledgeArtifactProvenance values")
    artifacts = tuple(sorted(set(artifact_values)))
    warning_values = tuple(sorted(set(str(item) for item in warnings)))
    cite = citation or Citation()
    payload = {
        "fact_type": KnowledgeFactType(fact_type).value,
        "statement": str(statement),
        "source_installation_ids": list(sources),
        "source_artifacts": [item.to_dict() for item in artifacts],
        "citation": cite.to_dict(),
        "warnings": list(warning_values),
    }
    return KnowledgeFact(
        fact_id="fact-" + canonical_hash(payload)[:24],
        fact_type=KnowledgeFactType(fact_type),
        statement=str(statement),
        source_installation_ids=sources,
        source_artifacts=artifacts,
        citation=cite,
        warnings=warning_values,
    )


@dataclass(frozen=True)
class KnowledgePack:
    catalog_hash: str
    facts: tuple[KnowledgeFact, ...]
    pack_hash: str
    catalog_projection: str = KNOWLEDGE_CATALOG_PROJECTION
    resource_manifest_version: int = RESOURCE_MANIFEST_VERSION
    schema: str = KNOWLEDGE_PACK_SCHEMA
    version: int = KNOWLEDGE_PACK_VERSION

    def __post_init__(self) -> None:
        if self.schema != KNOWLEDGE_PACK_SCHEMA or self.version != KNOWLEDGE_PACK_VERSION:
            raise KnowledgePackError("unsupported knowledge-pack schema/version")
        if self.catalog_projection != KNOWLEDGE_CATALOG_PROJECTION:
            raise KnowledgePackError("unsupported knowledge catalog projection")
        for label, value in (("catalog_hash", self.catalog_hash), ("pack_hash", self.pack_hash)):
            if not isinstance(value, str) or len(value) != 64 \
                    or any(char not in "0123456789abcdef" for char in value):
                raise KnowledgePackError(f"{label} must be a lower-case SHA-256")
        ids = [item.fact_id for item in self.facts]
        if len(ids) != len(set(ids)) or tuple(ids) != tuple(sorted(ids)):
            raise KnowledgePackError("knowledge facts must have unique, sorted identities")
        if self.calculate_hash() != self.pack_hash:
            raise KnowledgePackError("knowledge pack hash does not match its contents")

    def _payload_without_hash(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "resource_manifest_version": self.resource_manifest_version,
            "catalog_projection": self.catalog_projection,
            "catalog_hash": self.catalog_hash,
            "facts": [item.to_dict() for item in self.facts],
        }

    def calculate_hash(self) -> str:
        return canonical_hash(self._payload_without_hash())

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload_without_hash(), "pack_hash": self.pack_hash}

    def query(self, fact_type: KnowledgeFactType) -> tuple[KnowledgeFact, ...]:
        selected = KnowledgeFactType(fact_type)
        return tuple(item for item in self.facts if item.fact_type == selected)

    def provenance(self, fact_id: str) -> dict[str, Any]:
        fact = next((item for item in self.facts if item.fact_id == fact_id), None)
        if fact is None:
            raise KnowledgePackError(f"unknown fact: {fact_id}")
        return {
            "pack_hash": self.pack_hash,
            "catalog_hash": self.catalog_hash,
            "catalog_projection": self.catalog_projection,
            "fact_id": fact.fact_id,
            "fact_type": fact.fact_type.value,
            "source_installation_ids": list(fact.source_installation_ids),
            "source_artifacts": [item.to_dict() for item in fact.source_artifacts],
            "citation": fact.citation.to_dict(),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "KnowledgePack":
        allowed = {
            "schema", "version", "resource_manifest_version", "catalog_projection",
            "catalog_hash", "facts", "pack_hash",
        }
        if not isinstance(document, Mapping):
            raise KnowledgePackError("knowledge pack must be an object")
        unknown = set(document) - allowed
        if unknown:
            raise KnowledgePackError(f"knowledge pack contains unsupported fields: {sorted(unknown)}")
        data = dict(document)
        for required in (
            "schema", "version", "resource_manifest_version", "catalog_projection",
            "catalog_hash", "facts", "pack_hash",
        ):
            if required not in data:
                raise KnowledgePackError(f"knowledge pack is missing {required}")
        if not isinstance(data["facts"], list):
            raise KnowledgePackError("knowledge pack facts must be a list")
        data["facts"] = tuple(KnowledgeFact.from_dict(item) for item in data["facts"])
        return cls(**data)


def _resource_facts(manifest: ResourceManifest) -> tuple[KnowledgeFact, ...]:
    source = (manifest.installation_id,)
    if manifest.resource_kind == ResourceKind.PHREEQC_RUNTIME:
        identity = manifest.executable_sha256 or manifest.content_sha256
        architecture = ", ".join(manifest.architectures) or "not recorded"
        targets = ", ".join(manifest.operating_system_targets) or "not recorded"
        return (make_fact(
            KnowledgeFactType.ACTIVE_RUNTIME_FACT,
            f"Active PHREEQC runtime {manifest.display_name} has installed version "
            f"{manifest.installed_version or 'unspecified'} and executable/content SHA-256 "
            f"{identity}.",
            source_installation_ids=source,
            citation=manifest.citation,
            warnings=manifest.warnings,
        ), make_fact(
            KnowledgeFactType.ACTIVE_RUNTIME_FACT,
            f"The active runtime provider is {manifest.provider}; its official source is "
            f"{manifest.official_source_url or 'not recorded'}, archive filename is "
            f"{manifest.archive_filename or 'not recorded'}, source SHA-256 is "
            f"{manifest.source_sha256 or 'not recorded'}, architectures are {architecture}, and "
            f"operating-system targets are {targets}. Redistribution state is "
            f"{manifest.redistribution_state.value}; test status is {manifest.test_status.value}.",
            source_installation_ids=source,
            citation=manifest.citation,
            warnings=manifest.warnings,
        ),)
    if manifest.resource_kind in {
        ResourceKind.PHREEQC_OFFICIAL_DATABASE,
        ResourceKind.EXTERNAL_THERMODYNAMIC_DATABASE,
        ResourceKind.DATABASE_EXTENSION,
    }:
        summary = manifest.supported_summary
        identity = manifest.database_sha256 or manifest.content_sha256
        identity_fact = make_fact(
            KnowledgeFactType.ACTIVE_DATABASE_FACT,
            f"Active thermodynamic database {manifest.display_name} has installed version "
            f"{manifest.installed_version or 'unspecified'}, family "
            f"{manifest.database_family or 'unknown'}, and database/content SHA-256 {identity}.",
            source_installation_ids=source,
            citation=manifest.citation,
            warnings=manifest.warnings,
        )
        contents_fact = make_fact(
            KnowledgeFactType.ACTIVE_DATABASE_FACT,
            f"The active database registry parsed {summary.master_species_count} master species, "
            f"{summary.solution_species_count} solution species, and {summary.phase_count} phases. "
            "Availability is database-specific and is not experimental validation.",
            source_installation_ids=source,
            citation=manifest.citation,
            warnings=tuple(sorted(set((*manifest.warnings, *manifest.domain_notes)))),
        )
        names_fact = make_fact(
            KnowledgeFactType.ACTIVE_DATABASE_FACT,
            "Parsed availability for the active database: master species ["
            + ", ".join(summary.master_species)
            + "]; solution species ["
            + ", ".join(summary.solution_species)
            + "]; phases ["
            + ", ".join(summary.phases)
            + "]. Parsed presence is not evidence that a species or phase is appropriate for a "
            "particular experiment.",
            source_installation_ids=source,
            citation=manifest.citation,
            warnings=tuple(sorted(set((*manifest.warnings, *manifest.domain_notes)))),
        )
        minimum = manifest.temperature_range.minimum_celsius
        maximum = manifest.temperature_range.maximum_celsius
        temperature = (
            f"{minimum} to {maximum} °C" if minimum is not None and maximum is not None
            else manifest.temperature_range.notes or "not established by the registry"
        )
        limitation_fact = make_fact(
            KnowledgeFactType.ACTIVE_DATABASE_FACT,
            f"Active database compatibility status is {manifest.compatibility_status.value}; "
            f"test status is {manifest.test_status.value}; applicable temperature information is "
            f"{temperature}. Domain limitations: "
            f"{'; '.join(manifest.domain_notes) or 'none recorded'}. Dependencies: "
            f"{', '.join(manifest.dependencies) or 'none'}; conflicts: "
            f"{', '.join(manifest.conflicts) or 'none'}. This is compatibility evidence, not "
            "experimental validation.",
            source_installation_ids=source,
            citation=manifest.citation,
            warnings=tuple(sorted(set((*manifest.warnings, *manifest.domain_notes)))),
        )
        documentation_fact = make_fact(
            KnowledgeFactType.DOCUMENTATION_STATEMENT,
            f"The active database manifest cites {manifest.citation.title or 'its reviewed source'} "
            f"and records redistribution state {manifest.redistribution_state.value}.",
            source_installation_ids=source,
            citation=manifest.citation,
            warnings=manifest.warnings,
        )
        return identity_fact, contents_fact, names_fact, limitation_fact, documentation_fact
    return ()


def build_knowledge_pack(
    store: CatalogStore,
    *,
    documentation_facts: Iterable[KnowledgeFact] = (),
    project_policies: Iterable[str] = (),
    model_inferences: Iterable[str] = (),
) -> KnowledgePack:
    """Rebuild deterministic bytes from the non-self-referential catalog projection."""
    state = store.load()
    facts: list[KnowledgeFact] = []
    for _resource_id, installation_id in sorted(state.active.items()):
        manifest = store.get_installation(installation_id, state=state)
        if manifest.resource_kind == ResourceKind.DOCUMENTATION_KNOWLEDGE_PACK:
            continue
        facts.extend(_resource_facts(manifest))
    for fact in documentation_facts:
        if not isinstance(fact, KnowledgeFact) \
                or fact.fact_type != KnowledgeFactType.DOCUMENTATION_STATEMENT:
            raise KnowledgePackError(
                "documentation_facts must be explicit documentation_statement facts")
        facts.append(fact)
    projection = state.knowledge_projection_dict()
    for record in projection["activation_history"]:
        facts.append(make_fact(
            KnowledgeFactType.DOCUMENTATION_STATEMENT,
            f"Knowledge catalog projection sequence {record['projection_sequence']} records "
            f"action {record['action']} for resource {record['resource_id']}, installation "
            f"{record['installation_id']}, previous installation "
            f"{record['previous_installation_id'] or 'none'}, and proposal "
            f"{record['proposal_id'] or 'bootstrap/no proposal'}.",
            source_installation_ids=(record["installation_id"],),
        ))
    for statement in (*DEFAULT_PROJECT_POLICIES, *tuple(project_policies)):
        facts.append(make_fact(KnowledgeFactType.PROJECT_POLICY, str(statement)))
    for statement in model_inferences:
        facts.append(make_fact(KnowledgeFactType.MODEL_INFERENCE, str(statement)))
    ordered = tuple(sorted({item.fact_id: item for item in facts}.values(),
                           key=lambda item: item.fact_id))
    payload = {
        "schema": KNOWLEDGE_PACK_SCHEMA,
        "version": KNOWLEDGE_PACK_VERSION,
        "resource_manifest_version": RESOURCE_MANIFEST_VERSION,
        "catalog_projection": KNOWLEDGE_CATALOG_PROJECTION,
        "catalog_hash": state.knowledge_projection_hash,
        "facts": [item.to_dict() for item in ordered],
    }
    return KnowledgePack(
        catalog_hash=state.knowledge_projection_hash,
        facts=ordered,
        pack_hash=canonical_hash(payload),
    )


def knowledge_pack_bytes(pack: KnowledgePack) -> bytes:
    """Return the one canonical on-disk encoding used for a knowledge pack."""
    if not isinstance(pack, KnowledgePack):
        raise KnowledgePackError("serialization requires a validated KnowledgePack")
    try:
        return (json.dumps(
            pack.to_dict(), sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False,
        ) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise KnowledgePackError(f"knowledge pack is not strict JSON: {exc}") from exc


def knowledge_pack_installation_id(pack: KnowledgePack) -> str:
    """Return the installation identity of the canonical serialized pack bytes."""
    return make_installation_id(
        KNOWLEDGE_PACK_RESOURCE_ID,
        f"v{pack.version}",
        sha256_bytes(knowledge_pack_bytes(pack)),
    )


def write_knowledge_pack(pack: KnowledgePack, output_dir: str | Path) -> Path:
    payload = knowledge_pack_bytes(pack)
    destination = Path(output_dir).expanduser().absolute()
    if destination.exists() and destination.is_symlink():
        raise KnowledgePackError("knowledge output directory must not be a symlink")
    destination.mkdir(parents=True, exist_ok=True)
    versioned = destination / f"knowledge-pack-v{pack.version}-{pack.pack_hash}.json"
    if versioned.exists():
        try:
            existing_raw = versioned.read_bytes()
            existing = KnowledgePack.from_dict(json.loads(existing_raw.decode("utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KnowledgePackError) as exc:
            raise KnowledgePackError("existing versioned knowledge pack is invalid") from exc
        if existing.to_dict() != pack.to_dict() or existing_raw != payload:
            raise KnowledgePackError("knowledge pack hash already identifies different contents")
    else:
        atomic_write_bytes(versioned, payload, create_only=True)
    atomic_write_bytes(destination / "knowledge_pack.json", payload)
    return versioned


def _configured_resource_root(root: str | Path | None = None) -> Path:
    raw: str | Path | None = root
    if raw is None:
        raw = os.getenv("WPI_RESOURCE_ROOT", "").strip()
    if not raw:
        raise KnowledgePackError("scientific resource root is not configured")
    candidate = Path(raw).expanduser()
    if any(part == ".." for part in candidate.parts):
        raise KnowledgePackError("scientific resource root contains an unsafe parent traversal")
    candidate = candidate.absolute()
    if candidate.exists() and candidate.is_symlink():
        raise KnowledgePackError("scientific resource root must not be a symlink")
    return candidate


def load_active_knowledge_context(
    root: str | Path | None = None,
) -> ActiveKnowledgeContext:
    """Load only a knowledge pack that exactly matches the active catalog.

    The loader performs no network access and never creates a resource root. A missing,
    malformed, oversized, symlink-backed, or stale pack is rejected rather than being exposed
    to an AI provider as a current scientific fact.
    """
    resource_root = _configured_resource_root(root)
    if not resource_root.is_dir():
        raise KnowledgePackError("scientific resource root is unavailable")
    try:
        store = CatalogStore(resource_root)
        state = store.load()
    except CatalogError as exc:
        raise KnowledgePackError("scientific resource catalog is unavailable") from exc
    if not state.active:
        raise KnowledgePackError("scientific resource catalog has no active installations")

    knowledge_installation_id = state.active.get(KNOWLEDGE_PACK_RESOURCE_ID)
    if not knowledge_installation_id:
        raise KnowledgePackError(
            "scientific resource catalog has no active knowledge-pack manifest")
    try:
        knowledge_manifest = store.get_installation(
            knowledge_installation_id, state=state)
    except CatalogError as exc:
        raise KnowledgePackError("active knowledge-pack manifest is unavailable") from exc
    if knowledge_manifest.resource_kind != ResourceKind.DOCUMENTATION_KNOWLEDGE_PACK:
        raise KnowledgePackError("active knowledge-pack manifest has the wrong resource kind")

    path = store.knowledge_dir / "knowledge_pack.json"
    if path.is_symlink() or not path.is_file():
        raise KnowledgePackError("active scientific knowledge pack is unavailable")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise KnowledgePackError("active scientific knowledge pack is unavailable") from exc
    if size <= 0 or size > MAX_ACTIVE_KNOWLEDGE_BYTES:
        raise KnowledgePackError("active scientific knowledge pack has an invalid size")
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
        pack = KnowledgePack.from_dict(document)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KnowledgePackError) as exc:
        raise KnowledgePackError("active scientific knowledge pack is invalid") from exc
    if knowledge_manifest.content_sha256 != sha256_bytes(raw) \
            or knowledge_manifest.source_sha256 != pack.pack_hash:
        raise KnowledgePackError(
            "active scientific knowledge pack does not match its active manifest")
    try:
        configured_manifest_path = Path(knowledge_manifest.install_path)
        if configured_manifest_path.is_symlink() or not configured_manifest_path.is_file():
            raise KnowledgePackError(
                "active knowledge-pack manifest path must be a regular non-symlink file")
        manifest_path = configured_manifest_path.resolve(strict=True)
        manifest_raw = manifest_path.read_bytes()
    except OSError as exc:
        raise KnowledgePackError(
            "active knowledge-pack manifest path is unavailable") from exc
    if sha256_bytes(manifest_raw) != knowledge_manifest.content_sha256:
        raise KnowledgePackError(
            "active knowledge-pack manifest path does not match its content identity")
    if pack.catalog_hash != state.knowledge_projection_hash:
        raise KnowledgePackError("active scientific knowledge pack is stale")

    active_installations = set(state.active.values())
    facts = tuple(
        fact for fact in pack.facts if fact.fact_type != KnowledgeFactType.MODEL_INFERENCE)
    if not facts:
        raise KnowledgePackError("active scientific knowledge pack has no resource facts")
    for fact in facts:
        if fact.fact_type in {
            KnowledgeFactType.ACTIVE_RUNTIME_FACT,
            KnowledgeFactType.ACTIVE_DATABASE_FACT,
        } and (not fact.source_installation_ids
               or not set(fact.source_installation_ids).issubset(active_installations)):
            raise KnowledgePackError(
                "active scientific knowledge fact does not reference an active installation")

    active_manifests = {
        installation_id: store.get_installation(installation_id, state=state)
        for installation_id in active_installations
    }
    observed_kinds = {manifest.resource_kind for manifest in active_manifests.values()}
    database_kinds = {
        ResourceKind.PHREEQC_OFFICIAL_DATABASE,
        ResourceKind.EXTERNAL_THERMODYNAMIC_DATABASE,
        ResourceKind.DATABASE_EXTENSION,
    }
    if ResourceKind.PHREEQC_RUNTIME not in observed_kinds \
            or not observed_kinds.intersection(database_kinds):
        raise KnowledgePackError(
            "active catalog does not contain both a PHREEQC runtime and database")

    return ActiveKnowledgeContext(
        catalog_hash=state.knowledge_projection_hash,
        catalog_generation=state.generation,
        pack_hash=pack.pack_hash,
        facts=facts,
    )


def scientific_resource_facts(root: str | Path | None = None) -> dict[str, Any]:
    """Return a bounded AI/tool payload, or a stable fail-closed unavailable result."""
    try:
        return load_active_knowledge_context(root).to_ai_dict()
    except KnowledgePackError as exc:
        message = str(exc)
        if "not configured" in message:
            reason = "resource_root_not_configured"
        elif "stale" in message:
            reason = "knowledge_pack_stale"
        elif "catalog" in message:
            reason = "resource_catalog_unavailable"
        else:
            reason = "knowledge_pack_unavailable"
        return {
            "available": False,
            "reason": reason,
            "instruction": (
                "Do not claim a current PHREEQC runtime version, database identity, "
                "database contents, or resource hash from model memory."
            ),
        }


def knowledge_pack_manifest(
    pack: KnowledgePack,
    path: str | Path,
    *,
    installed_at: str = "",
    verified_at: str = "",
) -> ResourceManifest:
    """Create the managed self-manifest for already-written deterministic pack bytes."""
    configured_location = Path(path).expanduser()
    if configured_location.is_symlink() or not configured_location.is_file():
        raise KnowledgePackError("knowledge pack path must be a regular non-symlink file")
    location = configured_location.resolve(strict=True)
    try:
        raw = location.read_bytes()
        written_pack = KnowledgePack.from_dict(json.loads(raw.decode("utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KnowledgePackError) as exc:
        raise KnowledgePackError("knowledge pack artifact is invalid") from exc
    if written_pack.to_dict() != pack.to_dict() or raw != knowledge_pack_bytes(pack):
        raise KnowledgePackError("knowledge pack artifact differs from the supplied pack")
    content_sha256 = sha256_bytes(raw)
    installation_id = knowledge_pack_installation_id(pack)
    return ResourceManifest(
        resource_id=KNOWLEDGE_PACK_RESOURCE_ID,
        installation_id=installation_id,
        resource_kind=ResourceKind.DOCUMENTATION_KNOWLEDGE_PACK,
        display_name="WPI PHREEQC scientific knowledge pack",
        provider="WPI Virtual LAB deterministic builder",
        discovered_version=f"v{pack.version}",
        installed_version=f"v{pack.version}",
        archive_filename=location.name,
        source_sha256=pack.pack_hash,
        content_sha256=content_sha256,
        install_path=str(location),
        installed_at=installed_at,
        verified_at=verified_at,
        citation=Citation(
            title="WPI Virtual LAB versioned PHREEQC scientific knowledge pack",
            authors=("WPI Virtual LAB",),
            source_location=(
                f"{location.name}; semantic pack SHA-256 {pack.pack_hash}; "
                f"catalog projection {pack.catalog_projection}"),
            extraction_confidence=1.0,
        ),
        rights_notice="Contains only project-authored structured facts and citations.",
        redistribution_state=RedistributionState.PERMITTED,
        test_status=TestStatus.PASSED,
        warnings=(
            "Catalog identity uses the declared projection that excludes only knowledge-pack "
            "self-manifests to avoid a content-hash cycle.",
        ),
        rollback_state=RollbackState.CANDIDATE,
    )
