"""Phase 3 durable evidence provenance, review, revision, and export contracts."""
from __future__ import annotations

import csv
import io
import json
import types

import pytest

from flyash_phreeqc_ml import workspace_store
from flyash_phreeqc_ml.literature import evidence_review
from flyash_phreeqc_ml.literature import evidence_schema as E
from flyash_phreeqc_ml.literature import evidence_store, extraction, research_agent
from flyash_phreeqc_ml.literature.source_schema import PaperCandidate, SearchResult


@pytest.fixture()
def context(tmp_path):
    store = workspace_store.WorkspaceStore(tmp_path / "workspace")
    project = store.create_project("Synthetic evidence project")
    material = store.create_material(project.project_id, "Synthetic evidence material")
    return store, project, material


def _manual_payload(*, title="Structured source", source="manual", doi=None):
    return {
        "schema_kind": E.SCHEMA_LEACHING,
        "provenance": {
            "source": source,
            "doi": doi,
            "title": title,
            "authors": ["A. Researcher"],
            "year": 2024,
            "url": "https://example.invalid/evidence",
            "discovery_route": {
                "method": "manual",
                "source": source,
                "executed_queries": [],
                "record_identifier": "manual-1",
            },
        },
        "source_location": {
            "page": "7",
            "page_range": "7-8",
            "table": "Table 2",
            "figure": "Figure 3",
            "section": "Methods",
            "supplementary_item": "Table S1",
            "dataset_record_identifier": "dataset-row-4",
            "note": "reported condition row",
        },
        "topic": "alkaline leaching",
        "claim": "reported leachate pH",
        "reported_values": {"pH": 12.1, "unreported": None},
        "units": {"pH": None},
        "conditions": {"NaOH_M": 0.5},
        "extraction_scope": E.SCOPE_MANUAL,
        "extraction_status": E.STATUS_MANUAL,
        "extraction_confidence": 0.8,
        "field_confidence": {"pH": 0.9},
        "conflicts": ["temperature_C"],
        "notes": "structured note only",
    }


class FakeAIClient:
    def __init__(self, payload):
        self.payload = payload
        self.messages = self

    def create(self, **kwargs):
        return types.SimpleNamespace(content=[types.SimpleNamespace(
            type="text", text=json.dumps(self.payload))])


def test_provenance_requires_doi_or_title_plus_source(context):
    store, project, material = context
    assert not E.Provenance(title="Bare title").is_present
    with pytest.raises(evidence_store.MissingProvenanceError):
        evidence_review.create_manual_evidence(
            store, project.project_id, material.material_id,
            _manual_payload(title="Bare title", source=""), creator="author")
    doi_only = _manual_payload(title=None, source="", doi="10.1000/example")
    created = evidence_review.create_manual_evidence(
        store, project.project_id, material.material_id, doi_only, creator="author")
    assert created.payload["provenance"]["doi"] == "10.1000/example"


def test_manual_lifecycle_restart_review_and_revision(context):
    store, project, material = context
    draft = evidence_review.create_manual_evidence(
        store, project.project_id, material.material_id, _manual_payload(), creator="author")
    assert draft.status == E.REVIEW_DRAFT
    assert draft.payload["reported_values"]["unreported"] is None
    assert draft.payload["source_location"]["figure"] == "Figure 3"

    linked = evidence_review.link_material(store, draft.artifact_id)
    assert draft.artifact_id in linked.evidence_references
    reopened_store = workspace_store.WorkspaceStore(store.root)
    reopened = evidence_review.get_evidence(reopened_store, draft.artifact_id)
    assert reopened.payload_hash == draft.payload_hash

    edited = evidence_review.edit_draft(
        reopened_store, draft.artifact_id, {"notes": "edited structured note"},
        editor="author", reason="corrected note")
    submitted = evidence_review.submit_for_review(
        reopened_store, edited.artifact_id, submitter="author")
    assert submitted.status == E.REVIEW_NEEDS_REVIEW
    reviewed = evidence_review.mark_reviewed(
        reopened_store, submitted.artifact_id, reviewer="reviewer", reason="checked source")
    assert reviewed.status == E.REVIEW_REVIEWED
    assert reviewed.payload["review_status"] == E.REVIEW_REVIEWED
    assert reviewed.payload["review_time"] and reviewed.reviewed_at

    with pytest.raises(evidence_review.InvalidEvidenceTransition):
        evidence_review.edit_draft(
            reopened_store, reviewed.artifact_id, {"notes": "silent mutation"}, editor="x")
    revision = evidence_review.revise_evidence(
        reopened_store, reviewed.artifact_id, {"notes": "new interpretation"},
        creator="author", reason="new source check")
    assert revision.status == E.REVIEW_NEEDS_REVIEW
    assert revision.revision == reviewed.revision + 1
    assert revision.payload["previous_revision_id"] == reviewed.artifact_id
    assert revision.payload["previous_revision_hash"] == reviewed.payload_hash
    assert evidence_review.get_evidence(reopened_store, reviewed.artifact_id).status == \
        E.REVIEW_REVIEWED
    assert evidence_review.list_evidence(
        reopened_store, project_id=project.project_id,
        review_status=E.REVIEW_REVIEWED) == [reviewed]
    assert evidence_review.list_evidence(
        reopened_store, project_id=project.project_id, latest_only=True) == [revision]


