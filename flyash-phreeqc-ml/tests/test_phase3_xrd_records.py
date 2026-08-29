"""Durable measured-XRD/reference advisory journeys with synthetic data only."""
from __future__ import annotations

import copy
import hashlib
import json
import re

import pytest

from flyash_phreeqc_ml import phase3_artifacts
from flyash_phreeqc_ml import workspace_store as ws
from flyash_phreeqc_ml.instruments import xrd_advisory as xrd
from flyash_phreeqc_ml.instruments import xrd_records


MEASURED_BYTES = b"2theta,Counts\n20.00,100\n29.40,70\n40.00,30\n55.00,-2\n"
CHANGED_MEASURED_BYTES = b"2theta,Counts\n20.10,100\n29.40,70\n40.00,30\n55.00,-2\n"
REFERENCE_A_BYTES = b"2theta,Relative Intensity\n20.05,100\n29.35,70\n40.00,30\n"
REFERENCE_B_BYTES = b"2theta,Relative Intensity\n20.04,100\n29.45,60\n45.00,20\n"


@pytest.fixture
def context(tmp_path):
    store = ws.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project("Synthetic XRD project")
    material = store.create_material(project.project_id, "Synthetic material")
    return store, project, material


def _pattern(project_id: str, material_id: str, raw: bytes = MEASURED_BYTES):
    return xrd.import_measured_pattern_csv(
        raw,
        source_filename="synthetic-measured.csv",
        metadata={
            "project_id": project_id,
            "material_id": material_id,
            "sample_id": "SYN-XRD-001",
            "radiation_source": "Cu Kalpha",
            "wavelength_angstrom": 1.5406,
            "instrument": "Synthetic teaching diffractometer",
            "method": "Synthetic scan; no experimental claim",
            "intensity_unit": "counts",
            "operator": "Synthetic operator",
            "lab": "Synthetic lab",
            "step_size_deg": 0.02,
        },
        user_peak_list=[20.0, 29.4, 40.0, 55.0],
        peak_selection_provenance={
            "provided_by": "Synthetic reviewer",
            "notes": "Explicit test-only peak list",
            "parameters": {},
        },
    )


def _reference(raw: bytes, *, name: str, card: str, filename: str, **metadata):
    values = {
        "source_name": "Synthetic user reference collection",
        "source_record_id": card,
        "phase_name": name,
        "formula": "A2B",
        "polymorph": f"{name} synthetic form",
        "radiation_source": "Cu Kalpha",
        "wavelength_angstrom": 1.5406,
        "title": f"{name} synthetic peak table",
        "authors": ["Synthetic Author"],
        "year": 2026,
        "url": "https://example.invalid/test-only-reference",
        "review_status": "reviewed",  # imported assertions cannot bypass durable review
    }
    values.update(metadata)
    return xrd.import_reference_csv(
        raw, source_filename=filename, source_metadata=values)


def _save_pattern(store, project, material, raw: bytes = MEASURED_BYTES):
    return xrd_records.save_measured_pattern(
        store,
        project_id=project.project_id,
        material_id=material.material_id,
        pattern=_pattern(project.project_id, material.material_id, raw),
        creator="Synthetic curator",
        reason="Persist exact synthetic measured pattern",
    )


def _save_references(store, project, material):
    first = xrd_records.save_xrd_reference(
        store,
        project_id=project.project_id,
        material_id=material.material_id,
        reference=_reference(
            REFERENCE_A_BYTES,
            name="Synthetic Alpha",
            card="SYN-A",
            filename="synthetic-alpha.csv",
        ),
        creator="Synthetic curator",
        reason="Persist synthetic reference A",
    )
    second = xrd_records.save_xrd_reference(
        store,
        project_id=project.project_id,
        material_id=material.material_id,
        reference=_reference(
            REFERENCE_B_BYTES,
            name="Synthetic Beta",
            card="SYN-B",
            filename="synthetic-beta.csv",
        ),
        creator="Synthetic curator",
        reason="Persist synthetic reference B",
    )
    return first, second


