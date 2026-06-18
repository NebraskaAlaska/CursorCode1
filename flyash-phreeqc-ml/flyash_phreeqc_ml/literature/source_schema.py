"""Scholarly **source** vocabulary + the paper-candidate schema (pure data, no AI, no network).

This module defines *which scholarly sources the app talks to* and the structured, **provenance-
carrying** record a search returns. Every search result is a :class:`PaperCandidate` that records
*which API returned it* and *which query found it*, so a candidate can always be cited.

**Google Scholar is deliberately NOT a supported automated source.** It has no official API and
its terms forbid scraping; the app uses official / reliable scholarly APIs instead (OpenAlex,
Crossref, Semantic Scholar, PubMed) and treats Google Scholar as a *manual* tool for the
researcher's own browser. :data:`GOOGLE_SCHOLAR_SUPPORTED` is ``False`` and
:func:`ensure_supported_source` rejects it with a clear message — there is no scraper here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Supported (official / reliable) scholarly sources
# --------------------------------------------------------------------------- #
SOURCE_OPENALEX = "openalex"
SOURCE_CROSSREF = "crossref"
SOURCE_SEMANTIC_SCHOLAR = "semantic_scholar"
SOURCE_PUBMED = "pubmed"
SOURCE_DOI = "doi"                  # resolve a single DOI's metadata
SOURCE_MANUAL = "manual"            # a user-entered / user-uploaded paper (no scraping)

SUPPORTED_SOURCES = (SOURCE_OPENALEX, SOURCE_CROSSREF, SOURCE_SEMANTIC_SCHOLAR,
                     SOURCE_PUBMED, SOURCE_DOI, SOURCE_MANUAL)

# Sources that perform a free-text *search* (vs. a single-DOI lookup or a manual entry).
SEARCHABLE_SOURCES = (SOURCE_OPENALEX, SOURCE_CROSSREF, SOURCE_SEMANTIC_SCHOLAR, SOURCE_PUBMED)

# Default search sources (all keyless / open; an optional polite-pool email is read from env).
DEFAULT_SEARCH_SOURCES = (SOURCE_OPENALEX, SOURCE_CROSSREF, SOURCE_SEMANTIC_SCHOLAR)

SOURCE_LABELS = {
    SOURCE_OPENALEX: "OpenAlex",
    SOURCE_CROSSREF: "Crossref",
    SOURCE_SEMANTIC_SCHOLAR: "Semantic Scholar",
    SOURCE_PUBMED: "PubMed / PMC",
    SOURCE_DOI: "DOI metadata",
    SOURCE_MANUAL: "Manual entry / uploaded paper",
}

# A short note on each source's reliability + access (shown in the UI). Reliability weights the
# ranking only as a gentle tie-breaker (never overrides relevance).
SOURCE_RELIABILITY = {
    SOURCE_OPENALEX: 0.9, SOURCE_CROSSREF: 0.85, SOURCE_SEMANTIC_SCHOLAR: 0.9,
    SOURCE_PUBMED: 0.9, SOURCE_DOI: 0.8, SOURCE_MANUAL: 0.6,
}

# --------------------------------------------------------------------------- #
# Google Scholar — manual only, NEVER automated/scraped
# --------------------------------------------------------------------------- #
GOOGLE_SCHOLAR_SUPPORTED = False
GOOGLE_SCHOLAR_NOTE = (
    "Google Scholar has no official API and its terms forbid automated scraping, so the app does "
    "not search or scrape it. Use Google Scholar manually in your own browser if you like; the app "
    "searches official / reliable scholarly APIs (OpenAlex, Crossref, Semantic Scholar, PubMed) "
    "instead, and every result carries its source + query for provenance.")


class UnsupportedSourceError(ValueError):
    """Raised when an unsupported source (e.g. Google Scholar) is requested."""


def ensure_supported_source(name: str) -> str:
    """Return the normalised source name, or raise for an unsupported one (e.g. Google Scholar)."""
    n = str(name or "").strip().lower().replace(" ", "_").replace("-", "_")
    if n in ("google_scholar", "googlescholar", "scholar", "gscholar"):
        raise UnsupportedSourceError(GOOGLE_SCHOLAR_NOTE)
    if n not in SUPPORTED_SOURCES:
        raise UnsupportedSourceError(
            f"'{name}' is not a supported scholarly source. Supported: "
            f"{', '.join(SUPPORTED_SOURCES)}.")
    return n


# --------------------------------------------------------------------------- #
# Paper candidate (provenance-carrying search result)
# --------------------------------------------------------------------------- #
def normalize_doi(doi) -> str | None:
    """Return a bare, lower-cased DOI (no URL prefix), or ``None``."""
    s = str(doi or "").strip()
    if not s:
        return None
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "https://dx.doi.org/"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
    s = s.strip().strip("/")
    return s.lower() or None


@dataclass
class PaperCandidate:
    """One search result — always carries its source API + the query that found it (provenance)."""

    title: str
    authors: list = field(default_factory=list)     # author display names
    year: int | None = None
    doi: str | None = None                           # bare DOI (normalised)
    url: str | None = None
    venue: str | None = None
    abstract: str | None = None
    source: str = ""                                 # which API returned it (provenance)
    query: str | None = None                         # the query that found it (provenance)
    citation_count: int | None = None
    is_open_access: bool | None = None

    def __post_init__(self):
        self.doi = normalize_doi(self.doi)
        if not self.url and self.doi:
            self.url = f"https://doi.org/{self.doi}"

    @property
    def has_doi(self) -> bool:
        return bool(self.doi)

    @property
    def has_abstract(self) -> bool:
        return bool((self.abstract or "").strip())

    @property
    def dedup_key(self) -> str:
        """A stable identity for de-duplication across sources (DOI, else normalised title)."""
        if self.doi:
            return f"doi:{self.doi}"
        return "title:" + " ".join((self.title or "").lower().split())

    def citation(self) -> str:
        """A short human citation string (provenance for display)."""
        who = (self.authors[0] + " et al." if len(self.authors) > 1
               else (self.authors[0] if self.authors else "Unknown"))
        bits = [who]
        if self.year:
            bits.append(f"({self.year})")
        if self.title:
            bits.append(self.title)
        if self.venue:
            bits.append(f"— {self.venue}")
        if self.doi:
            bits.append(f"https://doi.org/{self.doi}")
        return " ".join(bits)

    def to_dict(self) -> dict:
        return {
            "title": self.title, "authors": list(self.authors), "year": self.year,
            "doi": self.doi, "url": self.url, "venue": self.venue,
            "abstract": self.abstract, "source": self.source, "query": self.query,
            "citation_count": self.citation_count, "is_open_access": self.is_open_access,
            "citation": self.citation(),
        }


@dataclass
class SearchResult:
    """The outcome of one source's search (never raises out of a client)."""

    source: str
    candidates: list = field(default_factory=list)   # list[PaperCandidate]
    query: str = ""
    ok: bool = True
    error: str | None = None
    note: str | None = None

    def to_summary(self) -> dict:
        return {"source": self.source, "query": self.query, "ok": self.ok,
                "n": len(self.candidates), "error": self.error, "note": self.note}
