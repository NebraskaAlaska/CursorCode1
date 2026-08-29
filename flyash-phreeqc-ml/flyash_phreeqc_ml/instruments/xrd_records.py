"""Durable coordinators for measured-XRD patterns, references, and advisory runs.

This module performs no peak detection or phase matching.  It validates persistence boundaries,
then delegates the scientific comparison to :mod:`xrd_advisory`.  Exact artifact revision and hash
identities travel with every saved run so reopening cannot silently bind newer inputs.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, fields, replace
from typing import Any, Iterable

from .. import phase3_artifacts
from .. import workspace_store as ws
from . import xrd_advisory as xrd


XRD_MACHINE_ID = "xrd_advisory"
XRD_RUN_OUTPUT_TYPE = "tentative_advisory_possible_match"
XRD_RUN_EPISTEMIC_TYPE = "advisory_interpretation"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_GENERATED_CLAIM = re.compile(
    r"\b(?:identified|confirmed\s+phase|validated\s+phase|quantified\s+phase)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LoadedMeasuredPattern:
    artifact: ws.ArtifactRecord
    pattern: xrd.MeasuredXrdPattern


@dataclass(frozen=True)
class LoadedXrdReference:
    artifact: ws.ArtifactRecord
    reference: xrd.ExternalXrdReference


@dataclass(frozen=True)
class SavedXrdAdvisoryRun:
    run: ws.RunRecord
    pattern_artifact: ws.ArtifactRecord
    reference_artifacts: tuple[ws.ArtifactRecord, ...]
    match_payload: dict


@dataclass(frozen=True)
class LoadedXrdAdvisoryRun:
    run: ws.RunRecord
    pattern: LoadedMeasuredPattern
    references: tuple[LoadedXrdReference, ...]
    match_payload: dict
    is_stale: bool
    stale_reasons: tuple[str, ...]


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ws.MalformedRecordError(f"{field_name} is required")
    return text


def _validate_context(
    store: ws.WorkspaceStore, *, project_id: str, material_id: str,
) -> None:
    store.get_project(project_id)
    material = store.get_material(material_id)
    if material.project_id != project_id:
        raise ws.WorkspaceStoreError("material does not belong to the selected project")


def _validate_artifact_integrity(record: ws.ArtifactRecord) -> None:
    if ws.identity_hash(record.input_snapshot) != record.input_hash:
        raise ws.MalformedRecordError("artifact input hash does not match its stored snapshot")
    if ws.identity_hash(record.payload) != record.payload_hash:
        raise ws.MalformedRecordError("artifact payload hash does not match its stored payload")


def _validate_artifact_context(
    record: ws.ArtifactRecord, *, project_id: str, material_id: str, record_type: str,
) -> None:
    if record.project_id != project_id or record.material_id != material_id:
        raise ws.WorkspaceStoreError("XRD artifact belongs to another project/material context")
    if record.record_type != record_type:
        raise ws.MalformedRecordError(
            f"expected {record_type!r} artifact, found {record.record_type!r}")


def _domain_record(cls, payload: dict):
    if not isinstance(payload, dict):
        raise ws.MalformedRecordError("XRD artifact payload must be an object")
    known = {field.name for field in fields(cls)}
    try:
        value = cls(**{key: item for key, item in payload.items() if key in known})
    except (TypeError, ValueError) as exc:
        raise ws.MalformedRecordError(f"cannot reconstruct XRD domain record: {exc}") from exc
    if value.schema_version != xrd.XRD_RECORD_SCHEMA_VERSION:
        raise ws.UnsupportedSchemaError(
            "XRD domain record schema differs from the supported version")
    return value


def _require_sha256(value: Any, field_name: str) -> str:
    digest = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ws.MalformedRecordError(f"{field_name} must be an exact SHA-256 digest")
    return digest


def _pattern_source_identity(pattern: xrd.MeasuredXrdPattern) -> dict:
    return {
        "source_kind": "user_supplied_measured_xrd_csv",
        "pattern_id": _required_text(pattern.pattern_id, "pattern_id"),
        "sample_id": _required_text(pattern.sample_id, "sample_id"),
        "source_filename": _required_text(pattern.source_filename, "source_filename"),
        "source_sha256": _require_sha256(pattern.source_sha256, "source_sha256"),
        "data_format": pattern.data_format,
    }


def _reference_source_identity(reference: xrd.ExternalXrdReference) -> dict:
    return {
        "source_kind": "user_supplied_xrd_reference",
        "reference_id": _required_text(reference.reference_id, "reference_id"),
        "source_filename": _required_text(reference.source_filename, "source_filename"),
        "source_sha256": _require_sha256(reference.source_sha256, "source_sha256"),
        "data_format": reference.data_format,
        "source_name": _required_text(reference.source_name, "source_name"),
        "source_record_id": reference.source_record_id,
        "license_status": str(reference.license_status or xrd.LICENSE_UNKNOWN),
        "redistribution_permission_status": str(
            reference.redistribution_permission_status or xrd.REDISTRIBUTION_UNKNOWN),
    }


def _source_snapshot(identity: dict, *, workflow: str) -> dict:
    return {"workflow": workflow, "source_identity": dict(identity)}


def _stable_artifact_id(contract: str, identity: dict) -> str:
    """Return a retry-stable artifact ID for one exact persistence operation."""
    digest = ws.identity_hash({"contract": contract, "identity": identity})
    return f"{ws.ARTIFACT_PREFIX}{digest[:32]}"


def _stable_run_id(contract: str, identity: dict) -> str:
    """Return a retry-stable run ID for one exact persistence operation."""
    digest = ws.identity_hash({"contract": contract, "identity": identity})
    return f"{ws.RUN_PREFIX}{digest[:32]}"


def _assert_exact_artifact(
    artifact: ws.ArtifactRecord,
    *,
    artifact_id: str,
    logical_id: str,
    project_id: str,
    material_id: str,
    record_type: str,
    revision: int,
    input_snapshot: dict,
    payload: dict,
    source_identity: dict,
    creator: str,
    reason: str,
    provenance: dict,
    previous_artifact_id: str | None,
) -> None:
    """Reject a deterministic-ID collision instead of adopting different state."""
    expected = {
        "artifact_id": artifact_id,
        "logical_id": logical_id,
        "project_id": project_id,
        "material_id": material_id,
        "record_type": record_type,
        "revision": revision,
        "input_snapshot": input_snapshot,
        "payload": payload,
        "source_identity": source_identity,
        "creator": creator,
        "reason": reason,
        "provenance": provenance,
        "previous_artifact_id": previous_artifact_id,
    }
    actual = {key: getattr(artifact, key) for key in expected}
    if actual != expected or artifact.related_run_id is not None:
        raise ws.DuplicateRecordError(
            "XRD persistence identity collides with a different artifact")


def _finalize_exact_artifact(
    store: ws.WorkspaceStore,
    artifact: ws.ArtifactRecord,
    *,
    reviewer: str,
    reason: str,
    status: str = "finalized",
) -> ws.ArtifactRecord:
    """Finalize a retry-stable draft, accepting only the exact completed state."""
    if artifact.status == "draft":
        try:
            artifact = store.update_artifact(
                artifact.artifact_id,
                expected_payload_hash=artifact.payload_hash,
                expected_input_hash=artifact.input_hash,
                expected_status="draft",
                status=status,
                reviewer=reviewer,
                reason=reason,
            )
        except ws.WorkspaceStoreError:
            # A concurrent writer may have completed the same deterministic
            # operation. Reopen it and validate the terminal metadata below.
            artifact = store.get_artifact(artifact.artifact_id)
    if (artifact.status != status or artifact.reviewer != reviewer
            or artifact.reason != reason or not artifact.reviewed_at):
        raise ws.DuplicateRecordError(
            "recoverable XRD artifact does not match the exact terminal state")
    return artifact


def save_measured_pattern(
    store: ws.WorkspaceStore,
    *,
    project_id: str,
    material_id: str,
    pattern: xrd.MeasuredXrdPattern,
    creator: str,
    reason: str,
) -> ws.ArtifactRecord:
    """Save and material-link an immutable measured-pattern artifact."""
    _validate_context(store, project_id=project_id, material_id=material_id)
    actor = _required_text(creator, "creator")
    why = _required_text(reason, "reason")
    if not isinstance(pattern, xrd.MeasuredXrdPattern):
        raise ws.MalformedRecordError("pattern must be a validated MeasuredXrdPattern")
    if pattern.project_id != project_id or pattern.material_id != material_id:
        raise ws.WorkspaceStoreError(
            "measured pattern project/material metadata does not match the selected context")
    identity = _pattern_source_identity(pattern)
    input_snapshot = _source_snapshot(identity, workflow="measured_xrd_import")
    payload = pattern.to_dict()
    provenance = {
        "record_authority": "instruments.xrd_advisory.MeasuredXrdPattern",
        "scientific_role": "measured signal; not phase identity",
        "persistence_contract": "xrd_measured_pattern_save_v1",
    }
    operation = {
        "project_id": project_id,
        "material_id": material_id,
        "record_type": ws.ARTIFACT_XRD_PATTERN,
        "input_snapshot": input_snapshot,
        "payload": payload,
        "source_identity": identity,
        "creator": actor,
        "reason": why,
        "provenance": provenance,
    }
    artifact_id = _stable_artifact_id("xrd_measured_pattern_save_v1", operation)
    try:
        artifact = store.get_artifact(artifact_id)
    except ws.RecordNotFoundError:
        try:
            artifact = store.create_artifact(
                project_id,
                material_id,
                ws.ARTIFACT_XRD_PATTERN,
                input_snapshot,
                status="draft",
                payload=payload,
                source_identity=identity,
                creator=actor,
                reason=why,
                provenance=provenance,
                artifact_id=artifact_id,
            )
        except ws.DuplicateRecordError:
            artifact = store.get_artifact(artifact_id)
    _assert_exact_artifact(
        artifact,
        artifact_id=artifact_id,
        logical_id=artifact_id,
        project_id=project_id,
        material_id=material_id,
        record_type=ws.ARTIFACT_XRD_PATTERN,
        revision=1,
        input_snapshot=input_snapshot,
        payload=payload,
        source_identity=identity,
        creator=actor,
        reason=why,
        provenance=provenance,
        previous_artifact_id=None,
    )
    finalized = _finalize_exact_artifact(
        store, artifact, reviewer=actor, reason=why)
    store.link_artifact_to_material(finalized.artifact_id)
    return finalized


def load_measured_pattern(
    store: ws.WorkspaceStore,
    artifact_id: str,
    *,
    project_id: str,
    material_id: str,
) -> LoadedMeasuredPattern:
    """Reload one exact measured pattern, failing closed on scope or identity drift."""
    _validate_context(store, project_id=project_id, material_id=material_id)
    artifact = store.get_artifact(artifact_id)
    _validate_artifact_context(
        artifact,
        project_id=project_id,
        material_id=material_id,
        record_type=ws.ARTIFACT_XRD_PATTERN,
    )
    _validate_artifact_integrity(artifact)
    pattern = _domain_record(xrd.MeasuredXrdPattern, artifact.payload)
    if pattern.project_id != project_id or pattern.material_id != material_id:
        raise ws.MalformedRecordError("measured pattern payload context differs from its envelope")
    identity = _pattern_source_identity(pattern)
    if identity != artifact.source_identity:
        raise ws.MalformedRecordError("measured pattern source identity differs from its envelope")
    return LoadedMeasuredPattern(artifact=artifact, pattern=pattern)


def revise_measured_pattern_peaks(
    store: ws.WorkspaceStore,
    artifact_id: str,
    *,
    project_id: str,
    material_id: str,
    peaks: list | tuple,
    provenance: dict | None,
    creator: str,
    reason: str,
) -> ws.ArtifactRecord:
    """Attach a reviewed user peak list as a new immutable measured-pattern revision."""
    actor = _required_text(creator, "creator")
    why = _required_text(reason, "reason")
    loaded = load_measured_pattern(
        store, artifact_id, project_id=project_id, material_id=material_id)
    if loaded.artifact.status != "finalized":
        raise ws.ImmutableRecordError(
            "peak-list revisions require a finalized measured-pattern artifact")
    revised_pattern = xrd.attach_user_peak_list(
        loaded.pattern, peaks, provenance=dict(provenance or {}))
    revision_provenance = dict(loaded.artifact.provenance)
    revision_provenance["peak_list_revision"] = {
        "previous_artifact_id": loaded.artifact.artifact_id,
        "previous_payload_hash": loaded.artifact.payload_hash,
        "selection_method": "user_supplied_peak_list",
        "reason": why,
    }
    payload = revised_pattern.to_dict()
    previous_hash = ws.identity_hash(loaded.artifact.to_dict())
    operation = {
        "previous_artifact_id": loaded.artifact.artifact_id,
        "previous_artifact_hash": previous_hash,
        "logical_id": loaded.artifact.logical_id,
        "project_id": project_id,
        "material_id": material_id,
        "record_type": ws.ARTIFACT_XRD_PATTERN,
        "revision": loaded.artifact.revision + 1,
        "status": "finalized",
        "input_snapshot": loaded.artifact.input_snapshot,
        "payload": payload,
        "source_identity": loaded.artifact.source_identity,
        "creator": actor,
        "reason": why,
        "provenance": revision_provenance,
    }
    artifact_id = _stable_artifact_id("xrd_measured_peak_revision_v1", operation)
    try:
        finalized = store.get_artifact(artifact_id)
    except ws.RecordNotFoundError:
        try:
            # A reviewed peak-list revision is terminal at creation. This avoids
            # exposing a half-transitioned draft head if the process stops after
            # the child write but before a second lifecycle write.
            finalized = store.create_artifact(
                project_id,
                material_id,
                ws.ARTIFACT_XRD_PATTERN,
                loaded.artifact.input_snapshot,
                status="finalized",
                payload=payload,
                source_identity=loaded.artifact.source_identity,
                creator=actor,
                reviewer=actor,
                reason=why,
                provenance=revision_provenance,
                revision=loaded.artifact.revision + 1,
                previous_artifact_id=loaded.artifact.artifact_id,
                artifact_id=artifact_id,
                logical_id=loaded.artifact.logical_id,
            )
        except ws.DuplicateRecordError:
            finalized = store.get_artifact(artifact_id)
    _assert_exact_artifact(
        finalized,
        artifact_id=artifact_id,
        logical_id=loaded.artifact.logical_id,
        project_id=project_id,
        material_id=material_id,
        record_type=ws.ARTIFACT_XRD_PATTERN,
        revision=loaded.artifact.revision + 1,
        input_snapshot=loaded.artifact.input_snapshot,
        payload=payload,
        source_identity=loaded.artifact.source_identity,
        creator=actor,
        reason=why,
        provenance=revision_provenance,
        previous_artifact_id=loaded.artifact.artifact_id,
    )
    if finalized.previous_artifact_hash != previous_hash:
        raise ws.DuplicateRecordError(
            "recoverable XRD peak revision has a different predecessor identity")
    finalized = _finalize_exact_artifact(
        store, finalized, reviewer=actor, reason=why)
    store.link_artifact_to_material(finalized.artifact_id)
    return finalized


def save_xrd_reference(
    store: ws.WorkspaceStore,
    *,
    project_id: str,
    material_id: str,
    reference: xrd.ExternalXrdReference,
    creator: str,
    reason: str,
) -> ws.ArtifactRecord:
    """Save a user reference as material-linked ``needs_review`` evidence for matching."""
    _validate_context(store, project_id=project_id, material_id=material_id)
    actor = _required_text(creator, "creator")
    why = _required_text(reason, "reason")
    if not isinstance(reference, xrd.ExternalXrdReference):
        raise ws.MalformedRecordError("reference must be a validated ExternalXrdReference")
    identity = _reference_source_identity(reference)
    imported_review_status = str(reference.review_status or "needs_review")
    stored_reference = replace(reference, review_status="needs_review")
    input_snapshot = _source_snapshot(
        identity, workflow="user_supplied_xrd_reference_import")
    payload = stored_reference.to_dict()
    provenance = {
        "record_authority": "instruments.xrd_advisory.ExternalXrdReference",
        "imported_review_status": imported_review_status,
        "durable_review_status": "needs_review",
        "trust_note": (
            "source metadata retained; reference has not been scientifically endorsed"),
        "review_events": [],
        "persistence_contract": "xrd_reference_import_v1",
    }
    operation = {
        "project_id": project_id,
        "material_id": material_id,
        "record_type": ws.ARTIFACT_XRD_REFERENCE,
        "input_snapshot": input_snapshot,
        "payload": payload,
        "source_identity": identity,
        "creator": actor,
        "reason": why,
        "provenance": provenance,
    }
    artifact_id = _stable_artifact_id("xrd_reference_import_v1", operation)
    try:
        artifact = store.get_artifact(artifact_id)
    except ws.RecordNotFoundError:
        try:
            artifact = store.create_artifact(
                project_id,
                material_id,
                ws.ARTIFACT_XRD_REFERENCE,
                input_snapshot,
                status="needs_review",
                payload=payload,
                source_identity=identity,
                creator=actor,
                reason=why,
                provenance=provenance,
                artifact_id=artifact_id,
            )
        except ws.DuplicateRecordError:
            artifact = store.get_artifact(artifact_id)
    _assert_exact_artifact(
        artifact,
        artifact_id=artifact_id,
        logical_id=artifact_id,
        project_id=project_id,
        material_id=material_id,
        record_type=ws.ARTIFACT_XRD_REFERENCE,
        revision=1,
        input_snapshot=input_snapshot,
        payload=payload,
        source_identity=identity,
        creator=actor,
        reason=why,
        provenance=provenance,
        previous_artifact_id=None,
    )
    if artifact.status != "needs_review":
        raise ws.DuplicateRecordError(
            "recoverable XRD reference import has an invalid lifecycle state")
    store.link_artifact_to_material(artifact.artifact_id)
    return artifact


def load_xrd_reference(
    store: ws.WorkspaceStore,
    artifact_id: str,
    *,
    project_id: str,
    material_id: str,
) -> LoadedXrdReference:
    """Reload one exact user-supplied reference with license and review metadata intact."""
    _validate_context(store, project_id=project_id, material_id=material_id)
    artifact = store.get_artifact(artifact_id)
    _validate_artifact_context(
        artifact,
        project_id=project_id,
        material_id=material_id,
        record_type=ws.ARTIFACT_XRD_REFERENCE,
    )
    _validate_artifact_integrity(artifact)
    reference = _domain_record(xrd.ExternalXrdReference, artifact.payload)
    identity = _reference_source_identity(reference)
    if identity != artifact.source_identity:
        raise ws.MalformedRecordError("XRD reference source identity differs from its envelope")
    expected_review_status = {
        "needs_review": "needs_review",
        "reviewed": "reviewed",
        "rejected": "rejected",
    }.get(artifact.status)
    if expected_review_status is None or reference.review_status != expected_review_status:
        raise ws.MalformedRecordError("XRD reference review lifecycle is inconsistent")
    return LoadedXrdReference(artifact=artifact, reference=reference)


def review_xrd_reference(
    store: ws.WorkspaceStore,
    artifact_id: str,
    *,
    project_id: str,
    material_id: str,
    review_status: str,
    reviewer: str,
    reason: str,
) -> ws.ArtifactRecord:
    """Resolve a reference in a new terminal revision, preserving exact historical inputs."""
    resolved_status = str(review_status or "").strip()
    if resolved_status not in {"reviewed", "rejected"}:
        raise ws.MalformedRecordError("reference review_status must be reviewed or rejected")
    actor = _required_text(reviewer, "reviewer")
    why = _required_text(reason, "reason")
    loaded = load_xrd_reference(
        store, artifact_id, project_id=project_id, material_id=material_id)
    if loaded.artifact.status != "needs_review":
        raise ws.ImmutableRecordError("only a needs_review XRD reference can be resolved")
    reference = replace(loaded.reference, review_status=resolved_status)
    provenance = dict(loaded.artifact.provenance)
    events = list(provenance.get("review_events") or [])
    events.append({"status": resolved_status, "reviewer": actor, "reason": why})
    provenance.update({"durable_review_status": resolved_status, "review_events": events})
    payload = reference.to_dict()
    previous_hash = ws.identity_hash(loaded.artifact.to_dict())
    operation = {
        "previous_artifact_id": loaded.artifact.artifact_id,
        "previous_artifact_hash": previous_hash,
        "logical_id": loaded.artifact.logical_id,
        "project_id": project_id,
        "material_id": material_id,
        "record_type": ws.ARTIFACT_XRD_REFERENCE,
        "revision": loaded.artifact.revision + 1,
        "status": resolved_status,
        "input_snapshot": loaded.artifact.input_snapshot,
        "payload": payload,
        "source_identity": loaded.artifact.source_identity,
        "creator": actor,
        "reason": why,
        "provenance": provenance,
    }
    artifact_id = _stable_artifact_id("xrd_reference_review_v1", operation)
    try:
        draft = store.get_artifact(artifact_id)
    except ws.RecordNotFoundError:
        try:
            draft = store.create_artifact(
                project_id,
                material_id,
                ws.ARTIFACT_XRD_REFERENCE,
                loaded.artifact.input_snapshot,
                status=resolved_status,
                payload=payload,
                source_identity=loaded.artifact.source_identity,
                creator=actor,
                reviewer=actor,
                reason=why,
                provenance=provenance,
                revision=loaded.artifact.revision + 1,
                previous_artifact_id=loaded.artifact.artifact_id,
                artifact_id=artifact_id,
                logical_id=loaded.artifact.logical_id,
            )
        except ws.DuplicateRecordError:
            # Either the same deterministic review won a concurrent create, or a
            # different review extended the lineage. Only the former may recover.
            draft = store.get_artifact(artifact_id)
    _assert_exact_artifact(
        draft,
        artifact_id=artifact_id,
        logical_id=loaded.artifact.logical_id,
        project_id=project_id,
        material_id=material_id,
        record_type=ws.ARTIFACT_XRD_REFERENCE,
        revision=loaded.artifact.revision + 1,
        input_snapshot=loaded.artifact.input_snapshot,
        payload=payload,
        source_identity=loaded.artifact.source_identity,
        creator=actor,
        reason=why,
        provenance=provenance,
        previous_artifact_id=loaded.artifact.artifact_id,
    )
    if draft.previous_artifact_hash != previous_hash:
        raise ws.DuplicateRecordError(
            "recoverable XRD review has a different previous-revision identity")
    updated = _finalize_exact_artifact(
        store, draft, reviewer=actor, reason=why, status=resolved_status)
    store.link_artifact_to_material(updated.artifact_id)
    return updated


def require_source_hash_match(
    artifact: ws.ArtifactRecord, source_bytes: bytes | bytearray,
) -> None:
    """Fail closed when newly supplied bytes differ from an artifact's exact source hash."""
    if not isinstance(source_bytes, (bytes, bytearray)):
        raise ws.MalformedRecordError("source_bytes must contain the exact uploaded bytes")
    expected = _require_sha256(
        artifact.source_identity.get("source_sha256"), "artifact source_sha256")
    actual = hashlib.sha256(bytes(source_bytes)).hexdigest()
    if actual != expected:
        raise ws.WorkspaceStoreError("XRD source hash changed; saved identity cannot be reused")