def test_measured_pattern_and_reference_restart_preserve_exact_source_and_scope(context):
    store, project, material = context
    pattern_artifact = _save_pattern(store, project, material)
    reference_artifact, _ = _save_references(store, project, material)

    assert pattern_artifact.status == "finalized"
    assert pattern_artifact.source_identity["source_sha256"] == hashlib.sha256(
        MEASURED_BYTES).hexdigest()
    assert reference_artifact.status == "needs_review"
    material_record = store.get_material(material.material_id)
    assert pattern_artifact.artifact_id in material_record.measurement_references
    assert reference_artifact.artifact_id in material_record.measurement_references

    reopened = ws.WorkspaceStore(store.root)
    loaded_pattern = xrd_records.load_measured_pattern(
        reopened,
        pattern_artifact.artifact_id,
        project_id=project.project_id,
        material_id=material.material_id,
    )
    loaded_reference = xrd_records.load_xrd_reference(
        reopened,
        reference_artifact.artifact_id,
        project_id=project.project_id,
        material_id=material.material_id,
    )
    assert loaded_pattern.pattern.instrument == "Synthetic teaching diffractometer"
    assert loaded_pattern.pattern.source_sha256 == hashlib.sha256(MEASURED_BYTES).hexdigest()
    assert loaded_reference.reference.review_status == "needs_review"
    assert loaded_reference.reference.license_status == xrd.LICENSE_UNKNOWN
    assert (loaded_reference.reference.redistribution_permission_status
            == xrd.REDISTRIBUTION_UNKNOWN)
    assert loaded_reference.artifact.provenance["imported_review_status"] == "reviewed"
    assert "public" not in loaded_reference.reference.license_status.lower()


def test_durable_measured_pattern_requires_sample_id(context):
    store, project, material = context
    pattern = _pattern(project.project_id, material.material_id)
    pattern.sample_id = ""
    with pytest.raises(ws.MalformedRecordError, match="sample_id is required"):
        xrd_records.save_measured_pattern(
            store,
            project_id=project.project_id,
            material_id=material.material_id,
            pattern=pattern,
            creator="Synthetic curator",
            reason="Blank sample identity must fail closed",
        )
    assert store.list_artifacts(
        project_id=project.project_id,
        material_id=material.material_id,
        record_type=ws.ARTIFACT_XRD_PATTERN,
    ) == []


def test_finalized_peak_edit_creates_new_immutable_revision(context):
    store, project, material = context
    original = _save_pattern(store, project, material)
    with pytest.raises(ws.ImmutableRecordError):
        store.update_artifact(original.artifact_id, payload={"silently": "changed"})

    revised = xrd_records.revise_measured_pattern_peaks(
        store,
        original.artifact_id,
        project_id=project.project_id,
        material_id=material.material_id,
        peaks=[20.0, {"two_theta_deg": 29.4, "relative_intensity": 85,
                      "note": "retained after review"}],
        provenance={
            "provided_by": "Synthetic peak reviewer",
            "user_edits": [{"action": "remove", "two_theta_deg": 40.0}],
            "parameters": {},
        },
        creator="Synthetic peak reviewer",
        reason="Record explicit peak-list review",
    )
    assert revised.status == "finalized"
    assert revised.revision == 2
    assert revised.logical_id == original.logical_id
    assert revised.previous_artifact_id == original.artifact_id
    assert revised.source_identity == original.source_identity
    assert revised.payload_hash != original.payload_hash

    old_loaded = xrd_records.load_measured_pattern(
        store, original.artifact_id,
        project_id=project.project_id, material_id=material.material_id)
    new_loaded = xrd_records.load_measured_pattern(
        store, revised.artifact_id,
        project_id=project.project_id, material_id=material.material_id)
    assert len(old_loaded.pattern.user_peak_list) == 4
    assert [item["two_theta_deg"] for item in new_loaded.pattern.user_peak_list] == [20.0, 29.4]
    with pytest.raises(ws.ImmutableRecordError):
        store.update_artifact(revised.artifact_id, payload={"silently": "changed"})


