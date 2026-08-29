"""Manual evidence editor and explicit human-review UI for Phase 3."""
from __future__ import annotations

import json

import streamlit as st

import app_ui
from flyash_phreeqc_ml import phase3_artifacts, workspace_store
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines
from flyash_phreeqc_ml.literature import evidence_review
from flyash_phreeqc_ml.literature import evidence_schema as E


def _records(store, context):
    try:
        project = store.get_project(context.get("active_project_id"))
    except workspace_store.WorkspaceStoreError:
        return None, None
    material = None
    try:
        if context.get("active_material_id"):
            material = store.get_material(context["active_material_id"])
    except workspace_store.WorkspaceStoreError:
        pass
    return project, material


def _key(name: str, project, suffix: str = "") -> str:
    return f"phase3_evidence_{name}__{project.project_id}__{suffix or 'new'}"


def _json_object(text: str, label: str) -> dict:
    value = json.loads(text or "{}")
    if not isinstance(value, dict):
        raise evidence_review.EvidenceReviewError(f"{label} must be a JSON object")
    return value


def _year(value: str):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise evidence_review.EvidenceReviewError("citation year must be an integer") from exc


def _payload_controls(project, *, prefix: str, base: dict | None = None) -> dict | None:
    base = dict(base or {})
    provenance = dict(base.get("provenance") or {})
    location = dict(base.get("source_location") or {})
    schema_kind = st.selectbox(
        "Evidence type",
        E.SCHEMA_KINDS,
        index=E.SCHEMA_KINDS.index(base.get("schema_kind"))
        if base.get("schema_kind") in E.SCHEMA_KINDS else 0,
        format_func=lambda value: value.title(),
        key=_key("schema", project, prefix),
    )
    title = st.text_input(
        "Source title", str(provenance.get("title") or ""),
        key=_key("title", project, prefix))
    c1, c2, c3 = st.columns(3)
    source = c1.text_input(
        "Source / provider", str(provenance.get("source") or "manual"),
        key=_key("source", project, prefix))
    doi = c2.text_input("DOI", str(provenance.get("doi") or ""),
                        key=_key("doi", project, prefix))
    year = c3.text_input("Year", str(provenance.get("year") or ""),
                         key=_key("year", project, prefix))
    authors = st.text_input(
        "Authors (semicolon-separated)",
        "; ".join(provenance.get("authors") or []),
        key=_key("authors", project, prefix))
    url = st.text_input("URL", str(provenance.get("url") or ""),
                        key=_key("url", project, prefix))
    with st.expander("Discovery route and exact source location", expanded=False):
        route = dict(provenance.get("discovery_route") or {})
        a, b = st.columns(2)
        discovery_method = a.text_input(
            "Discovery method", str(route.get("method") or "manual"),
            key=_key("route_method", project, prefix))
        record_identifier = b.text_input(
            "Dataset / source record identifier",
            str(route.get("record_identifier") or ""),
            key=_key("route_id", project, prefix))
        query = st.text_input(
            "Query or discovery note", str(route.get("query") or provenance.get("query") or ""),
            key=_key("query", project, prefix))
        a, b, c = st.columns(3)
        page = a.text_input("Page", str(location.get("page") or ""),
                            key=_key("page", project, prefix))
        page_range = b.text_input("Page range", str(location.get("page_range") or ""),
                                  key=_key("page_range", project, prefix))
        section = c.text_input("Section", str(location.get("section") or ""),
                               key=_key("section", project, prefix))
        a, b, c = st.columns(3)
        table = a.text_input("Table", str(location.get("table") or ""),
                             key=_key("table", project, prefix))
        figure = b.text_input("Figure", str(location.get("figure") or ""),
                              key=_key("figure", project, prefix))
        supplement = c.text_input(
            "Supplementary item", str(location.get("supplementary_item") or ""),
            key=_key("supplement", project, prefix))
        location_note = st.text_input(
            "Source-location note", str(location.get("note") or ""),
            key=_key("location_note", project, prefix))
    topic = st.text_input("Topic", str(base.get("topic") or ""),
                          key=_key("topic", project, prefix))
    claim = st.text_input("Reported claim", str(base.get("claim") or ""),
                          key=_key("claim", project, prefix))
    with st.expander("Structured values, conditions, confidence, and conflicts", expanded=False):
        values_text = st.text_area(
            "Reported values (JSON)", json.dumps(base.get("reported_values") or {}, indent=2),
            key=_key("values", project, prefix))
        units_text = st.text_area(
            "Units (JSON)", json.dumps(base.get("units") or {}, indent=2),
            key=_key("units", project, prefix))
        conditions_text = st.text_area(
            "Conditions (JSON)", json.dumps(base.get("conditions") or {}, indent=2),
            key=_key("conditions", project, prefix))
        confidence = st.number_input(
            "Overall extraction confidence", min_value=0.0, max_value=1.0,
            value=float(base.get("extraction_confidence") or 0.0), step=0.05,
            key=_key("confidence", project, prefix))
        field_confidence_text = st.text_area(
            "Per-field confidence (JSON)",
            json.dumps(base.get("field_confidence") or {}, indent=2),
            key=_key("field_confidence", project, prefix))
        conflicts = st.text_area(
            "Conflicts (one per line)", "\n".join(base.get("conflicts") or []),
            key=_key("conflicts", project, prefix))
    notes = st.text_area(
        "Structured notes (do not paste a paper, full abstract, or raw model response)",
        str(base.get("notes") or ""), key=_key("notes", project, prefix))
    try:
        return {
            "schema_kind": schema_kind,
            "provenance": {
                "source": source,
                "doi": doi or None,
                "title": title or None,
                "url": url or None,
                "authors": [item.strip() for item in authors.split(";") if item.strip()],
                "year": _year(year),
                "query": query or None,
                "discovery_route": {
                    "method": discovery_method,
                    "source": source,
                    "query": query or None,
                    "executed_queries": [],
                    "record_identifier": record_identifier or None,
                },
            },
            "source_location": {
                "page": page or None, "page_range": page_range or None,
                "table": table or None, "figure": figure or None,
                "section": section or None,
                "supplementary_item": supplement or None,
                "dataset_record_identifier": record_identifier or None,
                "note": location_note or None,
            },
            "topic": topic or None,
            "claim": claim or None,
            "reported_values": _json_object(values_text, "Reported values"),
            "units": _json_object(units_text, "Units"),
            "conditions": _json_object(conditions_text, "Conditions"),
            "extraction_scope": E.SCOPE_MANUAL,
            "extraction_status": E.STATUS_MANUAL,
            "extraction_confidence": float(confidence),
            "field_confidence": _json_object(
                field_confidence_text, "Per-field confidence"),
            "conflicts": [item.strip() for item in conflicts.splitlines() if item.strip()],
            "notes": notes or None,
        }
    except (json.JSONDecodeError, evidence_review.EvidenceReviewError) as exc:
        st.error(f"Evidence fields are not ready: {exc}")
        return None


