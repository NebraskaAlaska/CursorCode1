"""Full-envelope integrity and controlled legacy compatibility regressions.

Every value is a synthetic fixture; no scientific result is represented here.
"""
from __future__ import annotations

import json

import pytest

from flyash_phreeqc_ml import phase3_artifacts, workspace_store as ws
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines
from flyash_phreeqc_ml.literature import evidence_review
from flyash_phreeqc_ml.literature import evidence_schema as E


@pytest.fixture()
def context(tmp_path):
    store = ws.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project("Synthetic envelope project")
    material = store.create_material(project.project_id, "Synthetic envelope material")
    return store, project, material


def _run(store, project, material):
    return store.create_run(
        project.project_id,
        material.material_id,
        machines.EXPERIMENTAL_DESIGN,
        {"fixture": "synthetic input"},
        status="advisory",
        output_type="advisory_interpretation",
        epistemic_type="advisory_interpretation",
        result_data={"fixture": {"value": 1.25}},
        warnings=["Synthetic warning"],
        validation_state="not_applicable_advisory",
        model_identity={"model": "synthetic-none"},
        environment_identity={"runtime": "synthetic"},
        evidence_identity=[{"label": "synthetic evidence label"}],
        result_location="synthetic/result/location",
        legacy_references=[{"label": "synthetic legacy label"}],
    )


def _artifact(store, project, material):
    run = _run(store, project, material)
    root = store.create_artifact(
        project.project_id,
        material.material_id,
        ws.ARTIFACT_EXPERIMENT_PLAN,
        {"fixture": "synthetic artifact input"},
        status="needs_review",
        payload={"fixture": "synthetic artifact payload"},
        source_identity={"source_type": "synthetic_fixture"},
        creator="Synthetic author",
        reason="Synthetic root",
        provenance={"source": "synthetic_fixture"},
    )
    return store.create_artifact(
        project.project_id,
        material.material_id,
        ws.ARTIFACT_EXPERIMENT_PLAN,
        root.input_snapshot,
        status="reviewed",
        payload=root.payload,
        source_identity=root.source_identity,
        creator="Synthetic reviewer",
        reviewer="Synthetic reviewer",
        reason="Synthetic source checked",
        provenance=root.provenance,
        revision=2,
        previous_artifact_id=root.artifact_id,
        logical_id=root.logical_id,
        related_run_id=run.run_id,
    )


@pytest.mark.parametrize("field", [
    "status", "reviewer", "reason", "reviewed_at", "record_type",
    "source_identity", "provenance", "related_run_id", "logical_id",
    "revision", "previous_artifact_id", "previous_artifact_hash",
    "project_id", "material_id", "creator", "created_at", "updated_at",
    "input_snapshot", "payload", "schema_version",
])
def test_artifact_full_envelope_rejects_direct_field_tamper(context, field):
    store, project, material = context
    artifact = _artifact(store, project, material)
    other_project = store.create_project("Synthetic other envelope project")
    other_material = store.create_material(
        other_project.project_id, "Synthetic other envelope material")
    changes = {
        "status": "rejected",
        "reviewer": "Synthetic forged reviewer",
        "reason": "Synthetic forged reason",
        "reviewed_at": "2020-01-01T00:00:00Z",
        "record_type": ws.ARTIFACT_SUSTAINABILITY_SCREEN,
        "source_identity": {"source_type": "synthetic_forgery"},
        "provenance": {"source": "synthetic_forgery"},
        "related_run_id": None,
        "logical_id": "art_" + "b" * 32,
        "revision": 3,
        "previous_artifact_id": "art_" + "c" * 32,
        "previous_artifact_hash": "0" * 64,
        "project_id": other_project.project_id,
        "material_id": other_material.material_id,
        "creator": "Synthetic forged creator",
        "created_at": "2020-01-01T00:00:00Z",
        "updated_at": "2020-01-01T00:00:00Z",
        "input_snapshot": {"fixture": "synthetic changed input"},
        "payload": {"fixture": "synthetic changed payload"},
        "schema_version": 1,
    }
    path = store.root / "artifacts" / f"{artifact.artifact_id}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document[field] = changes[field]
    if field == "input_snapshot":
        document["input_hash"] = ws.identity_hash(document[field])
    elif field == "payload":
        document["payload_hash"] = ws.identity_hash(document[field])
    path.write_text(json.dumps(document), encoding="utf-8")

    reopened = ws.WorkspaceStore(store.root)
    with pytest.raises(ws.WorkspaceStoreError):
        reopened.get_artifact(artifact.artifact_id)
    with pytest.raises(ws.WorkspaceStoreError):
        reopened.list_artifacts(project_id=project.project_id)