def test_reference_review_retains_unknown_license_and_becomes_immutable(context):
    store, project, material = context
    pattern_artifact = _save_pattern(store, project, material)
    reference_artifact, other_reference = _save_references(store, project, material)
    pre_review_run = xrd_records.save_tentative_match_run(
        store,
        project_id=project.project_id,
        material_id=material.material_id,
        pattern_artifact_id=pattern_artifact.artifact_id,
        reference_artifact_ids=[reference_artifact.artifact_id, other_reference.artifact_id],
        creator="Synthetic analyst",
        reason="Persist an advisory using the explicit needs-review revision",
    ).run
    reviewed = xrd_records.review_xrd_reference(
        store,
        reference_artifact.artifact_id,
        project_id=project.project_id,
        material_id=material.material_id,
        review_status="reviewed",
        reviewer="Synthetic reference reviewer",
        reason="Source metadata inspected; scientific match remains advisory",
    )
    loaded = xrd_records.load_xrd_reference(
        store,
        reviewed.artifact_id,
        project_id=project.project_id,
        material_id=material.material_id,
    )
    assert reviewed.status == loaded.reference.review_status == "reviewed"
    assert reviewed.artifact_id != reference_artifact.artifact_id
    assert reviewed.logical_id == reference_artifact.logical_id
    assert reviewed.previous_artifact_id == reference_artifact.artifact_id
    assert reviewed.revision == reference_artifact.revision + 1
    assert loaded.reference.license_status == "unknown"
    assert loaded.reference.redistribution_permission_status == "unknown"
    assert loaded.artifact.provenance["review_events"][-1]["reviewer"] \
        == "Synthetic reference reviewer"
    original = xrd_records.load_xrd_reference(
        store,
        reference_artifact.artifact_id,
        project_id=project.project_id,
        material_id=material.material_id,
    )
    assert original.reference.review_status == original.artifact.status == "needs_review"
    stale, _ = store.run_staleness(pre_review_run)
    assert stale is True
    historical = xrd_records.load_tentative_match_run(
        store,
        pre_review_run.run_id,
        project_id=project.project_id,
        material_id=material.material_id,
    )
    assert historical.is_stale is True
    assert reference_artifact.artifact_id in {
        item.artifact.artifact_id for item in historical.references}
    assert reviewed.artifact_id not in {
        item.artifact.artifact_id for item in historical.references}
    with pytest.raises(ws.ImmutableRecordError):
        xrd_records.review_xrd_reference(
            store,
            reviewed.artifact_id,
            project_id=project.project_id,
            material_id=material.material_id,
            review_status="rejected",
            reviewer="Synthetic reference reviewer",
            reason="Attempted second resolution",
        )