def _related_run(store, record):
    dependency = phase3_artifacts.artifact_dependency(record)
    if record.material_id is None:
        return None
    for run in reversed(store.list_runs(
            project_id=record.project_id, material_id=record.material_id)):
        if dependency in list(run.input_snapshot.get("artifact_identities") or []):
            return run
    return None


def _record_card(store, project, material, record) -> None:
    payload = record.payload
    provenance = payload.get("provenance") or {}
    title = provenance.get("title") or provenance.get("doi") or "Untitled evidence"
    linked_material = "Project-level evidence"
    if record.material_id:
        try:
            linked = store.get_material(record.material_id)
        except workspace_store.WorkspaceStoreError:
            linked_material = "Linked material unavailable"
        else:
            linked_material = linked.name if linked.project_id == project.project_id \
                else "Cross-project link refused"
    confidence = payload.get("extraction_confidence")
    confidence_label = f"{float(confidence):.0%}" \
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) \
        else "Not supplied"
    with st.container(border=True):
        st.markdown(f"**{title}**")
        bits = [str(provenance.get("year") or "Year not supplied"),
                str(payload.get("schema_kind") or "Evidence"),
                record.status.replace("_", " ").title()]
        st.caption(" · ".join(bits))
        app_ui.render_epistemic_badge("literature_evidence")
        st.write(payload.get("topic") or payload.get("claim") or "No topic or claim supplied.")
        st.caption(
            f"Material: {linked_material} · Confidence: {confidence_label} · "
            f"{payload.get('source_location_summary') or 'Exact source location not supplied.'}"
        )
        if st.button("Open evidence", type="primary",
                     key=_key("open", project, record.artifact_id)):
            st.session_state[_key("selected", project)] = record.artifact_id
            st.rerun()
        with st.expander("Full provenance and structured details", expanded=False):
            st.json({
                "citation": provenance,
                "source_location": payload.get("source_location"),
                "reported_values": payload.get("reported_values"),
                "units": payload.get("units"),
                "conditions": payload.get("conditions"),
                "field_confidence": payload.get("field_confidence"),
                "conflicts": payload.get("conflicts"),
                "review": {
                    "status": record.status, "reviewer": payload.get("reviewer"),
                    "time": payload.get("review_time"), "reason": payload.get("review_reason"),
                    "revision": record.revision,
                    "previous_revision_id": payload.get("previous_revision_id"),
                    "previous_revision_hash": payload.get("previous_revision_hash"),
                },
            })
        with st.expander("Raw structured JSON", expanded=False):
            st.json(record.to_dict())