def _generated_advisory_text(payload: dict) -> str:
    pieces: list[str] = []
    for key in ("comparison_status", "disclaimer", "explanation", "wording_note"):
        pieces.append(str(payload.get(key) or ""))
    pieces.extend(str(item) for item in payload.get("warnings", []) or [])
    pieces.extend(str(item) for item in payload.get("limitations", []) or [])
    for candidate in payload.get("candidates", []) or []:
        for key in ("wording", "note", "dominant_peak_warning"):
            pieces.append(str(candidate.get(key) or ""))
        pieces.extend(str(item) for item in candidate.get("limitations", []) or [])
    return " ".join(pieces)


def _require_advisory_wording(payload: dict) -> None:
    text = _generated_advisory_text(payload)
    if _UNSAFE_GENERATED_CLAIM.search(text):
        raise ws.MalformedRecordError("XRD advisory output contains an unsafe phase claim")
    guidance = " ".join(
        str(payload.get(key) or "")
        for key in ("comparison_status", "disclaimer", "wording_note")
    ).lower()
    for required in ("tentative", "advisory", "possible match"):
        if required not in guidance:
            raise ws.MalformedRecordError(f"XRD advisory output is missing {required!r} wording")
    if "check against an appropriate reference source" not in guidance:
        raise ws.MalformedRecordError(
            "XRD advisory output lacks the required reference-source check")


