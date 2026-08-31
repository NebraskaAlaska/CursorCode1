"""Legacy literature search plus read-only historical evidence compatibility.

UI only: search runs through the official-API clients (`literature.research_agent`), ranking is
transparent (`literature.ranking`), extraction is AI + consent-gated and never fabricates
(`literature.extraction`), and every new manual or AI-created row is routed into the durable
``ArtifactRecord`` lifecycle (`literature.evidence_review`).  Historical per-run JSONL is displayed
read-only; it is not a second writable authority.  The app does NOT scrape Google Scholar, train a
model, or predict strength.
"""
from __future__ import annotations

import streamlit as st

import app_ui
from flyash_phreeqc_ml import run_manager
from flyash_phreeqc_ml import workspace_store
from flyash_phreeqc_ml.ai import config as ai_config
from flyash_phreeqc_ml.literature import evidence_schema as E
from flyash_phreeqc_ml.literature import evidence_review, evidence_store, extraction, research_agent
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


def _durable_target(store, context):
    """Resolve the exact active project/material or return ``None`` for read-only mode."""
    if store is None or not isinstance(context, dict):
        return None
    project_id = str(context.get("active_project_id") or "").strip()
    material_id = str(context.get("active_material_id") or "").strip()
    if not project_id or not material_id:
        return None
    try:
        project = store.get_project(project_id)
        material = store.get_material(material_id)
    except workspace_store.WorkspaceStoreError:
        return None
    if material.project_id != project.project_id:
        return None
    return project.project_id, material.material_id


def _create_durable_row(store, context, evidence, *, creator: str, ai_created: bool):
    """Create and material-link one durable record; never write historical JSONL."""
    target = _durable_target(store, context)
    if target is None:
        raise evidence_review.EvidenceReviewError(
            "select an active project and material before saving evidence")
    project_id, material_id = target
    if ai_created:
        record = evidence_review.create_ai_evidence(
            store, project_id, material_id, evidence, creator=creator)
    else:
        record = evidence_review.create_manual_evidence(
            store, project_id, material_id, evidence, creator=creator)
    evidence_review.link_material(store, record.artifact_id)
    return record


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def _render_evidence_library(
    selected_run,
    dev_mode: bool = False,
    *,
    store=None,
    context=None,
) -> None:
    app_ui.render_page_header(
        "Evidence Library",
        "Search reliable scholarly APIs, extract structured experimental variables (with citations + "
        "confidence), and save suggestions into the durable review queue. Historical run JSONL "
        "below is read-only. The app does not predict strength or scrape Google Scholar.",
        eyebrow="Search · rank · route to review")
    st.caption("🔎 " + ss.GOOGLE_SCHOLAR_NOTE)
    st.caption(
        "New evidence is stored only as a durable evidence artifact. AI extraction always starts "
        "Needs review and only the manual review surface can accept or reject it.")

    cfg = ai_config.resolve_config()
    live_ai_enabled = ai_config.is_enabled()

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

    _render_results(store, context, selected_run, domain, cfg, live_ai_enabled)
    _render_manual_entry(store, context, domain)
    _render_evidence_table(selected_run, domain)


def _render_results(store, context, run, domain, cfg, live_ai_enabled: bool) -> None:
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
            _render_candidate(store, context, schema_kind, i, sc, consent, cfg)


def _render_candidate(store, context, schema_kind, i, sc, consent, cfg) -> None:
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
        target_ready = _durable_target(store, context) is not None
        disabled = not (sc.has_extractable_data and consent and live_ai_enabled and target_ready)
        if col1.button("Extract evidence", key=f"evlib_extract_{i}", disabled=disabled):
            with st.spinner("Extracting (AI reads the abstract; values are cited + confidence-scored)…"):
                ev = extraction.extract_evidence(cand, schema_kind)
            try:
                record = _create_durable_row(
                    store, context, ev,
                    creator="AI-assisted literature extraction", ai_created=True)
                st.success(
                    f"Extracted ({ev.confidence_label} confidence, {ev.extraction_scope}) and "
                    f"saved as {record.status.replace('_', ' ')}. Review it in Manual evidence & "
                    "review; it was not auto-approved.")
                st.rerun()
            except (workspace_store.WorkspaceStoreError, ValueError) as exc:
                st.error(f"Could not create durable evidence: {exc}")
        if not target_ready:
            col2.caption("Select an active project and material before saving an extraction.")
        elif disabled and not live_ai_enabled:
            col2.caption("Enable AI in **Settings** + consent above to extract values from the abstract.")
        elif disabled and not sc.has_extractable_data:
            col2.caption("No clear numeric data in the abstract — open the paper or enter values manually.")


