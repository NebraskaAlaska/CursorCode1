"""Focused adversarial coverage for durable Phase 3 store invariants.

All records and values in this module are synthetic test fixtures.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from flyash_phreeqc_ml import phase3_artifacts, workspace_store as ws
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines
from flyash_phreeqc_ml.literature import evidence_review
from flyash_phreeqc_ml.literature import evidence_schema as E


@pytest.fixture()
def context(tmp_path):
    store = ws.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project("Synthetic hardening project")
    material = store.create_material(project.project_id, "Synthetic hardening material")
    return store, project, material


def _draft(store, project, material, **changes):
    values = {
        "record_type": ws.ARTIFACT_EXPERIMENT_PLAN,
        "input_snapshot": {"fixture": "synthetic input"},
        "payload": {"fixture": "synthetic payload"},
        "creator": "Synthetic author",
        "reason": "Synthetic hardening test",
    }
    values.update(changes)
    return store.create_artifact(
        project.project_id,
        material.material_id,
        values.pop("record_type"),
        values.pop("input_snapshot"),
        **values,
    )


def _run(store, project, material, **changes):
    values = {
        "status": "advisory",
        "output_type": "advisory_interpretation",
        "epistemic_type": "advisory_interpretation",
        "result_data": {"fixture": {"value": 1.25}},
    }
    values.update(changes)
    return store.create_run(
        project.project_id,
        material.material_id,
        machines.EXPERIMENTAL_DESIGN,
        {"fixture": "synthetic input"},
        **values,
    )


def _save_kwargs(project, material):
    return {
        "project_id": project.project_id,
        "material_id": material.material_id,
        "record_type": ws.ARTIFACT_EXPERIMENT_PLAN,
        "machine_id": machines.EXPERIMENTAL_DESIGN,
        "input_snapshot": {"mode": "generic_user_defined", "fixture": "synthetic"},
        "payload": {"rows": [{"sample_id": "SYN-001", "measured_value": None}]},
        "source_identity": {"source_type": "synthetic_test_fixture"},
        "creator": "Synthetic researcher",
        "reason": "Synthetic retry hardening test",
        "warnings": ["Synthetic advisory fixture only"],
        "provenance": {"source": "synthetic_test_fixture"},
    }


def _reviewed_evidence(store, project, material):
    draft = evidence_review.create_manual_evidence(
        store, project.project_id, material.material_id,
        {
            "schema_kind": E.SCHEMA_LEACHING,
            "topic": "Synthetic leaching topic",
            "claim": "Synthetic contextual claim",
            "provenance": {
                "source": "synthetic_test_fixture",
                "title": "Synthetic evidence fixture",
                "doi": "10.0000/synthetic",
                "year": 2026,
            },
            "source_location": {"page": "1"},
            "extraction_scope": E.SCOPE_MANUAL,
            "extraction_status": E.STATUS_MANUAL,
            "extraction_confidence": 0.8,
        },
        creator="Synthetic evidence author",
    )
    submitted = evidence_review.submit_for_review(
        store, draft.artifact_id, submitter="Synthetic evidence author")
    reviewed = evidence_review.mark_reviewed(
        store, submitted.artifact_id,
        reviewer="Synthetic evidence reviewer",
        reason="Synthetic evidence source checked",
    )
    evidence_review.link_material(store, reviewed.artifact_id)
    return evidence_review.get_evidence(store, reviewed.artifact_id)


def test_list_enumeration_rejects_live_and_broken_record_file_symlinks(context, tmp_path):
    store, _project, _material = context
    outside = tmp_path / "outside.json"
    outside.write_text('{"not": "a workspace record"}', encoding="utf-8")
    directory = store.root / "projects"
    live_id = "prj_" + "a" * 32
    broken_id = "prj_" + "b" * 32
    try:
        (directory / f"{live_id}.json").symlink_to(outside)
        (directory / f"{broken_id}.json").symlink_to(tmp_path / "missing.json")
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ws.UnsafePathError, match="must not be a symlink"):
        store.list_projects(include_archived=True)
    (directory / f"{live_id}.json").unlink()
    with pytest.raises(ws.UnsafePathError, match="must not be a symlink"):
        store.list_projects(include_archived=True)


def test_artifact_lineage_rejects_sequential_and_concurrent_branching(context):
    store, project, material = context
    root = _draft(store, project, material)
    second = store.create_artifact_revision(
        root.artifact_id,
        payload={"fixture": "synthetic revision two"},
        creator="Synthetic author",
        reason="Synthetic revision two",
    )
    with pytest.raises(ws.DuplicateRecordError, match="latest logical revision"):
        store.create_artifact_revision(
            root.artifact_id,
            payload={"fixture": "synthetic branch"},
            creator="Synthetic author",
            reason="Synthetic branch attempt",
        )
    with pytest.raises(ws.ImmutableRecordError, match="historical artifact revision"):
        store.update_artifact(root.artifact_id, payload={"fixture": "silent history rewrite"})
    assert sorted(item.revision for item in store.list_artifacts(
        logical_id=root.logical_id)) == [1, 2]

    concurrent_root = _draft(
        store, project, material,
        input_snapshot={"fixture": "synthetic concurrent input"},
    )

    def append(index):
        return store.create_artifact_revision(
            concurrent_root.artifact_id,
            payload={"fixture": f"synthetic concurrent revision {index}"},
            creator="Synthetic concurrent author",
            reason=f"Synthetic concurrent attempt {index}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(append, index) for index in (1, 2)]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except ws.DuplicateRecordError:
            outcomes.append(None)
    assert sum(item is not None for item in outcomes) == 1
    assert sorted(item.revision for item in store.list_artifacts(
        logical_id=concurrent_root.logical_id)) == [1, 2]


def test_artifact_lineage_listing_is_root_to_head_even_with_same_timestamp(context):
    store, project, material = context
    root_id = "art_" + "f" * 32
    child_id = "art_" + "0" * 32
    root = _draft(store, project, material, artifact_id=root_id)
    child = store.create_artifact(
        project.project_id,
        material.material_id,
        root.record_type,
        root.input_snapshot,
        payload={"fixture": "synthetic ordered revision"},
        creator="Synthetic ordering author",
        reason="Synthetic ordering regression",
        revision=2,
        previous_artifact_id=root.artifact_id,
        artifact_id=child_id,
        logical_id=root.logical_id,
    )
    assert root.created_at == child.created_at
    assert [item.artifact_id for item in store.list_artifacts(
        logical_id=root.logical_id)] == [root.artifact_id, child.artifact_id]
    assert [item.revision for item in store.list_artifacts(
        logical_id=root.logical_id)] == [1, 2]


def test_get_and_list_validate_the_complete_artifact_lineage(context):
    store, project, material = context
    root = _draft(store, project, material)
    second = store.create_artifact_revision(
        root.artifact_id,
        payload={"fixture": "synthetic revision two"},
        creator="Synthetic author",
        reason="Synthetic revision two",
    )
    path = store.root / "artifacts" / f"{second.artifact_id}.json"
    sibling_id = "art_" + "c" * 32
    sibling = json.loads(path.read_text(encoding="utf-8"))
    sibling["artifact_id"] = sibling_id
    sibling["envelope_hash"] = ws.artifact_envelope_hash(sibling)
    (path.parent / f"{sibling_id}.json").write_text(json.dumps(sibling), encoding="utf-8")
    with pytest.raises(ws.MalformedRecordError, match="duplicate, missing, or branched"):
        store.get_artifact(root.artifact_id)
    with pytest.raises(ws.MalformedRecordError, match="duplicate, missing, or branched"):
        store.list_artifacts(project_id=project.project_id)


def test_new_runs_bind_result_integrity_and_legacy_schema_v1_remains_readable(context):
    store, project, material = context
    run = _run(store, project, material)
    assert run.result_hash == ws.identity_hash(run.result_data)
    assert run.envelope_hash == ws.run_envelope_hash(run)
    path = store.root / "runs" / f"{run.run_id}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = 1
    document.pop("result_hash")
    document.pop("envelope_hash")
    path.write_text(json.dumps(document), encoding="utf-8")
    legacy_text = path.read_text(encoding="utf-8")
    legacy = ws.WorkspaceStore(store.root).get_run(run.run_id)
    assert legacy.result_hash is None
    assert legacy.envelope_hash is None
    assert legacy.result_data == run.result_data
    assert path.read_text(encoding="utf-8") == legacy_text


@pytest.mark.parametrize("tamper", ["input", "result", "material_shape"])
def test_run_reads_reject_hash_tampering_and_malformed_material_binding(context, tamper):
    store, project, material = context
    run = _run(store, project, material)
    path = store.root / "runs" / f"{run.run_id}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if tamper == "input":
        document["input_snapshot"]["fixture"] = "tampered"
    elif tamper == "result":
        document["result_data"]["fixture"]["value"] = 999
    else:
        document["material_revision"] = "one"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ws.MalformedRecordError):
        ws.WorkspaceStore(store.root).get_run(run.run_id)
    with pytest.raises(ws.MalformedRecordError):
        ws.WorkspaceStore(store.root).list_runs(project_id=project.project_id)


def test_run_filename_identity_and_secret_safe_creation_fail_closed(context):
    store, project, material = context
    run = _run(store, project, material)
    path = store.root / "runs" / f"{run.run_id}.json"
    other_id = "run_" + "d" * 32
    (path.parent / f"{other_id}.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ws.MalformedRecordError, match="identity does not match"):
        store.list_runs(project_id=project.project_id)
    (path.parent / f"{other_id}.json").unlink()
    with pytest.raises(ws.MalformedRecordError, match="secret-like"):
        _run(store, project, material, result_data={"nested": {"api_token": "synthetic"}})


@pytest.mark.parametrize("secret_key", [
    "authToken", "clientSecret", "refresh-token", "API Key", "session cookie",
    "openaiApiKey", "vendorLicenseKey",
])
def test_normalized_secret_key_aliases_fail_closed_for_all_durable_record_shapes(
        context, secret_key):
    store, project, material = context
    secret_value = {"outer": {secret_key: "Synthetic credential-like value"}}
    with pytest.raises(ws.MalformedRecordError, match="secret-like"):
        _draft(store, project, material, payload=secret_value)
    with pytest.raises(ws.MalformedRecordError, match="secret-like"):
        _run(store, project, material, result_data=secret_value)
    with pytest.raises(ws.MalformedRecordError, match="secret-like"):
        store.update_material(material.material_id, process_conditions=secret_value)
    assert store.list_artifacts(project_id=project.project_id) == []
    assert store.list_runs(project_id=project.project_id) == []
    assert store.get_material(material.material_id).process_conditions == {}


def test_secret_key_normalization_does_not_block_scientific_word_substrings(context):
    store, project, material = context
    payload = {
        "secretion_rate": 0.25,
        "tokenization_method": "Synthetic categorical preprocessing label",
        "secreted_fraction": 0.1,
        "credentialed_operator_count": 1,
    }
    artifact = _draft(store, project, material, payload=payload)
    run = _run(store, project, material, result_data=payload)
    updated = store.update_material(material.material_id, process_conditions=payload)
    assert artifact.payload == payload
    assert run.result_data == payload
    assert updated.process_conditions == payload


def test_typed_material_references_require_exact_context_and_type_but_legacy_is_opaque(context):
    store, project_a, material_a = context
    project_b = store.create_project("Synthetic second project")
    material_b = store.create_material(project_b.project_id, "Synthetic second material")
    evidence_b = store.create_artifact(
        project_b.project_id,
        material_b.material_id,
        ws.ARTIFACT_EVIDENCE,
        {"fixture": "synthetic evidence"},
    )
    plan_a = _draft(store, project_a, material_a)
    run_b = _run(store, project_b, material_b)

    with pytest.raises(ws.WorkspaceStoreError, match="cross-context artifact"):
        store.update_material(
            material_a.material_id,
            evidence_references=[evidence_b.artifact_id],
        )
    with pytest.raises(ws.WorkspaceStoreError, match="incompatible artifact type"):
        store.update_material(
            material_a.material_id,
            measurement_references=[plan_a.artifact_id],
        )
    with pytest.raises(ws.WorkspaceStoreError, match="cross-context run"):
        store.update_material(material_a.material_id, associated_run_ids=[run_b.run_id])
    with pytest.raises(ws.UnsafePathError):
        store.update_material(material_a.material_id, evidence_references=["art_not-a-durable-id"])

    updated = store.update_material(
        material_a.material_id,
        measurement_references=["legacy opaque measurement label"],
        evidence_references=["legacy opaque evidence label"],
        associated_run_ids=["legacy opaque run label"],
    )
    assert updated.measurement_references == ["legacy opaque measurement label"]
    assert updated.evidence_references == ["legacy opaque evidence label"]
    assert updated.associated_run_ids == ["legacy opaque run label"]


def test_generic_advisory_retry_recovers_exact_orphan_draft(context, monkeypatch):
    store, project, material = context
    kwargs = _save_kwargs(project, material)
    real_create_run = store.create_run

    def fail_create_run(*_args, **_values):
        raise RuntimeError("synthetic interruption after artifact create")

    monkeypatch.setattr(store, "create_run", fail_create_run)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        phase3_artifacts.save_advisory_artifact_run(store, **kwargs)
    orphan = store.list_artifacts(project_id=project.project_id)
    assert len(orphan) == 1 and orphan[0].status == "draft"
    monkeypatch.setattr(store, "create_run", real_create_run)

    recovered = phase3_artifacts.save_advisory_artifact_run(store, **kwargs)
    repeated = phase3_artifacts.save_advisory_artifact_run(store, **kwargs)
    assert recovered.artifact.artifact_id == orphan[0].artifact_id
    assert repeated.artifact.artifact_id == recovered.artifact.artifact_id
    assert repeated.run.run_id == recovered.run.run_id
    assert len(store.list_artifacts(project_id=project.project_id)) == 1
    assert len(store.list_runs(project_id=project.project_id)) == 1


def test_generic_advisory_retry_recovers_run_before_finalize_and_repairs_link(
        context, monkeypatch):
    store, project, material = context
    kwargs = _save_kwargs(project, material)
    real_update_artifact = store.update_artifact

    def fail_finalize(*_args, **_values):
        raise RuntimeError("synthetic interruption before finalization")

    monkeypatch.setattr(store, "update_artifact", fail_finalize)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        phase3_artifacts.save_advisory_artifact_run(store, **kwargs)
    assert len(store.list_runs(project_id=project.project_id)) == 1
    monkeypatch.setattr(store, "update_artifact", real_update_artifact)

    recovered = phase3_artifacts.save_advisory_artifact_run(store, **kwargs)
    assert recovered.artifact.status == "finalized"
    assert recovered.artifact.related_run_id == recovered.run.run_id
    assert len(store.list_runs(project_id=project.project_id)) == 1
    assert recovered.run.run_id in store.get_material(material.material_id).associated_run_ids


def test_generic_advisory_retry_repairs_run_written_before_material_link(
        context, monkeypatch):
    store, project, material = context
    kwargs = _save_kwargs(project, material)
    real_update_material = store.update_material
    interrupted = {"done": False}

    def interrupt_run_link(material_id, **changes):
        if "associated_run_ids" in changes and not interrupted["done"]:
            interrupted["done"] = True
            raise RuntimeError("synthetic interruption after run write")
        return real_update_material(material_id, **changes)

    monkeypatch.setattr(store, "update_material", interrupt_run_link)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        phase3_artifacts.save_advisory_artifact_run(store, **kwargs)
    assert len(list((store.root / "runs").glob("run_*.json"))) == 1
    assert store._read_material_record(material.material_id).associated_run_ids == []

    recovered = phase3_artifacts.save_advisory_artifact_run(store, **kwargs)
    assert recovered.artifact.status == "finalized"
    assert store.get_material(material.material_id).associated_run_ids == [recovered.run.run_id]
    assert len(store.list_runs(project_id=project.project_id)) == 1


def test_generic_advisory_retry_recovers_when_finalize_completed_then_raised(
        context, monkeypatch):
    store, project, material = context
    kwargs = _save_kwargs(project, material)
    real_update_artifact = store.update_artifact

    def finalize_then_fail(*args, **values):
        real_update_artifact(*args, **values)
        raise RuntimeError("synthetic interruption after finalization")

    monkeypatch.setattr(store, "update_artifact", finalize_then_fail)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        phase3_artifacts.save_advisory_artifact_run(store, **kwargs)
    monkeypatch.setattr(store, "update_artifact", real_update_artifact)

    recovered = phase3_artifacts.save_advisory_artifact_run(store, **kwargs)
    assert recovered.artifact.status == "finalized"
    assert len(store.list_artifacts(project_id=project.project_id)) == 1
    assert len(store.list_runs(project_id=project.project_id)) == 1


def test_exact_generic_reopen_rejects_context_and_result_corruption(context):
    store, project, material = context
    kwargs = _save_kwargs(project, material)
    saved = phase3_artifacts.save_advisory_artifact_run(store, **kwargs)
    other_project = store.create_project("Synthetic other reopen project")
    other_material = store.create_material(other_project.project_id, "Synthetic other material")
    with pytest.raises(ws.WorkspaceStoreError, match="another active context"):
        phase3_artifacts.require_exact_advisory_run(
            store,
            saved.run.run_id,
            project_id=other_project.project_id,
            material_id=other_material.material_id,
            machine_id=machines.EXPERIMENTAL_DESIGN,
            record_type=ws.ARTIFACT_EXPERIMENT_PLAN,
        )

    path = store.root / "runs" / f"{saved.run.run_id}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["result_data"]["artifact_payload"]["rows"][0]["sample_id"] = "TAMPERED"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ws.MalformedRecordError, match="payload hash changed"):
        phase3_artifacts.require_exact_advisory_run(
            ws.WorkspaceStore(store.root),
            saved.run.run_id,
            project_id=project.project_id,
            material_id=material.material_id,
            machine_id=machines.EXPERIMENTAL_DESIGN,
            record_type=ws.ARTIFACT_EXPERIMENT_PLAN,
        )


def test_phase3_run_cannot_be_downgraded_to_legacy_by_removing_result_hash(context):
    store, project, material = context
    saved = phase3_artifacts.save_advisory_artifact_run(
        store, **_save_kwargs(project, material))
    path = store.root / "runs" / f"{saved.run.run_id}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop("result_hash")
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ws.MalformedRecordError, match="missing its required result integrity hash"):
        ws.WorkspaceStore(store.root).get_run(saved.run.run_id)


@pytest.mark.parametrize(("change", "value", "message"), [
    ("output_type", "validated_result", "output_type is fixed"),
    ("validation_state", "validated_against_measured_data", "validation_state is fixed"),
    ("machine_id", machines.SUSTAINABILITY, "requires machine"),
])
def test_advisory_coordinator_refuses_epistemic_promotion_and_machine_rebinding(
        context, change, value, message):
    store, project, material = context
    kwargs = _save_kwargs(project, material)
    kwargs[change] = value
    with pytest.raises(ws.MalformedRecordError, match=message):
        phase3_artifacts.save_advisory_artifact_run(store, **kwargs)
    assert store.list_artifacts(project_id=project.project_id) == []
    assert store.list_runs(project_id=project.project_id) == []


def test_advisory_coordinator_persists_only_fixed_advisory_contract(context):
    store, project, material = context
    saved = phase3_artifacts.save_advisory_artifact_run(
        store, **_save_kwargs(project, material))
    assert saved.run.status == phase3_artifacts.ADVISORY_STATUS
    assert saved.run.output_type == phase3_artifacts.ADVISORY_OUTPUT_TYPE
    assert saved.run.epistemic_type == phase3_artifacts.ADVISORY_EPISTEMIC_TYPE
    assert saved.run.validation_state == phase3_artifacts.ADVISORY_VALIDATION_STATE


def test_reviewed_evidence_run_prewrite_failure_retries_one_deterministic_run(
        context, monkeypatch):
    store, project, material = context
    evidence = _reviewed_evidence(store, project, material)
    real_create_run = store.create_run

    def fail_before_write(*_args, **_kwargs):
        raise RuntimeError("synthetic evidence pre-write failure")

    monkeypatch.setattr(store, "create_run", fail_before_write)
    with pytest.raises(RuntimeError, match="pre-write"):
        phase3_artifacts.save_reviewed_evidence_run(
            store, evidence.artifact_id, machine_id=machines.LITERATURE_ENGINE)
    assert store.list_runs(project_id=project.project_id) == []
    monkeypatch.setattr(store, "create_run", real_create_run)
    first = phase3_artifacts.save_reviewed_evidence_run(
        store, evidence.artifact_id, machine_id=machines.LITERATURE_ENGINE)
    repeated = phase3_artifacts.save_reviewed_evidence_run(
        store, evidence.artifact_id, machine_id=machines.LITERATURE_ENGINE)
    assert repeated.run_id == first.run_id
    assert len(store.list_runs(project_id=project.project_id)) == 1


def test_reviewed_evidence_run_postwrite_response_loss_reconciles_exact_spec(
        context, monkeypatch):
    store, project, material = context
    evidence = _reviewed_evidence(store, project, material)
    real_create_run = store.create_run
    injected = {"done": False}

    def write_then_fail(*args, **kwargs):
        run = real_create_run(*args, **kwargs)
        if not injected["done"]:
            injected["done"] = True
            raise RuntimeError("synthetic evidence post-write response loss")
        return run

    monkeypatch.setattr(store, "create_run", write_then_fail)
    with pytest.raises(RuntimeError, match="post-write"):
        phase3_artifacts.save_reviewed_evidence_run(
            store, evidence.artifact_id, machine_id=machines.LITERATURE_ENGINE)
    recovered = phase3_artifacts.save_reviewed_evidence_run(
        store, evidence.artifact_id, machine_id=machines.LITERATURE_ENGINE)
    assert len(store.list_runs(project_id=project.project_id)) == 1
    assert store.get_material(material.material_id).associated_run_ids == [recovered.run_id]


def test_reviewed_evidence_run_rejects_deterministic_id_with_different_full_spec(
        context, monkeypatch):
    store, project, material = context
    evidence = _reviewed_evidence(store, project, material)
    real_create_run = store.create_run

    def persist_wrong_spec(*args, **kwargs):
        kwargs["warnings"] = ["Synthetic colliding warning"]
        return real_create_run(*args, **kwargs)

    monkeypatch.setattr(store, "create_run", persist_wrong_spec)
    with pytest.raises(ws.DuplicateRecordError, match="different full run spec: warnings"):
        phase3_artifacts.save_reviewed_evidence_run(
            store, evidence.artifact_id, machine_id=machines.LITERATURE_ENGINE)
    assert len(store.list_runs(project_id=project.project_id)) == 1
