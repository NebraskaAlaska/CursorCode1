"""Durable review workflow around the authoritative Phase 1B ICP processor.

This module owns review lifecycle and provenance, not scientific QC.  Every
correction is applied through :func:`icp_processor.resolve_reviewable_issue`,
and every saved/reloaded/finalized result is reproduced by
:func:`icp_processor.process`.  It never reconstructs concentration, censoring,
duplicate, residual, or validation-eligibility rules.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
from typing import Any, Iterable, Mapping, Sequence

from .. import workspace_store as workspace_records
from . import icp_processor as icp


ICP_REVIEW_SCHEMA_VERSION = 1
ICP_REVIEW_RECORD_TYPE = "icp_review"
ICP_MACHINE_ID = "icp_data_processor"
ICP_QC_CONTRACT_VERSION = icp.QC_CONTRACT_VERSION
ICP_REVIEW_STATUSES = ("draft", "needs_review", "finalized", "superseded")
_EDITABLE_STATUSES = frozenset({"draft", "needs_review"})
_IMMUTABLE_STATUSES = frozenset({"finalized", "superseded", "reviewed", "rejected"})
MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_SOURCE_ROWS = 100_000
_FINALIZATION_PROTOCOL = "phase3_icp_finalization_v1"


class IcpReviewError(ValueError):
    """Base class for a controlled ICP review failure."""


class IcpSourceMismatchError(IcpReviewError):
    """A saved resolution was offered a different source file/table."""


class IcpReviewStateError(IcpReviewError):
    """The review lifecycle or durable envelope is inconsistent."""


class IcpFinalizationError(IcpReviewStateError):
    """Explicit finalization requirements are not satisfied."""


@dataclass(frozen=True)
class IcpReviewFinalization:
    """The immutable artifact and optional linked RunRecord created together."""

    artifact: Any
    run: Any | None
    result: icp.IcpResult


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp(value: Any | None) -> str:
    if value is None:
        return _utc_now()
    text = str(value).strip()
    if not text:
        raise IcpReviewError("resolution timestamp must be explicit and non-blank")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IcpReviewError(f"invalid resolution timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IcpReviewError("resolution timestamp must include a timezone")
    return text


def _json_safe(value: Any, *, path: str = "value") -> Any:
    """Return deterministic JSON-safe evidence without hiding non-finite tokens."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise IcpReviewError(f"{path} contains a non-string field name")
            out[key] = _json_safe(child, path=f"{path}.{key}")
        return out
    if isinstance(value, (list, tuple)):
        return [_json_safe(child, path=f"{path}[{index}]")
                for index, child in enumerate(value)]
    # Pandas/numpy scalars expose item(); accept the scalar without importing either
    # dependency into this backend.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            converted = item()
        except Exception as exc:  # pragma: no cover - exotic third-party scalar
            raise IcpReviewError(f"{path} is not JSON-safe") from exc
        if converted is not value:
            return _json_safe(converted, path=path)
    raise IcpReviewError(f"{path} is not JSON-safe: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise IcpReviewError(f"ICP review evidence is not finite JSON-safe data: {exc}") from exc


def deterministic_hash(value: Any) -> str:
    """SHA-256 identity for an already JSON-safe deterministic value."""
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalise_original_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    raw_rows = list(rows or [])
    if not raw_rows:
        raise IcpReviewError("at least one supplied ICP row is required")
    if len(raw_rows) > MAX_SOURCE_ROWS:
        raise IcpReviewError(f"ICP source exceeds the {MAX_SOURCE_ROWS:,}-row safety limit")
    normalised: list[dict] = []
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, Mapping):
            raise IcpReviewError(f"ICP source row {index} must be a mapping")
        row = _json_safe(dict(raw), path=f"rows[{index}]")
        if row.get("qc_resolutions"):
            raise IcpReviewError(
                "supplied rows may not embed qc_resolutions; pass corrections through the "
                "durable review API so provenance is validated"
            )
        normalised.append(row)
    return normalised


def rows_sha256(rows: Iterable[Mapping[str, Any]]) -> str:
    """Hash exact deterministic row content and order after safe scalar normalization."""
    return deterministic_hash(_normalise_original_rows(rows))


