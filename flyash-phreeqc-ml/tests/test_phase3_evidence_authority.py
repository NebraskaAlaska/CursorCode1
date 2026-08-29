"""Evidence-authority, AI-review, and structured-content acceptance tests."""
from __future__ import annotations

import json

import pytest

from flyash_phreeqc_ml import workspace_store
from flyash_phreeqc_ml.literature import evidence_review
from flyash_phreeqc_ml.literature import evidence_schema as E
from flyash_phreeqc_ml.literature import evidence_store
from ui import evidence_library


def _payload() -> dict:
    return {
        "schema_kind": E.SCHEMA_LEACHING,
        "provenance": {
            "source": "synthetic test source",
            "title": "Synthetic evidence authority fixture",
            "authors": ["Synthetic Author"],
            "year": 2026,
            "discovery_route": {
                "method": "manual",
                "source": "synthetic test source",
                "executed_queries": [],
                "record_identifier": "SYN-EVIDENCE-AUTHORITY-1",
            },
        },
        "source_location": {"page": "4", "table": "Table 1"},
        "topic": "synthetic authority test",
        "claim": "A concise structured claim.",
        "reported_values": {"pH": 12.0, "unreported": None},
        "units": {"pH": None},
        "conditions": {"temperature_C": 20.0},
        "extraction_scope": E.SCOPE_MANUAL,
        "extraction_status": E.STATUS_MANUAL,
        "extraction_confidence": 0.5,
        "field_confidence": {"pH": 0.5},
        "conflicts": [],
        "notes": "Concise structured note; no copied source prose.",
    }


@pytest.fixture()
def durable_context(tmp_path):
    store = workspace_store.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project("Synthetic evidence authority project")
    material = store.create_material(project.project_id, "Synthetic evidence material")
    context = {
        "active_project_id": project.project_id,
        "active_material_id": material.material_id,
        "active_run_id": None,
    }
    return store, project, material, context


def test_legacy_jsonl_is_read_only_while_legacy_actions_route_to_artifacts(
    tmp_path, durable_context,
):
    store, project, material, context = durable_context
    legacy_path = tmp_path / "legacy" / "evidence_leaching.jsonl"
    legacy_path.parent.mkdir()
    historical = {"schema_kind": E.SCHEMA_LEACHING, "pH": 11.5}
    original = json.dumps(historical) + "\n"
    legacy_path.write_text(original, encoding="utf-8")

    evidence = E.LeachingEvidence(
        provenance=E.Provenance(
            source="synthetic test source", title="Synthetic routed evidence"),
        source_location=E.SourceLocation(page="4"),
        pH=12.0,
        notes="Concise structured note.",
    )
    manual = evidence_library._create_durable_row(
        store, context, evidence, creator="Synthetic researcher", ai_created=False)
    ai = evidence_library._create_durable_row(
        store, context, evidence, creator="AI-assisted extraction", ai_created=True)

    assert manual.record_type == workspace_store.ARTIFACT_EVIDENCE
    assert manual.status == E.REVIEW_DRAFT
    assert manual.payload["creation_origin"] == "manual"
    assert ai.status == E.REVIEW_NEEDS_REVIEW
    assert ai.payload["creation_origin"] == "ai"
    assert legacy_path.read_text(encoding="utf-8") == original
    assert evidence_store.read_evidence(legacy_path) == [historical]
    with pytest.raises(evidence_store.LegacyEvidenceReadOnlyError):
        evidence_store.add_evidence(legacy_path, evidence)
    with pytest.raises(evidence_store.LegacyEvidenceReadOnlyError):
        evidence_store.save_evidence(legacy_path, [evidence])

    linked = store.get_material(material.material_id).evidence_references
    assert manual.artifact_id in linked and ai.artifact_id in linked
    listed_ids = {
        record.artifact_id for record in evidence_review.list_evidence(
            store, project_id=project.project_id)
    }
    assert listed_ids == {manual.artifact_id, ai.artifact_id}


