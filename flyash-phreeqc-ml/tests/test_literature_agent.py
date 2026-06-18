"""Tests for the Literature Research Agent + Evidence Library (mocked APIs + AI; no network).

Covers the honesty contract: schemas validate, query generation works, ranking prioritises
relevant papers, extraction leaves missing values null + never fabricates + never stores the raw
model response, evidence requires provenance, confidence labels are correct, Google Scholar
scraping is NOT implemented, and the assistant routes an unsupported-domain prediction toward the
literature / evidence workflow.
"""
from __future__ import annotations

import ast
import json
import types
from pathlib import Path

import pytest

import flyash_phreeqc_ml as pkg
from flyash_phreeqc_ml.literature import evidence_schema as E
from flyash_phreeqc_ml.literature import evidence_store, extraction, ranking, research_agent
from flyash_phreeqc_ml.literature import search_clients as sc
from flyash_phreeqc_ml.literature import source_schema as ss
from flyash_phreeqc_ml.literature.source_schema import PaperCandidate

_LIT_DIR = Path(pkg.__file__).resolve().parent / "literature"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    from flyash_phreeqc_ml.ai import config as ai_config
    for name in (ai_config.API_KEY_ENV, ai_config.MODEL_ENV, ai_config.PROVIDER_ENV):
        monkeypatch.delenv(name, raising=False)
    ai_config.clear_runtime_overrides()
    monkeypatch.setattr(ai_config, "_secrets_get", lambda name: None)
    yield
    ai_config.clear_runtime_overrides()


class FakeAIClient:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.messages = self

    def create(self, **kwargs):
        payload = self._payloads.pop(0)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end")


def _openalex_payload(title, year=2021, doi="10.1/x", abstract="32 MPa compressive strength PET"):
    inv = {w: [i] for i, w in enumerate((abstract or "").split())}
    return {"results": [{"title": title, "publication_year": year, "doi": doi,
                         "cited_by_count": 40, "open_access": {"is_oa": True},
                         "authorships": [{"author": {"display_name": "A Smith"}}],
                         "abstract_inverted_index": inv}]}


# --------------------------------------------------------------------------- #
# Schemas validate correctly
# --------------------------------------------------------------------------- #
def test_schemas_validate():
    prov = E.Provenance(source="openalex", doi="10.1/abc", title="A paper", authors=["X"], year=2020)
    leach = E.LeachingEvidence(provenance=prov, leachant="NaOH", concentration_M=0.5, pH=12.8,
                               element_values_mM={"Ca": 5.0, "Si": None})
    comp = E.CompositeEvidence(provenance=prov, plastic_type="PET", compressive_strength_MPa=32.0)
    assert leach.has_provenance and comp.has_provenance
    assert leach.to_row()["leachant"] == "NaOH" and leach.to_row()["pH"] == 12.8
    assert comp.to_row()["compressive_strength_MPa"] == 32.0
    # A row with no provenance is not evidence.
    assert not E.LeachingEvidence().has_provenance
    # Columns exist for both schemas.
    assert "compressive_strength_MPa" in E.columns_for(E.SCHEMA_COMPOSITE)
    assert "pH" in E.columns_for(E.SCHEMA_LEACHING)


# --------------------------------------------------------------------------- #
# Query generation
# --------------------------------------------------------------------------- #
def test_query_generation_fly_ash_plastic_strength():
    qs = research_agent.generate_search_queries(
        "find papers for fly ash PET plastic compressive strength 28 days")
    joined = " ".join(qs).lower()
    assert any("fly ash" in q.lower() for q in qs)
    assert "plastic" in joined or "pet" in joined
    assert "strength" in joined and "mechanical" in joined          # scholarly augmentation
    assert research_agent.infer_domain("fly ash PET plastic compressive strength") == \
        research_agent.DOMAIN_COMPOSITE


def test_query_generation_class_c_fly_ash_leaching():
    qs = research_agent.generate_search_queries("Class C fly ash NaOH leaching Ca Si pH")
    joined = " ".join(qs).lower()
    assert any("fly ash" in q.lower() for q in qs)
    assert "leaching" in joined and "dissolution" in joined
    assert research_agent.infer_domain("Class C fly ash NaOH leaching pH") == \
        research_agent.DOMAIN_LEACHING