def source_identity(
    rows: Iterable[Mapping[str, Any]], *, source_bytes: bytes | bytearray | memoryview | None = None,
    source_filename: str | None = None, source_import_id: str | None = None,
) -> dict:
    """Build an exact source identity from supplied bytes or deterministic rows.

    Byte-backed imports retain the exact byte hash as the primary identity and a
    second deterministic-row hash for parser/replay integrity.  Manually entered
    rows use the deterministic-row hash as their primary identity.
    """
    original_rows = _normalise_original_rows(rows)
    row_hash = deterministic_hash(original_rows)
    filename = ""
    if source_filename:
        raw_name = str(source_filename).replace("\x00", "").strip()
        # Treat both POSIX and Windows separators as path metadata and retain only
        # the display filename; this module never opens caller-supplied paths.
        filename = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
        if filename in {".", ".."}:
            filename = ""
        if filename and not filename.lower().endswith(".csv"):
            raise IcpReviewError(
                "ICP source_filename must end with .csv; deceptive extensions are refused")
    identity: dict[str, Any] = {
        "identity_kind": "deterministic_rows",
        "sha256": row_hash,
        "source_sha256": row_hash,
        "rows_sha256": row_hash,
        "row_count": len(original_rows),
        "source_filename": filename or None,
        "source_import_id": str(source_import_id).strip() if source_import_id else None,
    }
    if source_bytes is not None:
        if not isinstance(source_bytes, (bytes, bytearray, memoryview)):
            raise IcpReviewError("source_bytes must be bytes-like")
        exact = bytes(source_bytes)
        if len(exact) > MAX_SOURCE_BYTES:
            raise IcpReviewError(
                f"ICP source exceeds the {MAX_SOURCE_BYTES // (1024 * 1024)} MiB safety limit")
        identity.update({
            "identity_kind": "supplied_bytes",
            "sha256": hashlib.sha256(exact).hexdigest(),
            "source_sha256": hashlib.sha256(exact).hexdigest(),
            "byte_count": len(exact),
        })
    return identity


def _validated_source_identity(value: Any) -> dict:
    identity = deepcopy(dict(value or {})) if isinstance(value, Mapping) else {}
    kind = identity.get("identity_kind")
    if kind not in {"deterministic_rows", "supplied_bytes"}:
        raise IcpSourceMismatchError("saved ICP source identity kind is missing or unsupported")
    for field in ("sha256", "rows_sha256"):
        digest = identity.get(field)
        if not isinstance(digest, str) or len(digest) != 64 \
                or any(character not in "0123456789abcdef" for character in digest):
            raise IcpSourceMismatchError(f"saved ICP source {field} is malformed")
    if identity.get("source_sha256") != identity.get("sha256"):
        raise IcpSourceMismatchError("saved ICP source SHA-256 aliases are inconsistent")
    row_count = identity.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 1:
        raise IcpSourceMismatchError("saved ICP source row count is malformed")
    if kind == "deterministic_rows" and identity["sha256"] != identity["rows_sha256"]:
        raise IcpSourceMismatchError("saved deterministic-row source identity is inconsistent")
    if kind == "supplied_bytes":
        byte_count = identity.get("byte_count")
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise IcpSourceMismatchError("saved ICP source byte count is malformed")
    return identity


def _assign_row_ids(original_rows: Sequence[dict]) -> tuple[list[dict], list[dict]]:
    # Calling the processor is intentional: row identity and any future processor
    # behavior stay aligned with the existing Phase 1B authority.
    identified = icp.process(deepcopy(list(original_rows))).corrected
    if len(identified) != len(original_rows):  # pragma: no cover - processor contract guard
        raise IcpReviewStateError("ICP processor changed row cardinality unexpectedly")
    review_rows: list[dict] = []
    assignments: list[dict] = []
    seen: set[str] = set()
    for index, (source_row, processed_row) in enumerate(zip(original_rows, identified)):
        row_id = str(processed_row.row_id or "").strip()
        if not row_id or row_id in seen:
            raise IcpReviewError(f"ICP row IDs must be unique; duplicate {row_id!r}")
        seen.add(row_id)
        review_row = deepcopy(source_row)
        review_row["row_id"] = row_id
        review_rows.append(review_row)
        assignments.append({"row_index": index, "row_id": row_id})
    return review_rows, assignments


def _rows_from_assignments(original_rows: Sequence[dict], assignments: Sequence[Mapping[str, Any]]) -> list[dict]:
    if len(original_rows) != len(assignments):
        raise IcpReviewStateError("row-ID assignments do not match the original row count")
    review_rows: list[dict] = []
    seen: set[str] = set()
    for index, (source_row, assignment) in enumerate(zip(original_rows, assignments)):
        if int(assignment.get("row_index", -1)) != index:
            raise IcpReviewStateError("row-ID assignment order is malformed")
        row_id = str(assignment.get("row_id") or "").strip()
        supplied_id = str(source_row.get("row_id") or "").strip()
        if not row_id or row_id in seen or (supplied_id and supplied_id != row_id):
            raise IcpReviewStateError("row-ID assignments are missing, duplicated, or inconsistent")
        seen.add(row_id)
        row = deepcopy(source_row)
        row["row_id"] = row_id
        review_rows.append(row)
    return review_rows


