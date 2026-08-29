"""Durable manual/AI evidence lifecycle over the Phase 3 artifact store.

This module owns evidence validation and review transitions, not persistence
mechanics. ``WorkspaceStore`` remains the sole durable authority and historical
per-run JSONL files remain readable through :mod:`evidence_store` without being
rewritten or promoted automatically.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone
from typing import Iterable, Mapping

from . import evidence_schema as E
from . import evidence_store

RECORD_TYPE = "evidence"
_REVIEW_FIELDS = {
    "review_status", "reviewer", "review_time", "review_reason", "revision",
    "previous_revision_id", "previous_revision_hash",
}
_CONTEXT_FIELDS = {"project_id", "material_id"}
_ORIGIN_FIELDS = {"creation_origin"}
_DERIVED_EVIDENCE_FIELDS = {
    "citation", "confidence_label", "source_location_summary",
    *(f"citation_{key}" for key in ("source", "doi", "title", "url", "authors", "year", "query")),
    *(f"source_{key}" for key in E.SourceLocation.__dataclass_fields__),
}
_DURABLE_EVIDENCE_FIELDS = frozenset(
    set(E.LeachingEvidence.__dataclass_fields__)
    | set(E.CompositeEvidence.__dataclass_fields__)
    | _DERIVED_EVIDENCE_FIELDS
)


class EvidenceReviewError(ValueError):
    """Base class for controlled evidence validation/lifecycle failures."""


class InvalidEvidenceTransition(EvidenceReviewError):
    """A requested lifecycle transition is not permitted."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _finite_confidence(value, *, field_name: str) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, bool):
        raise EvidenceReviewError(f"{field_name} must be a finite number from 0 to 1")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceReviewError(f"{field_name} must be a finite number from 0 to 1") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise EvidenceReviewError(f"{field_name} must be a finite number from 0 to 1")
    return number


def _provenance_dict(value) -> dict:
    if isinstance(value, E.Provenance):
        provenance = value.to_dict()
    elif isinstance(value, Mapping):
        provenance = dict(value)
    else:
        provenance = {}
    source = _clean_text(provenance.get("source") or provenance.get("provider")) or ""
    doi = _clean_text(provenance.get("doi"))
    title = _clean_text(provenance.get("title"))
    if not doi and not (title and source):
        raise evidence_store.MissingProvenanceError(
            "evidence requires a DOI or a title plus source/provider")
    route = provenance.get("discovery_route")
    if isinstance(route, E.DiscoveryRoute):
        route = route.to_dict()
    elif isinstance(route, Mapping):
        route = dict(route)
    else:
        route = {}
    executed = route.get("executed_queries") or []
    if not isinstance(executed, list):
        raise EvidenceReviewError("discovery_route.executed_queries must be a list")
    route = E.DiscoveryRoute(
        method=str(route.get("method") or ("manual" if source == "manual" else "")),
        source=str(route.get("source") or source),
        query=_clean_text(route.get("query") or provenance.get("query")),
        executed_queries=[str(item).strip() for item in executed if str(item).strip()],
        record_identifier=_clean_text(route.get("record_identifier")),
    ).to_dict()
    authors = provenance.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    elif not isinstance(authors, (list, tuple)):
        raise EvidenceReviewError("provenance.authors must be a list")
    normalized = E.Provenance(
        source=source,
        doi=doi,
        title=title,
        url=_clean_text(provenance.get("url")),
        authors=[str(item).strip() for item in authors if str(item).strip()],
        year=provenance.get("year"),
        query=_clean_text(provenance.get("query")),
        discovery_route=route,
    ).to_dict()
    return normalized


def _source_location_dict(value) -> dict:
    if isinstance(value, E.SourceLocation):
        raw = value.to_dict()
    elif isinstance(value, Mapping):
        raw = dict(value)
    elif value in (None, ""):
        raw = {}
    else:
        raise EvidenceReviewError("source_location must be a structured object")
    aliases = {
        "supplementary_item": "supplement",
        "dataset_record_identifier": "dataset",
        "note": "free_note",
    }
    return E.SourceLocation(**{
        key: _clean_text(raw.get(key) if raw.get(key) is not None else raw.get(alias))
        for key, alias in ((name, aliases.get(name))
                           for name in E.SourceLocation.__dataclass_fields__)
    }).to_dict()


