"""Literature **research agent** — turn a user query into ranked, cited paper candidates (no AI).

Given a natural query ("find papers for fly ash PET plastic compressive strength 28 days") it:
generates a few scholarly search queries, searches the **official** APIs (via
:mod:`search_clients`), de-duplicates, **ranks transparently** (:mod:`ranking`), explains why each
paper is useful, and flags whether each likely has extractable numeric data. It **never fabricates
a value** — extraction (AI) is a separate, explicit step (:mod:`extraction`). Pure orchestration:
no AI, no result-path; the network lives only in the mockable client layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import ranking, search_clients
from . import source_schema

# Domain tags the evidence schemas + ranking understand.
DOMAIN_LEACHING = "leaching"
DOMAIN_COMPOSITE = "composite"

# Phrases to strip from a query so it reads as a scholarly search.
_NOISE_RE = re.compile(
    r"\b(find|search|get|show|me|some|please|papers?|articles?|studies?|study|literature|for|about|"
    r"on|the|a|an|of)\b", re.I)

_COMPOSITE_CUES = ("plastic", "pet", "hdpe", "pp ", "polymer", "composite", "compressive",
                   "flexural", "strength", "mechanical", "geopolymer", "brick", "mortar", "concrete",
                   "fibre", "fiber", "paver", "binder", "curing")
_LEACHING_CUES = ("leach", "dissolution", "dissolv", "icp", "ph", "naoh", "koh", "hcl", "acid",
                  "release", "molar", "leachate", "geochem")


def infer_domain(query: str) -> str:
    """Guess the evidence domain from the query (composite/mechanical vs leaching/geochemistry)."""
    low = " " + str(query or "").lower() + " "
    comp = sum(1 for c in _COMPOSITE_CUES if c in low)
    leach = sum(1 for c in _LEACHING_CUES if c in low)
    return DOMAIN_COMPOSITE if comp >= leach and comp > 0 else DOMAIN_LEACHING


def _clean(query: str) -> str:
    s = _NOISE_RE.sub(" ", str(query or ""))
    return " ".join(s.split())


def generate_search_queries(query_text: str, domain: str | None = None) -> list:
    """Generate a few scholarly search-query variants (deterministic; deduped, ≤4)."""
    domain = domain or infer_domain(query_text)
    core = _clean(query_text)
    queries = [core] if core else []
    low = core.lower()
    if domain == DOMAIN_COMPOSITE:
        augments = ["compressive strength mechanical properties", "geopolymer composite", "durability"]
    else:
        augments = ["leaching dissolution", "ICP elemental release", "pH speciation"]
    for aug in augments:
        extra = " ".join(w for w in aug.split() if w.lower() not in low)
        if extra:
            q = (core + " " + extra).strip()
            if q and q not in queries:
                queries.append(q)
    return queries[:4] or [str(query_text or "").strip()]


@dataclass
class ResearchResult:
    """The outcome of a literature search: ranked candidates + provenance of the search itself."""

    query: str
    domain: str
    queries: list = field(default_factory=list)              # the scholarly queries run
    ranked: list = field(default_factory=list)               # list[ranking.ScoredCandidate]
    source_summaries: list = field(default_factory=list)     # list[dict] (per-source outcome)
    note: str | None = None

    @property
    def n_candidates(self) -> int:
        return len(self.ranked)

    @property
    def n_extractable(self) -> int:
        return sum(1 for s in self.ranked if s.has_extractable_data)

    def to_dict(self) -> dict:
        return {"query": self.query, "domain": self.domain, "queries": list(self.queries),
                "n_candidates": self.n_candidates, "n_extractable": self.n_extractable,
                "source_summaries": list(self.source_summaries), "note": self.note,
                "ranked": [s.to_dict() for s in self.ranked]}


def research(query_text: str, *, domain: str | None = None, sources=None,
             limit: int = 10, top_n: int = 20) -> ResearchResult:
    """Run the literature search and return ranked, cited candidates (never raises).

    Searches each selected official source for the *primary* scholarly query, merges + de-dups, and
    ranks transparently. Extraction is a separate explicit step — this returns candidates only.
    """
    domain = domain or infer_domain(query_text)
    queries = generate_search_queries(query_text, domain)
    primary = queries[0] if queries else str(query_text or "")
    sources = list(sources or source_schema.DEFAULT_SEARCH_SOURCES)

    results = search_clients.search_sources(primary, sources, limit=limit)
    candidates = search_clients.merge_dedup(results)
    ranked = ranking.rank_candidates(primary, candidates, domain=domain, top_n=top_n)
    summaries = [r.to_summary() for r in results]
    note = None
    if not candidates:
        note = ("No candidates returned — the official scholarly APIs may be unreachable here, or "
                "the query is too narrow. You can also add a paper manually.")
    return ResearchResult(query=str(query_text or ""), domain=domain, queries=queries,
                          ranked=ranked, source_summaries=summaries, note=note)
