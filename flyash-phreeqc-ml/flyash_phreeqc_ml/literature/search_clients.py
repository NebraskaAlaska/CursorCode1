"""Search clients for **official / reliable** scholarly APIs (mockable; no AI, no scraping).

Each client wraps an official API (OpenAlex, Crossref, Semantic Scholar, PubMed) behind one tiny
network function, :func:`_http_get_json`, so tests monkeypatch *that* and never touch the network.
All four APIs are **keyless** (open) — an optional *polite-pool email* / API key is read from the
**environment only** (never hard-coded). Every client is defensive: a network/parse failure returns
an empty :class:`SearchResult` with an error note; nothing raises out of a search.

**There is no Google Scholar client** — it has no official API and must not be scraped. Requesting
it raises :class:`UnsupportedSourceError` (see :mod:`source_schema`); the app uses these official
APIs instead. A **manual** path (:func:`manual_candidate`) lets a researcher enter / upload a paper
they found themselves (including via Google Scholar in their own browser) — no scraping involved.
"""
from __future__ import annotations

import os

from .source_schema import (
    DEFAULT_SEARCH_SOURCES,
    SEARCHABLE_SOURCES,
    SOURCE_CROSSREF,
    SOURCE_MANUAL,
    SOURCE_OPENALEX,
    SOURCE_PUBMED,
    SOURCE_SEMANTIC_SCHOLAR,
    PaperCandidate,
    SearchResult,
    ensure_supported_source,
    normalize_doi,
)

DEFAULT_TIMEOUT = 12.0
DEFAULT_LIMIT = 10
_USER_AGENT = "flyash-phreeqc-ml-literature/1.0 (research prototype; non-commercial)"


# --------------------------------------------------------------------------- #
# Environment-only configuration (NEVER hard-coded; all APIs work without these)
# --------------------------------------------------------------------------- #
def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def polite_email() -> str | None:
    """An optional contact email for the OpenAlex/Crossref 'polite pool' (env only)."""
    return _env("OPENALEX_EMAIL") or _env("CROSSREF_EMAIL") or _env("LITERATURE_EMAIL")


def semantic_scholar_key() -> str | None:
    return _env("SEMANTIC_SCHOLAR_API_KEY") or _env("S2_API_KEY")


def ncbi_key() -> str | None:
    return _env("NCBI_API_KEY")


# --------------------------------------------------------------------------- #
# The single network point (lazy stdlib; never raises). Tests monkeypatch this.
# --------------------------------------------------------------------------- #
def _http_get_json(url: str, params: dict | None = None, headers: dict | None = None,
                   timeout: float = DEFAULT_TIMEOUT):
    """GET ``url`` (with query ``params``) → parsed JSON dict, or ``None`` on any failure."""
    import json
    import urllib.parse
    import urllib.request
    try:
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as resp:           # noqa: S310 (https only)
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:                                                        # noqa: BLE001
        return None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #
class SearchClient:
    """Base client. Subclasses implement :meth:`search` and a parser; never raise."""

    name = ""

    def search(self, query: str, *, limit: int = DEFAULT_LIMIT) -> SearchResult:  # pragma: no cover
        raise NotImplementedError