@pytest.mark.parametrize("field", [
    "status", "output_type", "epistemic_type", "validation_state", "warnings",
    "model_identity", "environment_identity", "evidence_identity", "result_location",
    "legacy_references", "machine_id", "material_revision", "composition_revision",
    "assumption_revision", "material_snapshot_hash", "project_id", "material_id",
    "input_snapshot", "result_data", "created_at", "schema_version",
])
def test_run_full_envelope_rejects_direct_field_tamper(context, field):
    store, project, material = context
    run = _run(store, project, material)
    other_project = store.create_project("Synthetic other run project")
    other_material = store.create_material(other_project.project_id, "Synthetic other run material")
    changes = {
        "status": "validated",
        "output_type": "validated_result",
        "epistemic_type": "validated_result",
        "validation_state": "validated_against_measured_data",
        "warnings": [],
        "model_identity": {"model": "synthetic forged model"},
        "environment_identity": {"runtime": "synthetic forged runtime"},
        "evidence_identity": [{"label": "synthetic forged evidence"}],
        "result_location": "synthetic/forged/location",
        "legacy_references": [{"label": "synthetic forged legacy"}],
        "machine_id": machines.SUSTAINABILITY,
        "material_revision": 2,
        "composition_revision": 2,
        "assumption_revision": 2,
        "material_snapshot_hash": "0" * 64,
        "project_id": other_project.project_id,
        "material_id": other_material.material_id,
        "input_snapshot": {"fixture": "synthetic changed input"},
        "result_data": {"fixture": {"value": 999}},
        "created_at": "2020-01-01T00:00:00Z",
        "schema_version": 1,
    }
    path = store.root / "runs" / f"{run.run_id}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document[field] = changes[field]
    if field == "input_snapshot":
        document["input_hash"] = ws.identity_hash(document[field])
    elif field == "result_data":
        document["result_hash"] = ws.identity_hash(document[field])
    path.write_text(json.dumps(document), encoding="utf-8")

    reopened = ws.WorkspaceStore(store.root)
    with pytest.raises(ws.WorkspaceStoreError):
        reopened.get_run(run.run_id)
    with pytest.raises(ws.WorkspaceStoreError):
        reopened.list_runs(project_id=project.project_id)


@pytest.mark.parametrize("kind", ["artifact", "run"])
def test_schema_two_records_reject_unknown_unhashed_fields(context, kind):
    store, project, material = context
    record = _artifact(store, project, material) if kind == "artifact" \
        else _run(store, project, material)
    path = store.root / f"{kind}s" / f"{getattr(record, kind + '_id')}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["synthetic_hidden_field"] = "must not survive projection"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ws.MalformedRecordError, match="unsupported schema-2 fields"):
        getattr(ws.WorkspaceStore(store.root), f"get_{kind}")(getattr(record, kind + "_id"))