def test_advisory_run_exact_dependencies_restart_ambiguity_and_safe_wording(context):
    store, project, material = context
    pattern_artifact = _save_pattern(store, project, material)
    first_reference, second_reference = _save_references(store, project, material)
    first_reference = xrd_records.review_xrd_reference(
        store,
        first_reference.artifact_id,
        project_id=project.project_id,
        material_id=material.material_id,
        review_status="reviewed",
        reviewer="Synthetic reviewer",
        reason="Synthetic source metadata inspected",
    )

    saved = xrd_records.save_tentative_match_run(
        store,
        project_id=project.project_id,
        material_id=material.material_id,
        pattern_artifact_id=pattern_artifact.artifact_id,
        reference_artifact_ids=[first_reference.artifact_id, second_reference.artifact_id],
        tolerance=0.1,
        creator="Synthetic analyst",
        reason="Save synthetic tentative advisory comparison",
    )
    assert saved.run.status == "advisory"
    assert saved.run.output_type == xrd_records.XRD_RUN_OUTPUT_TYPE
    assert saved.run.validation_state == "not_applicable_advisory"
    assert store.run_staleness(saved.run) == (False, [])
    dependencies = saved.run.input_snapshot["artifact_identities"]
    assert [item["artifact_id"] for item in dependencies] == [
        pattern_artifact.artifact_id,
        first_reference.artifact_id,
        second_reference.artifact_id,
    ]
    assert saved.run.input_snapshot["pattern_source_identity"]["source_sha256"] \
        == hashlib.sha256(MEASURED_BYTES).hexdigest()
    assert saved.match_payload["overlap_ambiguity_count"] >= 1
    assert "needs_review" in " ".join(saved.run.warnings)

    reopened = xrd_records.load_tentative_match_run(
        ws.WorkspaceStore(store.root),
        saved.run.run_id,
        project_id=project.project_id,
        material_id=material.material_id,
    )
    assert reopened.is_stale is False
    assert reopened.pattern.artifact.artifact_id == pattern_artifact.artifact_id
    assert [item.artifact.artifact_id for item in reopened.references] == [
        first_reference.artifact_id, second_reference.artifact_id]
    assert reopened.match_payload == saved.match_payload
    generated = xrd_records._generated_advisory_text(reopened.match_payload).lower()
    assert all(term in generated for term in (
        "tentative", "advisory", "possible match",
        "check against an appropriate reference source"))
    assert re.search(
        r"\b(?:identified|confirmed\s+phase|validated\s+phase|quantified\s+phase)\b",
        generated,
    ) is None


def test_source_change_stales_historical_run_but_does_not_rebind_it(context):
    store, project, material = context
    original_pattern = _save_pattern(store, project, material)
    references = _save_references(store, project, material)
    saved = xrd_records.save_tentative_match_run(
        store,
        project_id=project.project_id,
        material_id=material.material_id,
        pattern_artifact_id=original_pattern.artifact_id,
        reference_artifact_ids=[item.artifact_id for item in references],
        creator="Synthetic analyst",
        reason="Save before source change",
    )
    xrd_records.require_source_hash_match(original_pattern, MEASURED_BYTES)
    with pytest.raises(ws.WorkspaceStoreError, match="source hash changed"):
        xrd_records.require_source_hash_match(original_pattern, CHANGED_MEASURED_BYTES)

    changed_pattern = _save_pattern(store, project, material, CHANGED_MEASURED_BYTES)
    assert changed_pattern.source_identity["source_sha256"] != \
        original_pattern.source_identity["source_sha256"]
    stale, reasons = store.run_staleness(saved.run)
    assert stale is True
    assert any("material" in reason for reason in reasons)

    historical = xrd_records.load_tentative_match_run(
        store,
        saved.run.run_id,
        project_id=project.project_id,
        material_id=material.material_id,
    )
    assert historical.is_stale is True
    assert historical.pattern.artifact.artifact_id == original_pattern.artifact_id
    assert historical.pattern.pattern.source_sha256 == hashlib.sha256(MEASURED_BYTES).hexdigest()

    bad_dependencies = copy.deepcopy(saved.run.input_snapshot["artifact_identities"])
    bad_dependencies[0]["payload_hash"] = "0" * 64
    with pytest.raises(ws.WorkspaceStoreError, match="payload identity changed"):
        phase3_artifacts.require_exact_artifact_dependencies(
            store,
            bad_dependencies,
            project_id=project.project_id,
            material_id=material.material_id,
        )