# --------------------------------------------------------------------------- #
# Ranking prioritises relevant papers
# --------------------------------------------------------------------------- #
def test_ranking_prioritises_relevant_papers():
    relevant = PaperCandidate(title="Compressive strength of fly ash PET plastic composite",
                              abstract="28 day compressive strength 32 MPa of fly ash PET composite",
                              year=2022, doi="10.1/a", citation_count=50, source="openalex")
    weak = PaperCandidate(title="A review of unrelated soil chemistry", abstract="soil pH",
                          year=2005, source="crossref")
    ranked = ranking.rank_candidates("fly ash PET plastic compressive strength", [weak, relevant],
                                     domain="composite")
    assert ranked[0].candidate is relevant
    assert ranked[0].score > ranked[1].score
    assert ranked[0].has_extractable_data and ranked[0].why


# --------------------------------------------------------------------------- #
# Extraction: missing → null, never fabricates, abstract scope, no raw response
# --------------------------------------------------------------------------- #
def test_extraction_leaves_missing_null_and_does_not_fabricate():
    cand = PaperCandidate(title="X", doi="10.1/x", abstract="compressive strength 32 MPa at 28 days",
                          source="openalex", authors=["A"])
    # The model reports ONLY compressive strength + curing; everything else must stay null.
    payload = {"values": {"compressive_strength_MPa": 32.0, "curing_time": "28 days"},
               "extraction_scope": "abstract", "confidence": 0.7}
    ev = extraction.extract_evidence(cand, E.SCHEMA_COMPOSITE, client=FakeAIClient([payload]))
    assert ev.compressive_strength_MPa == 32.0 and ev.curing_time == "28 days"
    assert ev.flexural_strength_MPa is None and ev.density_kg_m3 is None    # NOT fabricated
    assert ev.water_absorption_pct is None and ev.plastic_form is None
    assert ev.has_provenance and ev.provenance.doi == "10.1/x"              # value cites the paper


def test_extraction_ai_off_returns_empty_with_status():
    cand = PaperCandidate(title="X", doi="10.1/x", abstract="32 MPa", source="openalex")
    ev = extraction.extract_evidence(cand, E.SCHEMA_COMPOSITE)             # no client → AI off
    assert ev.extraction_status == E.STATUS_AI_OFF
    assert ev.compressive_strength_MPa is None                            # nothing invented
    assert ev.has_provenance


def test_extraction_no_abstract_cannot_extract():
    cand = PaperCandidate(title="X", doi="10.1/x", abstract=None, source="pubmed")
    ev = extraction.extract_evidence(cand, E.SCHEMA_LEACHING, client=FakeAIClient([{}]))
    assert ev.extraction_status == E.STATUS_NO_TEXT and ev.pH is None


def test_extraction_stores_no_raw_llm_response():
    cand = PaperCandidate(title="X", doi="10.1/x", abstract="pH 12.8 Ca 5 mM",
                          source="openalex", authors=["A"])
    sentinel = "HIDDEN_LIT_COT_SENTINEL"
    payload = {"values": {"pH": 12.8, "element_values_mM": {"Ca": 5.0}},
               "extraction_scope": "abstract", "confidence": 0.6,
               "hidden_chain_of_thought": sentinel, "raw_reasoning": sentinel}
    ev = extraction.extract_evidence(cand, E.SCHEMA_LEACHING, client=FakeAIClient([payload]))
    assert ev.pH == 12.8 and ev.element_values_mM.get("Ca") == 5.0
    assert sentinel not in json.dumps(ev.to_row())                        # raw text never stored
    # The paper's abstract text is not stored in the evidence row either.
    assert "pH 12.8 Ca 5 mM" not in json.dumps(ev.to_row())


# --------------------------------------------------------------------------- #
# Confidence labels (+ abstract-only cap)
# --------------------------------------------------------------------------- #
def test_confidence_labels():
    assert E.confidence_band(0.9) == E.CONF_HIGH
    assert E.confidence_band(0.5) == E.CONF_MEDIUM
    assert E.confidence_band(0.1) == E.CONF_LOW
    # An abstract-only extraction can never be labelled "high" (capped to medium).
    prov = E.Provenance(source="openalex", doi="10.1/x", title="t")
    ev = E.CompositeEvidence(provenance=prov, extraction_confidence=0.95,
                             extraction_scope=E.SCOPE_ABSTRACT)
    assert ev.confidence_label == E.CONF_MEDIUM
    ev_full = E.CompositeEvidence(provenance=prov, extraction_confidence=0.95,
                                  extraction_scope=E.SCOPE_FULL_TEXT)
    assert ev_full.confidence_label == E.CONF_HIGH