def _apply_corrections(
    review_rows: Sequence[dict], corrections: Iterable[Mapping[str, Any]],
) -> tuple[list[dict], list[dict]]:
    order = [str(row["row_id"]) for row in review_rows]
    by_id = {str(row["row_id"]): deepcopy(row) for row in review_rows}
    applied: list[dict] = []
    for index, raw in enumerate(corrections or []):
        if not isinstance(raw, Mapping):
            raise IcpReviewError(f"correction {index} must be a mapping")
        correction = _json_safe(dict(raw), path=f"corrections[{index}]")
        row_id = str(correction.get("row_id") or "").strip()
        if row_id not in by_id:
            raise IcpReviewError(f"correction targets unknown row_id {row_id!r}")
        resolved_at = _timestamp(correction.get("resolved_at"))
        updated = icp.resolve_reviewable_issue(
            by_id[row_id], field=str(correction.get("field") or ""),
            replacement_value=correction.get("replacement_value"),
            resolved_by=str(correction.get("resolved_by") or ""),
            reason=str(correction.get("reason") or ""), resolved_at=resolved_at,
        )
        actual = dict(updated["qc_resolutions"][-1])
        if "original_value" in correction \
                and correction.get("original_value") != actual.get("original_value"):
            raise IcpReviewStateError(
                f"correction provenance for {row_id!r} does not match the original value")
        by_id[row_id] = updated
        applied.append({"row_id": row_id, **actual})
    return [by_id[row_id] for row_id in order], applied


def _candidate_snapshot(row: icp.CorrectedRow) -> dict:
    return {
        "row_id": row.row_id,
        "sample_id": row.sample_id,
        "element": row.element,
        "role": row.role,
        "supplied_concentration": row.supplied_concentration,
        "supplied_unit": row.supplied_unit,
        "supplied_dilution_factor": row.supplied_dilution_factor,
        "value_mM": row.value_mM,
        "qc_status_before_selection": row.qc_status,
        "qc_codes_before_selection": list(row.qc_codes),
    }


def _duplicate_groups(
    corrected_rows: Sequence[dict],
) -> dict[tuple[str, str, str], list[icp.CorrectedRow]]:
    preliminary = icp.process(deepcopy(list(corrected_rows)))
    groups: dict[tuple[str, str, str], list[icp.CorrectedRow]] = {}
    for row in preliminary.corrected:
        if row.role in (icp.MEASURED, icp.PREDICTED):
            groups.setdefault((row.sample_id, row.element, row.role), []).append(row)
    return {key: candidates for key, candidates in groups.items() if len(candidates) > 1}


def _duplicate_candidate_sets(corrected_rows: Sequence[dict]) -> list[dict]:
    return [{
        "sample_id": key[0],
        "element": key[1],
        "role": key[2],
        "candidate_row_ids": [row.row_id for row in candidates],
        "candidates": [_candidate_snapshot(row) for row in candidates],
    } for key, candidates in _duplicate_groups(corrected_rows).items()]


def _enrich_duplicate_selections(
    corrected_rows: Sequence[dict], selections: Iterable[Mapping[str, Any]],
) -> list[dict]:
    groups = _duplicate_groups(corrected_rows)

    enriched: list[dict] = []
    for index, raw in enumerate(selections or []):
        if not isinstance(raw, Mapping):
            raise IcpReviewError(f"duplicate selection {index} must be a mapping")
        selection = _json_safe(dict(raw), path=f"duplicate_selections[{index}]")
        key = (
            str(selection.get("sample_id") or "").strip(),
            icp.canonical_element(selection.get("element"))
            or str(selection.get("element") or "").strip(),
            icp.canonical_role(selection.get("role")),
        )
        candidates = groups.get(key, [])
        if len(candidates) < 2:
            raise IcpReviewError(f"duplicate selection does not identify a duplicate candidate set: {key!r}")
        candidate_ids = [row.row_id for row in candidates]
        selected_id = str(selection.get("selected_row_id") or "").strip()
        if selected_id not in candidate_ids:
            raise IcpReviewError(
                f"selected_row_id {selected_id!r} is not among duplicate candidates {candidate_ids!r}")
        resolver = str(selection.get("resolved_by") or "").strip()
        reason = str(selection.get("reason") or "").strip()
        if not resolver or not reason:
            raise IcpReviewError("duplicate resolution requires resolved_by and reason")
        actual_candidates = [_candidate_snapshot(row) for row in candidates]
        if "candidate_row_ids" in selection \
                and list(selection.get("candidate_row_ids") or []) != candidate_ids:
            raise IcpReviewStateError("saved duplicate candidate IDs no longer match processor output")
        if "candidates" in selection and selection.get("candidates") != actual_candidates:
            raise IcpReviewStateError("saved duplicate candidate provenance no longer matches processor output")
        enriched.append({
            "sample_id": key[0], "element": key[1], "role": key[2],
            "candidate_row_ids": candidate_ids,
            "candidates": actual_candidates,
            "selected_row_id": selected_id,
            "resolved_by": resolver,
            "reason": reason,
            "resolved_at": _timestamp(selection.get("resolved_at")),
        })
    return enriched