def test_cross_project_artifacts_and_runs_fail_closed(context):
    store, project_a, material_a = context
    pattern_a = _save_pattern(store, project_a, material_a)
    references_a = _save_references(store, project_a, material_a)
    run_a = xrd_records.save_tentative_match_run(
        store,
        project_id=project_a.project_id,
        material_id=material_a.material_id,
        pattern_artifact_id=pattern_a.artifact_id,
        reference_artifact_ids=[item.artifact_id for item in references_a],
        creator="Synthetic analyst",
        reason="Project A comparison",
    ).run

    project_b = store.create_project("Synthetic project B")
    material_b = store.create_material(project_b.project_id, "Synthetic material B")
    with pytest.raises(ws.WorkspaceStoreError, match="another project/material"):
        xrd_records.load_measured_pattern(
            store,
            pattern_a.artifact_id,
            project_id=project_b.project_id,
            material_id=material_b.material_id,
        )
    with pytest.raises(ws.WorkspaceStoreError, match="another project/material"):
        xrd_records.load_tentative_match_run(
            store,
            run_a.run_id,
            project_id=project_b.project_id,
            material_id=material_b.material_id,
        )
    pattern_b = _save_pattern(store, project_b, material_b)
    with pytest.raises(ws.WorkspaceStoreError, match="another project/material"):
        xrd_records.save_tentative_match_run(
            store,
            project_id=project_b.project_id,
            material_id=material_b.material_id,
            pattern_artifact_id=pattern_b.artifact_id,
            reference_artifact_ids=[references_a[0].artifact_id],
            creator="Synthetic analyst",
            reason="Cross-project reference must be blocked",
        )


def test_rejected_reference_is_not_eligible_for_matching(context):
    store, project, material = context
    pattern = _save_pattern(store, project, material)
    reference, _ = _save_references(store, project, material)
    rejected = xrd_records.review_xrd_reference(
        store,
        reference.artifact_id,
        project_id=project.project_id,
        material_id=material.material_id,
        review_status="rejected",
        reviewer="Synthetic reviewer",
        reason="Synthetic reference provenance rejected",
    )
    with pytest.raises(ws.WorkspaceStoreError, match="rejected XRD references"):
        xrd_records.save_tentative_match_run(
            store,
            project_id=project.project_id,
            material_id=material.material_id,
            pattern_artifact_id=pattern.artifact_id,
            reference_artifact_ids=[rejected.artifact_id],
            creator="Synthetic analyst",
            reason="Rejected reference must fail closed",
        )


def test_match_payload_hash_tampering_fails_reopen(context):
    store, project, material = context
    pattern = _save_pattern(store, project, material)
    references = _save_references(store, project, material)
    saved = xrd_records.save_tentative_match_run(
        store,
        project_id=project.project_id,
        material_id=material.material_id,
        pattern_artifact_id=pattern.artifact_id,
        reference_artifact_ids=[item.artifact_id for item in references],
        creator="Synthetic analyst",
        reason="Hash-binding test",
    )

    # Simulate a corrupted local result while retaining the immutable input snapshot/hash.
    path = store.root / "runs" / f"{saved.run.run_id}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["result_data"]["match_payload"]["tolerance_deg"] = 9.9
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ws.MalformedRecordError, match="payload hash changed"):
        xrd_records.load_tentative_match_run(
            ws.WorkspaceStore(store.root),
            saved.run.run_id,
            project_id=project.project_id,
            material_id=material.material_id,
        )