def _require_match_bindings(
    payload: dict,
    pattern: xrd.MeasuredXrdPattern,
    references: Iterable[xrd.ExternalXrdReference],
    *,
    tolerance: Any,
) -> None:
    measured_identity = payload.get("measured_peak_identity")
    if not isinstance(measured_identity, dict):
        raise ws.MalformedRecordError("XRD advisory output lacks measured-pattern identity")
    expected_pattern = {
        "pattern_id": pattern.pattern_id,
        "source_filename": pattern.source_filename,
        "source_sha256": pattern.source_sha256,
    }
    if any(measured_identity.get(key) != value for key, value in expected_pattern.items()):
        raise ws.MalformedRecordError("XRD advisory output is bound to another measured pattern")
    expected_references = [reference.identity() for reference in references]
    if ws.identity_hash(payload.get("reference_identities")) != ws.identity_hash(
            expected_references):
        raise ws.MalformedRecordError("XRD advisory output is bound to other references")
    if payload.get("tolerance_deg") != tolerance:
        raise ws.MalformedRecordError("XRD advisory tolerance differs from its saved input")


def _unique_artifact_ids(values: Iterable[str]) -> list[str]:
    ids = [str(value or "").strip() for value in values]
    if not ids or any(not value for value in ids):
        raise ws.MalformedRecordError("at least one XRD reference artifact is required")
    if len(ids) != len(set(ids)):
        raise ws.MalformedRecordError("duplicate XRD reference artifacts are not permitted")
    return ids