def _result_snapshot(result: icp.IcpResult) -> dict:
    return _json_safe({
        "corrected": result.corrected_table(),
        "residuals": result.residual_table(),
        "qc_summary": result.qc_summary(),
        "warnings": list(result.warnings),
        "resolution_provenance": deepcopy(result.resolution_provenance),
        "explanation": result.explanation,
    }, path="processed")


def _reviewable_issues(result: icp.IcpResult) -> list[dict]:
    return [{
        "row_id": row.row_id,
        "qc_status": row.qc_status,
        "qc_codes": list(row.qc_codes),
        "qc_reasons": list(row.qc_reasons),
    } for row in result.corrected if row.qc_status == icp.QC_REVIEW_REQUIRED]


def prepare_icp_review(
    *, project_id: str, material_id: str, rows: Iterable[Mapping[str, Any]], creator: str,
    source_bytes: bytes | bytearray | memoryview | None = None,
    source_filename: str | None = None, source_import_id: str | None = None,
    corrections: Iterable[Mapping[str, Any]] = (),
    duplicate_selections: Iterable[Mapping[str, Any]] = (), apply_blank: bool = True,
    status: str = "draft", reason: str = "", provenance: Mapping[str, Any] | None = None,
) -> dict:
    """Prepare a deterministic, store-ready ICP review without writing state."""
    if not str(project_id or "").strip() or not str(material_id or "").strip():
        raise IcpReviewError("project_id and material_id are required")
    creator = str(creator or "").strip()
    if not creator:
        raise IcpReviewError("creator is required")
    if status not in {"draft", "needs_review"}:
        raise IcpReviewStateError("a prepared review must be draft or needs_review")

    original_rows = _normalise_original_rows(rows)
    identity = source_identity(
        original_rows, source_bytes=source_bytes, source_filename=source_filename,
        source_import_id=source_import_id,
    )
    review_rows, assignments = _assign_row_ids(original_rows)
    corrected_rows, applied_corrections = _apply_corrections(review_rows, corrections)
    candidate_sets = _duplicate_candidate_sets(corrected_rows)
    selections = _enrich_duplicate_selections(corrected_rows, duplicate_selections)
    result = icp.process(
        deepcopy(corrected_rows), apply_blank=bool(apply_blank),
        duplicate_resolutions=deepcopy(selections),
    )
    processed = _result_snapshot(result)
    review_rows_hash = deterministic_hash(review_rows)
    payload = {
        "schema_version": ICP_REVIEW_SCHEMA_VERSION,
        "qc_contract_version": ICP_QC_CONTRACT_VERSION,
        "original_rows": original_rows,
        "row_id_assignments": assignments,
        "review_rows_sha256": review_rows_hash,
        "apply_blank": bool(apply_blank),
        "corrections": applied_corrections,
        "duplicate_candidate_sets": candidate_sets,
        "duplicate_selections": selections,
        "reviewable_issues": _reviewable_issues(result),
        "processed": processed,
        "processed_output_hash": deterministic_hash(processed),
    }
    input_snapshot = {
        "schema_version": ICP_REVIEW_SCHEMA_VERSION,
        "record_type": ICP_REVIEW_RECORD_TYPE,
        "qc_contract_version": ICP_QC_CONTRACT_VERSION,
        "source_identity": identity,
        "original_rows": original_rows,
        "row_id_assignments": assignments,
        "review_rows_sha256": review_rows_hash,
        "apply_blank": bool(apply_blank),
        "corrections": applied_corrections,
        "duplicate_selections": selections,
    }
    supplied_provenance = _json_safe(dict(provenance or {}), path="provenance")
    supplied_provenance.update({
        "processor_module": "flyash_phreeqc_ml.instruments.icp_processor",
        "qc_contract_version": ICP_QC_CONTRACT_VERSION,
        "review_schema_version": ICP_REVIEW_SCHEMA_VERSION,
    })
    return {
        "project_id": str(project_id),
        "material_id": str(material_id),
        "record_type": ICP_REVIEW_RECORD_TYPE,
        "status": status,
        "input_snapshot": input_snapshot,
        "source_identity": identity,
        "payload": payload,
        "creator": creator,
        "reason": str(reason or "").strip(),
        "provenance": supplied_provenance,
    }


