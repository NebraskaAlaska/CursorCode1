"""Phase 3 persistence/security/provenance adversarial boundary tests.

All fixtures are synthetic and local. No real measurements, source papers, licensed reference
records, credentials, or external services are used.
"""
from __future__ import annotations

import csv
import io
import json

import pytest

from flyash_phreeqc_ml import workspace_store as ws
from flyash_phreeqc_ml.instruments import icp_review
from flyash_phreeqc_ml.instruments import xrd_advisory as xrd
from flyash_phreeqc_ml.literature import evidence_review
from flyash_phreeqc_ml.literature import evidence_schema as E
from flyash_phreeqc_ml.literature import evidence_store


@pytest.fixture()
def context(tmp_path):
    store = ws.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project("Synthetic security review project")
    material = store.create_material(project.project_id, "Synthetic security review material")
    return store, project, material


def _evidence_payload(**changes):
    payload = {
        "schema_kind": E.SCHEMA_LEACHING,
        "provenance": {
            "source": "manual",
            "title": "Synthetic structured source",
            "authors": ["Synthetic Author"],
            "year": 2026,
            "url": "https://example.invalid/synthetic-source",
            "discovery_route": {
                "method": "manual",
                "source": "manual",
                "executed_queries": [],
                "record_identifier": "SYN-SEC-1",
            },
        },
        "source_location": {"page": "7", "table": "Synthetic table 1"},
        "topic": "synthetic leaching",
        "claim": "synthetic structured claim",
        "reported_values": {"pH": 12.0},
        "units": {"pH": None},
        "conditions": {},
        "extraction_scope": E.SCOPE_MANUAL,
        "extraction_status": E.STATUS_MANUAL,
        "extraction_confidence": 0.5,
        "field_confidence": {"pH": 0.5},
        "conflicts": [],
        "notes": "Synthetic structured note",
    }
    payload.update(changes)
    return payload


def _xrd_metadata(**changes):
    payload = {
        "project_id": "prj_synthetic",
        "material_id": "mat_synthetic",
        "sample_id": "SYN-XRD-SEC",
        "radiation_source": "Cu Kalpha",
        "wavelength_angstrom": 1.5406,
    }
    payload.update(changes)
    return payload


def _reference_metadata(**changes):
    payload = {
        "source_name": "Synthetic user reference",
        "source_record_id": "SYN-REF-SEC",
        "phase_name": "Synthetic phase",
        "radiation_source": "Cu Kalpha",
        "wavelength_angstrom": 1.5406,
    }
    payload.update(changes)
    return payload


def test_artifact_ids_duplicate_paths_and_file_symlinks_fail_closed(context, tmp_path):
    store, project, material = context
    fixed_id = "art_" + "a" * 32
    store.create_artifact(
        project.project_id,
        material.material_id,
        ws.ARTIFACT_EXPERIMENT_PLAN,
        {"fixture": "synthetic"},
        artifact_id=fixed_id,
    )
    with pytest.raises(ws.DuplicateRecordError):
        store.create_artifact(
            project.project_id,
            material.material_id,
            ws.ARTIFACT_EXPERIMENT_PLAN,
            {"fixture": "duplicate synthetic"},
            artifact_id=fixed_id,
        )

    for unsafe_id in ("../" + fixed_id, "/tmp/" + fixed_id, "art_" + "g" * 32):
        with pytest.raises(ws.UnsafePathError):
            store.get_artifact(unsafe_id)

    outside = tmp_path / "outside-artifact.json"
    outside.write_text("{}", encoding="utf-8")
    linked_id = "art_" + "b" * 32
    artifact_dir = store.root / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    try:
        (artifact_dir / f"{linked_id}.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ws.UnsafePathError):
        store.get_artifact(linked_id)


def test_artifact_reads_reject_malformed_future_and_hash_tampered_records(context):
    store, project, material = context
    artifact = store.create_artifact(
        project.project_id,
        material.material_id,
        ws.ARTIFACT_SUSTAINABILITY_SCREEN,
        {"fixture": "synthetic"},
        payload={"screening": "advisory"},
    )
    artifact_dir = store.root / "artifacts"
    artifact_path = artifact_dir / f"{artifact.artifact_id}.json"
    document = json.loads(artifact_path.read_text(encoding="utf-8"))
    document["payload"]["screening"] = "tampered"
    artifact_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ws.MalformedRecordError, match="payload hash"):
        store.get_artifact(artifact.artifact_id)
    with pytest.raises(ws.MalformedRecordError, match="payload hash"):
        store.list_artifacts(project_id=project.project_id)

    malformed_id = "art_" + "c" * 32
    (artifact_dir / f"{malformed_id}.json").write_text(json.dumps({
        "schema_version": ws.SCHEMA_VERSION,
        "artifact_id": malformed_id,
    }), encoding="utf-8")
    with pytest.raises(ws.MalformedRecordError, match="record fields"):
        store.get_artifact(malformed_id)

    future_id = "art_" + "d" * 32
    (artifact_dir / f"{future_id}.json").write_text(json.dumps({
        "schema_version": ws.SCHEMA_VERSION + 1,
        "artifact_id": future_id,
    }), encoding="utf-8")
    with pytest.raises(ws.UnsupportedSchemaError):
        store.get_artifact(future_id)