def save_tentative_match_run(
    store: ws.WorkspaceStore,
    *,
    project_id: str,
    material_id: str,
    pattern_artifact_id: str,
    reference_artifact_ids: Iterable[str],
    tolerance: float = xrd.DEFAULT_MATCH_TOLERANCE_DEG,
    creator: str,
    reason: str,
) -> SavedXrdAdvisoryRun:
    """Match exact artifacts through ``xrd_advisory`` and save an immutable advisory run."""
    actor = _required_text(creator, "creator")
    why = _required_text(reason, "reason")
    pattern_loaded = load_measured_pattern(
        store, pattern_artifact_id, project_id=project_id, material_id=material_id)
    if pattern_loaded.artifact.status != "finalized":
        raise ws.ImmutableRecordError("XRD advisory runs require a finalized measured pattern")

    reference_ids = _unique_artifact_ids(reference_artifact_ids)
    references = tuple(
        load_xrd_reference(
            store, artifact_id, project_id=project_id, material_id=material_id)
        for artifact_id in reference_ids
    )
    rejected = [item.artifact.artifact_id for item in references
                if item.artifact.status == "rejected"]
    if rejected:
        raise ws.WorkspaceStoreError(
            "rejected XRD references cannot be used for advisory matching: "
            + ", ".join(rejected))

    # These links intentionally happen before RunRecord creation. Material identity then captures
    # the complete measurement set and the newly saved run is not immediately stale.
    artifacts = (pattern_loaded.artifact, *(item.artifact for item in references))
    for artifact in artifacts:
        store.link_artifact_to_material(artifact.artifact_id)

    result = xrd.match_measured_peaks(
        pattern_loaded.pattern,
        tolerance=tolerance,
        references=[item.reference for item in references],
    )
    match_payload = result.to_dict()
    _require_advisory_wording(match_payload)
    _require_match_bindings(
        match_payload,
        pattern_loaded.pattern,
        [item.reference for item in references],
        tolerance=result.tolerance_deg,
    )
    dependencies = [phase3_artifacts.artifact_dependency(artifact) for artifact in artifacts]
    review_warnings = [
        f"Reference artifact {item.artifact.artifact_id} remains needs_review and is not trusted; "
        "the comparison remains tentative and advisory."
        for item in references if item.artifact.status == "needs_review"
    ]
    source_identities = [dict(artifact.source_identity) for artifact in artifacts]
    input_snapshot = {
        "workflow": "tentative_measured_xrd_reference_match",
        "artifact_identities": dependencies,
        "pattern_source_identity": source_identities[0],
        "reference_source_identities": source_identities[1:],
        "tolerance_deg": result.tolerance_deg,
        "match_payload_hash": ws.identity_hash(match_payload),
    }
    result_data = {
        "comparison_status": "tentative_advisory_possible_match",
        "artifact_identities": dependencies,
        "match_payload": match_payload,
        "match_payload_hash": ws.identity_hash(match_payload),
        "save_provenance": {"creator": actor, "reason": why},
    }
    warnings = [
        *match_payload.get("warnings", []),
        *review_warnings,
        "Tentative advisory possible matches only; check against an appropriate reference source.",
    ]
    legacy_references = [
        {"record_type": artifact.record_type, "artifact_id": artifact.artifact_id}
        for artifact in artifacts
    ]
    material = store.get_material(material_id)
    run_operation = {
        "project_id": project_id,
        "material_id": material_id,
        "machine_id": XRD_MACHINE_ID,
        "input_snapshot": input_snapshot,
        "material_revision": material.revision,
        "composition_revision": material.composition_revision,
        "assumption_revision": material.assumption_revision,
        "material_snapshot_hash": ws.material_identity_hash(material),
        "status": "advisory",
        "output_type": XRD_RUN_OUTPUT_TYPE,
        "epistemic_type": XRD_RUN_EPISTEMIC_TYPE,
        "result_data": result_data,
        "warnings": warnings,
        "validation_state": "not_applicable_advisory",
        "legacy_references": legacy_references,
    }
    run_id = _stable_run_id("xrd_tentative_match_run_v1", run_operation)
    try:
        run = store.get_run(run_id)
    except ws.RecordNotFoundError:
        try:
            run = store.create_run(
                project_id,
                material_id,
                XRD_MACHINE_ID,
                input_snapshot,
                status="advisory",
                output_type=XRD_RUN_OUTPUT_TYPE,
                epistemic_type=XRD_RUN_EPISTEMIC_TYPE,
                result_data=result_data,
                warnings=warnings,
                validation_state="not_applicable_advisory",
                legacy_references=legacy_references,
                run_id=run_id,
            )
        except ws.DuplicateRecordError:
            run = store.get_run(run_id)
    expected_run = {"run_id": run_id, **run_operation}
    expected_run.update({
        "input_hash": ws.identity_hash(input_snapshot),
        "result_hash": ws.identity_hash(result_data),
        "model_identity": {},
        "environment_identity": {},
        "evidence_identity": [],
        "result_location": None,
    })
    actual_run = {key: getattr(run, key) for key in expected_run}
    if actual_run != expected_run:
        raise ws.DuplicateRecordError(
            "XRD match persistence identity collides with a different run")
    # ``create_run`` writes the immutable run before adding the material index.
    # Repair only that idempotent index if an interruption occurred between them.
    material = store.get_material(material_id)
    if run.run_id not in material.associated_run_ids:
        store.update_material(
            material_id,
            associated_run_ids=[*material.associated_run_ids, run.run_id],
        )
    stale, stale_reasons = store.run_staleness(run)
    if stale:
        raise ws.WorkspaceStoreError(
            "new XRD advisory run is unexpectedly stale: " + "; ".join(stale_reasons))
    return SavedXrdAdvisoryRun(
        run=run,
        pattern_artifact=pattern_loaded.artifact,
        reference_artifacts=tuple(item.artifact for item in references),
        match_payload=match_payload,
    )