def test_lifecycle_requires_named_human_actions_and_revision_reason(context):
    store, project, material = context
    with pytest.raises(evidence_review.EvidenceReviewError):
        evidence_review.create_manual_evidence(
            store, project.project_id, material.material_id, _manual_payload(), creator="")
    draft = evidence_review.create_manual_evidence(
        store, project.project_id, material.material_id, _manual_payload(), creator="author")
    with pytest.raises(evidence_review.EvidenceReviewError):
        evidence_review.edit_draft(store, draft.artifact_id, {"notes": "change"}, editor="")
    with pytest.raises(evidence_review.EvidenceReviewError):
        evidence_review.submit_for_review(store, draft.artifact_id, submitter="")
    submitted = evidence_review.submit_for_review(
        store, draft.artifact_id, submitter="author")
    with pytest.raises(evidence_review.EvidenceReviewError):
        evidence_review.mark_reviewed(
            store, submitted.artifact_id, reviewer="", reason="checked")
    reviewed = evidence_review.mark_reviewed(
        store, submitted.artifact_id, reviewer="reviewer", reason="checked")
    with pytest.raises(evidence_review.EvidenceReviewError):
        evidence_review.revise_evidence(
            store, reviewed.artifact_id, {"notes": "change"}, creator="author", reason="")


def test_rejected_or_unreviewed_never_lists_or_exports_as_reviewed(context):
    store, project, material = context
    needs_review = evidence_review.create_ai_evidence(
        store, project.project_id, material.material_id, _manual_payload(), creator="extractor")
    assert needs_review.status == E.REVIEW_NEEDS_REVIEW
    assert evidence_review.list_evidence(
        store, project_id=project.project_id, review_status=E.REVIEW_REVIEWED) == []
    rejected = evidence_review.reject_evidence(
        store, needs_review.artifact_id, reviewer="reviewer", reason="source mismatch")
    assert rejected.status == E.REVIEW_REJECTED
    package = json.loads(evidence_review.export_package([rejected], E.SCHEMA_LEACHING))
    assert package["metadata"]["reviewed_count"] == 0
    assert package["records"][0]["review_status"] == E.REVIEW_REJECTED


def test_ai_abstract_extraction_cannot_self_label_full_text():
    candidate = PaperCandidate(
        title="Abstract source", doi="10.1000/abstract", source="openalex",
        query="fly ash leaching", abstract="pH 12.1 was reported")
    extracted = extraction.extract_evidence(
        candidate,
        E.SCHEMA_LEACHING,
        client=FakeAIClient({
            "values": {"pH": 12.1},
            "extraction_scope": "full_text",
            "confidence": 0.99,
            "field_confidence": {"pH": 0.8},
        }),
    )
    assert extracted.extraction_scope == E.SCOPE_ABSTRACT
    assert extracted.review_status == E.REVIEW_NEEDS_REVIEW
    assert extracted.creation_origin == "ai"
    assert extracted.confidence_label == E.CONF_MEDIUM
    route = extracted.provenance.to_dict()["discovery_route"]
    assert route["executed_queries"] == ["fly ash leaching"]