# --------------------------------------------------------------------------- #
# Evidence rows require source / provenance (store)
# --------------------------------------------------------------------------- #
def test_evidence_store_requires_provenance(tmp_path):
    path = evidence_store.evidence_path(tmp_path, E.SCHEMA_LEACHING)
    good = E.LeachingEvidence(provenance=E.Provenance(source="openalex", doi="10.1/x", title="t"),
                              pH=12.5)
    evidence_store.add_evidence(path, good)
    assert len(evidence_store.read_evidence(path)) == 1
    with pytest.raises(evidence_store.MissingProvenanceError):
        evidence_store.add_evidence(path, E.LeachingEvidence(pH=12.5))      # no source → rejected


def test_evidence_store_csv_export(tmp_path):
    path = evidence_store.evidence_path(tmp_path, E.SCHEMA_COMPOSITE)
    ev = E.CompositeEvidence(provenance=E.Provenance(source="openalex", doi="10.1/x", title="t"),
                             compressive_strength_MPa=30.0, extraction_scope=E.SCOPE_FULL_TEXT)
    evidence_store.add_evidence(path, ev)
    csv_text = evidence_store.export_csv(path, E.SCHEMA_COMPOSITE)
    assert "compressive_strength_MPa" in csv_text.splitlines()[0]
    assert "30.0" in csv_text


def test_evidence_store_refuses_protected_paths(tmp_path):
    with pytest.raises(ValueError):
        evidence_store.add_evidence(
            Path("flyash_phreeqc_ml/data/raw/evidence.jsonl"),
            E.LeachingEvidence(provenance=E.Provenance(source="x", doi="10.1/x")))


# --------------------------------------------------------------------------- #
# Google Scholar scraping is NOT implemented
# --------------------------------------------------------------------------- #
def test_google_scholar_is_not_supported():
    assert ss.GOOGLE_SCHOLAR_SUPPORTED is False
    assert "google_scholar" not in ss.SUPPORTED_SOURCES
    assert "google_scholar" not in sc.CLIENTS
    for name in ("google_scholar", "google scholar", "scholar", "gscholar"):
        with pytest.raises(ss.UnsupportedSourceError):
            ss.ensure_supported_source(name)
        with pytest.raises(ss.UnsupportedSourceError):
            sc.get_client(name)


def test_no_scholar_scraper_libraries_imported():
    """No literature module imports a Google-Scholar scraping library (scholarly / serpapi / etc.)."""
    scrapers = ("scholarly", "serpapi", "scholarly_py", "scholar", "selenium", "bs4",
                "beautifulsoup", "mechanicalsoup")
    offenders = []
    for path in _LIT_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for n in names:
                if any(s == (n or "").split(".")[0] for s in scrapers):
                    offenders.append(f"{path.name} -> {n}")
    assert not offenders, f"a scholar-scraper library is imported: {offenders}"


# --------------------------------------------------------------------------- #
# End-to-end research with mocked APIs
# --------------------------------------------------------------------------- #
def test_research_end_to_end_with_mocked_api(monkeypatch):
    monkeypatch.setattr(sc, "_http_get_json",
                        lambda url, params=None, headers=None, timeout=12.0:
                        _openalex_payload("Compressive strength of fly ash PET plastic composite")
                        if "openalex" in url else {"results": []})
    res = research_agent.research("fly ash PET plastic compressive strength 28 days",
                                  sources=[ss.SOURCE_OPENALEX])
    assert res.n_candidates == 1 and res.domain == research_agent.DOMAIN_COMPOSITE
    assert res.ranked[0].has_extractable_data
    assert res.ranked[0].candidate.source == "openalex"                    # provenance present


def test_search_clients_graceful_without_network(monkeypatch):
    monkeypatch.setattr(sc, "_http_get_json",
                        lambda url, params=None, headers=None, timeout=12.0: None)  # network fails
    results = sc.search_sources("anything", [ss.SOURCE_OPENALEX, ss.SOURCE_CROSSREF])
    assert all(not r.ok for r in results) and all(r.candidates == [] for r in results)


# --------------------------------------------------------------------------- #
# Assistant routes an unsupported-domain prediction toward literature/evidence
# --------------------------------------------------------------------------- #
def test_assistant_routes_unsupported_prediction_to_literature():
    from flyash_phreeqc_ml.agent import agent_orchestrator as orch
    from flyash_phreeqc_ml.agent import agent_state
    s = agent_state.AgentState()
    r = orch.respond(s, "predict the compressive strength of a fly ash + waste plastic composite")
    msg = r.assistant_message.lower()
    assert "cannot run a validated" in msg and "model yet" in msg
    assert "literature" in msg and ("evidence" in msg or "dataset" in msg)