def _record_dict(record: Any) -> dict:
    if isinstance(record, Mapping):
        return deepcopy(dict(record))
    to_dict = getattr(record, "to_dict", None)
    if callable(to_dict):
        return deepcopy(dict(to_dict()))
    if hasattr(record, "__dict__"):
        return deepcopy(vars(record))
    raise IcpReviewStateError("durable ICP review record has an unsupported shape")


def _assert_icp_record(record: Any, *, project_id: str | None = None,
                       material_id: str | None = None) -> dict:
    data = _record_dict(record)
    if data.get("record_type") != ICP_REVIEW_RECORD_TYPE:
        raise IcpReviewStateError("artifact is not an ICP review")
    if project_id is not None and data.get("project_id") != project_id:
        raise IcpReviewStateError("ICP review does not belong to the selected project")
    if material_id is not None and data.get("material_id") != material_id:
        raise IcpReviewStateError("ICP review does not belong to the selected material")
    if data.get("status") not in ICP_REVIEW_STATUSES:
        raise IcpReviewStateError(f"unsupported ICP review status: {data.get('status')!r}")
    return data


def _prepared_kwargs(prepared: Mapping[str, Any]) -> dict:
    required = {
        "project_id", "material_id", "record_type", "status", "input_snapshot",
        "source_identity", "payload", "creator", "reason", "provenance",
    }
    missing = required - set(prepared)
    if missing or prepared.get("record_type") != ICP_REVIEW_RECORD_TYPE:
        raise IcpReviewStateError(f"malformed prepared ICP review; missing {sorted(missing)}")
    return {key: deepcopy(prepared[key]) for key in required}


def save_icp_review(store: Any, prepared: Mapping[str, Any], *, artifact_id: str | None = None) -> Any:
    """Create/update a draft; editing an immutable review creates a new revision."""
    kwargs = _prepared_kwargs(prepared)
    if artifact_id is None:
        return store.create_artifact(**kwargs)
    current = store.get_artifact(artifact_id)
    current_data = _assert_icp_record(
        current, project_id=str(kwargs["project_id"]), material_id=str(kwargs["material_id"]),
    )
    changes = {key: value for key, value in kwargs.items()
               if key not in {"project_id", "material_id", "record_type"}}
    if current_data["status"] in _IMMUTABLE_STATUSES:
        return store.create_artifact_revision(artifact_id, **changes)
    if current_data["status"] not in _EDITABLE_STATUSES:
        raise IcpReviewStateError(f"ICP review status {current_data['status']!r} is not editable")
    # Creator is immutable within one artifact revision; a revision records its own
    # creator through create_artifact_revision above.
    changes.pop("creator", None)
    return store.update_artifact(
        artifact_id,
        expected_payload_hash=current_data.get("payload_hash"),
        expected_input_hash=current_data.get("input_hash"),
        expected_status=current_data.get("status"),
        **changes,
    )


def load_icp_review(store: Any, artifact_id: str, *, project_id: str | None = None,
                    material_id: str | None = None) -> Any:
    record = store.get_artifact(artifact_id)
    _assert_icp_record(record, project_id=project_id, material_id=material_id)
    return record


def list_icp_reviews(store: Any, *, project_id: str, material_id: str | None = None) -> list[Any]:
    records = store.list_artifacts(
        project_id=project_id, material_id=material_id, record_type=ICP_REVIEW_RECORD_TYPE)
    return [record for record in records
            if _assert_icp_record(record, project_id=project_id,
                                  material_id=material_id).get("record_type")
            == ICP_REVIEW_RECORD_TYPE]