def _selected_editor(store, project, material, record) -> None:
    st.divider()
    provenance = record.payload.get("provenance") or {}
    st.markdown(f"### {provenance.get('title') or record.artifact_id}")
    st.caption(
        f"Revision {record.revision} · {record.status.replace('_', ' ')} · "
        "literature context, not a measurement of the active material"
    )
    if st.button("Close evidence", key=_key("close", project, record.artifact_id)):
        st.session_state.pop(_key("selected", project), None)
        st.rerun()

    linked = bool(material and record.artifact_id in material.evidence_references)
    a, b = st.columns(2)
    if material and a.button(
            "Unlink from active material" if linked else "Link to active material",
            key=_key("link", project, record.artifact_id)):
        try:
            if linked:
                evidence_review.unlink_material(store, record.artifact_id)
            else:
                if record.material_id != material.material_id:
                    raise evidence_review.EvidenceReviewError(
                        "this evidence belongs to a different material")
                evidence_review.link_material(store, record.artifact_id)
            st.rerun()
        except (workspace_store.WorkspaceStoreError, ValueError) as exc:
            st.error(str(exc))
    if record.material_id and b.button(
            "Open related material", key=_key("material", project, record.artifact_id)):
        try:
            related_material = store.get_material(record.material_id)
            if related_material.project_id != project.project_id:
                raise evidence_review.EvidenceReviewError(
                    "related material does not belong to this project")
            store.set_active_context(project.project_id, related_material.material_id, None)
            st.session_state["_vl_pending_context_selection"] = {
                "project_id": project.project_id,
                "material_id": related_material.material_id,
                "run_id": None,
            }
            st.session_state["_vl_pending_nav_page"] = "Material Workspace"
            st.rerun()
        except (workspace_store.WorkspaceStoreError, ValueError) as exc:
            st.error(str(exc))

    if record.status == E.REVIEW_DRAFT:
        st.markdown("**Edit draft**")
        changes = _payload_controls(
            project, prefix=f"edit_{record.artifact_id}", base=record.payload)
        a, b = st.columns(2)
        editor = a.text_input("Editor", key=_key("editor", project, record.artifact_id))
        reason = b.text_input("Edit reason", key=_key("edit_reason", project, record.artifact_id))
        if st.button("Save draft edits", key=_key("edit_save", project, record.artifact_id)):
            try:
                if changes is None:
                    return
                evidence_review.edit_draft(
                    store, record.artifact_id, changes, editor=editor, reason=reason)
                st.success("Draft updated with provenance retained.")
                st.rerun()
            except (workspace_store.WorkspaceStoreError, ValueError) as exc:
                st.error(str(exc))
        a, b = st.columns(2)
        submitter = a.text_input("Submitter", key=_key("submitter", project, record.artifact_id))
        submit_reason = b.text_input(
            "Submission reason", key=_key("submit_reason", project, record.artifact_id))
        if st.button("Submit for review", type="primary",
                     key=_key("submit", project, record.artifact_id)):
            try:
                evidence_review.submit_for_review(
                    store, record.artifact_id, submitter=submitter,
                    reason=submit_reason)
                st.rerun()
            except (workspace_store.WorkspaceStoreError, ValueError) as exc:
                st.error(str(exc))
    elif record.status == E.REVIEW_NEEDS_REVIEW:
        st.warning("A named human reviewer must explicitly accept or reject this record.")
        a, b = st.columns(2)
        reviewer = a.text_input("Reviewer", key=_key("reviewer", project, record.artifact_id))
        reason = b.text_input("Review reason", key=_key("review_reason", project, record.artifact_id))
        a, b = st.columns(2)
        if a.button("Mark reviewed", type="primary",
                    key=_key("review", project, record.artifact_id)):
            try:
                evidence_review.mark_reviewed(
                    store, record.artifact_id, reviewer=reviewer, reason=reason)
                st.rerun()
            except (workspace_store.WorkspaceStoreError, ValueError) as exc:
                st.error(str(exc))
        if b.button("Reject", key=_key("reject", project, record.artifact_id)):
            try:
                evidence_review.reject_evidence(
                    store, record.artifact_id, reviewer=reviewer, reason=reason)
                st.rerun()
            except (workspace_store.WorkspaceStoreError, ValueError) as exc:
                st.error(str(exc))
    else:
        st.markdown("**Create a needs-review revision**")
        changes = _payload_controls(
            project, prefix=f"revise_{record.artifact_id}", base=record.payload)
        a, b = st.columns(2)
        creator = a.text_input("Revision creator", key=_key("rev_creator", project, record.artifact_id))
        reason = b.text_input("Revision reason", key=_key("rev_reason", project, record.artifact_id))
        if st.button("Create revision", key=_key("revise", project, record.artifact_id)):
            try:
                if changes is None:
                    return
                revised = evidence_review.revise_evidence(
                    store, record.artifact_id, changes, creator=creator, reason=reason)
                evidence_review.link_material(store, revised.artifact_id)
                st.session_state[_key("selected", project)] = revised.artifact_id
                st.rerun()
            except (workspace_store.WorkspaceStoreError, ValueError) as exc:
                st.error(str(exc))

    refreshed = evidence_review.get_evidence(
        store, record.artifact_id, project_id=project.project_id)
    if refreshed.status == E.REVIEW_REVIEWED:
        run = _related_run(store, refreshed)
        if run is None and st.button(
                "Save reviewed evidence to Results / History",
                key=_key("evidence_run", project, refreshed.artifact_id)):
            try:
                run = phase3_artifacts.save_reviewed_evidence_run(
                    store, refreshed.artifact_id, machine_id=machines.LITERATURE_ENGINE)
                store.set_active_context(project.project_id, refreshed.material_id, run.run_id)
                st.success("Reviewed evidence linked to an immutable run.")
                st.rerun()
            except workspace_store.WorkspaceStoreError as exc:
                st.error(str(exc))
        elif run is not None and st.button(
                "Open related run", key=_key("open_run", project, refreshed.artifact_id)):
            store.set_active_context(project.project_id, refreshed.material_id, run.run_id)
            st.session_state["_vl_pending_context_selection"] = {
                "project_id": project.project_id,
                "material_id": refreshed.material_id,
                "run_id": run.run_id,
            }
            st.session_state["_vl_pending_nav_page"] = "Results"
            st.rerun()