def test_ai_payload_cannot_self_review_or_disguise_its_origin(durable_context):
    store, project, material, _context = durable_context
    payload = _payload()
    payload.update({
        "review_status": E.REVIEW_REVIEWED,
        "reviewer": "AI extractor",
        "review_time": "2026-08-29T00:00:00Z",
        "review_reason": "self-approved",
        "creation_origin": "manual",
    })
    record = evidence_review.create_ai_evidence(
        store, project.project_id, material.material_id,
        payload, creator="AI-assisted extraction")

    assert record.status == E.REVIEW_NEEDS_REVIEW
    assert record.payload["review_status"] == E.REVIEW_NEEDS_REVIEW
    assert record.payload["reviewer"] is None
    assert record.payload["review_time"] is None
    assert record.payload["review_reason"] is None
    assert record.payload["creation_origin"] == "ai"
    misrouted_payload = _payload()
    misrouted_payload.update({
        "creation_origin": "ai",
        "review_status": E.REVIEW_REVIEWED,
        "extraction_scope": E.SCOPE_FULL_TEXT,
    })
    misrouted = evidence_review.create_manual_evidence(
        store, project.project_id, material.material_id,
        misrouted_payload, creator="Incorrect manual wrapper caller")
    assert misrouted.status == E.REVIEW_NEEDS_REVIEW
    assert misrouted.payload["creation_origin"] == "ai"
    assert misrouted.payload["extraction_scope"] == E.SCOPE_ABSTRACT
    with pytest.raises(evidence_review.InvalidEvidenceTransition):
        evidence_review.submit_for_review(
            store, record.artifact_id, submitter="AI-assisted extraction")

    reviewed = evidence_review.mark_reviewed(
        store, record.artifact_id,
        reviewer="Named human reviewer", reason="Explicit source-location check")
    assert reviewed.status == E.REVIEW_REVIEWED
    assert reviewed.payload["creation_origin"] == "ai"
    with pytest.raises(evidence_review.EvidenceReviewError):
        evidence_review.revise_evidence(
            store, reviewed.artifact_id, {"creation_origin": "manual"},
            creator="Synthetic researcher", reason="Attempted origin rewrite")


def test_generic_artifact_cannot_forge_ai_full_text_evidence_scope(durable_context):
    store, project, material, _context = durable_context
    payload = evidence_review.normalize_evidence_payload(
        _payload(),
        project_id=project.project_id,
        material_id=material.material_id,
        ai_created=False,
        review_status=E.REVIEW_NEEDS_REVIEW,
    )
    payload["creation_origin"] = "ai"
    payload["extraction_scope"] = E.SCOPE_FULL_TEXT
    artifact = store.create_artifact(
        project.project_id,
        material.material_id,
        workspace_store.ARTIFACT_EVIDENCE,
        evidence_review._scientific_snapshot(payload),
        status=E.REVIEW_NEEDS_REVIEW,
        payload=payload,
        source_identity=evidence_review._source_identity(payload),
        creator="Synthetic generic-store caller",
        provenance=payload["provenance"],
    )

    with pytest.raises(evidence_review.EvidenceReviewError, match="abstract-only"):
        evidence_review.get_evidence(store, artifact.artifact_id)
    with pytest.raises(evidence_review.EvidenceReviewError, match="abstract-only"):
        evidence_review.export_json([artifact])


@pytest.mark.parametrize("forbidden", [
    {"verbatimAbstract": "copied source prose"},
    {"abstract_content": "copied source prose"},
    {"articleBody": "copied source prose"},
    {"manuscript-copy": "copied source prose"},
    {"sourceDocumentText": "copied source prose"},
    {"documentText": "copied source prose"},
    {"paper_html": "copied source markup"},
    {"pdf_content": "copied source prose"},
    {"full_content": "copied source prose"},
    {"llmCompletion": "raw completion"},
    {"rawLLM": "raw model response"},
    {"ai_generation": "raw generation"},
    {"assistantMessage": "raw assistant message"},
    {"modelTranscript": "raw transcript"},
    {"completion_payload": "raw completion"},
    {"chatMessages": "raw chat output"},
    {"hiddenReasoning": "hidden reasoning"},
    {"reasoning_trace": "reasoning trace"},
    {"llm": {"response": "nested raw response"}},
    {"raw": {"completion": "nested raw completion"}},
    {"source": {"document": "nested source document"}},
])
def test_semantic_source_copy_and_raw_model_aliases_are_rejected(
    durable_context, forbidden,
):
    store, project, material, _context = durable_context
    payload = _payload()
    payload["extra"] = forbidden
    with pytest.raises(evidence_store.ForbiddenEvidenceContentError):
        evidence_review.create_manual_evidence(
            store, project.project_id, material.material_id,
            payload, creator="Synthetic researcher")


