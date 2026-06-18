"""Evidence Library — search reliable scholarly APIs, extract structured evidence, curate a dataset.

UI only: search runs through the official-API clients (`literature.research_agent`), ranking is
transparent (`literature.ranking`), extraction is AI + consent-gated and never fabricates
(`literature.extraction`), and rows are stored per-run with provenance (`literature.evidence_store`).
The app does NOT scrape Google Scholar, does NOT train a model, and does NOT predict strength — this
builds an evidence database a *future* ML / surrogate model could learn from.
"""
from __future__ import annotations

import streamlit as st

import app_ui
from flyash_phreeqc_ml import run_manager
from flyash_phreeqc_ml.ai import config as ai_config
from flyash_phreeqc_ml.literature import evidence_schema as E
from flyash_phreeqc_ml.literature import evidence_store, extraction, research_agent
from flyash_phreeqc_ml.literature import source_schema as ss

_DOMAIN_OPTIONS = {research_agent.DOMAIN_LEACHING: "Leaching / geochemistry",
                   research_agent.DOMAIN_COMPOSITE: "Composite / mechanical"}
_SCHEMA_FOR_DOMAIN = {research_agent.DOMAIN_LEACHING: E.SCHEMA_LEACHING,
                      research_agent.DOMAIN_COMPOSITE: E.SCHEMA_COMPOSITE}


# --------------------------------------------------------------------------- #
# Session helpers
# --------------------------------------------------------------------------- #
def _rk(run, suffix):
    return f"evlib_{suffix}__{run or '_none_'}"


def _evidence_rows(run, schema_kind):
    key = _rk(run, f"evidence_{schema_kind}")
    if key not in st.session_state:
        st.session_state[key] = (
            evidence_store.read_evidence(_evidence_path(run, schema_kind)) if run else [])
    return st.session_state[key]


def _evidence_path(run, schema_kind):
    if not run:
        return None
    return evidence_store.evidence_path(run_manager.run_outputs_dir(run), schema_kind)


def _add_row(run, schema_kind, evidence):
    rows = _evidence_rows(run, schema_kind)
    rows.append(evidence.to_row())
    if run:                                              # persist to the gitignored run library
        try:
            evidence_store.add_evidence(_evidence_path(run, schema_kind), evidence)
        except Exception as exc:                         # noqa: BLE001
            st.warning(f"Could not save to the run library: {exc}")


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def _render_evidence_library(selected_run, dev_mode: bool = False) -> None:
    app_ui.render_page_header(
        "Evidence Library",
        "Search reliable scholarly APIs, extract structured experimental variables (with citations + "
        "confidence), and curate an evidence dataset for a future ML / surrogate model. The app does "
        "not predict strength yet, and it never scrapes Google Scholar.",
        eyebrow="Search · rank · extract · curate")
    st.caption("🔎 " + ss.GOOGLE_SCHOLAR_NOTE)

    cfg = ai_config.resolve_config()

    # --- search controls --------------------------------------------------- #
    with st.container(border=True):
        app_ui.section_header("Find papers")
        query = st.text_input("Search query",
                              placeholder="e.g. fly ash PET plastic compressive strength 28 days",
                              key="evlib_query")
        c1, c2 = st.columns([1, 2])
        domain = c1.selectbox("Domain", list(_DOMAIN_OPTIONS),
                              format_func=lambda d: _DOMAIN_OPTIONS[d], key="evlib_domain")
        sources = c2.multiselect(
            "Sources (official scholarly APIs)", list(ss.SEARCHABLE_SOURCES),
            default=list(ss.DEFAULT_SEARCH_SOURCES),
            format_func=lambda s: ss.SOURCE_LABELS.get(s, s), key="evlib_sources")
        if st.button("Search literature", type="primary", key="evlib_search") and query.strip():
            with st.spinner("Searching official scholarly APIs…"):
                st.session_state[_rk(selected_run, "research")] = research_agent.research(
                    query, domain=domain, sources=sources or list(ss.DEFAULT_SEARCH_SOURCES))

    _render_results(selected_run, domain, cfg)
    _render_manual_entry(selected_run, domain)
    _render_evidence_table(selected_run, domain)


def _render_results(run, domain, cfg) -> None:
    res = st.session_state.get(_rk(run, "research"))
    if res is None:
        return
    schema_kind = _SCHEMA_FOR_DOMAIN[domain]
    with st.container(border=True):
        app_ui.section_header("Ranked paper candidates")
        st.caption(f"Queries run: {', '.join(res.queries)} · domain: {res.domain}")
        # per-source provenance of the search itself
        for s in res.source_summaries:
            mark = "✅" if s["ok"] else "⚠️"
            st.caption(f"{mark} {ss.SOURCE_LABELS.get(s['source'], s['source'])}: {s['n']} result(s)"
                       + (f" — {s['error']}" if s.get("error") else ""))
        if not res.ranked:
            app_ui.render_warning_panel(
                "No candidates", res.note or "No results — try broader terms or add a paper manually.",
                level="info")
            return

        consent = _extraction_consent(cfg)
        for i, sc in enumerate(res.ranked[:15]):
            _render_candidate(run, schema_kind, i, sc, consent, cfg)