def _as_payload(evidence) -> dict:
    if isinstance(evidence, E.Evidence):
        return evidence.to_row()
    if isinstance(evidence, Mapping):
        return copy.deepcopy(dict(evidence))
    raise EvidenceReviewError("evidence must be a structured evidence object or mapping")


def normalize_evidence_payload(
    evidence,
    *,
    project_id: str,
    material_id: str | None,
    ai_created: bool,
    review_status: str | None = None,
) -> dict:
    """Return one finite, provenance-complete, structured evidence payload."""
    payload = _as_payload(evidence)
    evidence_store.validate_structured_payload(
        payload, allowed_top_level_fields=_DURABLE_EVIDENCE_FIELDS)
    schema_kind = str(payload.get("schema_kind") or "").strip()
    if schema_kind not in E.SCHEMA_KINDS:
        raise EvidenceReviewError(f"unsupported evidence schema_kind: {schema_kind!r}")

    payload["project_id"] = str(project_id)
    payload["material_id"] = str(material_id) if material_id is not None else None
    payload["schema_kind"] = schema_kind
    payload["provenance"] = _provenance_dict(payload.get("provenance"))
    payload["source_location"] = _source_location_dict(payload.get("source_location"))
    payload["source_location_summary"] = E.SourceLocation(
        **payload["source_location"]).summary
    payload["topic"] = _clean_text(payload.get("topic"))
    payload["claim"] = _clean_text(payload.get("claim"))
    payload["notes"] = _clean_text(payload.get("notes"))
    for key in ("reported_values", "units", "conditions", "field_confidence"):
        value = payload.get(key)
        if value in (None, ""):
            payload[key] = {}
        elif not isinstance(value, Mapping):
            raise EvidenceReviewError(f"{key} must be a structured object")
        else:
            payload[key] = copy.deepcopy(dict(value))
    payload["extraction_confidence"] = _finite_confidence(
        payload.get("extraction_confidence"), field_name="extraction_confidence")
    payload["field_confidence"] = {
        str(key): _finite_confidence(value, field_name=f"field_confidence.{key}")
        for key, value in payload["field_confidence"].items()
    }
    scope = str(payload.get("extraction_scope") or
                (E.SCOPE_ABSTRACT if ai_created else E.SCOPE_MANUAL)).strip()
    if scope not in E.EXTRACTION_SCOPES:
        raise EvidenceReviewError(f"unsupported extraction_scope: {scope!r}")
    # The current AI path receives title + abstract only. A model response cannot
    # upgrade that supplied scope to full text.
    if ai_created:
        scope = E.SCOPE_ABSTRACT
    payload["extraction_scope"] = scope
    extraction_status = str(payload.get("extraction_status") or
                            (E.STATUS_OK if ai_created else E.STATUS_MANUAL)).strip()
    if extraction_status not in E.EXTRACTION_STATUSES:
        raise EvidenceReviewError(f"unsupported extraction_status: {extraction_status!r}")
    payload["extraction_status"] = extraction_status
    conflicts = payload.get("conflicts") or []
    if not isinstance(conflicts, list):
        raise EvidenceReviewError("conflicts must be a list")
    payload["conflicts"] = [str(item).strip() for item in conflicts if str(item).strip()]
    origin = payload.get("creation_origin")
    if origin not in (None, "", "manual", "ai"):
        raise EvidenceReviewError(f"unsupported evidence creation_origin: {origin!r}")
    payload["creation_origin"] = str(origin).strip() if origin not in (None, "") else None

    lifecycle = review_status or payload.get("review_status") \
        or (E.REVIEW_NEEDS_REVIEW if ai_created else E.REVIEW_DRAFT)
    lifecycle = str(lifecycle).strip()
    if lifecycle not in E.REVIEW_STATUSES:
        raise EvidenceReviewError(f"unsupported review_status: {lifecycle!r}")
    if ai_created and lifecycle != E.REVIEW_NEEDS_REVIEW:
        lifecycle = E.REVIEW_NEEDS_REVIEW
    payload["review_status"] = lifecycle
    payload.setdefault("reviewer", None)
    payload.setdefault("review_time", None)
    payload.setdefault("review_reason", None)
    revision = payload.get("revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise EvidenceReviewError("evidence revision must be a positive integer")
    payload["revision"] = revision
    payload.setdefault("previous_revision_id", None)
    payload.setdefault("previous_revision_hash", None)
    evidence_store.validate_structured_payload(
        payload, allowed_top_level_fields=_DURABLE_EVIDENCE_FIELDS)
    return payload


def _scientific_snapshot(payload: dict) -> dict:
    return {key: copy.deepcopy(value) for key, value in payload.items()
            if key not in _REVIEW_FIELDS}


def _source_identity(payload: dict) -> dict:
    return {
        "provenance": copy.deepcopy(payload["provenance"]),
        "source_location": copy.deepcopy(payload["source_location"]),
    }


def _validate_canonical_evidence_payload(
    payload,
    *,
    project_id: str,
    material_id: str | None,
    review_status: str,
) -> dict:
    """Revalidate a durable/exported payload without changing its stored meaning."""
    if not isinstance(payload, dict):
        raise EvidenceReviewError("evidence payload must be a structured object")
    # Re-run the closed scientific schema/provenance/finite-value validator on
    # every durable read. ``ai_created=False`` is intentional here: an AI-origin
    # record may have been explicitly reviewed, and a read must validate that
    # terminal state rather than force it back to needs_review.
    try:
        normalized = normalize_evidence_payload(
            payload,
            project_id=project_id,
            material_id=material_id,
            ai_created=False,
            review_status=review_status,
        )
    except (evidence_store.MissingProvenanceError,
            evidence_store.ForbiddenEvidenceContentError) as exc:
        raise EvidenceReviewError(str(exc)) from exc
    if normalized != payload:
        raise EvidenceReviewError("evidence payload is not in its canonical durable form")
    origin = payload.get("creation_origin")
    if origin not in {"manual", "ai"}:
        raise EvidenceReviewError(
            "durable evidence must retain manual or AI creation origin")
    if origin == "ai" and payload.get("extraction_scope") != E.SCOPE_ABSTRACT:
        raise EvidenceReviewError(
            "AI-origin durable evidence must retain its abstract-only extraction scope")
    return payload


def _validate_evidence_envelope(store, record):
    """Require the durable envelope and evidence-domain lifecycle to agree exactly."""
    payload = _validate_canonical_evidence_payload(
        record.payload,
        project_id=record.project_id,
        material_id=record.material_id,
        review_status=record.status,
    )
    if payload.get("project_id") != record.project_id \
            or payload.get("material_id") != record.material_id:
        raise EvidenceReviewError("evidence payload context differs from its envelope")
    if payload.get("review_status") != record.status:
        raise EvidenceReviewError("evidence review lifecycle differs from its envelope")
    if payload.get("revision") != record.revision:
        raise EvidenceReviewError("evidence payload revision differs from its envelope")
    try:
        expected_source = _source_identity(payload)
        expected_snapshot = _scientific_snapshot(payload)
        expected_provenance = payload["provenance"]
    except KeyError as exc:
        raise EvidenceReviewError(
            f"evidence payload lacks required envelope field: {exc.args[0]}") from exc
    if record.source_identity != expected_source:
        raise EvidenceReviewError("evidence source identity differs from its envelope")
    if record.input_snapshot != expected_snapshot:
        raise EvidenceReviewError("evidence scientific snapshot differs from its envelope")
    if record.provenance != expected_provenance:
        raise EvidenceReviewError("evidence provenance differs from its envelope")

    if record.revision == 1:
        if payload.get("previous_revision_id") is not None \
                or payload.get("previous_revision_hash") is not None:
            raise EvidenceReviewError("evidence root payload has invalid lineage metadata")
    else:
        if payload.get("previous_revision_id") != record.previous_artifact_id:
            raise EvidenceReviewError("evidence payload previous revision differs from its envelope")
        previous = store.get_artifact(record.previous_artifact_id)
        if previous.record_type != RECORD_TYPE \
                or payload.get("previous_revision_hash") != previous.payload_hash:
            raise EvidenceReviewError("evidence payload previous-revision hash differs")

    if record.status in {E.REVIEW_REVIEWED, E.REVIEW_REJECTED}:
        if (payload.get("reviewer") != record.reviewer
                or payload.get("review_time") != record.reviewed_at
                or payload.get("review_reason") != record.reason):
            raise EvidenceReviewError("evidence review metadata differs from its envelope")
    elif any(payload.get(key) is not None for key in (
            "reviewer", "review_time", "review_reason")) \
            or record.reviewer or record.reviewed_at is not None:
        raise EvidenceReviewError("unresolved evidence carries terminal review metadata")
    return record


def _get_evidence_record(store, artifact_id):
    record = store.get_artifact(artifact_id)
    if getattr(record, "record_type", None) != RECORD_TYPE:
        raise EvidenceReviewError(f"artifact {artifact_id} is not an evidence record")
    return _validate_evidence_envelope(store, record)


def create_evidence(
    store,
    project_id: str,
    material_id: str | None,
    evidence,
    *,
    creator: str,
    ai_created: bool = False,
):
    """Create manual draft evidence or AI-origin ``needs_review`` evidence."""
    creator = str(creator or "").strip()
    if not creator:
        raise EvidenceReviewError("evidence creator is required")
    raw_payload = _as_payload(evidence)
    ai_origin = ai_created or str(raw_payload.get("creation_origin") or "").strip() == "ai"
    lifecycle = E.REVIEW_NEEDS_REVIEW if ai_origin else E.REVIEW_DRAFT
    payload = normalize_evidence_payload(
        raw_payload, project_id=project_id, material_id=material_id,
        ai_created=ai_origin, review_status=lifecycle)
    payload["reviewer"] = None
    payload["review_time"] = None
    payload["review_reason"] = None
    payload["creation_origin"] = "ai" if ai_origin else "manual"
    payload["revision"] = 1
    payload["previous_revision_id"] = None
    payload["previous_revision_hash"] = None
    return store.create_artifact(
        project_id,
        material_id,
        RECORD_TYPE,
        _scientific_snapshot(payload),
        status=lifecycle,
        payload=payload,
        source_identity=_source_identity(payload),
        creator=creator,
        provenance=payload["provenance"],
    )


def create_manual_evidence(store, project_id, material_id, evidence, *, creator: str):
    return create_evidence(store, project_id, material_id, evidence,
                           creator=creator, ai_created=False)


def create_ai_evidence(store, project_id, material_id, evidence, *, creator: str):
    return create_evidence(store, project_id, material_id, evidence,
                           creator=creator, ai_created=True)


def edit_draft(store, artifact_id, changes: Mapping, *, editor: str, reason: str = ""):
    """Edit an existing draft in place; submitted/terminal records are not editable."""
    editor = str(editor or "").strip()
    if not editor:
        raise EvidenceReviewError("draft editor is required")
    record = _get_evidence_record(store, artifact_id)
    if record.status != E.REVIEW_DRAFT:
        raise InvalidEvidenceTransition("only draft evidence can be edited in place")
    if not isinstance(changes, Mapping):
        raise EvidenceReviewError("evidence changes must be a structured mapping")
    forbidden = set(changes) & (_REVIEW_FIELDS | _CONTEXT_FIELDS | _ORIGIN_FIELDS)
    if forbidden:
        raise EvidenceReviewError(
            f"lifecycle/context/origin fields cannot be edited: {sorted(forbidden)}")
    payload = copy.deepcopy(record.payload)
    payload.update(copy.deepcopy(dict(changes)))
    payload["review_status"] = E.REVIEW_DRAFT
    payload["reviewer"] = None
    payload["review_time"] = None
    payload["review_reason"] = None
    payload = normalize_evidence_payload(
        payload, project_id=record.project_id, material_id=record.material_id,
        ai_created=False, review_status=E.REVIEW_DRAFT)
    return store.update_artifact(
        artifact_id,
        expected_payload_hash=record.payload_hash,
        expected_input_hash=record.input_hash,
        expected_status=E.REVIEW_DRAFT,
        input_snapshot=_scientific_snapshot(payload),
        payload=payload,
        source_identity=_source_identity(payload),
        provenance=payload["provenance"],
        reason=str(reason or f"draft edited by {editor}").strip(),
    )


def submit_for_review(store, artifact_id, *, submitter: str, reason: str = ""):
    submitter = str(submitter or "").strip()
    if not submitter:
        raise EvidenceReviewError("review submitter is required")
    record = _get_evidence_record(store, artifact_id)
    if record.status != E.REVIEW_DRAFT:
        raise InvalidEvidenceTransition("only draft evidence can be submitted for review")
    payload = normalize_evidence_payload(
        record.payload, project_id=record.project_id, material_id=record.material_id,
        ai_created=False, review_status=E.REVIEW_NEEDS_REVIEW)
    payload["review_status"] = E.REVIEW_NEEDS_REVIEW
    payload["reviewer"] = None
    payload["review_time"] = None
    payload["review_reason"] = None
    return store.update_artifact(
        artifact_id,
        expected_payload_hash=record.payload_hash,
        expected_status=E.REVIEW_DRAFT,
        status=E.REVIEW_NEEDS_REVIEW,
        payload=payload,
        reason=str(reason or f"submitted for review by {submitter}").strip(),
    )


def _review_transition(store, artifact_id, *, target: str, reviewer: str, reason: str):
    record = _get_evidence_record(store, artifact_id)
    if record.status != E.REVIEW_NEEDS_REVIEW:
        raise InvalidEvidenceTransition("only needs-review evidence can be reviewed or rejected")
    reviewer = str(reviewer or "").strip()
    reason = str(reason or "").strip()
    if not reviewer or not reason:
        raise EvidenceReviewError("reviewer and review reason are required")
    reviewed_at = _now()
    payload = copy.deepcopy(record.payload)
    payload["review_status"] = target
    payload["reviewer"] = reviewer
    payload["review_time"] = reviewed_at
    payload["review_reason"] = reason
    payload = normalize_evidence_payload(
        payload, project_id=record.project_id, material_id=record.material_id,
        ai_created=False, review_status=target)
    return store.update_artifact(
        artifact_id,
        expected_payload_hash=record.payload_hash,
        expected_status=E.REVIEW_NEEDS_REVIEW,
        status=target,
        payload=payload,
        reviewer=reviewer,
        reason=reason,
        reviewed_at=reviewed_at,
    )


def mark_reviewed(store, artifact_id, *, reviewer: str, reason: str):
    """Explicit human action that alone can produce ``reviewed`` evidence."""
    return _review_transition(store, artifact_id, target=E.REVIEW_REVIEWED,
                              reviewer=reviewer, reason=reason)


def reject_evidence(store, artifact_id, *, reviewer: str, reason: str):
    return _review_transition(store, artifact_id, target=E.REVIEW_REJECTED,
                              reviewer=reviewer, reason=reason)


def revise_evidence(store, artifact_id, changes: Mapping, *, creator: str, reason: str):
    """Create a needs-review revision of reviewed/rejected evidence; never mutate history."""
    creator = str(creator or "").strip()
    reason = str(reason or "").strip()
    if not creator or not reason:
        raise EvidenceReviewError("revision creator and reason are required")
    previous = _get_evidence_record(store, artifact_id)
    if previous.status not in {E.REVIEW_REVIEWED, E.REVIEW_REJECTED}:
        raise InvalidEvidenceTransition(
            "only reviewed or rejected evidence requires an immutable new revision")
    if not isinstance(changes, Mapping):
        raise EvidenceReviewError("evidence changes must be a structured mapping")
    forbidden = set(changes) & (_REVIEW_FIELDS | _CONTEXT_FIELDS | _ORIGIN_FIELDS)
    if forbidden:
        raise EvidenceReviewError(
            f"lifecycle/context/origin fields cannot be edited: {sorted(forbidden)}")
    payload = copy.deepcopy(previous.payload)
    payload.update(copy.deepcopy(dict(changes)))
    payload["review_status"] = E.REVIEW_NEEDS_REVIEW
    payload["reviewer"] = None
    payload["review_time"] = None
    payload["review_reason"] = None
    payload["revision"] = previous.revision + 1
    payload["previous_revision_id"] = previous.artifact_id
    payload["previous_revision_hash"] = previous.payload_hash
    payload = normalize_evidence_payload(
        payload, project_id=previous.project_id, material_id=previous.material_id,
        ai_created=False, review_status=E.REVIEW_NEEDS_REVIEW)
    return store.create_artifact_revision(
        artifact_id,
        status=E.REVIEW_NEEDS_REVIEW,
        payload=payload,
        input_snapshot=_scientific_snapshot(payload),
        source_identity=_source_identity(payload),
        creator=creator,
        reason=reason,
        provenance=payload["provenance"],
    )


def get_evidence(store, artifact_id, *, project_id: str | None = None,
                 material_id: str | None = None):
    """Read one evidence artifact, optionally enforcing its project/material boundary."""
    record = _get_evidence_record(store, artifact_id)
    if project_id is not None and record.project_id != str(project_id):
        raise EvidenceReviewError("evidence does not belong to the requested project")
    if material_id is not None and record.material_id != str(material_id):
        raise EvidenceReviewError("evidence does not belong to the requested material")
    return record


def list_evidence(
    store,
    *,
    project_id: str,
    material_id: str | None = None,
    review_status: str | None = None,
    schema_kind: str | None = None,
    topic: str | None = None,
    claim: str | None = None,
    latest_only: bool = False,
):
    if review_status is not None and review_status not in E.REVIEW_STATUSES:
        raise EvidenceReviewError(f"unsupported review status: {review_status!r}")
    records = store.list_artifacts(
        project_id=project_id, material_id=material_id,
        record_type=RECORD_TYPE, status=review_status)
    visible = []
    for record in records:
        _validate_evidence_envelope(store, record)
        payload = record.payload if isinstance(record.payload, dict) else {}
        # Envelope status is authoritative; a malformed payload can never promote
        # unreviewed/rejected evidence to reviewed.
        if review_status == E.REVIEW_REVIEWED and (
                record.status != E.REVIEW_REVIEWED
                or payload.get("review_status") != E.REVIEW_REVIEWED):
            continue
        if schema_kind is not None and payload.get("schema_kind") != schema_kind:
            continue
        if topic is not None and payload.get("topic") != topic:
            continue
        if claim is not None and payload.get("claim") != claim:
            continue
        visible.append(record)
    if latest_only:
        latest = {}
        for record in visible:
            current = latest.get(record.logical_id)
            if current is None or record.revision > current.revision:
                latest[record.logical_id] = record
        visible = list(latest.values())
    return sorted(visible, key=lambda item: (item.logical_id, item.revision, item.artifact_id))


def link_material(store, artifact_id):
    _get_evidence_record(store, artifact_id)
    return store.link_artifact_to_material(artifact_id, evidence=True)


def unlink_material(store, artifact_id):
    _get_evidence_record(store, artifact_id)
    return store.unlink_artifact_from_material(artifact_id, evidence=True)


def _artifact_rows(records: Iterable) -> list[dict]:
    rows = []
    for record in records:
        if getattr(record, "record_type", None) != RECORD_TYPE:
            raise EvidenceReviewError("exports accept evidence artifacts only")
        payload = _validate_canonical_evidence_payload(
            record.payload,
            project_id=record.project_id,
            material_id=record.material_id,
            review_status=record.status,
        )
        if payload.get("revision") != record.revision:
            raise EvidenceReviewError("evidence payload revision differs from its envelope")
        row = copy.deepcopy(payload)
        row.update({
            "artifact_id": record.artifact_id,
            "logical_id": record.logical_id,
            "artifact_status": record.status,
            "artifact_revision": record.revision,
            "artifact_created_at": record.created_at,
            "artifact_updated_at": record.updated_at,
            "related_run_id": record.related_run_id,
        })
        row["review_status"] = record.status
        if record.status != E.REVIEW_REVIEWED:
            # Defensive: no rejected/unreviewed envelope may export as reviewed.
            row["reviewer"] = record.reviewer or row.get("reviewer")
        evidence_store.validate_structured_payload(row)
        rows.append(row)
    return sorted(rows, key=lambda row: (
        str(row.get("logical_id") or ""), int(row.get("artifact_revision") or 0),
        str(row.get("artifact_id") or "")))


def export_json(records: Iterable) -> str:
    return evidence_store.to_json(_artifact_rows(records))


def export_csv(records: Iterable, schema_kind: str) -> str:
    return evidence_store.to_csv(_artifact_rows(records), schema_kind)


def export_package(records: Iterable, schema_kind: str) -> str:
    rows = _artifact_rows(records)
    return evidence_store.to_package(
        rows, schema_kind,
        metadata={"reviewed_count": sum(
            1 for row in rows if row.get("review_status") == E.REVIEW_REVIEWED)},
    )


# Readable service aliases for UI/application callers.
review_evidence = mark_reviewed
link_evidence_to_material = link_material
unlink_evidence_from_material = unlink_material