def load_tentative_match_run(
    store: ws.WorkspaceStore,
    run_id: str,
    *,
    project_id: str,
    material_id: str,
) -> LoadedXrdAdvisoryRun:
    """Reopen the exact pattern/reference revisions and advisory payload saved by a run."""
    _validate_context(store, project_id=project_id, material_id=material_id)
    run = store.get_run(run_id)
    if run.project_id != project_id or run.material_id != material_id:
        raise ws.WorkspaceStoreError("XRD run belongs to another project/material context")
    if run.machine_id != XRD_MACHINE_ID or run.output_type != XRD_RUN_OUTPUT_TYPE:
        raise ws.MalformedRecordError("run is not a measured-XRD advisory match")
    if (run.status != "advisory"
            or run.epistemic_type != XRD_RUN_EPISTEMIC_TYPE
            or run.validation_state != "not_applicable_advisory"):
        raise ws.MalformedRecordError(
            "XRD run attempts to leave its fixed advisory epistemic contract")
    if ws.identity_hash(run.input_snapshot) != run.input_hash:
        raise ws.MalformedRecordError("XRD run input hash differs from its snapshot")
    dependencies = run.input_snapshot.get("artifact_identities")
    if not isinstance(dependencies, list) or len(dependencies) < 2:
        raise ws.MalformedRecordError("XRD run has incomplete artifact dependencies")
    exact = phase3_artifacts.require_exact_artifact_dependencies(
        store, dependencies, project_id=project_id, material_id=material_id)
    pattern_records = [item for item in exact if item.record_type == ws.ARTIFACT_XRD_PATTERN]
    reference_records = [item for item in exact if item.record_type == ws.ARTIFACT_XRD_REFERENCE]
    if len(pattern_records) != 1 or len(reference_records) != len(exact) - 1:
        raise ws.MalformedRecordError("XRD run dependency types are invalid")

    result_dependencies = run.result_data.get("artifact_identities")
    if ws.identity_hash(result_dependencies) != ws.identity_hash(dependencies):
        raise ws.MalformedRecordError("XRD run result dependency identities differ from its inputs")
    match_payload = run.result_data.get("match_payload")
    if not isinstance(match_payload, dict):
        raise ws.MalformedRecordError("XRD run has no advisory match payload")
    stored_match_hash = run.input_snapshot.get("match_payload_hash")
    if stored_match_hash != ws.identity_hash(match_payload):
        raise ws.MalformedRecordError("XRD advisory match payload hash changed")
    if run.result_data.get("match_payload_hash") != stored_match_hash:
        raise ws.MalformedRecordError("XRD advisory match payload identities disagree")
    if run.result_data.get("comparison_status") != XRD_RUN_OUTPUT_TYPE:
        raise ws.MalformedRecordError(
            "XRD advisory result lifecycle differs from its run envelope")
    _require_advisory_wording(match_payload)

    expected_sources = [dict(item.source_identity) for item in exact]
    supplied_sources = [run.input_snapshot.get("pattern_source_identity"),
                        *(run.input_snapshot.get("reference_source_identities") or [])]
    if ws.identity_hash(expected_sources) != ws.identity_hash(supplied_sources):
        raise ws.MalformedRecordError("XRD run source identities differ from exact dependencies")

    pattern = load_measured_pattern(
        store, pattern_records[0].artifact_id,
        project_id=project_id, material_id=material_id)
    references = tuple(
        load_xrd_reference(
            store, item.artifact_id, project_id=project_id, material_id=material_id)
        for item in reference_records
    )
    _require_match_bindings(
        match_payload,
        pattern.pattern,
        [item.reference for item in references],
        tolerance=run.input_snapshot.get("tolerance_deg"),
    )
    stale, stale_reasons = store.run_staleness(run)
    return LoadedXrdAdvisoryRun(
        run=run,
        pattern=pattern,
        references=references,
        match_payload=match_payload,
        is_stale=stale,
        stale_reasons=tuple(stale_reasons),
    )