def test_nested_nonfinite_and_secret_like_artifact_fields_leave_no_partial_record(context):
    store, project, material = context
    with pytest.raises(ws.MalformedRecordError, match="finite JSON-safe"):
        store.create_artifact(
            project.project_id,
            material.material_id,
            ws.ARTIFACT_EVIDENCE,
            {},
            payload={"nested": {"reported_value": float("inf")}},
        )
    with pytest.raises(ws.MalformedRecordError, match="secret-like"):
        store.create_artifact(
            project.project_id,
            material.material_id,
            ws.ARTIFACT_EVIDENCE,
            {},
            provenance={"nested": {"authorization": "Synthetic value; must not persist"}},
        )
    assert store.list_artifacts(project_id=project.project_id) == []


def test_terminal_creation_requires_review_metadata_and_terminal_record_is_immutable(context):
    store, project, material = context
    with pytest.raises(ws.MalformedRecordError, match="reviewer/resolver and reason"):
        store.create_artifact(
            project.project_id,
            material.material_id,
            ws.ARTIFACT_EXPERIMENT_PLAN,
            {"fixture": "synthetic"},
            status="finalized",
        )
    terminal = store.create_artifact(
        project.project_id,
        material.material_id,
        ws.ARTIFACT_EXPERIMENT_PLAN,
        {"fixture": "synthetic"},
        status="finalized",
        reviewer="Synthetic reviewer",
        reason="Synthetic red-team resolution",
    )
    assert terminal.reviewed_at
    with pytest.raises(ws.ImmutableRecordError):
        store.update_artifact(terminal.artifact_id, payload={"silent": "mutation"})


def test_icp_and_xrd_imports_refuse_oversized_sources_and_deceptive_extensions(monkeypatch):
    rows = [{"sample_id": "SYN-1", "element": "Ca", "concentration": 1.0}]
    monkeypatch.setattr(icp_review, "MAX_SOURCE_BYTES", 8)
    with pytest.raises(icp_review.IcpReviewError, match="safety limit"):
        icp_review.source_identity(
            rows, source_bytes=b"123456789", source_filename="synthetic.csv")
    with pytest.raises(icp_review.IcpReviewError, match="deceptive extensions"):
        icp_review.source_identity(
            rows, source_bytes=b"ok", source_filename="synthetic.csv.exe")

    measured = b"2theta,Intensity\n20,1\n"
    monkeypatch.setattr(xrd, "MAX_XRD_SOURCE_BYTES", len(measured) - 1)
    with pytest.raises(xrd.XrdDataError, match="safety limit"):
        xrd.import_measured_pattern_csv(
            measured, source_filename="synthetic.csv", metadata=_xrd_metadata())

    monkeypatch.setattr(xrd, "MAX_XRD_SOURCE_BYTES", 1024)
    with pytest.raises(xrd.XrdDataError, match="deceptive extensions"):
        xrd.import_measured_pattern_csv(
            measured, source_filename="synthetic.csv.exe", metadata=_xrd_metadata())
    with pytest.raises(xrd.XrdDataError, match="deceptive extensions"):
        xrd.import_reference_json(
            b'{"peaks":[{"two_theta":20}]}',
            source_filename="synthetic.json.csv",
            source_metadata=_reference_metadata(),
        )


