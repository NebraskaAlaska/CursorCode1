"""Streamlit presentation for the durable Phase 3 ICP review authority."""
from __future__ import annotations

import csv
from io import StringIO
import json
from typing import Any

import pandas as pd
import streamlit as st

import app_ui
from flyash_phreeqc_ml import workspace_store
from flyash_phreeqc_ml.instruments import icp_processor, icp_review
from flyash_phreeqc_ml.instruments.phase3_icp_export import safe_csv_bytes
from ui.common import _guard_uploaded_file


_SYNTHETIC_ROWS = [
    {"row_id": "measured-a", "sample_id": "SYN-1", "element": "Ca",
     "concentration": 2.0, "unit": "mM", "dilution_factor": 1.0,
     "measured_or_predicted": "measured"},
    {"row_id": "predicted-a", "sample_id": "SYN-1", "element": "Ca",
     "concentration": 1.5, "unit": "mM", "dilution_factor": 1.0,
     "measured_or_predicted": "predicted"},
]


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
    return f"phase3_icp_{name}__{project.project_id}__{material.material_id}"


def _state_key(project, material) -> str:
    return _key("state", project, material)


def _current_state(project, material) -> dict:
    return dict(st.session_state.get(_state_key(project, material)) or {})


def _save_state(project, material, state: dict) -> None:
    st.session_state[_state_key(project, material)] = state


def _parse_rows_json(text: str) -> list[dict]:
    value = json.loads(text or "[]")
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise icp_review.IcpReviewError("ICP rows must be a JSON list of objects")
    return value