def reprocess_icp_review(record: Any) -> icp.IcpResult:
    """Reproduce a saved review from its immutable original rows and resolutions."""
    data = _assert_icp_record(record)
    payload = deepcopy(dict(data.get("payload") or {}))
    if payload.get("schema_version") != ICP_REVIEW_SCHEMA_VERSION:
        raise IcpReviewStateError("unsupported ICP review payload schema")
    if payload.get("qc_contract_version") != ICP_QC_CONTRACT_VERSION:
        raise IcpReviewStateError(
            "saved ICP review uses a different processor/QC contract; migrate it explicitly")
    original_rows = _normalise_original_rows(payload.get("original_rows") or [])
    identity = _validated_source_identity(data.get("source_identity"))
    if deterministic_hash(original_rows) != identity.get("rows_sha256"):
        raise IcpSourceMismatchError("saved original ICP rows no longer match their source identity")
    if len(original_rows) != identity.get("row_count"):
        raise IcpSourceMismatchError("saved original ICP row count no longer matches its identity")
    review_rows = _rows_from_assignments(original_rows, payload.get("row_id_assignments") or [])
    if deterministic_hash(review_rows) != payload.get("review_rows_sha256"):
        raise IcpSourceMismatchError("saved ICP row-ID snapshot no longer matches its identity")
    corrected_rows, applied = _apply_corrections(review_rows, payload.get("corrections") or [])
    if applied != payload.get("corrections"):
        raise IcpReviewStateError("saved ICP correction provenance is not reproducible")
    candidate_sets = _duplicate_candidate_sets(corrected_rows)
    if candidate_sets != payload.get("duplicate_candidate_sets"):
        raise IcpReviewStateError("saved ICP duplicate candidate provenance is not reproducible")
    selections = _enrich_duplicate_selections(
        corrected_rows, payload.get("duplicate_selections") or [])
    if selections != payload.get("duplicate_selections"):
        raise IcpReviewStateError("saved ICP duplicate provenance is not reproducible")
    result = icp.process(
        corrected_rows, apply_blank=bool(payload.get("apply_blank", True)),
        duplicate_resolutions=selections,
    )
    processed = _result_snapshot(result)
    if deterministic_hash(processed) != payload.get("processed_output_hash") \
            or processed != payload.get("processed"):
        raise IcpReviewStateError("saved ICP processed output is not reproducible")
    input_snapshot = data.get("input_snapshot")
    if isinstance(input_snapshot, Mapping):
        expected_input = {
            "schema_version": ICP_REVIEW_SCHEMA_VERSION,
            "record_type": ICP_REVIEW_RECORD_TYPE,
            "qc_contract_version": ICP_QC_CONTRACT_VERSION,
            "source_identity": identity,
            "original_rows": original_rows,
            "row_id_assignments": payload.get("row_id_assignments"),
            "review_rows_sha256": payload.get("review_rows_sha256"),
            "apply_blank": bool(payload.get("apply_blank", True)),
            "corrections": payload.get("corrections"),
            "duplicate_selections": payload.get("duplicate_selections"),
        }
        if dict(input_snapshot) != expected_input:
            raise IcpReviewStateError("artifact input snapshot does not match its ICP review payload")
        if data.get("input_hash") and deterministic_hash(input_snapshot) != data.get("input_hash"):
            raise IcpReviewStateError("artifact input hash does not match its ICP snapshot")
    if data.get("payload_hash") and deterministic_hash(payload) != data.get("payload_hash"):
        raise IcpReviewStateError("artifact payload hash does not match its ICP payload")
    return result


def assert_icp_source_matches(
    record: Any, *, rows: Iterable[Mapping[str, Any]] | None = None,
    source_bytes: bytes | bytearray | memoryview | None = None,
) -> None:
    """Require the current external source, never a caller-supplied hash, to match."""
    data = _assert_icp_record(record)
    identity = _validated_source_identity(data.get("source_identity"))
    kind = identity.get("identity_kind")
    if kind == "supplied_bytes":
        if source_bytes is None:
            raise IcpSourceMismatchError("exact source bytes are required for this file-backed review")
        exact = bytes(source_bytes)
        if len(exact) != identity.get("byte_count") \
                or hashlib.sha256(exact).hexdigest() != identity.get("sha256"):
            raise IcpSourceMismatchError("current ICP source bytes do not match the saved source")
        if rows is not None and rows_sha256(rows) != identity.get("rows_sha256"):
            raise IcpSourceMismatchError("current parsed ICP rows do not match the saved source rows")
        return
    if kind == "deterministic_rows":
        if rows is None:
            raise IcpSourceMismatchError("exact source rows are required for this manually entered review")
        if rows_sha256(rows) != identity.get("sha256"):
            raise IcpSourceMismatchError("current ICP source rows do not match the saved source")
        return
    raise IcpSourceMismatchError("saved ICP source identity kind is missing or unsupported")


def _finalization_blockers(result: icp.IcpResult) -> list[dict]:
    blockers: list[dict] = []
    for row in result.corrected:
        if row.qc_status != icp.QC_REVIEW_REQUIRED:
            continue
        # Explicitly unselected duplicate candidates remain in the processor table
        # as review-required/ineligible evidence.  Their candidate-set resolution is
        # complete, so that one processor-authored state does not block snapshot
        # finalization; it still cannot enter validation.
        non_selection_codes = [code for code in row.qc_codes
                               if code not in {icp.QC_DUPLICATE_NOT_SELECTED, icp.QC_RESOLVED}]
        if non_selection_codes:
            blockers.append({
                "row_id": row.row_id,
                "qc_codes": non_selection_codes,
                "qc_reasons": list(row.qc_reasons),
            })
    return blockers