def test_xrd_license_provenance_normalizes_missing_and_blocks_false_redistribution():
    reference_csv = b"2theta,Intensity\n20,100\n"
    unknown = xrd.import_reference_csv(
        reference_csv,
        source_filename="synthetic.csv",
        source_metadata=_reference_metadata(
            license_status="   ", redistribution_permission_status="  "),
    )
    assert unknown.license_status == xrd.LICENSE_UNKNOWN
    assert unknown.redistribution_permission_status == xrd.REDISTRIBUTION_UNKNOWN

    with pytest.raises(xrd.XrdDataError, match="cannot be recorded as redistributable"):
        xrd.import_reference_csv(
            reference_csv,
            source_filename="synthetic.csv",
            source_metadata=_reference_metadata(
                license_status="proprietary/restricted",
                redistribution_permission_status="permitted_by_cited_source",
                doi="10.0/synthetic-restricted",
                redistribution_basis="Synthetic restricted record statement",
            ),
        )
    for unsupported in ("permitted", "redistributable under license", "Permitted by license"):
        with pytest.raises(xrd.XrdDataError, match="natural-language permission claims"):
            xrd.import_reference_csv(
                reference_csv,
                source_filename="synthetic.csv",
                source_metadata=_reference_metadata(
                    license_status="CC-BY-4.0",
                    redistribution_permission_status=unsupported,
                    doi="10.0/synthetic-open",
                    redistribution_basis="Synthetic open-license statement",
                ),
            )
    with pytest.raises(xrd.XrdDataError, match="cannot be recorded as redistributable"):
        xrd.ExternalXrdReference(
            reference_id="xrdref_" + "a" * 32,
            phase_name="Synthetic direct-construction phase",
            peaks=[{"two_theta_deg": 20.0}],
            source_name="Synthetic direct source",
            doi="10.0/synthetic-restricted-direct",
            license_status="proprietary/restricted",
            redistribution_permission_status="permitted_by_cited_source",
            source_filename="synthetic.csv",
            source_sha256="a" * 64,
            source_metadata={
                "redistribution_basis": "Synthetic restricted direct statement"},
        )
    for secret_field in (
            "license_key", "license-key", "license key", "licenseKey",
            "vendorLicenseKey", "openaiApiKey", "authToken", "clientSecret"):
        with pytest.raises(xrd.XrdDataError, match="secret-like metadata"):
            xrd.import_reference_csv(
                reference_csv,
                source_filename="synthetic.csv",
                source_metadata=_reference_metadata(**{
                    secret_field: "Synthetic secret-like value must never persist"}),
            )


def test_xrd_refuses_filesystem_paths_and_requires_cited_redistribution_basis(tmp_path):
    measured_path = tmp_path / "synthetic-measured.csv"
    measured_path.write_text("2theta,intensity\n20,10\n", encoding="utf-8")
    with pytest.raises(xrd.XrdDataError, match="filesystem path"):
        xrd.import_measured_pattern_csv(
            measured_path,
            source_filename="synthetic-measured.csv",
            metadata={"sample_id": "Synthetic test data"},
        )

    reference_csv = b"2theta,intensity\n20,100\n"
    metadata = _reference_metadata(
        license_status="CC-BY-4.0",
        redistribution_permission_status="permitted_by_cited_source",
        url="https://example.invalid/synthetic-license",
    )
    with pytest.raises(xrd.XrdDataError, match="license citation or basis"):
        xrd.import_reference_csv(
            reference_csv,
            source_filename="synthetic-reference.csv",
            source_metadata=metadata,
        )
    accepted = xrd.import_reference_csv(
        reference_csv,
        source_filename="synthetic-reference.csv",
        source_metadata={**metadata, "redistribution_basis": "Synthetic license section 2"},
    )
    assert accepted.redistribution_permission_status == "permitted_by_cited_source"
    assert accepted.source_metadata["redistribution_basis"] == "Synthetic license section 2"