def test_schema_one_legacy_reads_do_not_rewrite_and_authorized_artifact_update_upgrades(context):
    store, project, material = context
    run = _run(store, project, material)
    artifact = store.create_artifact(
        project.project_id, material.material_id, ws.ARTIFACT_EXPERIMENT_PLAN,
        {"fixture": "synthetic legacy input"}, payload={"fixture": "synthetic legacy payload"})
    for kind, record in (("runs", run), ("artifacts", artifact)):
        record_id = run.run_id if kind == "runs" else artifact.artifact_id
        path = store.root / kind / f"{record_id}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["schema_version"] = 1
        document.pop("envelope_hash")
        if kind == "runs":
            document.pop("result_hash")
        path.write_text(json.dumps(document), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        getattr(ws.WorkspaceStore(store.root), "get_run" if kind == "runs" else "get_artifact")(
            record_id)
        assert path.read_text(encoding="utf-8") == before

    upgraded = store.update_artifact(
        artifact.artifact_id, expected_status="draft", reason="Synthetic authorized update")
    assert upgraded.schema_version == ws.SCHEMA_VERSION
    assert upgraded.envelope_hash == ws.artifact_envelope_hash(upgraded)


def test_schema_one_artifact_lineage_keeps_pre_envelope_predecessor_identity(context):
    store, project, material = context
    root = store.create_artifact(
        project.project_id, material.material_id, ws.ARTIFACT_EXPERIMENT_PLAN,
        {"fixture": "synthetic legacy lineage input"},
        payload={"fixture": "synthetic legacy root"})
    child = store.create_artifact_revision(
        root.artifact_id,
        payload={"fixture": "synthetic legacy child"},
        creator="Synthetic legacy author",
        reason="Synthetic legacy revision",
    )
    root_path = store.root / "artifacts" / f"{root.artifact_id}.json"
    child_path = store.root / "artifacts" / f"{child.artifact_id}.json"
    root_document = json.loads(root_path.read_text(encoding="utf-8"))
    root_document["schema_version"] = 1
    root_document.pop("envelope_hash")
    child_document = json.loads(child_path.read_text(encoding="utf-8"))
    child_document["schema_version"] = 1
    child_document.pop("envelope_hash")
    child_document["previous_artifact_hash"] = ws.identity_hash(root_document)
    root_path.write_text(json.dumps(root_document), encoding="utf-8")
    child_path.write_text(json.dumps(child_document), encoding="utf-8")

    reopened = ws.WorkspaceStore(store.root)
    assert reopened.get_artifact(child.artifact_id).previous_artifact_hash \
        == ws.identity_hash(root_document)
    assert [item.revision for item in reopened.list_artifacts(
        logical_id=root.logical_id)] == [1, 2]


def test_schema_two_records_require_envelope_digest(context):
    store, project, material = context
    artifact = _artifact(store, project, material)
    run = _run(store, project, material)
    for kind, record in (("artifacts", artifact), ("runs", run)):
        record_id = artifact.artifact_id if kind == "artifacts" else run.run_id
        path = store.root / kind / f"{record_id}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document.pop("envelope_hash")
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ws.MalformedRecordError, match="envelope_hash"):
            getattr(ws.WorkspaceStore(store.root),
                    "get_artifact" if kind == "artifacts" else "get_run")(record_id)


def test_evidence_domain_rejects_generic_terminal_forgery_and_lifecycle_disagreement(context):
    store, project, material = context
    forged = store.create_artifact(
        project.project_id,
        material.material_id,
        ws.ARTIFACT_EVIDENCE,
        {"fixture": "synthetic forged evidence"},
        status="reviewed",
        payload={"review_status": "reviewed", "fixture": "not a closed evidence payload"},
        creator="Synthetic forger",
        reviewer="Synthetic forger",
        reason="Synthetic forged review",
    )
    store.link_artifact_to_material(forged.artifact_id, evidence=True)
    with pytest.raises(ws.MalformedRecordError, match="strict domain/envelope validation"):
        phase3_artifacts.save_reviewed_evidence_run(
            store, forged.artifact_id, machine_id=machines.LITERATURE_ENGINE)

    valid = evidence_review.create_manual_evidence(
        store,
        project.project_id,
        material.material_id,
        {
            "schema_kind": E.SCHEMA_LEACHING,
            "provenance": {"source": "synthetic", "title": "Synthetic source"},
            "source_location": {"page": "1"},
            "extraction_scope": E.SCOPE_MANUAL,
            "extraction_status": E.STATUS_MANUAL,
        },
        creator="Synthetic author",
    )
    submitted = evidence_review.submit_for_review(
        store, valid.artifact_id, submitter="Synthetic author")
    store.update_artifact(
        submitted.artifact_id,
        expected_status="needs_review",
        status="reviewed",
        reviewer="Synthetic generic reviewer",
        reason="Payload was intentionally left unresolved",
    )
    # Either canonical-domain validation or the subsequent explicit lifecycle
    # agreement check may reject first; both are the required fail-closed outcome.
    with pytest.raises(evidence_review.EvidenceReviewError):
        evidence_review.get_evidence(store, submitted.artifact_id)