class OpenAlexClient(SearchClient):
    name = SOURCE_OPENALEX
    BASE = "https://api.openalex.org/works"

    def search(self, query, *, limit=DEFAULT_LIMIT):
        params = {"search": query, "per-page": min(int(limit), 25), "mailto": polite_email()}
        data = _http_get_json(self.BASE, params)
        if data is None:
            return SearchResult(self.name, [], query=query, ok=False,
                                error="OpenAlex unavailable (no network or request failed).")
        out = [self._parse(w, query) for w in (data.get("results") or [])[:limit]]
        return SearchResult(self.name, [c for c in out if c.title], query=query)

    @staticmethod
    def _abstract(inverted):
        if not isinstance(inverted, dict):
            return None
        pos = {}
        for word, idxs in inverted.items():
            for i in idxs:
                pos[i] = word
        return " ".join(pos[i] for i in sorted(pos)) if pos else None

    def _parse(self, w, query):
        authors = [(a.get("author") or {}).get("display_name") for a in (w.get("authorships") or [])]
        oa = w.get("open_access") or {}
        return PaperCandidate(
            title=w.get("title") or "", authors=[a for a in authors if a],
            year=_as_int(w.get("publication_year")), doi=normalize_doi(w.get("doi")),
            url=(w.get("doi") or w.get("id")),
            venue=((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
            abstract=self._abstract(w.get("abstract_inverted_index")),
            source=self.name, query=query, citation_count=_as_int(w.get("cited_by_count")),
            is_open_access=bool(oa.get("is_oa")) if oa else None)


class CrossrefClient(SearchClient):
    name = SOURCE_CROSSREF
    BASE = "https://api.crossref.org/works"

    def search(self, query, *, limit=DEFAULT_LIMIT):
        params = {"query": query, "rows": min(int(limit), 25), "mailto": polite_email(),
                  "select": "DOI,title,author,issued,container-title,abstract,is-referenced-by-count"}
        data = _http_get_json(self.BASE, params)
        if data is None:
            return SearchResult(self.name, [], query=query, ok=False,
                                error="Crossref unavailable (no network or request failed).")
        items = ((data.get("message") or {}).get("items")) or []
        out = [self._parse(it, query) for it in items[:limit]]
        return SearchResult(self.name, [c for c in out if c.title], query=query)

    def _parse(self, it, query):
        authors = [" ".join(p for p in (a.get("given"), a.get("family")) if p)
                   for a in (it.get("author") or [])]
        title = (it.get("title") or [None])[0] or ""
        year = None
        parts = ((it.get("issued") or {}).get("date-parts") or [[None]])
        if parts and parts[0]:
            year = _as_int(parts[0][0])
        abstract = it.get("abstract")
        if abstract:
            import re
            abstract = re.sub(r"<[^>]+>", " ", abstract).strip()       # strip JATS XML tags
        return PaperCandidate(
            title=title, authors=[a for a in authors if a], year=year,
            doi=normalize_doi(it.get("DOI")),
            venue=(it.get("container-title") or [None])[0], abstract=abstract,
            source=self.name, query=query,
            citation_count=_as_int(it.get("is-referenced-by-count")))


class SemanticScholarClient(SearchClient):
    name = SOURCE_SEMANTIC_SCHOLAR
    BASE = "https://api.semanticscholar.org/graph/v1/paper/search"

    def search(self, query, *, limit=DEFAULT_LIMIT):
        params = {"query": query, "limit": min(int(limit), 25),
                  "fields": "title,abstract,year,authors,externalIds,venue,citationCount,isOpenAccess"}
        key = semantic_scholar_key()
        headers = {"x-api-key": key} if key else None
        data = _http_get_json(self.BASE, params, headers=headers)
        if data is None:
            return SearchResult(self.name, [], query=query, ok=False,
                                error="Semantic Scholar unavailable (no network / rate-limited).")
        out = [self._parse(p, query) for p in (data.get("data") or [])[:limit]]
        return SearchResult(self.name, [c for c in out if c.title], query=query)

    def _parse(self, p, query):
        doi = (p.get("externalIds") or {}).get("DOI")
        return PaperCandidate(
            title=p.get("title") or "",
            authors=[a.get("name") for a in (p.get("authors") or []) if a.get("name")],
            year=_as_int(p.get("year")), doi=normalize_doi(doi), venue=p.get("venue"),
            abstract=p.get("abstract"), source=self.name, query=query,
            citation_count=_as_int(p.get("citationCount")),
            is_open_access=p.get("isOpenAccess"))


class PubMedClient(SearchClient):
    """PubMed via E-utilities (esearch → esummary). Titles/authors/year/DOI; abstracts via efetch
    are future work (so a PubMed candidate is flagged abstract-light for extraction)."""

    name = SOURCE_PUBMED
    ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

    def search(self, query, *, limit=DEFAULT_LIMIT):
        key = ncbi_key()
        s = _http_get_json(self.ESEARCH, {"db": "pubmed", "term": query, "retmode": "json",
                                          "retmax": min(int(limit), 25), "api_key": key})
        if s is None:
            return SearchResult(self.name, [], query=query, ok=False,
                                error="PubMed unavailable (no network or request failed).")
        ids = ((s.get("esearchresult") or {}).get("idlist")) or []
        if not ids:
            return SearchResult(self.name, [], query=query)
        d = _http_get_json(self.ESUMMARY, {"db": "pubmed", "id": ",".join(ids),
                                           "retmode": "json", "api_key": key})
        if d is None:
            return SearchResult(self.name, [], query=query, ok=False, error="PubMed summary failed.")
        result = d.get("result") or {}
        out = [self._parse(result.get(i), query) for i in ids if isinstance(result.get(i), dict)]
        return SearchResult(self.name, [c for c in out if c.title], query=query)

    def _parse(self, rec, query):
        doi = None
        for aid in (rec.get("articleids") or []):
            if aid.get("idtype") == "doi":
                doi = aid.get("value")
        year = None
        pub = rec.get("pubdate") or ""
        if pub[:4].isdigit():
            year = int(pub[:4])
        return PaperCandidate(
            title=rec.get("title") or "",
            authors=[a.get("name") for a in (rec.get("authors") or []) if a.get("name")],
            year=year, doi=normalize_doi(doi), venue=rec.get("fulljournalname") or rec.get("source"),
            abstract=None, source=self.name, query=query)


# --------------------------------------------------------------------------- #
# Registry + multi-source search + de-dup + manual entry
# --------------------------------------------------------------------------- #
CLIENTS = {
    SOURCE_OPENALEX: OpenAlexClient,
    SOURCE_CROSSREF: CrossrefClient,
    SOURCE_SEMANTIC_SCHOLAR: SemanticScholarClient,
    SOURCE_PUBMED: PubMedClient,
}


def get_client(source: str) -> SearchClient:
    """Return a client for a *supported searchable* source (raises for Google Scholar / unknown)."""
    name = ensure_supported_source(source)
    if name not in CLIENTS:
        from .source_schema import UnsupportedSourceError
        raise UnsupportedSourceError(f"'{source}' is supported but not searchable here.")
    return CLIENTS[name]()


def search_one(source: str, query: str, *, limit: int = DEFAULT_LIMIT) -> SearchResult:
    return get_client(source).search(query, limit=limit)


def search_sources(query: str, sources=None, *, limit: int = DEFAULT_LIMIT) -> list:
    """Search each requested source (default :data:`DEFAULT_SEARCH_SOURCES`) → list[SearchResult].

    Unsupported sources (Google Scholar) and non-searchable ones are skipped with an error note —
    never a scraper, never a raise.
    """
    chosen = list(sources or DEFAULT_SEARCH_SOURCES)
    results = []
    for src in chosen:
        try:
            name = ensure_supported_source(src)
        except Exception as exc:                                            # noqa: BLE001
            results.append(SearchResult(str(src), [], query=query, ok=False, error=str(exc)))
            continue
        if name not in SEARCHABLE_SOURCES:
            results.append(SearchResult(name, [], query=query, ok=False,
                                        note="not a free-text search source"))
            continue
        results.append(search_one(name, query, limit=limit))
    return results


def merge_dedup(results) -> list:
    """Flatten + de-duplicate candidates across sources (by DOI, else normalised title).

    When the same paper appears in several sources, keep the variant that has an abstract (better
    for extraction) and record the additional source on it.
    """
    by_key: dict = {}
    for res in results:
        for cand in getattr(res, "candidates", []):
            key = cand.dedup_key
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = cand
            elif (not existing.has_abstract) and cand.has_abstract:
                cand.source = f"{cand.source}+{existing.source}"
                by_key[key] = cand
            elif existing.source and cand.source not in existing.source:
                existing.source = f"{existing.source}+{cand.source}"
    return list(by_key.values())


def manual_candidate(*, title, authors=None, year=None, doi=None, abstract=None, venue=None,
                     url=None) -> PaperCandidate:
    """Build a candidate from a paper the researcher entered / uploaded themselves (no scraping)."""
    return PaperCandidate(
        title=str(title or "").strip(), authors=list(authors or []), year=year,
        doi=doi, url=url, venue=venue, abstract=abstract, source=SOURCE_MANUAL,
        query="manual entry")
