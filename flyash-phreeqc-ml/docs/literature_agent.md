# Literature Research Agent + Evidence Library

The **Evidence Library** (`flyash_phreeqc_ml/literature/`) is a research-and-evidence layer that
searches **official / reliable scholarly APIs**, ranks the results transparently, optionally
extracts structured experimental variables with AI, and curates per-run **evidence datasets** that
the **Prediction Models** ML surrogate engine can learn from (after you review/approve them) — see
[`ml_surrogate_engine.md`](ml_surrogate_engine.md).

It exists because most materials domains (composite strength, thermal, durability, …) have **no
validated simulation engine yet**. Rather than dead-ending, the app helps you gather the evidence a
future model would need.

## What it does — and does not — do

| Does | Does **not** |
| --- | --- |
| search OpenAlex, Crossref, Semantic Scholar, PubMed (official APIs) | scrape Google Scholar (no official API — manual only) |
| rank candidates transparently + explain why each is useful | invent a value, a paper, or a citation |
| extract structured values with AI **when text is available** | treat AI-extracted values as measured truth |
| keep every value's **source + confidence + scope** | store raw model responses or full-text PDFs |
| build a per-run evidence dataset (CSV-exportable) | train a model or predict strength *itself* |

The **literature package itself** does not simulate, train, or predict — it builds an *evidence
database* with citations + provenance throughout. That curated database is what the separate
**Prediction Models** engine (`ml_models`) trains a surrogate on — but only **approved** rows, and
the resulting model is an *experimental* (never validated) screening estimate, not a measurement.

## Google Scholar is manual-only (never scraped)

Google Scholar has **no official API** and its terms forbid automated access, so the app **does not
search or scrape it**. Use Google Scholar manually in your own browser if you like, then add a paper
you found via the **"Add a paper manually"** path (no scraping involved). The app searches official
/ reliable scholarly APIs instead, and **every result carries its source + the query that found it**
for provenance. (`source_schema.GOOGLE_SCHOLAR_SUPPORTED is False`;
`source_schema.ensure_supported_source("google_scholar")` raises — there is no scraper in the code,
pinned by `tests/test_literature_agent.py::test_no_scholar_scraper_libraries_imported`.)

## Sources

| Source | Access | Notes |
| --- | --- | --- |
| **OpenAlex** | keyless (open) | broad coverage; optional polite-pool email via `OPENALEX_EMAIL` |
| **Crossref** | keyless (open) | DOI-rich metadata; optional `CROSSREF_EMAIL` |
| **Semantic Scholar** | keyless (rate-limited) | abstracts + citations; optional `S2_API_KEY` for higher limits |
| **PubMed / PMC** | keyless | biomedical ("where relevant"); titles/authors/DOI (abstracts via efetch = future) |
| **DOI metadata** | keyless | resolve a single DOI |
| **Manual entry** | — | a paper *you* found (incl. via Google Scholar in your browser) |

**API keys are read from environment variables only** — never hard-coded, never required (all four
search APIs work keyless). A network failure returns an empty result with an error note; nothing
raises, and nothing is fabricated.

## Modules

| Module | Role |
| --- | --- |
| `source_schema.py` | the supported sources + the provenance-carrying `PaperCandidate`; the Google-Scholar guard |
| `search_clients.py` | the API clients behind one mockable `_http_get_json`; multi-source search + de-dup; manual entry |
| `ranking.py` | transparent, deterministic relevance ranking + an explanation + an *extractable-data* flag |
| `evidence_schema.py` | `LeachingEvidence` + `CompositeEvidence` (provenance required, missing → null, confidence + scope) |
| `extraction.py` | the **only** AI module — extracts values from a paper's abstract; never fabricates; no raw response stored |
| `evidence_store.py` | the per-run JSONL store (provenance enforced) + CSV export; safe location only |
| `research_agent.py` | query generation + the search orchestration (ranked, cited candidates) |

## Evidence schemas

* **Leaching** — material, class/source, composition, leachant, concentration, solid mass, liquid
  volume, L/S, time, temperature, pH, elements measured, Ca/Si/Al/Fe/Na/K/Sc/REE values, analytical
  method, filtration, DOI/source, extraction confidence, notes.
* **Composite / mechanical** — binder, plastic type/form/size/dosage, water/binder ratio,
  activator/cement content, curing time, specimen geometry, compressive + flexural strength,
  density, water absorption, durability, DOI/source, extraction confidence, notes.

Every numeric field is `None` unless the paper stated it; each row carries a **citation** + an
**extraction confidence** (banded high/medium/low) + an **extraction scope**. An **abstract-only**
extraction is capped at *medium* — an abstract is not full experimental data.

## AI extraction rules

* AI is used **only** to read text the paper provides (the abstract). Missing values stay `None`;
  uncertain values get low confidence; **conflicting values are flagged**; no hidden condition is
  inferred. With no AI key, extraction returns an empty row with an honest status (`ai_unavailable`)
  — it never invents a value.
* The **raw model response is never stored** — only the validated structured fields + provenance.
  The paper's abstract / full text is **not** stored either (no copyrighted text is persisted).

## Integration with the Assistant

When you ask for a prediction in an unsupported domain ("predict the compressive strength of a fly
ash + waste plastic composite"), the assistant says plainly it **cannot run a validated strength
model yet**, and offers to **search literature** + **build an evidence / training dataset** (plus
the existing structure-the-plan / data-template offers). The Evidence Library is where that search +
curation happens. Once you have approved enough evidence and **trained a model in Prediction
Models**, the assistant additionally offers that *experimental* ML surrogate estimate — never a
fabricated number, and never PHREEQC (which is the leaching engine, not the strength engine).

## Boundaries (tests)

`tests/test_literature_agent.py` pins the honesty contract (schemas, query generation, ranking,
extraction missing→null / no-fabrication / no-raw-response, provenance-required, confidence labels,
Google-Scholar-not-implemented, assistant routing). `tests/test_ai_boundary.py` pins the imports:
only `extraction` reaches the AI client; no literature module imports an executor or the
comparison/residual/mapping **result path**, and the result path never imports the literature
package — extracted evidence is a *library for the future*, never a measured/validated value. The
evidence store writes only to a gitignored per-run `outputs/literature/` location.