def _finalization_run_spec(data: Mapping[str, Any], result: icp.IcpResult) -> dict:
    """Build the exact retry identity and create arguments for an ICP finalization run."""
    artifact_id = str(data.get("artifact_id") or "")
    run_snapshot = deepcopy(dict(data.get("input_snapshot") or {}))
    run_snapshot.update({
        "finalization_protocol": _FINALIZATION_PROTOCOL,
        "artifact_id": artifact_id,
        "artifact_revision": data.get("revision"),
        "processed_output_hash": (data.get("payload") or {}).get("processed_output_hash"),
        "artifact_identities": [{
            "artifact_id": artifact_id,
            "revision": data.get("revision"),
            "input_hash": data.get("input_hash"),
            "payload_hash": data.get("payload_hash"),
        }],
    })
    result_data = {
        "artifact_id": artifact_id,
        "artifact_revision": data.get("revision"),
        "artifact_input_hash": data.get("input_hash"),
        "artifact_payload_hash": data.get("payload_hash"),
        "source_identity": deepcopy(data.get("source_identity") or {}),
        "processed_output_hash": (data.get("payload") or {}).get("processed_output_hash"),
        **_result_snapshot(result),
    }
    retry_identity = {
        "protocol": _FINALIZATION_PROTOCOL,
        "artifact_id": artifact_id,
        "artifact_revision": data.get("revision"),
        "artifact_input_hash": data.get("input_hash"),
        "artifact_payload_hash": data.get("payload_hash"),
    }
    return {
        "run_id": "run_" + deterministic_hash(retry_identity)[:32],
        "project_id": data.get("project_id"),
        "material_id": data.get("material_id"),
        "machine_id": ICP_MACHINE_ID,
        "input_snapshot": run_snapshot,
        "status": "completed",
        "output_type": ICP_REVIEW_RECORD_TYPE,
        # A finalized review can retain both measured and predicted rows; the
        # row-level roles remain explicit, while the mixed top-level result is
        # never promoted to measured truth.
        "epistemic_type": "advisory_interpretation",
        "result_data": result_data,
        "warnings": list(result.warnings),
        "validation_state": "finalized_qc_review; only processor-eligible rows may be used",
    }


def _assert_finalization_run_matches(run: Any, spec: Mapping[str, Any]) -> None:
    """Refuse to repair through a run that is not the exact expected ICP snapshot."""
    data = _record_dict(run)
    expected = {
        key: deepcopy(spec[key]) for key in (
            "run_id", "project_id", "material_id", "machine_id", "input_snapshot",
            "status", "output_type", "epistemic_type", "result_data", "warnings",
            "validation_state",
        )
    }
    actual = {key: deepcopy(data.get(key)) for key in expected}
    if actual != expected:
        raise IcpFinalizationError(
            "existing ICP finalization run does not match the exact artifact identity")


def _get_or_create_finalization_run(store: Any, spec: Mapping[str, Any]) -> Any:
    """Create one deterministic run or reuse it after an interrupted finalization."""
    try:
        run = store.get_run(spec["run_id"])
    except workspace_records.RecordNotFoundError:
        create_kwargs = {key: deepcopy(value) for key, value in spec.items()
                         if key not in {"project_id", "material_id", "machine_id",
                                        "input_snapshot"}}
        try:
            run = store.create_run(
                spec["project_id"], spec["material_id"], spec["machine_id"],
                deepcopy(spec["input_snapshot"]), **create_kwargs,
            )
        except workspace_records.DuplicateRecordError:
            # A concurrent retry may have won the create-only write.  Re-read and
            # verify the exact record rather than creating a second run.
            run = store.get_run(spec["run_id"])
    _assert_finalization_run_matches(run, spec)
    return run