def _extraction_consent(cfg) -> bool:
    if not live_ai_enabled:
        st.caption("⚪ AI extraction needs an API key (configure in **Settings**). You can still "
                   "search, rank, and add papers/values manually.")
        return False
    return st.checkbox("Allow AI to read selected abstracts to extract values",
                       key="evlib_extract_consent", help=extraction.EXTRACTION_DATA_NOTICE)


def _render_manual_entry(store, context, domain) -> None:
    schema_kind = _SCHEMA_FOR_DOMAIN[domain]
    with app_ui.advanced_expander("Add a paper / values manually (no scraping)"):
        st.caption("Found a paper yourself (e.g. via Google Scholar in your browser)? Add it here — "
                   "the app never scrapes it. This creates a durable draft; a source (DOI or "
                   "title plus provider) is required.")
        title = st.text_input("Title", key="evlib_m_title")
        doi = st.text_input("DOI (optional if a title is given)", key="evlib_m_doi")
        notes = st.text_input("Notes / key values to record", key="evlib_m_notes")
        creator = st.text_input("Creator", key="evlib_m_creator")
        target_ready = _durable_target(store, context) is not None
        if not target_ready:
            st.caption("Select an active project and material to enable durable evidence creation.")
        if st.button("Create durable evidence draft", key="evlib_m_add", disabled=not target_ready):
            prov = E.Provenance(source=ss.SOURCE_MANUAL, doi=doi or None, title=title or None,
                                query="manual entry")
            if not prov.is_present:
                st.warning(
                    "A DOI or a title plus source/provider is required (a value with no source "
                    "is not evidence).")
            elif not creator.strip():
                st.warning("Name the creator before saving the durable draft.")
            else:
                cls = E.LeachingEvidence if schema_kind == E.SCHEMA_LEACHING else E.CompositeEvidence
                ev = cls(provenance=prov, extraction_scope=E.SCOPE_MANUAL,
                         extraction_status=E.STATUS_MANUAL, extraction_confidence=0.0,
                         notes=notes or "manual entry", review_status=E.REVIEW_DRAFT)
                try:
                    record = _create_durable_row(
                        store, context, ev, creator=creator, ai_created=False)
                    st.success(
                        f"Created durable draft {record.artifact_id}. Complete and submit it in "
                        "Manual evidence & review.")
                    st.rerun()
                except (workspace_store.WorkspaceStoreError, ValueError) as exc:
                    st.error(f"Could not create durable evidence: {exc}")


def _render_evidence_table(run, domain) -> None:
    schema_kind = _SCHEMA_FOR_DOMAIN[domain]
    rows = _evidence_rows(run, schema_kind)
    with st.container(border=True):
        app_ui.section_header(f"Historical run evidence (read-only) · {_DOMAIN_OPTIONS[domain]}")
        if not rows:
            st.caption(
                "No historical JSONL rows for this run. New entries appear in Manual evidence & "
                "review as durable artifacts, not in this compatibility view.")
            return
        columns = [c for c in E.columns_for(schema_kind)]
        table = [{c: r.get(c) for c in columns} for r in rows]
        st.dataframe(table, use_container_width=True, height=280)
        st.caption(
            f"{len(rows)} historical row(s) · unchanged on disk · not reviewed or promoted "
            "automatically. Create a durable record if you need an active review lifecycle.")
        try:
            csv_data = evidence_store.to_csv(rows, schema_kind)
        except (evidence_store.ForbiddenEvidenceContentError, ValueError) as exc:
            st.warning(f"Historical export blocked by the structured-content boundary: {exc}")
        else:
            st.download_button(
                "⬇️ Export historical evidence CSV", data=csv_data,
                file_name=f"evidence_{schema_kind}_historical.csv", mime="text/csv",
                key="evlib_export")


# The app dispatches to ``render``.
render = _render_evidence_library