@pytest.mark.parametrize("failure_stage", ["artifact_create", "artifact_finalize"])
def test_measured_pattern_save_retry_recovers_exact_partial_write(
        context, monkeypatch, failure_stage):
    store, project, material = context
    pattern = _pattern(project.project_id, material.material_id)
    values = {
        "project_id": project.project_id,
        "material_id": material.material_id,
        "pattern": pattern,
        "creator": "Synthetic retry curator",
        "reason": "Synthetic measured-pattern retry",
    }
    interrupted = {"done": False}
    if failure_stage == "artifact_create":
        real = store.create_artifact

        def write_then_interrupt(*args, **kwargs):
            saved = real(*args, **kwargs)
            if not interrupted["done"]:
                interrupted["done"] = True
                raise RuntimeError("synthetic interruption after pattern artifact write")
            return saved

        monkeypatch.setattr(store, "create_artifact", write_then_interrupt)
    else:
        real = store.update_artifact

        def write_then_interrupt(*args, **kwargs):
            saved = real(*args, **kwargs)
            if kwargs.get("status") == "finalized" and not interrupted["done"]:
                interrupted["done"] = True
                raise RuntimeError("synthetic interruption after pattern finalization write")
            return saved

        monkeypatch.setattr(store, "update_artifact", write_then_interrupt)

    with pytest.raises(RuntimeError, match="synthetic interruption"):
        xrd_records.save_measured_pattern(store, **values)
    monkeypatch.setattr(store, "create_artifact" if failure_stage == "artifact_create"
                        else "update_artifact", real)

    recovered = xrd_records.save_measured_pattern(store, **values)
    repeated = xrd_records.save_measured_pattern(store, **values)
    records = store.list_artifacts(
        project_id=project.project_id,
        material_id=material.material_id,
        record_type=ws.ARTIFACT_XRD_PATTERN,
    )
    assert recovered.artifact_id == repeated.artifact_id
    assert records == [recovered]
    assert recovered.status == "finalized"
    assert recovered.artifact_id in store.get_material(material.material_id).measurement_references


@pytest.mark.parametrize("revision_written", [False, True])
def test_reference_review_retry_recovers_exact_linear_revision(
        context, monkeypatch, revision_written):
    store, project, material = context
    reference, _other = _save_references(store, project, material)
    values = {
        "project_id": project.project_id,
        "material_id": material.material_id,
        "review_status": "reviewed",
        "reviewer": "Synthetic retry reviewer",
        "reason": "Synthetic retry source review",
    }
    interrupted = {"done": False}
    real = store.create_artifact

    def interrupt_revision_create(*args, **kwargs):
        if kwargs.get("previous_artifact_id") == reference.artifact_id \
                and not interrupted["done"]:
            interrupted["done"] = True
            if revision_written:
                real(*args, **kwargs)
            raise RuntimeError("synthetic interruption at review revision write")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "create_artifact", interrupt_revision_create)

    with pytest.raises(RuntimeError, match="synthetic interruption"):
        xrd_records.review_xrd_reference(store, reference.artifact_id, **values)
    monkeypatch.setattr(store, "create_artifact", real)

    recovered = xrd_records.review_xrd_reference(store, reference.artifact_id, **values)
    repeated_lineage = store.list_artifacts(logical_id=reference.logical_id)
    assert [item.revision for item in repeated_lineage] == [1, 2]
    assert recovered == repeated_lineage[-1]
    assert recovered.status == "reviewed"
    assert recovered.previous_artifact_id == reference.artifact_id
    assert recovered.artifact_id in store.get_material(material.material_id).measurement_references


@pytest.mark.parametrize("artifact_written", [False, True])
def test_reference_import_retry_recovers_exact_partial_write(
        context, monkeypatch, artifact_written):
    store, project, material = context
    reference = _reference(
        REFERENCE_A_BYTES,
        name="Synthetic retry reference",
        card="SYN-RETRY",
        filename="synthetic-retry-reference.csv",
    )
    values = {
        "project_id": project.project_id,
        "material_id": material.material_id,
        "reference": reference,
        "creator": "Synthetic retry curator",
        "reason": "Synthetic reference-import retry",
    }
    real_create = store.create_artifact
    interrupted = {"done": False}

    def interrupt_artifact_write(*args, **kwargs):
        if not interrupted["done"]:
            interrupted["done"] = True
            if artifact_written:
                real_create(*args, **kwargs)
            raise RuntimeError("synthetic interruption at reference artifact write")
        return real_create(*args, **kwargs)

    monkeypatch.setattr(store, "create_artifact", interrupt_artifact_write)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        xrd_records.save_xrd_reference(store, **values)
    monkeypatch.setattr(store, "create_artifact", real_create)

    recovered = xrd_records.save_xrd_reference(store, **values)
    repeated = xrd_records.save_xrd_reference(store, **values)
    records = store.list_artifacts(
        project_id=project.project_id,
        material_id=material.material_id,
        record_type=ws.ARTIFACT_XRD_REFERENCE,
    )
    assert recovered.artifact_id == repeated.artifact_id
    assert records == [recovered]
    assert recovered.status == "needs_review"
    assert recovered.artifact_id in store.get_material(material.material_id).measurement_references