def finalize_icp_review(
    store: Any, artifact_id: str, *, confirmation: str, finalized_by: str, reason: str,
    rows: Iterable[Mapping[str, Any]] | None = None,
    source_bytes: bytes | bytearray | memoryview | None = None,
    create_run: bool = True,
) -> IcpReviewFinalization:
    """Explicitly finalize one draft and optionally create its immutable RunRecord."""
    if confirmation != artifact_id:
        raise IcpFinalizationError("finalization confirmation must exactly match the artifact ID")
    reviewer = str(finalized_by or "").strip()
    final_reason = str(reason or "").strip()
    if not reviewer or not final_reason:
        raise IcpFinalizationError("finalized_by and reason are required")
    record = store.get_artifact(artifact_id)
    data = _assert_icp_record(record)
    result = reprocess_icp_review(record)
    # Finalization always binds to a caller-supplied current source.  For a manual
    # review this means deterministic rows; for a file-backed review both exact
    # bytes and their freshly parsed rows are required.
    assert_icp_source_matches(record, rows=rows, source_bytes=source_bytes)
    blockers = _finalization_blockers(result)
    if blockers:
        codes = sorted({code for item in blockers for code in item["qc_codes"]})
        raise IcpFinalizationError(
            "review-required ICP metadata or conflicting duplicate selections remain: "
            + ", ".join(codes))

    run = None
    run_spec = _finalization_run_spec(data, result) if create_run else None
    if data.get("status") == "finalized":
        if record.reviewer != reviewer or record.reason != final_reason:
            raise IcpFinalizationError(
                "finalized ICP review retry metadata does not match the immutable record")
        if create_run:
            if not record.related_run_id:
                raise IcpFinalizationError(
                    "finalized ICP review has no linked run and cannot be repaired in place")
            run = store.get_run(record.related_run_id)
            _assert_finalization_run_matches(run, run_spec)
        return IcpReviewFinalization(artifact=record, run=run, result=result)
    if data.get("status") not in _EDITABLE_STATUSES:
        raise IcpFinalizationError("only a draft or needs-review ICP record can be finalized")
    if create_run:
        run = _get_or_create_finalization_run(store, run_spec)
    run_data = _record_dict(run) if run is not None else {}
    try:
        finalized = store.update_artifact(
            artifact_id,
            expected_payload_hash=data.get("payload_hash"),
            expected_input_hash=data.get("input_hash"),
            expected_status=data.get("status"),
            status="finalized", reviewer=reviewer, reason=final_reason,
            reviewed_at=_utc_now(), related_run_id=run_data.get("run_id"),
        )
    except workspace_records.WorkspaceStoreError:
        # A write may have reached disk before an I/O interruption surfaced.  If
        # the exact immutable state is already present, return it idempotently;
        # otherwise propagate so a later retry can reuse the deterministic run.
        latest = store.get_artifact(artifact_id)
        if latest.status == "finalized" and latest.reviewer == reviewer \
                and latest.reason == final_reason \
                and latest.related_run_id == run_data.get("run_id") \
                and latest.input_hash == data.get("input_hash") \
                and latest.payload_hash == data.get("payload_hash"):
            finalized = latest
        else:
            raise
    return IcpReviewFinalization(artifact=finalized, run=run, result=result)


def finalized_validation_output(
    record: Any, *, rows: Iterable[Mapping[str, Any]] | None = None,
    source_bytes: bytes | bytearray | memoryview | None = None,
) -> dict:
    """Return only processor-authorized rows/residuals for a matching finalized review."""
    data = _assert_icp_record(record)
    if data.get("status") != "finalized":
        raise IcpReviewStateError("only a finalized ICP review may supply validation data")
    assert_icp_source_matches(record, rows=rows, source_bytes=source_bytes)
    result = reprocess_icp_review(record)
    eligible_rows = [row.to_dict() for row in result.corrected if row.validation_eligible]
    return {
        "artifact_id": data.get("artifact_id"),
        "revision": data.get("revision"),
        "source_identity": deepcopy(data.get("source_identity") or {}),
        "processed_output_hash": (data.get("payload") or {}).get("processed_output_hash"),
        "eligible_rows": eligible_rows,
        "residuals": result.residual_table(),
        "excluded_row_count": len(result.corrected) - len(eligible_rows),
        "eligibility_authority": "flyash_phreeqc_ml.instruments.icp_processor",
        "qc_contract_version": ICP_QC_CONTRACT_VERSION,
    }


__all__ = [
    "ICP_MACHINE_ID", "ICP_QC_CONTRACT_VERSION", "ICP_REVIEW_RECORD_TYPE",
    "ICP_REVIEW_SCHEMA_VERSION", "ICP_REVIEW_STATUSES", "IcpFinalizationError",
    "IcpReviewError", "IcpReviewFinalization", "IcpReviewStateError",
    "IcpSourceMismatchError", "assert_icp_source_matches", "deterministic_hash",
    "finalize_icp_review", "finalized_validation_output", "list_icp_reviews",
    "load_icp_review", "prepare_icp_review", "reprocess_icp_review", "rows_sha256",
    "save_icp_review", "source_identity",
]
