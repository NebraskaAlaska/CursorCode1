"""Transparent, **deterministic** relevance ranking for paper candidates (pure; no AI, no network).

Ranking is rule-based and explainable — no learned weights, no AI — so the user can see *why* a
paper ranks where it does and whether it likely has **extractable numeric data**. It never
fabricates anything: it only scores the title/abstract/metadata a search already returned.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import source_schema

# Words too common to be useful query terms.
_STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "with", "by", "from", "at",
    "find", "paper", "papers", "study", "studies", "using", "use", "data", "value", "values",
    "after", "via", "their", "this", "that", "is", "are", "was", "were",
}

# Domain keyword sets that signal *extractable experimental data* in an abstract.
_LEACH_KEYWORDS = ("leach", "dissolution", "dissolv", "icp", "release", "ph ", " ph", "molar",
                   "concentration", "leachate", "mg/l", "mmol", "mm ", "ppm")
_COMPOSITE_KEYWORDS = ("compressive", "flexural", "strength", "mpa", "density", "absorption",
                       "curing", "specimen", "geopolymer", "composite", "modulus", "binder")
# A number followed by a unit (strong sign of reported quantitative results).
_NUMBER_UNIT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mpa|mm|mg/?l|ppm|ppb|mol|°\s*c|wt\.?%|%|kg/?m3|g/?l|days?|hours?|"
    r"min|days|µm|um|m\b)", re.I)


def query_terms(query: str) -> list:
    """Significant lower-cased query terms (stopwords + 1-char tokens removed)."""
    toks = re.findall(r"[a-z0-9]+", str(query or "").lower())
    return [t for t in toks if t not in _STOPWORDS and len(t) > 1]


@dataclass
class ScoredCandidate:
    """A candidate plus its transparent relevance score, breakdown, and an extractability flag."""

    candidate: object                                    # PaperCandidate
    score: float = 0.0
    score_breakdown: dict = field(default_factory=dict)
    why: str = ""
    has_extractable_data: bool = False
    extractability_reason: str = ""

    def to_dict(self) -> dict:
        d = self.candidate.to_dict()
        d.update({"score": round(self.score, 3), "score_breakdown": dict(self.score_breakdown),
                  "why": self.why, "has_extractable_data": self.has_extractable_data,
                  "extractability_reason": self.extractability_reason})
        return d


def has_extractable_data(candidate, domain: str | None = None) -> tuple:
    """``(bool, reason)`` — does the abstract likely contain extractable numeric data?

    Requires an abstract (you cannot extract experiment values from a title alone) **and** a
    number-with-unit; domain keywords strengthen the signal. Conservative: abstract-only is still
    flagged "abstract-only" downstream (not full experimental data).
    """
    if not candidate.has_abstract:
        return False, "no abstract available — cannot extract values (title only)"
    text = (candidate.abstract or "").lower()
    has_number_unit = bool(_NUMBER_UNIT_RE.search(candidate.abstract or ""))
    kws = _LEACH_KEYWORDS if domain == "leaching" else _COMPOSITE_KEYWORDS if domain == "composite" \
        else _LEACH_KEYWORDS + _COMPOSITE_KEYWORDS
    has_kw = any(k in text for k in kws)
    if has_number_unit and has_kw:
        return True, "abstract reports numeric quantities with units + relevant keywords"
    if has_number_unit:
        return True, "abstract reports numeric quantities (check relevance)"
    return False, "abstract has no clear numeric experimental values"


def _term_overlap(terms, text) -> int:
    low = (text or "").lower()
    return sum(1 for t in set(terms) if t in low)


def score_candidate(query: str, candidate, *, domain=None) -> ScoredCandidate:
    """Score one candidate (transparent additive rules) + explain it + flag extractability."""
    terms = query_terms(query)
    n_terms = max(1, len(set(terms)))
    title_hits = _term_overlap(terms, candidate.title)
    abstract_hits = _term_overlap(terms, candidate.abstract or "")

    breakdown = {
        "title_match": round(2.0 * title_hits / n_terms, 3),           # title matches weigh most
        "abstract_match": round(1.0 * abstract_hits / n_terms, 3),
        "has_abstract": 0.4 if candidate.has_abstract else 0.0,
        "recency": _recency_bonus(candidate.year),
        "citations": _citation_bonus(candidate.citation_count),
        "open_access": 0.2 if candidate.is_open_access else 0.0,
        "source_reliability": round(0.3 * source_schema.SOURCE_RELIABILITY.get(
            (candidate.source or "").split("+")[0], 0.5), 3),
    }
    score = round(sum(breakdown.values()), 3)
    extractable, reason = has_extractable_data(candidate, domain)
    why = _explain(candidate, title_hits, abstract_hits, extractable)
    return ScoredCandidate(candidate=candidate, score=score, score_breakdown=breakdown, why=why,
                           has_extractable_data=extractable, extractability_reason=reason)


def _recency_bonus(year) -> float:
    if not year:
        return 0.0
    if year >= 2018:
        return 0.3
    if year >= 2010:
        return 0.15
    return 0.0


def _citation_bonus(n) -> float:
    if not n:
        return 0.0
    if n >= 100:
        return 0.3
    if n >= 20:
        return 0.2
    if n >= 5:
        return 0.1
    return 0.0


def _explain(candidate, title_hits, abstract_hits, extractable) -> str:
    bits = []
    if title_hits:
        bits.append(f"{title_hits} query term(s) in the title")
    if abstract_hits:
        bits.append(f"{abstract_hits} in the abstract")
    if candidate.year and candidate.year >= 2018:
        bits.append("recent")
    if candidate.citation_count and candidate.citation_count >= 20:
        bits.append(f"{candidate.citation_count} citations")
    if candidate.is_open_access:
        bits.append("open access")
    bits.append("has extractable data" if extractable else "no obvious numeric data (may need the full text)")
    return "; ".join(bits) if bits else "weak match"


def rank_candidates(query: str, candidates, *, domain=None, top_n=None) -> list:
    """Score + sort candidates (highest relevance first). Pure; returns list[ScoredCandidate]."""
    scored = [score_candidate(query, c, domain=domain) for c in candidates]
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[:top_n] if top_n else scored