@pytest.mark.parametrize("forbidden_key", [
    "full text", "paper-text", "raw ai response", "chain-of-thought",
])
def test_nested_source_text_and_raw_model_aliases_cannot_persist(
        context, forbidden_key):
    store, project, material = context
    payload = _evidence_payload()
    payload["provenance"]["nested"] = {forbidden_key: "Synthetic content must not persist"}
    with pytest.raises(evidence_store.ForbiddenEvidenceContentError):
        evidence_review.create_manual_evidence(
            store,
            project.project_id,
            material.material_id,
            payload,
            creator="Synthetic author",
        )
    assert evidence_review.list_evidence(store, project_id=project.project_id) == []


def test_evidence_csv_neutralizes_all_formula_prefixes_without_changing_numeric_negative(context):
    store, project, material = context
    payload = _evidence_payload(
        topic="-synthetic topic",
        claim="+synthetic claim",
        notes="@synthetic note",
        pH=-2.5,
    )
    payload["provenance"]["title"] = '=HYPERLINK("synthetic")'
    record = evidence_review.create_manual_evidence(
        store,
        project.project_id,
        material.material_id,
        payload,
        creator="Synthetic author",
    )
    row = next(csv.DictReader(io.StringIO(
        evidence_review.export_csv([record], E.SCHEMA_LEACHING))))
    assert row["citation_title"].startswith("'=")
    assert row["topic"].startswith("'-")
    assert row["claim"].startswith("'+")
    assert row["notes"].startswith("'@")
    assert row["pH"] == "-2.5"


def test_multigeneration_evidence_revision_preserves_complete_review_history(context):
    store, project, material = context
    draft = evidence_review.create_manual_evidence(
        store,
        project.project_id,
        material.material_id,
        _evidence_payload(),
        creator="Synthetic author",
    )
    submitted = evidence_review.submit_for_review(
        store, draft.artifact_id, submitter="Synthetic author")
    reviewed_v1 = evidence_review.mark_reviewed(
        store,
        submitted.artifact_id,
        reviewer="Synthetic reviewer one",
        reason="Synthetic source check one",
    )
    revision_v2 = evidence_review.revise_evidence(
        store,
        reviewed_v1.artifact_id,
        {"claim": "Synthetic revised claim two"},
        creator="Synthetic author",
        reason="Synthetic interpretation revision two",
    )
    reviewed_v2 = evidence_review.mark_reviewed(
        store,
        revision_v2.artifact_id,
        reviewer="Synthetic reviewer two",
        reason="Synthetic source check two",
    )
    revision_v3 = evidence_review.revise_evidence(
        store,
        reviewed_v2.artifact_id,
        {"claim": "Synthetic revised claim three"},
        creator="Synthetic author",
        reason="Synthetic interpretation revision three",
    )

    historical_v1 = evidence_review.get_evidence(store, reviewed_v1.artifact_id)
    historical_v2 = evidence_review.get_evidence(store, reviewed_v2.artifact_id)
    assert historical_v1.status == E.REVIEW_REVIEWED
    assert historical_v1.reviewer == "Synthetic reviewer one"
    assert historical_v1.payload["review_reason"] == "Synthetic source check one"
    assert historical_v2.status == E.REVIEW_REVIEWED
    assert historical_v2.reviewer == "Synthetic reviewer two"
    assert revision_v3.status == E.REVIEW_NEEDS_REVIEW
    assert revision_v3.revision == 3
    assert revision_v3.previous_artifact_id == reviewed_v2.artifact_id
    assert revision_v3.previous_artifact_hash == ws.identity_hash(reviewed_v2.to_dict())
    assert revision_v3.payload["previous_revision_id"] == reviewed_v2.artifact_id
    assert revision_v3.payload["previous_revision_hash"] == reviewed_v2.payload_hash
    assert evidence_review.list_evidence(
        store, project_id=project.project_id, latest_only=True) == [revision_v3]