@pytest.mark.parametrize("revision_written", [False, True])
def test_peak_revision_retry_recovers_exact_linear_terminal_child(
        context, monkeypatch, revision_written):
    store, project, material = context
    original = _save_pattern(store, project, material)
    values = {
        "project_id": project.project_id,
        "material_id": material.material_id,
        "peaks": [20.0, {"two_theta_deg": 29.4, "relative_intensity": 80}],
        "provenance": {
            "provided_by": "Synthetic retry peak reviewer",
            "notes": "Synthetic recovery fixture",
            "user_edits": [],
        },
        "creator": "Synthetic retry peak reviewer",
        "reason": "Synthetic peak-revision retry",
    }
    real_create = store.create_artifact
    interrupted = {"done": False}

    def interrupt_revision_write(*args, **kwargs):
        if kwargs.get("previous_artifact_id") == original.artifact_id \
                and not interrupted["done"]:
            interrupted["done"] = True
            if revision_written:
                real_create(*args, **kwargs)
            raise RuntimeError("synthetic interruption at peak revision write")
        return real_create(*args, **kwargs)

    monkeypatch.setattr(store, "create_artifact", interrupt_revision_write)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        xrd_records.revise_measured_pattern_peaks(store, original.artifact_id, **values)
    monkeypatch.setattr(store, "create_artifact", real_create)

    recovered = xrd_records.revise_measured_pattern_peaks(
        store, original.artifact_id, **values)
    lineage = store.list_artifacts(logical_id=original.logical_id)
    assert [item.revision for item in lineage] == [1, 2]
    assert recovered == lineage[-1]
    assert recovered.status == "finalized"
    assert recovered.previous_artifact_id == original.artifact_id
    assert recovered.artifact_id in store.get_material(material.material_id).measurement_references


@pytest.mark.parametrize("material_link_written", [False, True])
def test_tentative_match_retry_recovers_run_across_material_index_interruption(
        context, monkeypatch, material_link_written):
    store, project, material = context
    pattern = _save_pattern(store, project, material)
    references = _save_references(store, project, material)
    values = {
        "project_id": project.project_id,
        "material_id": material.material_id,
        "pattern_artifact_id": pattern.artifact_id,
        "reference_artifact_ids": [item.artifact_id for item in references],
        "creator": "Synthetic retry analyst",
        "reason": "Synthetic tentative-match retry",
    }
    real_update = store.update_material
    interrupted = {"done": False}

    def interrupt_material_index(material_id, **changes):
        if "associated_run_ids" in changes and not interrupted["done"]:
            interrupted["done"] = True
            if material_link_written:
                real_update(material_id, **changes)
            raise RuntimeError("synthetic interruption at run material index")
        return real_update(material_id, **changes)

    monkeypatch.setattr(store, "update_material", interrupt_material_index)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        xrd_records.save_tentative_match_run(store, **values)
    monkeypatch.setattr(store, "update_material", real_update)

    recovered = xrd_records.save_tentative_match_run(store, **values)
    repeated = xrd_records.save_tentative_match_run(store, **values)
    runs = store.list_runs(
        project_id=project.project_id,
        material_id=material.material_id,
        machine_id=xrd_records.XRD_MACHINE_ID,
    )
    assert recovered.run.run_id == repeated.run.run_id
    assert runs == [recovered.run]
    assert store.get_material(material.material_id).associated_run_ids == [recovered.run.run_id]
    reopened = xrd_records.load_tentative_match_run(
        store,
        recovered.run.run_id,
        project_id=project.project_id,
        material_id=material.material_id,
    )
    assert reopened.match_payload == recovered.match_payload