def test_concise_allowlisted_structured_notes_claims_and_values_are_allowed(
    durable_context,
):
    store, project, material, _context = durable_context
    payload = _payload()
    payload["reported_values"]["structured_label"] = "Concise test label"
    payload["conditions"]["instrument_model"] = "Synthetic instrument"
    record = evidence_review.create_manual_evidence(
        store, project.project_id, material.material_id,
        payload, creator="Synthetic researcher")
    assert record.payload["notes"] == payload["notes"]
    assert record.payload["claim"] == payload["claim"]
    assert record.payload["reported_values"]["structured_label"] == "Concise test label"
    assert record.payload["conditions"]["instrument_model"] == "Synthetic instrument"


@pytest.mark.parametrize("field", ["claim", "notes"])
def test_narrative_fields_refuse_full_source_or_raw_model_content(
    durable_context, field,
):
    store, project, material, _context = durable_context
    payload = _payload()
    payload[field] = "Full abstract: " + "copied " * 100
    with pytest.raises(evidence_store.ForbiddenEvidenceContentError):
        evidence_review.create_manual_evidence(
            store, project.project_id, material.material_id,
            payload, creator="Synthetic researcher")

    payload = _payload()
    payload[field] = '{"choices":[{"message":{"role":"assistant","content":"raw"}}]}'
    with pytest.raises(evidence_store.ForbiddenEvidenceContentError):
        evidence_review.create_manual_evidence(
            store, project.project_id, material.material_id,
            payload, creator="Synthetic researcher")


def test_unknown_fields_and_nested_oversize_content_fail_closed(durable_context):
    store, project, material, _context = durable_context
    unknown = _payload()
    unknown["abstract_summary"] = "An unbounded non-schema channel"
    with pytest.raises(evidence_store.ForbiddenEvidenceContentError, match="unsupported"):
        evidence_review.create_manual_evidence(
            store, project.project_id, material.material_id,
            unknown, creator="Synthetic researcher")

    oversized = _payload()
    oversized["conditions"]["hidden_text"] = (
        "copied " * (evidence_store.MAX_EVIDENCE_STRING_WORDS + 1))
    with pytest.raises(evidence_store.ForbiddenEvidenceContentError, match="concise"):
        evidence_review.create_manual_evidence(
            store, project.project_id, material.material_id,
            oversized, creator="Synthetic researcher")

    oversized_container = _payload()
    oversized_container["reported_values"] = {
        f"field_{index}": index
        for index in range(evidence_store.MAX_EVIDENCE_CONTAINER_ITEMS + 1)
    }
    with pytest.raises(evidence_store.ForbiddenEvidenceContentError, match="field limit"):
        evidence_review.create_manual_evidence(
            store, project.project_id, material.material_id,
            oversized_container, creator="Synthetic researcher")

    chunked_payload = _payload()
    concise_chunk = "bounded synthetic prose " * 15
    chunked_payload["conditions"] = {
        f"structured_field_{index}": concise_chunk
        for index in range(evidence_store.MAX_EVIDENCE_CONTAINER_ITEMS)
    }
    with pytest.raises(evidence_store.ForbiddenEvidenceContentError, match="payload limit"):
        evidence_review.create_manual_evidence(
            store, project.project_id, material.material_id,
            chunked_payload, creator="Synthetic researcher")

    with pytest.raises(evidence_store.ForbiddenEvidenceContentError, match="concise"):
        evidence_store.to_package(
            [_payload()],
            E.SCHEMA_LEACHING,
            metadata={"notes": "raw exported source prose " * 100},
        )

    raw_envelope = _payload()
    raw_envelope["conditions"]["nested_generation"] = {
        "id": "synthetic-completion",
        "object": "chat.completion",
        "model": "synthetic-model",
        "choices": [{
            "message": {"role": "assistant", "content": "raw generated response"},
            "finish_reason": "stop",
        }],
    }
    with pytest.raises(evidence_store.ForbiddenEvidenceContentError, match="raw model-response"):
        evidence_review.create_manual_evidence(
            store, project.project_id, material.material_id,
            raw_envelope, creator="Synthetic researcher")