@pytest.mark.parametrize("forbidden", [
    "abstract", "full_abstract", "full_text", "fullPaper", "fullPaperText",
    "raw_llm_response", "rawModelOutput", "model-response",
])
def test_source_text_and_raw_model_response_fields_are_rejected(context, forbidden):
    store, project, material = context
    payload = _manual_payload()
    payload[forbidden] = "must not persist"
    with pytest.raises(evidence_store.ForbiddenEvidenceContentError):
        evidence_review.create_manual_evidence(
            store, project.project_id, material.material_id, payload, creator="author")


def test_exports_are_deterministic_rich_and_csv_safe_for_text_only(context):
    store, project, material = context
    payload = _manual_payload(title="=HYPERLINK(\"bad\")")
    payload["reported_values"] = {"signed_scientific_value": -2.5, "missing": None}
    payload["pH"] = -2.5
    draft = evidence_review.create_manual_evidence(
        store, project.project_id, material.material_id, payload, creator="author")
    records = [draft]
    first = evidence_review.export_json(records)
    assert first == evidence_review.export_json(records)
    assert json.loads(first)[0]["provenance"]["title"].startswith("=")
    csv_text = evidence_review.export_csv(records, E.SCHEMA_LEACHING)
    row = next(csv.DictReader(io.StringIO(csv_text)))
    assert row["citation_title"].startswith("'=")
    assert row["pH"] == "-2.5"  # numeric scientific value is not text-sanitized
    package = json.loads(evidence_review.export_package(records, E.SCHEMA_LEACHING))
    assert package["record_type"] == "evidence"
    assert package["records"][0]["source_location"]["table"] == "Table 2"


def test_research_result_labels_only_executed_queries(monkeypatch):
    def fake_search(query, sources, *, limit):
        return [SearchResult(source="openalex", query=query, candidates=[])]

    monkeypatch.setattr(research_agent.search_clients, "search_sources", fake_search)
    result = research_agent.research(
        "Class C fly ash NaOH leaching Ca Si pH", sources=["openalex"])
    assert result.queries == [result.generated_queries[0]]
    assert len(result.generated_queries) > len(result.queries)
    assert result.to_dict()["queries"] == result.queries


def test_legacy_jsonl_is_read_tolerantly_without_rewrite(tmp_path):
    path = tmp_path / "legacy.jsonl"
    legacy = {"schema_kind": "leaching", "pH": 11.8,
              "provenance": {"source": "openalex", "title": "Legacy"}}
    original = json.dumps(legacy) + "\n{malformed\n"
    path.write_text(original, encoding="utf-8")
    assert evidence_store.read_evidence(path) == [legacy]
    assert path.read_text(encoding="utf-8") == original
    assert "11.8" in evidence_store.to_csv([legacy], E.SCHEMA_LEACHING)


def test_cross_project_material_binding_and_filtering(context):
    store, project, material = context
    second = store.create_project("Other project")
    other_material = store.create_material(second.project_id, "Other material")
    with pytest.raises(workspace_store.WorkspaceStoreError):
        evidence_review.create_manual_evidence(
            store, project.project_id, other_material.material_id,
            _manual_payload(), creator="author")
    record = evidence_review.create_manual_evidence(
        store, project.project_id, material.material_id, _manual_payload(), creator="author")
    with pytest.raises(evidence_review.EvidenceReviewError):
        evidence_review.get_evidence(
            store, record.artifact_id, project_id=second.project_id)
    assert evidence_review.list_evidence(store, project_id=second.project_id) == []
    assert evidence_review.list_evidence(store, project_id=project.project_id) == [record]


def test_source_location_accepts_plain_contract_aliases(context):
    store, project, material = context
    payload = _manual_payload()
    payload["source_location"] = {
        "supplement": "Figure S2",
        "dataset": "record-7",
        "free_note": "caption details",
    }
    record = evidence_review.create_manual_evidence(
        store, project.project_id, material.material_id, payload, creator="author")
    assert record.payload["source_location"] == {
        "page": None,
        "page_range": None,
        "table": None,
        "figure": None,
        "section": None,
        "supplementary_item": "Figure S2",
        "dataset_record_identifier": "record-7",
        "note": "caption details",
    }