def _render_candidate(run, schema_kind, i, sc, consent, cfg) -> None:
    cand = sc.candidate
    with st.container(border=True):
        head = f"**{cand.title or '(untitled)'}**"
        st.markdown(head)
        meta = " · ".join(x for x in [
            (cand.authors[0] + " et al." if len(cand.authors) > 1 else (cand.authors[0] if cand.authors else "")),
            str(cand.year) if cand.year else "",
            ss.SOURCE_LABELS.get((cand.source or "").split("+")[0], cand.source),
            (f"{cand.citation_count} citations" if cand.citation_count else ""),
        ] if x)
        st.caption(meta)
        badge = "🟢 likely extractable" if sc.has_extractable_data else "⚪ no obvious numeric data"
        st.caption(f"{badge} — {sc.extractability_reason} · relevance {sc.score:.2f}")
        st.caption(f"Why useful: {sc.why}")
        if cand.url:
            st.markdown(f"[Open source ↗]({cand.url})")
        col1, col2 = st.columns([1, 3])
        disabled = not (sc.has_extractable_data and consent and cfg.enabled)
        if col1.button("Extract evidence", key=f"evlib_extract_{i}", disabled=disabled):
            with st.spinner("Extracting (AI reads the abstract; values are cited + confidence-scored)…"):
                ev = extraction.extract_evidence(cand, schema_kind)
            _add_row(run, schema_kind, ev)
            st.success(f"Extracted ({ev.confidence_label} confidence, {ev.extraction_scope}). "
                       "Added to the evidence library below.")
            st.rerun()
        if disabled and not cfg.enabled:
            col2.caption("Enable AI in **Settings** + consent above to extract values from the abstract.")
        elif disabled and not sc.has_extractable_data:
            col2.caption("No clear numeric data in the abstract — open the paper or enter values manually.")


def _extraction_consent(cfg) -> bool:
    if not cfg.enabled:
        st.caption("⚪ AI extraction needs an API key (configure in **Settings**). You can still "
                   "search, rank, and add papers/values manually.")
        return False
    return st.checkbox("Allow AI to read selected abstracts to extract values",
                       key="evlib_extract_consent", help=extraction.EXTRACTION_DATA_NOTICE)


def _render_manual_entry(run, domain) -> None:
    schema_kind = _SCHEMA_FOR_DOMAIN[domain]
    with app_ui.advanced_expander("Add a paper / values manually (no scraping)"):
        st.caption("Found a paper yourself (e.g. via Google Scholar in your browser)? Add it here — "
                   "the app never scrapes it. A source (DOI or title) is required.")
        title = st.text_input("Title", key="evlib_m_title")
        doi = st.text_input("DOI (optional if a title is given)", key="evlib_m_doi")
        notes = st.text_input("Notes / key values to record", key="evlib_m_notes")
        if st.button("Add manual evidence row", key="evlib_m_add"):
            prov = E.Provenance(source=ss.SOURCE_MANUAL, doi=doi or None, title=title or None,
                                query="manual entry")
            if not prov.is_present:
                st.warning("A DOI or a title is required (a value with no source is not evidence).")
            else:
                cls = E.LeachingEvidence if schema_kind == E.SCHEMA_LEACHING else E.CompositeEvidence
                ev = cls(provenance=prov, extraction_scope=E.SCOPE_MANUAL,
                         extraction_status=E.STATUS_MANUAL, extraction_confidence=0.0,
                         notes=notes or "manual entry")
                _add_row(run, schema_kind, ev)
                st.success("Added a manual evidence row (fill values by editing the run library).")
                st.rerun()


def _render_evidence_table(run, domain) -> None:
    schema_kind = _SCHEMA_FOR_DOMAIN[domain]
    rows = _evidence_rows(run, schema_kind)
    with st.container(border=True):
        app_ui.section_header(f"Evidence library · {_DOMAIN_OPTIONS[domain]}")
        if not rows:
            st.caption("No evidence yet — search above and extract from a paper, or add one manually. "
                       "Every row keeps its source + confidence; this dataset can later train an "
                       "ML / surrogate model.")
            return
        columns = [c for c in E.columns_for(schema_kind)]
        table = [{c: r.get(c) for c in columns} for r in rows]
        st.dataframe(table, use_container_width=True, height=280)
        st.caption(f"{len(rows)} row(s) · every value is None unless the paper stated it · "
                   "labels: confidence (high/medium/low) + scope (abstract/full_text/manual) + citation.")
        st.download_button(
            "⬇️ Export evidence CSV", data=evidence_store.to_csv(rows, schema_kind),
            file_name=f"evidence_{schema_kind}.csv", mime="text/csv", key="evlib_export")
        st.caption("This evidence dataset is a curated input for a *future* ML / surrogate model — "
                   "it is not a validated prediction, and the app does not predict strength yet.")


# The app dispatches to ``render``.
render = _render_evidence_library