def _parse_csv(data: bytes) -> tuple[list[dict], list[str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise icp_review.IcpReviewError("ICP CSV must be UTF-8 encoded") from exc
    reader = csv.DictReader(StringIO(text))
    headings = [str(item or "").strip() for item in (reader.fieldnames or [])]
    if not headings or any(not item for item in headings):
        raise icp_review.IcpReviewError("ICP CSV needs one non-blank header row")
    if len(set(headings)) != len(headings):
        raise icp_review.IcpReviewError("ICP CSV headings must be unique")
    rows: list[dict] = []
    for row in reader:
        if None in row:
            raise icp_review.IcpReviewError("ICP CSV has more values than headings")
        cleaned = {str(key).strip(): (value if value != "" else None)
                   for key, value in row.items()}
        if any(value is not None for value in cleaned.values()):
            rows.append(cleaned)
    if not rows:
        raise icp_review.IcpReviewError("ICP CSV contains no data rows")
    return rows, headings


def _prepared_from_record(record) -> dict:
    return {
        "project_id": record.project_id,
        "material_id": record.material_id,
        "record_type": record.record_type,
        "status": record.status,
        "input_snapshot": record.input_snapshot,
        "source_identity": record.source_identity,
        "payload": record.payload,
        "creator": record.creator,
        "reason": record.reason,
        "provenance": record.provenance,
    }


def _load_state(record) -> dict:
    return {
        "artifact_id": record.artifact_id,
        "prepared": _prepared_from_record(record),
        "rows": list(record.payload.get("original_rows") or []),
        "source_bytes": None,
        "source_filename": record.source_identity.get("source_filename"),
        "corrections": list(record.payload.get("corrections") or []),
        "duplicate_selections": list(record.payload.get("duplicate_selections") or []),
        "dirty": False,
    }


def _rebuild(project, material, state: dict, *, creator: str, status: str,
             reason: str = "") -> dict:
    prepared = icp_review.prepare_icp_review(
        project_id=project.project_id,
        material_id=material.material_id,
        rows=state.get("rows") or [],
        creator=creator,
        source_bytes=state.get("source_bytes"),
        source_filename=state.get("source_filename"),
        corrections=state.get("corrections") or [],
        duplicate_selections=state.get("duplicate_selections") or [],
        status=status,
        reason=reason,
        provenance={
            "entry_surface": "Phase 3 ICP machine workspace",
            "source_column_headings": state.get("source_column_headings") or [],
        },
    )
    state["prepared"] = prepared
    state["corrections"] = list(prepared["payload"].get("corrections") or [])
    state["duplicate_selections"] = list(
        prepared["payload"].get("duplicate_selections") or [])
    state["dirty"] = True
    return state


_CORRECTABLE_QC_CODES = {
    "unit": {icp_processor.QC_MISSING_UNIT, icp_processor.QC_UNKNOWN_UNIT},
    "dilution_factor": {
        icp_processor.QC_MISSING_DILUTION,
        icp_processor.QC_NONNUMERIC_DILUTION,
        icp_processor.QC_NONFINITE_DILUTION,
        icp_processor.QC_NONPOSITIVE_DILUTION,
    },
    "role": {icp_processor.QC_MISSING_ROLE, icp_processor.QC_UNKNOWN_ROLE},
    "element": {icp_processor.QC_MISSING_ELEMENT, icp_processor.QC_UNKNOWN_ELEMENT},
    "sample_id": {icp_processor.QC_MISSING_SAMPLE_ID},
}


def _reviewable_correction_fields(processed_row: dict) -> tuple[str, ...]:
    """Expose only metadata fields the backend identified as missing or invalid."""
    codes = set(processed_row.get("qc_codes") or [])
    return tuple(field for field, issue_codes in _CORRECTABLE_QC_CODES.items()
                 if codes & issue_codes)


def _display_processed(prepared: dict) -> None:
    processed = (prepared.get("payload") or {}).get("processed") or {}
    summary = processed.get("qc_summary") or {}
    app_ui.render_metric_cards([
        {"label": "Usable", "value": summary.get(icp_processor.QC_USABLE, 0),
         "status": "success"},
        {"label": "Censored", "value": summary.get(icp_processor.QC_CENSORED, 0),
         "status": "warning"},
        {"label": "Review needed", "value": summary.get(icp_processor.QC_REVIEW_REQUIRED, 0),
         "status": "warning"},
        {"label": "Excluded", "value": summary.get(icp_processor.QC_EXCLUDED, 0),
         "status": "warning"},
    ])
    corrected = list(processed.get("corrected") or [])
    if corrected:
        concise_rows = []
        for row in corrected:
            supplied_value = row.get("supplied_concentration")
            supplied_unit = row.get("supplied_unit") or "unit not supplied"
            concise_rows.append({
                "Row": row.get("row_id"),
                "Sample": row.get("sample_id"),
                "Analyte / role": f"{row.get('element') or '—'} · {row.get('role') or '—'}",
                "Supplied reading": (
                    f"{supplied_value} {supplied_unit}"
                    if supplied_value is not None else f"Missing · {supplied_unit}"
                ),
                "QC state": str(row.get("qc_status") or "unknown").replace("_", " ").title(),
                "Validation eligible": "Yes" if row.get("validation_eligible") else "No",
            })
        st.dataframe(pd.DataFrame(concise_rows), use_container_width=True, hide_index=True)
        with st.expander("Complete processed row details", expanded=False):
            st.caption(
                "Includes conversion bindings, correction fields, QC codes, and exact row values."
            )
            st.dataframe(pd.DataFrame(corrected), use_container_width=True, hide_index=True)
    st.caption("Only rows marked validation eligible by the Phase 1B processor can enter comparison.")


def render_prepare(store, context) -> None:
    project, material = _records(store, context)
    if not project or not material:
        st.warning("Select an active project and material before reviewing ICP data.")
        return

    st.markdown("**Review supplied ICP rows**")
    st.caption("Import a long-form CSV or enter rows directly. Synthetic examples are labelled test data.")
    reviews = list(reversed(icp_review.list_icp_reviews(
        store, project_id=project.project_id, material_id=material.material_id)))
    if reviews:
        by_id = {record.artifact_id: record for record in reviews}
        chosen = st.selectbox(
            "Saved review",
            [None, *by_id],
            format_func=lambda value: "Start from a new source" if value is None else
            f"{value} · revision {by_id[value].revision} · {by_id[value].status.replace('_', ' ')}",
            key=_key("saved_select", project, material),
        )
        if chosen and st.button("Load exact saved review", key=_key("load", project, material)):
            record = icp_review.load_icp_review(
                store, chosen, project_id=project.project_id, material_id=material.material_id)
            _save_state(project, material, _load_state(record))
            st.session_state[_key("rows_json", project, material)] = json.dumps(
                record.payload.get("original_rows") or [], indent=2)
            st.rerun()

    state = _current_state(project, material)
    source_mode = st.radio(
        "Source",
        ("Enter long-form rows", "Upload long-form CSV"),
        horizontal=True,
        key=_key("source_mode", project, material),
    )
    uploaded = None
    if source_mode == "Upload long-form CSV":
        st.caption("Expected columns include sample_id, element, concentration, unit, dilution_factor, and measured_or_predicted.")
        uploaded = st.file_uploader(
            "ICP CSV", type=["csv"], key=_key("upload", project, material))
    else:
        rows_text = st.text_area(
            "Rows (JSON)",
            json.dumps(_SYNTHETIC_ROWS, indent=2),
            height=260,
            key=_key("rows_json", project, material),
        )
        st.info("The prefilled rows are Synthetic test data, not research measurements.")

    c1, c2 = st.columns(2)
    creator = c1.text_input("Researcher / resolver", key=_key("creator", project, material))
    draft_status = c2.selectbox(
        "Initial review state", ("draft", "needs_review"),
        format_func=lambda value: value.replace("_", " ").title(),
        key=_key("draft_status", project, material),
    )
    if st.button("Process supplied source", type="primary",
                 key=_key("process", project, material)):
        try:
            if source_mode == "Upload long-form CSV":
                if uploaded is None:
                    raise icp_review.IcpReviewError("choose a CSV file first")
                validated = _guard_uploaded_file(uploaded, allowed_extensions={".csv"})
                if validated is None:
                    raise icp_review.IcpReviewError("the uploaded CSV did not pass validation")
                source_bytes = validated.data
                rows, headings = _parse_csv(source_bytes)
                state.update({"rows": rows, "source_bytes": source_bytes,
                              "source_filename": uploaded.name,
                              "source_column_headings": headings})
            else:
                state.update({"rows": _parse_rows_json(rows_text), "source_bytes": None,
                              "source_filename": None, "source_column_headings": []})
            state["corrections"] = []
            state["duplicate_selections"] = []
            state = _rebuild(project, material, state, creator=creator, status=draft_status)
            _save_state(project, material, state)
            st.success("Source processed. Review QC states and explicit resolutions below.")
        except (json.JSONDecodeError, icp_review.IcpReviewError,
                icp_processor.QcResolutionError) as exc:
            st.error(f"ICP source refused: {exc}")

    prepared = state.get("prepared")
    if not prepared:
        return
    _display_processed(prepared)

    with st.expander("Correct reviewable metadata", expanded=False):
        st.caption(
            "Only metadata the authoritative processor marked missing or invalid may be "
            "corrected. Already-valid sample, analyte, role, unit, and dilution identities "
            "cannot be changed. Original values remain stored."
        )
        rows = list((prepared.get("payload") or {}).get("processed", {}).get("corrected") or [])
        fields_by_row = {
            str(row.get("row_id")): _reviewable_correction_fields(row)
            for row in rows if row.get("row_id") and _reviewable_correction_fields(row)
        }
        row_ids = list(fields_by_row)
        if row_ids:
            a, b = st.columns(2)
            row_id = a.selectbox("Row ID", row_ids, key=_key("correction_row", project, material))
            field = b.selectbox(
                "Reviewable field", fields_by_row[row_id],
                key=_key("correction_field", project, material))
            replacement = a.text_input("Replacement value", key=_key("correction_value", project, material))
            resolver = b.text_input("Resolved by", key=_key("correction_resolver", project, material))
            correction_reason = st.text_input("Resolution reason", key=_key("correction_reason", project, material))
            if st.button("Apply metadata correction", key=_key("correction_apply", project, material)):
                try:
                    state["corrections"] = [*(state.get("corrections") or []), {
                        "row_id": row_id,
                        "field": field,
                        "replacement_value": replacement,
                        "resolved_by": resolver,
                        "reason": correction_reason,
                    }]
                    state = _rebuild(
                        project, material, state,
                        creator=prepared.get("creator") or creator,
                        status=prepared.get("status")
                        if prepared.get("status") in {"draft", "needs_review"} else "draft",
                    )
                    _save_state(project, material, state)
                    st.success("Correction recorded with original and replacement provenance.")
                    st.rerun()
                except (icp_review.IcpReviewError, icp_processor.QcResolutionError) as exc:
                    st.error(str(exc))
        else:
            st.info("No missing or invalid correctable metadata is present in this review.")

    groups = list((prepared.get("payload") or {}).get("duplicate_candidate_sets") or [])
    if groups:
        with st.expander("Choose among duplicate candidates", expanded=False):
            labels = [f"{item['sample_id']} · {item['element']} · {item['role']}" for item in groups]
            index = st.selectbox("Candidate set", range(len(groups)),
                                 format_func=lambda value: labels[value],
                                 key=_key("duplicate_group", project, material))
            group = groups[index]
            selected = st.selectbox("Selected row ID", group["candidate_row_ids"],
                                    key=_key("duplicate_selected", project, material))
            a, b = st.columns(2)
            resolver = a.text_input("Resolved by", key=_key("duplicate_resolver", project, material))
            selection_reason = b.text_input("Selection reason", key=_key("duplicate_reason", project, material))
            st.dataframe(pd.DataFrame(group.get("candidates") or []),
                         use_container_width=True, hide_index=True)
            if st.button("Apply duplicate choice", key=_key("duplicate_apply", project, material)):
                try:
                    key_fields = (group["sample_id"], group["element"], group["role"])
                    retained = [item for item in state.get("duplicate_selections") or []
                                if (item.get("sample_id"), item.get("element"), item.get("role"))
                                != key_fields]
                    state["duplicate_selections"] = [*retained, {
                        "sample_id": group["sample_id"], "element": group["element"],
                        "role": group["role"], "selected_row_id": selected,
                        "resolved_by": resolver, "reason": selection_reason,
                    }]
                    state = _rebuild(
                        project, material, state,
                        creator=prepared.get("creator") or creator,
                        status=prepared.get("status")
                        if prepared.get("status") in {"draft", "needs_review"} else "draft",
                    )
                    _save_state(project, material, state)
                    st.success("Duplicate choice recorded with all candidate identities.")
                    st.rerun()
                except icp_review.IcpReviewError as exc:
                    st.error(str(exc))


def _record_for_results(store, context, project, material, state: dict):
    artifact_id = state.get("artifact_id")
    if not artifact_id and context.get("active_run_id"):
        try:
            run = store.get_run(context["active_run_id"])
            if run.machine_id == icp_review.ICP_MACHINE_ID:
                artifact_id = run.result_data.get("artifact_id")
        except workspace_store.WorkspaceStoreError:
            artifact_id = None
    if artifact_id:
        return icp_review.load_icp_review(
            store, artifact_id, project_id=project.project_id, material_id=material.material_id)
    return None


def _verified_validation_output(record, *, source_bytes: bytes | None = None) -> dict:
    """Verify the external source and return the backend-authorized validation view."""
    identity_kind = record.source_identity.get("identity_kind")
    if identity_kind == "supplied_bytes":
        if source_bytes is None:
            raise icp_review.IcpSourceMismatchError(
                "re-upload the exact source CSV before this file-backed review can be reused")
        rows, _headings = _parse_csv(source_bytes)
        return icp_review.finalized_validation_output(
            record, rows=rows, source_bytes=source_bytes)
    return icp_review.finalized_validation_output(
        record, rows=record.payload.get("original_rows") or [])


def render_results(store, context) -> bool:
    project, material = _records(store, context)
    if not project or not material:
        st.warning("Select an active project and material.")
        return True
    state = _current_state(project, material)
    record = _record_for_results(store, context, project, material, state)
    dirty = bool(state.get("dirty"))
    prepared = (state.get("prepared") if dirty else
                _prepared_from_record(record) if record is not None else
                state.get("prepared"))
    if not prepared:
        return False

    app_ui.render_epistemic_badge("advisory_interpretation")
    if dirty:
        st.markdown("**Review state:** Unsaved prepared revision")
        st.warning(
            "These exact processed rows contain unsaved changes. Save them before "
            "finalization; the last saved artifact is not being shown as the current result."
        )
    else:
        status = record.status if record is not None else prepared.get("status", "unsaved")
        st.markdown(f"**Review state:** {str(status).replace('_', ' ').title()}")
    _display_processed(prepared)
    processed = (prepared.get("payload") or {}).get("processed") or {}
    if processed.get("warnings"):
        st.warning(str(processed["warnings"][0]))
    with st.expander("All QC warnings and resolution provenance", expanded=False):
        for warning in processed.get("warnings") or []:
            st.markdown(f"- {warning}")
        st.json({
            "corrections": (prepared.get("payload") or {}).get("corrections") or [],
            "duplicate_selections": (prepared.get("payload") or {}).get("duplicate_selections") or [],
        })
    with st.expander("Technical source and output identity", expanded=False):
        st.json({
            "artifact_id": None if dirty else getattr(record, "artifact_id", None),
            "revision": None if dirty else getattr(record, "revision", None),
            "unsaved_prepared_revision": dirty,
            "based_on_artifact_id": getattr(record, "artifact_id", None) if dirty else None,
            "based_on_revision": getattr(record, "revision", None) if dirty else None,
            "source_identity": prepared.get("source_identity"),
            "processed_output_hash": (prepared.get("payload") or {}).get("processed_output_hash"),
            "qc_contract_version": (prepared.get("payload") or {}).get("qc_contract_version"),
        })

    corrected = list(processed.get("corrected") or [])
    if corrected:
        st.download_button(
            "Download processed QC CSV",
            safe_csv_bytes(corrected),
            "processed_icp_qc.csv",
            "text/csv",
            key=_key("download", project, material),
        )

    if record is None or dirty:
        a, b = st.columns(2)
        actor = a.text_input("Save as researcher", key=_key("save_actor", project, material))
        reason = b.text_input("Save reason", key=_key("save_reason", project, material))
        if record is None:
            label = "Save review draft"
        elif record.status in {"finalized", "rejected", "superseded"}:
            label = "Save as a new linked revision"
        else:
            label = "Save prepared changes"
        if st.button(label, key=_key("save", project, material)):
            try:
                if not actor.strip() or not reason.strip():
                    raise icp_review.IcpReviewError(
                        "researcher and save reason are required")
                prepared_to_save = dict(state.get("prepared") or prepared)
                prepared_to_save["creator"] = actor
                prepared_to_save["reason"] = reason
                saved = icp_review.save_icp_review(
                    store, prepared_to_save,
                    artifact_id=getattr(record, "artifact_id", None),
                )
                store.link_artifact_to_material(saved.artifact_id)
                state = _load_state(saved)
                _save_state(project, material, state)
                st.success(f"Saved {saved.artifact_id} revision {saved.revision}.")
                st.rerun()
            except (workspace_store.WorkspaceStoreError, icp_review.IcpReviewError) as exc:
                st.error(str(exc))

    if dirty:
        st.info("Finalization is blocked until this unsaved prepared revision is saved.")
    elif record is not None and record.status in {"draft", "needs_review"}:
        st.warning("Finalization is immutable and requires the exact current source plus explicit confirmation.")
        identity_kind = record.source_identity.get("identity_kind")
        source_bytes = None
        source_rows = record.payload.get("original_rows") or []
        source_verified = identity_kind != "supplied_bytes"
        if identity_kind == "supplied_bytes":
            replacement = st.file_uploader(
                "Re-upload the exact source CSV to finalize",
                type=["csv"], key=_key("final_source", project, material))
            if replacement is not None:
                validated = _guard_uploaded_file(replacement, allowed_extensions={".csv"})
                source_bytes = validated.data if validated is not None else None
                try:
                    if source_bytes is None:
                        raise icp_review.IcpReviewError(
                            "the re-uploaded CSV did not pass validation")
                    source_rows, _headings = _parse_csv(source_bytes)
                    icp_review.assert_icp_source_matches(
                        record, rows=source_rows, source_bytes=source_bytes)
                    source_verified = True
                    st.success("Exact source bytes and parsed rows match this saved review.")
                except icp_review.IcpReviewError as exc:
                    st.error(f"Finalization source refused: {exc}")
                    source_rows = []
                    source_verified = False
        confirmation = st.text_input(
            f"Type {record.artifact_id} to confirm", key=_key("confirmation", project, material))
        a, b = st.columns(2)
        reviewer = a.text_input("Finalized by", key=_key("finalizer", project, material))
        reason = b.text_input("Finalization reason", key=_key("final_reason", project, material))
        if st.button("Finalize immutable ICP review", type="primary",
                     key=_key("finalize", project, material),
                     disabled=not source_verified):
            try:
                final = icp_review.finalize_icp_review(
                    store,
                    record.artifact_id,
                    confirmation=confirmation,
                    finalized_by=reviewer,
                    reason=reason,
                    rows=source_rows,
                    source_bytes=source_bytes,
                )
                store.link_artifact_to_material(final.artifact.artifact_id)
                store.set_active_context(
                    project.project_id, material.material_id, final.run.run_id)
                _save_state(project, material, _load_state(final.artifact))
                st.success("ICP review finalized and linked to an immutable RunRecord.")
                st.rerun()
            except (workspace_store.WorkspaceStoreError, icp_review.IcpReviewError) as exc:
                st.error(str(exc))
    elif record is not None and record.status == "finalized":
        st.success("This finalized review is immutable. Load it under Prepare and change the source or a supported resolution to create a linked revision.")
    return True


def render_validation_gate(store, context) -> None:
    """Show only finalized, source-verifiable ICP evidence at the validation boundary."""
    project, material = _records(store, context)
    if not project or not material:
        return
    finalized = [record for record in icp_review.list_icp_reviews(
        store, project_id=project.project_id, material_id=material.material_id)
                 if record.status == "finalized"]
    st.markdown("**Finalized ICP review gate**")
    if not finalized:
        st.caption("No finalized ICP review is linked to this material.")
        return
    for record in reversed(finalized):
        with st.container(border=True):
            st.markdown(f"**Revision {record.revision}** · {record.artifact_id}")
            source_bytes = None
            if record.source_identity.get("identity_kind") == "supplied_bytes":
                st.caption(
                    "Re-upload the source CSV. Both its exact bytes and its parsed rows must "
                    "match the finalized artifact; the file is verified in memory and not stored."
                )
                supplied = st.file_uploader(
                    f"Exact source CSV for {record.artifact_id}", type=["csv"],
                    key=_key(f"validation_source_{record.artifact_id}", project, material),
                )
                if supplied is None:
                    st.warning("Validation reuse is blocked until the exact source CSV is supplied.")
                    continue
                validated = _guard_uploaded_file(supplied, allowed_extensions={".csv"})
                if validated is None:
                    st.warning("Validation reuse is blocked because the supplied CSV was refused.")
                    continue
                source_bytes = validated.data
            try:
                gated = _verified_validation_output(record, source_bytes=source_bytes)
            except icp_review.IcpReviewError as exc:
                st.error(f"Validation gate refused this review: {exc}")
                continue
            eligible_rows = [row for row in gated.get("eligible_rows") or []
                             if row.get("validation_eligible") is True]
            residuals = [row for row in gated.get("residuals") or []
                         if row.get("validation_eligible") is True]
            st.success("Validation-authorized processor output · exact source verified")
            app_ui.render_metric_cards([
                {"label": "QC-eligible rows", "value": len(eligible_rows),
                 "status": "success"},
                {"label": "Not eligible", "value": gated["excluded_row_count"],
                 "status": "warning"},
            ])
            st.caption("Eligibility is taken directly from the Phase 1B processor; no UI override exists.")
            st.caption(
                f"Artifact {gated['artifact_id']} · revision {gated['revision']} · "
                f"source SHA-256 {gated['source_identity'].get('sha256')}"
            )
            if eligible_rows:
                st.markdown("**Processor-eligible rows**")
                st.dataframe(pd.DataFrame(eligible_rows), use_container_width=True, hide_index=True)
                st.download_button(
                    "Download eligible ICP rows CSV", safe_csv_bytes(eligible_rows),
                    f"{record.artifact_id}_eligible_rows.csv", "text/csv",
                    key=_key(f"validation_rows_download_{record.artifact_id}", project, material),
                )
            if residuals:
                st.markdown("**Processor-authorized residuals**")
                st.dataframe(pd.DataFrame(residuals), use_container_width=True, hide_index=True)
                st.download_button(
                    "Download authorized residuals CSV", safe_csv_bytes(residuals),
                    f"{record.artifact_id}_residuals.csv", "text/csv",
                    key=_key(f"validation_residuals_download_{record.artifact_id}", project, material),
                )
            with st.expander("Exact validation identity", expanded=False):
                st.json({
                    "artifact_id": record.artifact_id,
                    "revision": record.revision,
                    "source_identity": gated["source_identity"],
                    "processed_output_hash": gated["processed_output_hash"],
                    "qc_contract_version": gated["qc_contract_version"],
                })


__all__ = [
    "render_prepare", "render_results", "render_validation_gate",
    "_parse_csv", "_verified_validation_output",
]
