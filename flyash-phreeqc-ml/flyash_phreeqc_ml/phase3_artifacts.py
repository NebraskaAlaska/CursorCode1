"""Small Phase 3 persistence coordinators over the existing :mod:`workspace_store`.

This module performs no scientific calculation. Domain modules validate their payloads first;
these helpers only bind an immutable typed artifact to an immutable RunRecord using exact hashes.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import workspace_store as ws
from .literature import evidence_review


@dataclass(frozen=True)
class SavedArtifactRun:
    artifact: ws.ArtifactRecord
    run: ws.RunRecord


ADVISORY_STATUS = "advisory"
ADVISORY_OUTPUT_TYPE = "advisory_interpretation"
ADVISORY_EPISTEMIC_TYPE = "advisory_interpretation"
ADVISORY_VALIDATION_STATE = "not_applicable_advisory"
_ADVISORY_RECORD_MACHINES = {
    ws.ARTIFACT_EXPERIMENT_PLAN: "experimental_design_assistant",
    ws.ARTIFACT_SUSTAINABILITY_SCREEN: "sustainability_cost_screening",
}


def _require_advisory_contract(
    *, record_type: str, machine_id: str, output_type: str, validation_state: str,
) -> None:
    expected_machine = _ADVISORY_RECORD_MACHINES.get(record_type)
    if expected_machine is None:
        raise ws.MalformedRecordError(
            f"record type {record_type!r} is not supported by the Phase 3 advisory coordinator")
    if machine_id != expected_machine:
        raise ws.MalformedRecordError(
            f"advisory record type {record_type!r} requires machine {expected_machine!r}")
    if output_type != ADVISORY_OUTPUT_TYPE:
        raise ws.MalformedRecordError(
            f"advisory output_type is fixed as {ADVISORY_OUTPUT_TYPE!r}")
    if validation_state != ADVISORY_VALIDATION_STATE:
        raise ws.MalformedRecordError(
            f"advisory validation_state is fixed as {ADVISORY_VALIDATION_STATE!r}")


def artifact_dependency(record: ws.ArtifactRecord) -> dict:
    return {
        "artifact_id": record.artifact_id,
        "logical_id": record.logical_id,
        "record_type": record.record_type,
        "revision": record.revision,
        "input_hash": record.input_hash,
        "payload_hash": record.payload_hash,
    }


def save_advisory_artifact_run(
    store: ws.WorkspaceStore,
    *,
    project_id: str,
    material_id: str,
    record_type: str,
    machine_id: str,
    input_snapshot: dict,
    payload: dict,
    source_identity: dict | None,
    creator: str,
    reason: str,
    output_type: str = ADVISORY_OUTPUT_TYPE,
    warnings: list[str] | tuple[str, ...] = (),
    validation_state: str = ADVISORY_VALIDATION_STATE,
    provenance: dict | None = None,
    evidence_identity: list[dict] | tuple[dict, ...] = (),
    dependency_artifacts: list[ws.ArtifactRecord] | tuple[ws.ArtifactRecord, ...] = (),
) -> SavedArtifactRun:
    """Persist one validated advisory payload and exact linked run.

    The artifact begins editable only long enough to allocate the RunRecord. Its scientific payload
    and hashes do not change during finalization; after the run link is attached it becomes immutable.
    """
    actor = str(creator or "").strip()
    why = str(reason or "").strip()
    if not actor or not why:
        raise ws.MalformedRecordError("creator and save reason are required")
    _require_advisory_contract(
        record_type=record_type,
        machine_id=machine_id,
        output_type=output_type,
        validation_state=validation_state,
    )
    scientific_input = dict(input_snapshot)
    scientific_payload = dict(payload)
    source = dict(source_identity or {})
    run_warnings = list(warnings)
    run_evidence = list(evidence_identity)
    external_dependencies = []
    for supplied in dependency_artifacts:
        record = store.get_artifact(supplied.artifact_id)
        if record.project_id != project_id or record.material_id != material_id:
            raise ws.WorkspaceStoreError(
                "advisory dependency belongs to another project/material context")
        if (record.revision != supplied.revision
                or record.input_hash != supplied.input_hash
                or record.payload_hash != supplied.payload_hash):
            raise ws.WorkspaceStoreError("advisory dependency changed since it was loaded")
        external_dependencies.append(artifact_dependency(record))

    supplied_provenance = dict(provenance or {})
    operation_identity = {
        "contract": "phase3_advisory_save_v2",
        "project_id": project_id,
        "material_id": material_id,
        "record_type": record_type,
        "machine_id": machine_id,
        "input_hash": ws.identity_hash(scientific_input),
        "payload_hash": ws.identity_hash(scientific_payload),
        "source_hash": ws.identity_hash(source),
        "creator": actor,
        "reason": why,
        "output_type": output_type,
        "warnings_hash": ws.identity_hash(run_warnings),
        "validation_state": validation_state,
        "provenance_hash": ws.identity_hash(supplied_provenance),
        "evidence_hash": ws.identity_hash(run_evidence),
        "external_dependencies": external_dependencies,
    }
    operation_hash = ws.identity_hash(operation_identity)
    artifact_id = f"{ws.ARTIFACT_PREFIX}{operation_hash[:32]}"
    run_identity = ws.identity_hash({"operation_hash": operation_hash, "kind": "run"})
    run_id = f"{ws.RUN_PREFIX}{run_identity[:32]}"
    stored_provenance = dict(supplied_provenance)
    stored_provenance["persistence_identity"] = {
        "contract": "phase3_advisory_save_v2",
        "operation_hash": operation_hash,
    }

    try:
        artifact = store.get_artifact(artifact_id)
    except ws.RecordNotFoundError:
        try:
            artifact = store.create_artifact(
                project_id,
                material_id,
                record_type,
                scientific_input,
                status="draft",
                payload=scientific_payload,
                source_identity=source,
                creator=actor,
                reason=why,
                provenance=stored_provenance,
                artifact_id=artifact_id,
            )
        except ws.DuplicateRecordError:
            # A concurrent writer won the atomic create. Reopen and validate it.
            artifact = store.get_artifact(artifact_id)

    expected_persistence = stored_provenance["persistence_identity"]
    if (artifact.project_id != project_id or artifact.material_id != material_id
            or artifact.record_type != record_type
            or artifact.input_snapshot != scientific_input
            or artifact.payload != scientific_payload
            or artifact.source_identity != source
            or artifact.creator != actor or artifact.reason != why
            or artifact.provenance != stored_provenance
            or artifact.provenance.get("persistence_identity") != expected_persistence):
        raise ws.DuplicateRecordError(
            "advisory persistence identity collides with a different artifact")
    if artifact.status == "finalized":
        return require_exact_advisory_run(
            store,
            str(artifact.related_run_id or ""),
            project_id=project_id,
            material_id=material_id,
            machine_id=machine_id,
            record_type=record_type,
        )
    if artifact.status != "draft" or artifact.related_run_id is not None:
        raise ws.WorkspaceStoreError("recoverable advisory artifact has an invalid lifecycle state")

    dependency = artifact_dependency(artifact)
    run_input = dict(scientific_input)
    run_input["artifact_identities"] = [dependency, *external_dependencies]
    result_data = {
        "artifact_identity": dependency,
        "artifact_payload": scientific_payload,
    }
    try:
        run = store.get_run(run_id)
    except ws.RecordNotFoundError:
        try:
            run = store.create_run(
                project_id,
                material_id,
                machine_id,
                run_input,
                status=ADVISORY_STATUS,
                output_type=ADVISORY_OUTPUT_TYPE,
                epistemic_type=ADVISORY_EPISTEMIC_TYPE,
                result_data=result_data,
                warnings=run_warnings,
                validation_state=ADVISORY_VALIDATION_STATE,
                evidence_identity=run_evidence,
                legacy_references=[{
                    "record_type": record_type,
                    "artifact_id": artifact.artifact_id,
                }],
                run_id=run_id,
            )
        except ws.DuplicateRecordError:
            run = store.get_run(run_id)
    if (run.project_id != project_id or run.material_id != material_id
            or run.machine_id != machine_id or run.input_snapshot != run_input
            or run.status != ADVISORY_STATUS or run.output_type != ADVISORY_OUTPUT_TYPE
            or run.epistemic_type != ADVISORY_EPISTEMIC_TYPE or run.result_data != result_data
            or run.warnings != run_warnings
            or run.validation_state != ADVISORY_VALIDATION_STATE
            or run.evidence_identity != run_evidence):
        raise ws.DuplicateRecordError(
            "advisory persistence identity collides with a different run")
    material = store.get_material(material_id)
    if run.run_id not in material.associated_run_ids:
        store.update_material(
            material_id,
            associated_run_ids=[*material.associated_run_ids, run.run_id],
        )
    store.update_artifact(
        artifact.artifact_id,
        expected_payload_hash=artifact.payload_hash,
        expected_input_hash=artifact.input_hash,
        expected_status="draft",
        status="finalized",
        reviewer=actor,
        reason=why,
        related_run_id=run.run_id,
    )
    return require_exact_advisory_run(
        store,
        run.run_id,
        project_id=project_id,
        material_id=material_id,
        machine_id=machine_id,
        record_type=record_type,
    )


def require_exact_artifact_dependencies(
    store: ws.WorkspaceStore,
    dependencies: list[dict],
    *,
    project_id: str,
    material_id: str,
) -> list[ws.ArtifactRecord]:
    """Reload exact artifact revisions/hashes or fail instead of silently rebinding."""
    loaded = []
    for supplied in dependencies:
        if not isinstance(supplied, dict):
            raise ws.MalformedRecordError("artifact dependency identity must be an object")
        artifact = store.get_artifact(str(supplied.get("artifact_id") or ""))
        if artifact.project_id != project_id or artifact.material_id != material_id:
            raise ws.WorkspaceStoreError("artifact dependency belongs to another context")
        if supplied.get("revision") != artifact.revision:
            raise ws.WorkspaceStoreError("artifact revision changed")
        if supplied.get("input_hash") != artifact.input_hash:
            raise ws.WorkspaceStoreError("artifact input identity changed")
        if supplied.get("payload_hash") != artifact.payload_hash:
            raise ws.WorkspaceStoreError("artifact payload identity changed")
        if supplied.get("logical_id") is not None \
                and supplied.get("logical_id") != artifact.logical_id:
            raise ws.WorkspaceStoreError("artifact logical identity changed")
        if supplied.get("record_type") is not None \
                and supplied.get("record_type") != artifact.record_type:
            raise ws.WorkspaceStoreError("artifact type identity changed")
        loaded.append(artifact)
    return loaded


def require_exact_advisory_run(
    store: ws.WorkspaceStore,
    run_id: str,
    *,
    project_id: str,
    material_id: str,
    machine_id: str,
    record_type: str,
) -> SavedArtifactRun:
    """Fail closed unless a generic Phase 3 run and its primary artifact agree exactly."""
    _require_advisory_contract(
        record_type=record_type,
        machine_id=machine_id,
        output_type=ADVISORY_OUTPUT_TYPE,
        validation_state=ADVISORY_VALIDATION_STATE,
    )
    run = store.get_run(run_id)
    if run.project_id != project_id or run.material_id != material_id:
        raise ws.WorkspaceStoreError("saved run belongs to another active context")
    if run.machine_id != machine_id:
        raise ws.WorkspaceStoreError("saved run belongs to another machine")
    if (run.status != ADVISORY_STATUS
            or run.output_type != ADVISORY_OUTPUT_TYPE
            or run.epistemic_type != ADVISORY_EPISTEMIC_TYPE
            or run.validation_state != ADVISORY_VALIDATION_STATE):
        raise ws.MalformedRecordError(
            "saved advisory run attempts to leave its fixed advisory epistemic contract")
    dependencies = run.input_snapshot.get("artifact_identities")
    if not isinstance(dependencies, list) or not dependencies:
        raise ws.MalformedRecordError("saved advisory run has no exact artifact dependency")
    loaded = require_exact_artifact_dependencies(
        store,
        dependencies,
        project_id=project_id,
        material_id=material_id,
    )
    artifact = loaded[0]
    expected_identity = artifact_dependency(artifact)
    if artifact.record_type != record_type or artifact.status != "finalized":
        raise ws.MalformedRecordError("saved advisory primary artifact type/status is invalid")
    if artifact.related_run_id != run.run_id:
        raise ws.MalformedRecordError("saved advisory artifact does not link back to its run")
    if dependencies[0] != expected_identity:
        raise ws.MalformedRecordError("saved advisory primary dependency identity is incomplete")
    result_identity = run.result_data.get("artifact_identity")
    result_payload = run.result_data.get("artifact_payload")
    if result_identity != expected_identity:
        raise ws.MalformedRecordError("saved advisory result identity differs from its input")
    if not isinstance(result_payload, dict) or result_payload != artifact.payload:
        raise ws.MalformedRecordError("saved advisory result payload differs from its artifact")
    original_input = dict(run.input_snapshot)
    original_input.pop("artifact_identities", None)
    if original_input != artifact.input_snapshot:
        raise ws.MalformedRecordError("saved advisory input differs from its artifact snapshot")
    return SavedArtifactRun(artifact=artifact, run=run)


def save_reviewed_evidence_run(
    store: ws.WorkspaceStore,
    artifact_id: str,
    *,
    machine_id: str,
) -> ws.RunRecord:
    """Create or reconcile the one exact run for a reviewed evidence/material snapshot."""
    try:
        artifact = evidence_review.get_evidence(store, artifact_id)
    except evidence_review.EvidenceReviewError as exc:
        raise ws.MalformedRecordError(
            f"reviewed evidence failed strict domain/envelope validation: {exc}") from exc
    if artifact.record_type != ws.ARTIFACT_EVIDENCE or artifact.status != "reviewed":
        raise ws.WorkspaceStoreError(
            "only explicitly reviewed evidence can create an evidence-linked run")
    if artifact.material_id is None:
        raise ws.WorkspaceStoreError(
            "evidence must be linked to a material before creating a run")
    material = store.get_material(artifact.material_id)
    if artifact.artifact_id not in material.evidence_references:
        raise ws.WorkspaceStoreError(
            "reviewed evidence must be linked to its material before creating a run")
    if machine_id != "literature_evidence_engine":
        raise ws.MalformedRecordError(
            "reviewed evidence runs require the literature_evidence_engine machine")
    dependency = artifact_dependency(artifact)
    citation = dict(artifact.payload.get("provenance") or {})
    location = dict(artifact.payload.get("source_location") or {})
    run_input = {"artifact_identities": [dependency]}
    result_data = {
        "artifact_identity": dependency,
        "evidence_summary": {
            "title": citation.get("title"),
            "doi": citation.get("doi"),
            "year": citation.get("year"),
            "topic": artifact.payload.get("topic"),
            "claim": artifact.payload.get("claim"),
            "source_location": location,
            "review_status": artifact.status,
            "extraction_confidence": artifact.payload.get("extraction_confidence"),
        },
    }
    warnings = [
        "Literature evidence is contextual and does not validate the active material's "
        "composition, measurement, simulation, or performance."
    ]
    evidence_identity = [dependency]
    legacy_references = [{
        "record_type": ws.ARTIFACT_EVIDENCE,
        "artifact_id": artifact.artifact_id,
    }]
    expected = {
        "project_id": artifact.project_id,
        "material_id": artifact.material_id,
        "machine_id": machine_id,
        "input_snapshot": run_input,
        "input_hash": ws.identity_hash(run_input),
        "material_revision": material.revision,
        "composition_revision": material.composition_revision,
        "assumption_revision": material.assumption_revision,
        "material_snapshot_hash": ws.material_identity_hash(material),
        "status": "reviewed_evidence",
        "output_type": "literature_evidence",
        "epistemic_type": "literature_evidence",
        "result_data": result_data,
        "result_hash": ws.identity_hash(result_data),
        "warnings": warnings,
        "validation_state": "not_applicable_literature_context",
        "model_identity": {},
        "environment_identity": {},
        "evidence_identity": evidence_identity,
        "result_location": None,
        "legacy_references": legacy_references,
    }
    operation_identity = {
        "contract": "phase3_reviewed_evidence_run_v2",
        "run_spec": expected,
    }
    run_id = f"{ws.RUN_PREFIX}{ws.identity_hash(operation_identity)[:32]}"

    try:
        run = store.get_run(run_id)
    except ws.RecordNotFoundError:
        try:
            run = store.create_run(
                artifact.project_id,
                artifact.material_id,
                machine_id,
                run_input,
                status=expected["status"],
                output_type=expected["output_type"],
                epistemic_type=expected["epistemic_type"],
                result_data=result_data,
                warnings=warnings,
                validation_state=expected["validation_state"],
                model_identity=expected["model_identity"],
                environment_identity=expected["environment_identity"],
                evidence_identity=evidence_identity,
                result_location=expected["result_location"],
                legacy_references=legacy_references,
                run_id=run_id,
            )
        except ws.DuplicateRecordError:
            # A concurrent exact writer won. Reopen and verify the complete spec.
            run = store.get_run(run_id)

    actual = {name: getattr(run, name) for name in expected}
    if actual != expected:
        mismatches = sorted(name for name in expected if actual.get(name) != expected[name])
        raise ws.DuplicateRecordError(
            "reviewed-evidence run identity collides with a different full run spec: "
            + ", ".join(mismatches))
    current_material = store.get_material(artifact.material_id)
    current_binding = (
        current_material.revision,
        current_material.composition_revision,
        current_material.assumption_revision,
        ws.material_identity_hash(current_material),
    )
    expected_binding = (
        expected["material_revision"],
        expected["composition_revision"],
        expected["assumption_revision"],
        expected["material_snapshot_hash"],
    )
    if current_binding != expected_binding:
        raise ws.WorkspaceStoreError(
            "material changed while the reviewed-evidence run was being persisted")
    if run.run_id not in current_material.associated_run_ids:
        store.update_material(
            current_material.material_id,
            associated_run_ids=[*current_material.associated_run_ids, run.run_id],
        )
    return store.get_run(run.run_id)
