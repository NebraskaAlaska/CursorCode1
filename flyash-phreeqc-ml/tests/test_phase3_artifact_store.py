"""Phase 3 typed artifact persistence and identity tests (synthetic fixtures only)."""
from __future__ import annotations

import json

import pytest

from flyash_phreeqc_ml import workspace_store as ws


@pytest.fixture()
def context(tmp_path):
    store = ws.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project("Synthetic test project")
    material = store.create_material(
        project.project_id,
        "Synthetic test material",
        composition_provenance={"source_type": "synthetic_demo"},
    )
    return store, project, material


def test_artifact_round_trip_restart_filter_and_material_link(context):
    store, project, material = context
    artifact = store.create_artifact(
        project.project_id,
        material.material_id,
        ws.ARTIFACT_XRD_PATTERN,
        {"fixture": "Synthetic test data", "rows": [[10.0, 2.0]]},
        status="draft",
        payload={"accepted_rows": 1},
        source_identity={"source_sha256": "a" * 64},
        creator="Synthetic test operator",
    )
    assert artifact.artifact_id.startswith(ws.ARTIFACT_PREFIX)
    assert artifact.logical_id == artifact.artifact_id
    assert artifact.input_hash == ws.identity_hash(artifact.input_snapshot)
    assert artifact.payload_hash == ws.identity_hash(artifact.payload)

    reopened = ws.WorkspaceStore(store.root)
    assert reopened.get_artifact(artifact.artifact_id).to_dict() == artifact.to_dict()
    assert [item.artifact_id for item in reopened.list_artifacts(
        project_id=project.project_id,
        material_id=material.material_id,
        record_type=ws.ARTIFACT_XRD_PATTERN,
        status="draft",
    )] == [artifact.artifact_id]
    linked = reopened.link_artifact_to_material(artifact.artifact_id)
    assert linked.measurement_references == [artifact.artifact_id]
    assert reopened.link_artifact_to_material(artifact.artifact_id).measurement_references == [
        artifact.artifact_id]


def test_terminal_artifact_is_immutable_and_revision_preserves_lineage(context):
    store, project, material = context
    draft = store.create_artifact(
        project.project_id,
        material.material_id,
        ws.ARTIFACT_ICP_REVIEW,
        {"fixture": "Synthetic test rows"},
        payload={"qc_summary": {"usable": 1}},
        creator="Synthetic resolver",
    )
    finalized = store.update_artifact(
        draft.artifact_id,
        expected_payload_hash=draft.payload_hash,
        status="finalized",
        reviewer="Synthetic resolver",
        reason="Synthetic test finalization",
    )
    assert finalized.reviewed_at
    with pytest.raises(ws.ImmutableRecordError):
        store.update_artifact(finalized.artifact_id, payload={"changed": True})

    revised = store.create_artifact_revision(
        finalized.artifact_id,
        status="draft",
        payload={"qc_summary": {"usable": 2}},
        creator="Synthetic resolver",
        reason="Synthetic test revision",
    )
    assert revised.revision == 2
    assert revised.logical_id == finalized.logical_id
    assert revised.previous_artifact_id == finalized.artifact_id
    assert revised.previous_artifact_hash == ws.identity_hash(finalized.to_dict())
    assert store.get_artifact(finalized.artifact_id).payload == {"qc_summary": {"usable": 1}}


def test_artifact_cross_project_and_related_run_binding_fail_closed(context):
    store, project, material = context
    other_project = store.create_project("Other synthetic project")
    other_material = store.create_material(other_project.project_id, "Other synthetic material")
    with pytest.raises(ws.WorkspaceStoreError):
        store.create_artifact(
            project.project_id,
            other_material.material_id,
            ws.ARTIFACT_EVIDENCE,
            {},
        )
    artifact = store.create_artifact(
        project.project_id,
        material.material_id,
        ws.ARTIFACT_EVIDENCE,
        {"fixture": "Synthetic evidence"},
    )
    other_run = store.create_run(
        other_project.project_id,
        other_material.material_id,
        "literature_evidence_engine",
        {"fixture": "Synthetic test data"},
        status="advisory",
        output_type="literature_evidence",
        epistemic_type="literature_evidence",
    )
    with pytest.raises(ws.WorkspaceStoreError):
        store.update_artifact(artifact.artifact_id, related_run_id=other_run.run_id)
    assert store.list_artifacts(project_id=other_project.project_id) == []


def test_artifact_future_schema_filename_identity_secret_and_nonfinite_rejected(context):
    store, project, material = context
    with pytest.raises(ws.MalformedRecordError, match="secret-like"):
        store.create_artifact(
            project.project_id,
            material.material_id,
            ws.ARTIFACT_EVIDENCE,
            {},
            payload={"api_token": "Synthetic secret-like field"},
        )
    with pytest.raises(ws.MalformedRecordError):
        store.create_artifact(
            project.project_id,
            material.material_id,
            ws.ARTIFACT_XRD_PATTERN,
            {"two_theta": float("inf")},
        )

    directory = store.root / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    artifact_id = "art_" + "b" * 32
    (directory / f"{artifact_id}.json").write_text(json.dumps({
        "schema_version": ws.SCHEMA_VERSION + 1,
        "artifact_id": artifact_id,
    }), encoding="utf-8")
    with pytest.raises(ws.UnsupportedSchemaError):
        store.get_artifact(artifact_id)

    mismatch = "art_" + "c" * 32
    (directory / f"{mismatch}.json").write_text(json.dumps({
        "schema_version": ws.SCHEMA_VERSION,
        "artifact_id": "art_" + "d" * 32,
    }), encoding="utf-8")
    with pytest.raises(ws.MalformedRecordError):
        store.get_artifact(mismatch)


def test_run_artifact_identity_becomes_stale_after_edit_but_not_terminal_link(context):
    store, project, material = context
    artifact = store.create_artifact(
        project.project_id,
        material.material_id,
        ws.ARTIFACT_EXPERIMENT_PLAN,
        {"fixture": "Synthetic plan input"},
        payload={"rows": [{"sample_id": "SYN-1"}]},
    )
    identity = {
        "artifact_id": artifact.artifact_id,
        "revision": artifact.revision,
        "input_hash": artifact.input_hash,
        "payload_hash": artifact.payload_hash,
    }
    run = store.create_run(
        project.project_id,
        material.material_id,
        "experimental_design_assistant",
        {"artifact_identities": [identity], "fixture": "Synthetic test data"},
        status="advisory",
        output_type="advisory_interpretation",
        epistemic_type="advisory_interpretation",
    )
    assert store.run_staleness(run) == (False, [])
    edited = store.update_artifact(artifact.artifact_id, payload={"rows": [
        {"sample_id": "SYN-2"}]})
    assert edited.payload_hash != artifact.payload_hash
    stale, reasons = store.run_staleness(run)
    assert stale and any("artifact payload changed" in reason for reason in reasons)


def test_workspace_runtime_root_is_gitignored():
    ignore = (__import__("pathlib").Path(__file__).resolve().parents[1] / ".gitignore").read_text()
    assert "outputs/virtual_lab_workspace/" in ignore
