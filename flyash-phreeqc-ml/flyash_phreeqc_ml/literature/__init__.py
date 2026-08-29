"""Literature research + evidence-library layer (off the scientific result path).

A research-and-evidence layer that searches **official / reliable scholarly APIs** (OpenAlex,
Crossref, Semantic Scholar, PubMed), ranks the results transparently, optionally extracts
structured experimental variables with AI (every value cited, missing → null, confidence
explicit), and routes new records into the durable evidence-review lifecycle. Historical per-run
JSONL remains read-only compatibility input. This package does **not** train a model, predict
strength, or scrape Google Scholar.

Modules: :mod:`source_schema` (sources + paper candidate), :mod:`evidence_schema` (leaching +
composite evidence), :mod:`search_clients` (the API clients — mockable), :mod:`ranking`
(transparent relevance ranking), :mod:`extraction` (AI extraction — the only AI-touching module),
:mod:`evidence_store` (read-only historical JSONL compatibility + safe exports),
:mod:`research_agent` (query generation + search orchestration), and :mod:`evidence_review` (the
single writable durable evidence lifecycle over the workspace artifact store).

Boundary (pinned by ``tests/test_ai_boundary.py``): only :mod:`extraction` may import the AI
client; nothing here imports an executor or the comparison/residual/mapping result path, and the
result path never imports this package — extracted evidence is a *library for the future*, never a
measured/validated value.
"""
from __future__ import annotations

from . import (
    evidence_review,
    evidence_schema,
    evidence_store,
    extraction,
    ranking,
    research_agent,
    search_clients,
    source_schema,
)

__all__ = [
    "source_schema", "evidence_schema", "search_clients", "ranking", "extraction",
    "evidence_store", "evidence_review", "research_agent",
]
