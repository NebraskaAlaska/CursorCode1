"""Measured-signal and user-reference UI for the single XRD advisory authority."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

import app_ui
from flyash_phreeqc_ml import workspace_store
from flyash_phreeqc_ml.instruments import xrd_advisory as xrd
from flyash_phreeqc_ml.instruments import xrd_records
from ui.common import _guard_uploaded_file


def _records(store, context):
    try:
        project = store.get_project(context.get("active_project_id"))
        material = store.get_material(context.get("active_material_id"))
    except workspace_store.WorkspaceStoreError:
        return None, None
    if material.project_id != project.project_id:
        return None, None
    return project, material


def _key(name: str, project, material) -> str:
    return f"phase3_xrd_{name}__{project.project_id}__{material.material_id}"


def _state(project, material) -> dict:
    return dict(st.session_state.get(_key("state", project, material)) or {})


def _save_state(project, material, state: dict) -> None:
    st.session_state[_key("state", project, material)] = state


def _optional_float(value: str, label: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise xrd.XrdDataError(f"{label} must be numeric when supplied") from exc
    return number


def _json_list(text: str, label: str) -> list:
    value = json.loads(text or "[]")
    if not isinstance(value, list):
        raise xrd.XrdDataError(f"{label} must be a JSON list")
    return value


def _plot_pattern(pattern: xrd.MeasuredXrdPattern) -> None:
    plot_rows = [row for row in pattern.plot_ready_rows()
                 if row.get("y_intensity") is not None]
    st.markdown("**Measured signal**")
    st.caption("This plot shows supplied intensity versus 2θ; it is not a phase result.")
    if not plot_rows:
        st.info("The imported positions have no supplied intensity values to plot.")
        return
    frame = pd.DataFrame(plot_rows)
    st.line_chart(frame, x="x_two_theta_deg", y="y_intensity")


def _patterns(store, project, material):
    return _latest_lineage_records(store.list_artifacts(
        project_id=project.project_id,
        material_id=material.material_id,
        record_type=workspace_store.ARTIFACT_XRD_PATTERN,
    ))


def _references(store, project, material):
    return _latest_lineage_records(store.list_artifacts(
        project_id=project.project_id,
        material_id=material.material_id,
        record_type=workspace_store.ARTIFACT_XRD_REFERENCE,
    ))


def _latest_lineage_records(records):
    """Show only one editable/current head per lineage; ancestors stay in run history."""
    heads = {}
    for record in records:
        current = heads.get(record.logical_id)
        if current is None or record.revision > current.revision:
            heads[record.logical_id] = record
    return sorted(
        heads.values(),
        key=lambda record: (record.created_at, record.revision, record.artifact_id),
        reverse=True,
    )


def _pattern_label(record) -> str:
    payload = record.payload
    return (f"{payload.get('sample_id') or 'Unnamed sample'} · revision {record.revision} · "
            f"{record.artifact_id}")


def _reference_label(record) -> str:
    payload = record.payload
    return (f"{payload.get('phase_name') or 'Unnamed reference'} · "
            f"{record.status.replace('_', ' ')} · {record.artifact_id}")


def _render_pattern_import(store, project, material, state: dict) -> None:
    st.markdown("**Import a measured pattern**")
    st.caption("CSV needs a 2θ column and may include intensity. Original order and row decisions are retained.")
    upload = st.file_uploader(
        "Measured XRD CSV", type=["csv"], key=_key("pattern_upload", project, material))
    a, b, c = st.columns(3)
    sample_id = a.text_input("Sample ID", key=_key("sample", project, material))
    radiation = b.text_input(
        "Radiation source", "Cu Kalpha", key=_key("radiation", project, material))
    wavelength = c.text_input(
        "Wavelength (Å, optional)", "1.5406", key=_key("wavelength", project, material))
    a, b = st.columns(2)
    instrument = a.text_input("Instrument", key=_key("instrument", project, material))
    method = b.text_input("Method", key=_key("method", project, material))
    a, b = st.columns(2)
    intensity_unit = a.text_input(
        "Intensity unit", key=_key("intensity_unit", project, material))
    intensity_type = b.text_input(
        "Intensity type", key=_key("intensity_type", project, material))
    duplicate_label = st.selectbox(
        "Duplicate 2θ policy",
        ("Retain every row", "Reject later duplicates in source order"),
        key=_key("duplicate_policy", project, material),
    )
    with st.expander("Instrument and column-mapping details", expanded=False):
        a, b, c = st.columns(3)
        step = a.text_input("Step size (°)", key=_key("step", project, material))
        measured_at = b.text_input("Measurement date/time", key=_key("measured_at", project, material))
        operator = c.text_input("Operator", key=_key("operator", project, material))
        lab = st.text_input("Lab", key=_key("lab", project, material))
        a, b = st.columns(2)
        two_theta_column = a.text_input(
            "2θ source heading (optional)", key=_key("theta_column", project, material))
        intensity_column = b.text_input(
            "Intensity source heading (optional)", key=_key("intensity_column", project, material))
    if st.button("Import and inspect measured signal", type="primary",
                 key=_key("pattern_import", project, material)):
        try:
            if upload is None:
                raise xrd.XrdDataError("choose a measured CSV first")
            validated = _guard_uploaded_file(upload, allowed_extensions={".csv"})
            if validated is None:
                raise xrd.XrdDataError("the measured CSV did not pass validation")
            mapping = {}
            if two_theta_column.strip():
                mapping["two_theta"] = two_theta_column.strip()
            if intensity_column.strip():
                mapping["intensity"] = intensity_column.strip()
            pattern = xrd.import_measured_pattern_csv(
                validated.data,
                source_filename=upload.name,
                metadata={
                    "project_id": project.project_id,
                    "material_id": material.material_id,
                    "sample_id": sample_id,
                    "radiation_source": radiation,
                    "wavelength_angstrom": _optional_float(wavelength, "Wavelength"),
                    "instrument": instrument,
                    "method": method,
                    "intensity_unit": intensity_unit or None,
                    "intensity_type": intensity_type or None,
                    "step_size_deg": _optional_float(step, "Step size"),
                    "measured_at": measured_at or None,
                    "operator": operator,
                    "lab": lab,
                    "two_theta_unit": "degrees 2theta",
                },
                column_mapping=mapping or None,
                duplicate_policy=(xrd.DUPLICATE_KEEP_ALL
                                  if duplicate_label == "Retain every row"
                                  else xrd.DUPLICATE_REJECT_LATER),
            )
            state["pattern_preview"] = pattern
            state["pattern_source_bytes"] = validated.data
            _save_state(project, material, state)
            st.success("Measured signal imported for review; no phase conclusion was produced.")
        except (ValueError, workspace_store.WorkspaceStoreError) as exc:
            st.error(f"Measured pattern refused: {exc}")
    pattern = state.get("pattern_preview")
    if not isinstance(pattern, xrd.MeasuredXrdPattern):
        return
    _plot_pattern(pattern)
    app_ui.render_metric_cards([
        {"label": "Imported rows", "value": pattern.raw_imported_row_count},
        {"label": "Accepted rows", "value": pattern.accepted_row_count,
         "status": "success"},
        {"label": "Rejected rows", "value": len(pattern.rejected_rows),
         "status": "warning" if pattern.rejected_rows else "success"},
    ])
    if pattern.warnings:
        st.warning(pattern.warnings[0])
    with st.expander("Import decisions and source identity", expanded=False):
        st.json({
            "source_filename": pattern.source_filename,
            "source_sha256": pattern.source_sha256,
            "original_headings": pattern.original_headings,
            "column_mapping": pattern.column_mapping,
            "duplicate_policy": pattern.duplicate_policy,
            "duplicate_two_theta": pattern.duplicate_two_theta,
            "source_order_preserved": pattern.source_order_preserved,
            "rejected_rows": pattern.rejected_rows,
            "warnings": pattern.warnings,
        })
    a, b = st.columns(2)
    creator = a.text_input("Imported by", key=_key("pattern_creator", project, material))
    reason = b.text_input("Import reason", key=_key("pattern_reason", project, material))
    if st.button("Save immutable measured pattern",
                 key=_key("pattern_save", project, material)):
        try:
            artifact = xrd_records.save_measured_pattern(
                store,
                project_id=project.project_id,
                material_id=material.material_id,
                pattern=pattern,
                creator=creator,
                reason=reason,
            )
            state["selected_pattern_id"] = artifact.artifact_id
            state.pop("pattern_preview", None)
            _save_state(project, material, state)
            st.success("Measured pattern saved with its exact source hash.")
            st.rerun()
        except (ValueError, workspace_store.WorkspaceStoreError) as exc:
            st.error(str(exc))


def _render_peak_revision(store, project, material, state: dict) -> None:
    st.markdown("**Review or attach a measured peak list**")
    patterns = _patterns(store, project, material)
    if not patterns:
        st.info("Save a measured pattern first.")
        return
    by_id = {record.artifact_id: record for record in patterns}
    selected = st.selectbox(
        "Measured pattern", list(by_id), format_func=lambda value: _pattern_label(by_id[value]),
        key=_key("peak_pattern", project, material))
    loaded = xrd_records.load_measured_pattern(
        store, selected, project_id=project.project_id, material_id=material.material_id)
    _plot_pattern(loaded.pattern)
    existing = loaded.pattern.user_peak_list or []
    peaks_text = st.text_area(
        "User-supplied peak list (JSON)", json.dumps(existing, indent=2), height=220,
        key=_key("peak_json", project, material),
        help="Each item may be a 2θ number or an object with two_theta_deg and optional relative_intensity.")
    a, b = st.columns(2)
    provider = a.text_input("Peak list reviewed by", key=_key("peak_provider", project, material))
    notes = b.text_input("Peak-selection notes", key=_key("peak_notes", project, material))
    reason = st.text_input("Revision reason", key=_key("peak_reason", project, material))
    if st.button("Save peak list as a new immutable revision",
                 key=_key("peak_save", project, material)):
        try:
            artifact = xrd_records.revise_measured_pattern_peaks(
                store,
                selected,
                project_id=project.project_id,
                material_id=material.material_id,
                peaks=_json_list(peaks_text, "Peak list"),
                provenance={"provided_by": provider, "notes": notes, "user_edits": []},
                creator=provider,
                reason=reason,
            )
            state["selected_pattern_id"] = artifact.artifact_id
            _save_state(project, material, state)
            st.success("Peak list saved as a linked measured-pattern revision.")
            st.rerun()
        except (json.JSONDecodeError, ValueError,
                workspace_store.WorkspaceStoreError) as exc:
            st.error(str(exc))
    with st.expander("Peak selection provenance", expanded=False):
        st.json(loaded.pattern.peak_selection_provenance or {
            "selection_method": "No user peak list supplied"})


def _render_reference_import(store, project, material, state: dict) -> None:
    st.markdown("**Import a user-supplied reference peak table**")
    st.caption("CSV and JSON are supported. Files stay in runtime storage and are not bundled with the application.")
    upload = st.file_uploader(
        "Reference peak table", type=["csv", "json"],
        key=_key("reference_upload", project, material))
    a, b = st.columns(2)
    phase_name = a.text_input("Phase name", key=_key("reference_phase", project, material))
    formula = b.text_input("Formula (optional)", key=_key("reference_formula", project, material))
    a, b = st.columns(2)
    polymorph = a.text_input("Polymorph / crystal form", key=_key("reference_form", project, material))
    source_name = b.text_input("Source / database name", key=_key("reference_source", project, material))
    a, b, c = st.columns(3)
    source_record_id = a.text_input("Record / card ID", key=_key("reference_record", project, material))
    radiation = b.text_input("Radiation source", "Cu Kalpha",
                             key=_key("reference_radiation", project, material))
    wavelength = c.text_input("Wavelength (Å)", "1.5406",
                              key=_key("reference_wavelength", project, material))
    with st.expander("Citation, license, and redistribution provenance", expanded=False):
        title = st.text_input("Reference title", key=_key("reference_title", project, material))
        authors = st.text_input("Authors (semicolon-separated)",
                                key=_key("reference_authors", project, material))
        a, b, c = st.columns(3)
        year = a.text_input("Year", key=_key("reference_year", project, material))
        doi = b.text_input("DOI", key=_key("reference_doi", project, material))
        url = c.text_input("URL", key=_key("reference_url", project, material))
        license_status = st.text_input(
            "License status", "unknown", key=_key("reference_license", project, material))
        redistribution = st.selectbox(
            "Redistribution permission",
            ("unknown", "not_permitted", "permitted_by_cited_source"),
            key=_key("reference_redistribution", project, material))
        redistribution_basis = st.text_input(
            "Redistribution basis / license citation",
            key=_key("reference_redistribution_basis", project, material),
            help=("Required with a cited DOI or URL before permission can be recorded as "
                  "permitted. Unknown remains unknown."),
        )
        notes = st.text_area("Reference notes", key=_key("reference_notes", project, material))
        st.caption("Unknown license remains unknown. Never enter a license key or proprietary database content.")
    if st.button("Import reference for review", type="primary",
                 key=_key("reference_import", project, material)):
        try:
            if upload is None:
                raise xrd.XrdDataError("choose a CSV or JSON reference first")
            validated = _guard_uploaded_file(upload, allowed_extensions={".csv", ".json"})
            if validated is None:
                raise xrd.XrdDataError("the reference file did not pass validation")
            metadata = {
                "phase_name": phase_name, "formula": formula, "polymorph": polymorph,
                "source_name": source_name, "source_record_id": source_record_id,
                "radiation_source": radiation,
                "wavelength_angstrom": _optional_float(wavelength, "Reference wavelength"),
                "title": title,
                "authors": [item.strip() for item in authors.split(";") if item.strip()],
                "year": int(year) if year.strip() else None,
                "doi": doi, "url": url, "license_status": license_status,
                "redistribution_permission_status": redistribution,
                "redistribution_basis": redistribution_basis,
                "notes": notes, "review_status": "needs_review",
            }
            if upload.name.lower().endswith(".json"):
                reference = xrd.import_reference_json(
                    validated.data, source_filename=upload.name,
                    source_metadata=metadata)
            else:
                reference = xrd.import_reference_csv(
                    validated.data, source_filename=upload.name,
                    source_metadata=metadata)
            state["reference_preview"] = reference
            _save_state(project, material, state)
            st.success("Reference imported as untrusted / needs review.")
        except (ValueError, workspace_store.WorkspaceStoreError) as exc:
            st.error(f"Reference refused: {exc}")
    reference = state.get("reference_preview")
    if isinstance(reference, xrd.ExternalXrdReference):
        st.dataframe(pd.DataFrame(reference.peaks), use_container_width=True, hide_index=True)
        st.warning(
            f"Review state: needs review · License: {reference.license_status} · "
            f"Redistribution: {reference.redistribution_permission_status}"
        )
        a, b = st.columns(2)
        creator = a.text_input("Imported by", key=_key("reference_creator", project, material))
        reason = b.text_input("Import reason", key=_key("reference_reason", project, material))
        if st.button("Save reference with provenance",
                     key=_key("reference_save", project, material)):
            try:
                artifact = xrd_records.save_xrd_reference(
                    store,
                    project_id=project.project_id,
                    material_id=material.material_id,
                    reference=reference,
                    creator=creator,
                    reason=reason,
                )
                state.pop("reference_preview", None)
                state["selected_reference_id"] = artifact.artifact_id
                _save_state(project, material, state)
                st.rerun()
            except (ValueError, workspace_store.WorkspaceStoreError) as exc:
                st.error(str(exc))

    references = _references(store, project, material)
    if not references:
        return
    st.markdown("**Saved reference review**")
    by_id = {record.artifact_id: record for record in references}
    selected = st.selectbox(
        "Saved reference", list(by_id), format_func=lambda value: _reference_label(by_id[value]),
        key=_key("reference_select", project, material))
    record = by_id[selected]
    payload = record.payload
    st.caption(
        f"Source: {payload.get('source_name')} · License: {payload.get('license_status')} · "
        f"Redistribution: {payload.get('redistribution_permission_status')}"
    )
    with st.expander("Reference provenance", expanded=False):
        st.json({"source_identity": record.source_identity,
                 "provenance": payload,
                 "review_events": record.provenance.get("review_events") or []})
    if record.status == "needs_review":
        a, b = st.columns(2)
        reviewer = a.text_input("Reference reviewer", key=_key("reference_reviewer", project, material))
        review_reason = b.text_input("Review reason", key=_key("reference_review_reason", project, material))
        a, b = st.columns(2)
        if a.button("Mark reference reviewed", key=_key("reference_review", project, material)):
            try:
                xrd_records.review_xrd_reference(
                    store, selected, project_id=project.project_id,
                    material_id=material.material_id, review_status="reviewed",
                    reviewer=reviewer, reason=review_reason)
                st.rerun()
            except (ValueError, workspace_store.WorkspaceStoreError) as exc:
                st.error(str(exc))
        if b.button("Reject reference", key=_key("reference_reject", project, material)):
            try:
                xrd_records.review_xrd_reference(
                    store, selected, project_id=project.project_id,
                    material_id=material.material_id, review_status="rejected",
                    reviewer=reviewer, reason=review_reason)
                st.rerun()
            except (ValueError, workspace_store.WorkspaceStoreError) as exc:
                st.error(str(exc))


def _render_comparison_prepare(store, project, material, state: dict) -> None:
    st.markdown("**Prepare a tentative advisory comparison**")
    patterns = _patterns(store, project, material)
    references = [record for record in _references(store, project, material)
                  if record.status != "rejected"]
    if not patterns or not references:
        st.info("Save one measured pattern with a user peak list and at least one non-rejected reference.")
        return
    pattern_by_id = {record.artifact_id: record for record in patterns}
    ref_by_id = {record.artifact_id: record for record in references}
    default_pattern = state.get("selected_pattern_id")
    pattern_id = st.selectbox(
        "Measured pattern", list(pattern_by_id),
        index=list(pattern_by_id).index(default_pattern)
        if default_pattern in pattern_by_id else 0,
        format_func=lambda value: _pattern_label(pattern_by_id[value]),
        key=_key("compare_pattern", project, material))
    reference_ids = st.multiselect(
        "User references", list(ref_by_id),
        format_func=lambda value: _reference_label(ref_by_id[value]),
        key=_key("compare_refs", project, material))
    tolerance = st.number_input(
        "Tolerance (±° 2θ)", min_value=0.01, max_value=2.0,
        value=float(xrd.DEFAULT_MATCH_TOLERANCE_DEG), step=0.05,
        key=_key("compare_tolerance", project, material))
    if st.button("Prepare tentative possible matches", type="primary",
                 key=_key("compare_prepare", project, material)):
        try:
            if not reference_ids:
                raise xrd.XrdDataError("choose at least one user reference")
            pattern = xrd_records.load_measured_pattern(
                store, pattern_id, project_id=project.project_id,
                material_id=material.material_id)
            loaded_references = [xrd_records.load_xrd_reference(
                store, artifact_id, project_id=project.project_id,
                material_id=material.material_id) for artifact_id in reference_ids]
            result = xrd.match_measured_peaks(
                pattern.pattern, tolerance=tolerance,
                references=[item.reference for item in loaded_references])
            state["comparison"] = {
                "pattern_artifact_id": pattern_id,
                "reference_artifact_ids": list(reference_ids),
                "tolerance": float(tolerance),
                "match_payload": result.to_dict(),
            }
            _save_state(project, material, state)
            st.success("Tentative advisory comparison prepared. Review it under Results before saving.")
        except (ValueError, workspace_store.WorkspaceStoreError) as exc:
            st.error(str(exc))


def render_prepare(store, context) -> None:
    project, material = _records(store, context)
    if not project or not material:
        st.warning("Select an active project and material before importing XRD data.")
        return
    state = _state(project, material)
    step = st.radio(
        "XRD preparation step",
        ("Measured pattern", "Peak list", "References", "Prepare comparison"),
        horizontal=True,
        key=_key("prepare_step", project, material),
    )
    if step == "Measured pattern":
        _render_pattern_import(store, project, material, state)
    elif step == "Peak list":
        _render_peak_revision(store, project, material, state)
    elif step == "References":
        _render_reference_import(store, project, material, state)
    else:
        _render_comparison_prepare(store, project, material, state)


def _current_result(store, context, project, material, state: dict):
    run_id = context.get("active_run_id")
    if run_id:
        try:
            run = store.get_run(run_id)
            if run.machine_id == xrd_records.XRD_MACHINE_ID \
                    and run.output_type == xrd_records.XRD_RUN_OUTPUT_TYPE:
                loaded = xrd_records.load_tentative_match_run(
                    store, run_id, project_id=project.project_id,
                    material_id=material.material_id)
                return {
                    "run": loaded.run,
                    "pattern": loaded.pattern.pattern,
                    "references": [item.reference for item in loaded.references],
                    "match_payload": loaded.match_payload,
                    "is_stale": loaded.is_stale,
                    "stale_reasons": list(loaded.stale_reasons),
                }
        except workspace_store.WorkspaceStoreError as exc:
            st.error(f"Saved XRD advisory run cannot be reopened safely: {exc}")
            return None
    comparison = state.get("comparison")
    if not isinstance(comparison, dict):
        return None
    try:
        pattern = xrd_records.load_measured_pattern(
            store, comparison["pattern_artifact_id"],
            project_id=project.project_id, material_id=material.material_id)
        references = [xrd_records.load_xrd_reference(
            store, artifact_id, project_id=project.project_id,
            material_id=material.material_id)
                      for artifact_id in comparison["reference_artifact_ids"]]
    except workspace_store.WorkspaceStoreError as exc:
        st.error(f"Prepared XRD artifacts changed: {exc}")
        return None
    return {"run": None, "pattern": pattern.pattern,
            "references": [item.reference for item in references],
            "match_payload": comparison["match_payload"], "comparison": comparison,
            "is_stale": False, "stale_reasons": []}


def render_results(store, context) -> bool:
    project, material = _records(store, context)
    if not project or not material:
        st.warning("Select an active project and material.")
        return True
    state = _state(project, material)
    current = _current_result(store, context, project, material, state)
    if current is None:
        return False
    payload = current["match_payload"]
    app_ui.render_epistemic_badge("advisory_interpretation")
    st.markdown("**Tentative advisory possible matches**")
    st.warning(
        "Check every possible match against an appropriate reference source and the full measured pattern."
    )
    _plot_pattern(current["pattern"])
    app_ui.render_metric_cards([
        {"label": "Candidate references", "value": len(payload.get("candidates") or [])},
        {"label": "Unmatched measured peaks", "value": len(payload.get("unmatched_measured") or []),
         "status": "warning"},
        {"label": "Overlap ambiguity", "value": payload.get("overlap_ambiguity_count", 0),
         "status": "warning"},
        {"label": "Tolerance", "value": f"±{payload.get('tolerance_deg')}° 2θ"},
    ])
    candidates = list(payload.get("candidates") or [])
    if candidates:
        rows = [{
            "possible match": item.get("phase"),
            "formula": item.get("formula"),
            "polymorph": item.get("polymorph"),
            "tentative confidence": item.get("tentative_confidence"),
            "matched peaks": item.get("n_matched"),
            "reference peaks": item.get("n_reference"),
            "radiation compatibility": item.get("radiation_compatibility"),
            "ambiguity count": item.get("ambiguity_overlap_count"),
        } for item in candidates]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No tentative possible match was produced from the supplied peak lists.")
    if payload.get("unmatched_measured"):
        st.caption("Unmatched measured peaks: " + ", ".join(
            str(value) for value in payload["unmatched_measured"]))
    if current["is_stale"]:
        st.warning("Historical advisory run — one or more current material inputs have changed.")
    with st.expander("Limitations, ambiguity, and all warnings", expanded=False):
        for item in payload.get("warnings") or []:
            st.markdown(f"- {item}")
        for item in payload.get("limitations") or []:
            st.markdown(f"- {item}")
        if current["stale_reasons"]:
            st.json(current["stale_reasons"])
    with st.expander("Reference source, license, and radiation provenance", expanded=False):
        st.json([reference.provenance() for reference in current["references"]])
    with st.expander("Technical identities and raw advisory envelope", expanded=False):
        st.json({
            "run_id": getattr(current.get("run"), "run_id", None),
            "measured_peak_identity": payload.get("measured_peak_identity"),
            "reference_identities": payload.get("reference_identities"),
            "radiation_compatibility": payload.get("radiation_compatibility"),
            "comparison_status": payload.get("comparison_status"),
            "match_payload": payload,
        })
    if current.get("run") is not None:
        st.success("Reopened with the exact measured-pattern and reference artifact identities.")
        return True
    a, b = st.columns(2)
    creator = a.text_input("Saved by", key=_key("match_creator", project, material))
    reason = b.text_input("Save reason", key=_key("match_reason", project, material))
    if st.button("Save immutable advisory run", type="primary",
                 key=_key("match_save", project, material)):
        comparison = current["comparison"]
        try:
            saved = xrd_records.save_tentative_match_run(
                store,
                project_id=project.project_id,
                material_id=material.material_id,
                pattern_artifact_id=comparison["pattern_artifact_id"],
                reference_artifact_ids=comparison["reference_artifact_ids"],
                tolerance=comparison["tolerance"],
                creator=creator,
                reason=reason,
            )
            store.set_active_context(project.project_id, material.material_id, saved.run.run_id)
            st.success("Tentative advisory run saved with exact source and license identities.")
            st.rerun()
        except (ValueError, workspace_store.WorkspaceStoreError) as exc:
            st.error(str(exc))
    return True


__all__ = ["render_prepare", "render_results"]