def render_editor(store, context) -> None:
    project, material = _records(store, context)
    if not project:
        st.warning("Select an active project before curating evidence.")
        return
    st.caption("Manual evidence works with AI disabled. Literature remains context, not measured validation.")
    mode = st.radio(
        "Evidence action", ("Browse and review", "Add evidence manually"),
        horizontal=True, key=_key("mode", project))
    if mode == "Add evidence manually":
        st.markdown("**Add evidence manually**")
        if not material:
            st.warning("Select a material to attach this evidence record.")
            return
        payload = _payload_controls(project, prefix="new")
        creator = st.text_input("Creator", key=_key("creator", project))
        if st.button("Save evidence draft", type="primary", key=_key("create", project)):
            try:
                if payload is None:
                    return
                record = evidence_review.create_manual_evidence(
                    store, project.project_id, material.material_id,
                    payload, creator=creator)
                evidence_review.link_material(store, record.artifact_id)
                st.session_state[_key("selected", project)] = record.artifact_id
                st.success("Evidence saved as a draft; it is not reviewed yet.")
                st.rerun()
            except (workspace_store.WorkspaceStoreError, ValueError) as exc:
                st.error(str(exc))
        return

    a, b, c = st.columns(3)
    status_filter = a.selectbox(
        "Review state", ("all", *E.REVIEW_STATUSES),
        format_func=lambda value: value.replace("_", " ").title(),
        key=_key("status_filter", project))
    schema_filter = b.selectbox(
        "Evidence type", ("all", *E.SCHEMA_KINDS),
        format_func=lambda value: value.title(), key=_key("schema_filter", project))
    scope = c.selectbox(
        "Material scope", ("Active material", "Whole project"),
        key=_key("scope", project))
    query = st.text_input("Filter by title, topic, or claim", key=_key("query_filter", project))
    records = evidence_review.list_evidence(
        store,
        project_id=project.project_id,
        material_id=material.material_id if material and scope == "Active material" else None,
        review_status=None if status_filter == "all" else status_filter,
        schema_kind=None if schema_filter == "all" else schema_filter,
        latest_only=False,
    )
    if query.strip():
        needle = query.strip().lower()
        records = [record for record in records if needle in " ".join([
            str((record.payload.get("provenance") or {}).get("title") or ""),
            str(record.payload.get("topic") or ""), str(record.payload.get("claim") or ""),
        ]).lower()]
    st.caption(f"{len(records)} durable evidence record(s), including retained revision history.")
    if records:
        st.download_button(
            "Export structured JSON", evidence_review.export_json(records),
            "evidence.json", "application/json", key=_key("json_export", project))
        for schema_kind in E.SCHEMA_KINDS:
            subset = [record for record in records
                      if record.payload.get("schema_kind") == schema_kind]
            if not subset:
                continue
            a, b = st.columns(2)
            a.download_button(
                f"Export {schema_kind} CSV", evidence_review.export_csv(subset, schema_kind),
                f"evidence_{schema_kind}.csv", "text/csv",
                key=_key(f"csv_{schema_kind}", project))
            b.download_button(
                f"Export {schema_kind} provenance package",
                evidence_review.export_package(subset, schema_kind),
                f"evidence_{schema_kind}_package.json", "application/json",
                key=_key(f"package_{schema_kind}", project))
        for record in reversed(records):
            _record_card(store, project, material, record)
    else:
        st.info("No evidence matches these filters.")

    selected_id = st.session_state.get(_key("selected", project))
    if selected_id:
        try:
            selected = evidence_review.get_evidence(
                store, selected_id, project_id=project.project_id)
        except (workspace_store.WorkspaceStoreError, ValueError):
            st.session_state.pop(_key("selected", project), None)
        else:
            _selected_editor(store, project, material, selected)


__all__ = ["render_editor"]
