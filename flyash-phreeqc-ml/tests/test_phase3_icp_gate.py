"""Phase 3 durable ICP operational-gate, recovery, and export regressions.

Every concentration and identity in this module is synthetic test-only data.
"""
from __future__ import annotations

import csv
import html
from io import StringIO
from pathlib import Path

import pytest

from flyash_phreeqc_ml import config, workspace_store
from flyash_phreeqc_ml.instruments import icp_review
from flyash_phreeqc_ml.instruments.phase3_icp_export import (
    IcpCsvExportError,
    safe_csv_text,
)
from ui import phase3_icp


AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP = Path(__file__).resolve().parents[1] / "app.py"
SYNTHETIC_CSV = (
    b"row_id,sample_id,element,concentration,unit,dilution_factor,"
    b"measured_or_predicted\n"
    b"measured,SYN-1,Ca,2.0,mM,1.0,measured\n"
    b"predicted,SYN-1,Ca,1.0,mM,1.0,predicted\n"
)


def _context(tmp_path):
    store = workspace_store.WorkspaceStore(tmp_path / "synthetic-icp-gate")
    project = store.create_project("Synthetic ICP gate test")
    material = store.create_material(project.project_id, "Synthetic ICP material")
    store.set_active_context(project.project_id, material.material_id, None)
    return store, project, material


def _prepared(project, material, source_bytes=SYNTHETIC_CSV):
    rows, headings = phase3_icp._parse_csv(source_bytes)
    prepared = icp_review.prepare_icp_review(
        project_id=project.project_id,
        material_id=material.material_id,
        rows=rows,
        creator="Synthetic test researcher",
        source_bytes=source_bytes,
        source_filename="synthetic-icp.csv",
        provenance={"fixture_status": "synthetic test only",
                    "source_column_headings": headings},
    )
    return prepared, rows, headings


def _text(at) -> str:
    values = []
    for kind in (
        "caption", "code", "error", "header", "info", "json", "markdown",
        "subheader", "success", "text", "title", "warning",
    ):
        values.extend(str(item.value) for item in getattr(at, kind, ()))
    return html.unescape(" ".join(values))


def test_safe_icp_csv_neutralizes_formula_text_and_preserves_negative_numbers():
    exported = safe_csv_text([{
        "sample_id": "=HYPERLINK(\"https://example.invalid\")",
        "resolver": "+cmd|' /C calc'!A0",
        "note": "  @unsafe",
        "negative_number": -1.25,
        "negative_numeric_text": "-2.50",
        "negative_exponent_text": "-3e-4",
    }])
    row = next(csv.DictReader(StringIO(exported)))
    assert row["sample_id"].startswith("'=")
    assert row["resolver"].startswith("'+")
    assert row["note"].startswith("'  @")
    assert row["negative_number"] == "-1.25"
    assert row["negative_numeric_text"] == "-2.50"
    assert row["negative_exponent_text"] == "-3e-4"
    with pytest.raises(IcpCsvExportError, match="finite"):
        safe_csv_text([{"value": float("nan")}])


def test_correction_ui_surface_exposes_only_processor_flagged_metadata_fields():
    valid = {
        "row_id": "valid", "qc_codes": [], "sample_id": "S1", "element": "Ca",
        "role": "measured", "input_unit": "mM", "dilution_factor": 1.0,
    }
    reviewable = {
        "row_id": "reviewable",
        "qc_codes": [
            "duplicate_measured",
            phase3_icp.icp_processor.QC_UNKNOWN_UNIT,
            phase3_icp.icp_processor.QC_MISSING_DILUTION,
        ],
    }
    assert phase3_icp._reviewable_correction_fields(valid) == ()
    assert phase3_icp._reviewable_correction_fields(reviewable) == (
        "unit", "dilution_factor")


def test_file_backed_gate_reparses_exact_bytes_and_refuses_same_rows_different_file(tmp_path):
    store, project, material = _context(tmp_path)
    prepared, rows, _headings = _prepared(project, material)
    draft = icp_review.save_icp_review(store, prepared)
    final = icp_review.finalize_icp_review(
        store, draft.artifact_id,
        confirmation=draft.artifact_id,
        finalized_by="Synthetic test reviewer",
        reason="Synthetic exact-file review completed",
        rows=rows,
        source_bytes=SYNTHETIC_CSV,
        create_run=False,
    ).artifact

    gated = phase3_icp._verified_validation_output(
        final, source_bytes=SYNTHETIC_CSV)
    assert gated["artifact_id"] == final.artifact_id
    assert gated["source_identity"] == final.source_identity
    assert {row["row_id"] for row in gated["eligible_rows"]} == {
        "measured", "predicted"}
    assert all(row["validation_eligible"] is True for row in gated["residuals"])

    # The parser yields the same rows, but the exact file identity differs.
    same_rows_different_bytes = SYNTHETIC_CSV + b"\n"
    reparsed, _ = phase3_icp._parse_csv(same_rows_different_bytes)
    assert reparsed == rows
    with pytest.raises(icp_review.IcpSourceMismatchError, match="bytes do not match"):
        phase3_icp._verified_validation_output(
            final, source_bytes=same_rows_different_bytes)


def test_interrupted_finalization_retry_reuses_one_exact_run(tmp_path, monkeypatch):
    store, project, material = _context(tmp_path)
    prepared, rows, _headings = _prepared(project, material)
    draft = icp_review.save_icp_review(store, prepared)
    original_update = store.update_artifact
    calls = {"count": 0}

    def fail_artifact_transition_once(*args, **kwargs):
        if kwargs.get("status") == "finalized" and calls["count"] == 0:
            calls["count"] += 1
            raise workspace_store.WorkspaceStoreError(
                "synthetic failure after durable run creation")
        return original_update(*args, **kwargs)

    monkeypatch.setattr(store, "update_artifact", fail_artifact_transition_once)
    with pytest.raises(workspace_store.WorkspaceStoreError, match="synthetic failure"):
        icp_review.finalize_icp_review(
            store, draft.artifact_id,
            confirmation=draft.artifact_id,
            finalized_by="Synthetic test reviewer",
            reason="Synthetic retryable finalization",
            rows=rows,
            source_bytes=SYNTHETIC_CSV,
        )
    orphan_safe_runs = store.list_runs(
        project_id=project.project_id, material_id=material.material_id,
        machine_id=icp_review.ICP_MACHINE_ID)
    assert len(orphan_safe_runs) == 1
    assert store.get_artifact(draft.artifact_id).status == "draft"

    # A fresh store simulates process recovery.  The deterministic exact run is
    # reused and linked; it is neither deleted nor duplicated.
    restarted = workspace_store.WorkspaceStore(store.root)
    recovered = icp_review.finalize_icp_review(
        restarted, draft.artifact_id,
        confirmation=draft.artifact_id,
        finalized_by="Synthetic test reviewer",
        reason="Synthetic retryable finalization",
        rows=rows,
        source_bytes=SYNTHETIC_CSV,
    )
    assert recovered.run.run_id == orphan_safe_runs[0].run_id
    assert recovered.artifact.related_run_id == orphan_safe_runs[0].run_id
    assert len(restarted.list_runs(
        project_id=project.project_id, material_id=material.material_id,
        machine_id=icp_review.ICP_MACHINE_ID)) == 1

    # A response-loss retry after success is idempotent as well.
    repeated = icp_review.finalize_icp_review(
        workspace_store.WorkspaceStore(store.root), draft.artifact_id,
        confirmation=draft.artifact_id,
        finalized_by="Synthetic test reviewer",
        reason="Synthetic retryable finalization",
        rows=rows,
        source_bytes=SYNTHETIC_CSV,
    )
    assert repeated.run.run_id == recovered.run.run_id
    assert len(workspace_store.WorkspaceStore(store.root).list_runs(
        project_id=project.project_id, material_id=material.material_id,
        machine_id=icp_review.ICP_MACHINE_ID)) == 1


def test_post_write_finalization_error_recovers_exact_state_without_second_run(
        tmp_path, monkeypatch):
    store, project, material = _context(tmp_path)
    prepared, rows, _headings = _prepared(project, material)
    draft = icp_review.save_icp_review(store, prepared)
    original_update = store.update_artifact
    injected = {"raised": False}

    def persist_then_report_io_error(*args, **kwargs):
        record = original_update(*args, **kwargs)
        if kwargs.get("status") == "finalized" and not injected["raised"]:
            injected["raised"] = True
            raise workspace_store.WorkspaceStoreError(
                "synthetic response loss after artifact write")
        return record

    monkeypatch.setattr(store, "update_artifact", persist_then_report_io_error)
    recovered_in_call = icp_review.finalize_icp_review(
        store, draft.artifact_id,
        confirmation=draft.artifact_id,
        finalized_by="Synthetic test reviewer",
        reason="Synthetic post-write recovery",
        rows=rows,
        source_bytes=SYNTHETIC_CSV,
    )
    assert injected["raised"] is True
    assert recovered_in_call.artifact.status == "finalized"
    assert recovered_in_call.artifact.related_run_id == recovered_in_call.run.run_id
    assert len(store.list_runs(
        project_id=project.project_id, material_id=material.material_id,
        machine_id=icp_review.ICP_MACHINE_ID)) == 1


def test_finalization_never_reuses_saved_rows_as_unverified_current_source(tmp_path):
    store, project, material = _context(tmp_path)
    prepared, _rows, _headings = _prepared(project, material)
    draft = icp_review.save_icp_review(store, prepared)
    with pytest.raises(icp_review.IcpSourceMismatchError, match="source bytes are required"):
        icp_review.finalize_icp_review(
            store, draft.artifact_id,
            confirmation=draft.artifact_id,
            finalized_by="Synthetic test reviewer",
            reason="Must supply current exact source",
        )
    assert store.get_artifact(draft.artifact_id).status == "draft"
    assert store.list_runs(
        project_id=project.project_id, material_id=material.material_id,
        machine_id=icp_review.ICP_MACHINE_ID) == []


def test_dirty_result_save_restart_file_finalize_and_operational_gate_apptest(
        tmp_path, monkeypatch):
    store, project, material = _context(tmp_path)
    prepared, _rows, _headings = _prepared(project, material)
    draft = icp_review.save_icp_review(store, prepared)
    changed_csv = SYNTHETIC_CSV.replace(b",2.0,", b",3.0,")
    changed_prepared, changed_rows, changed_headings = _prepared(
        project, material, changed_csv)
    state_key = phase3_icp._state_key(project, material)
    result_script = f"""
from flyash_phreeqc_ml.workspace_store import WorkspaceStore
from ui import phase3_icp
store = WorkspaceStore(r"{store.root}")
phase3_icp.render_results(store, store.get_active_context())
"""
    at = AppTest.from_string(result_script, default_timeout=60)
    at.session_state[state_key] = {
        "artifact_id": draft.artifact_id,
        "prepared": changed_prepared,
        "rows": changed_rows,
        "source_bytes": changed_csv,
        "source_filename": "synthetic-icp.csv",
        "source_column_headings": changed_headings,
        "corrections": [],
        "duplicate_selections": [],
        "dirty": True,
    }
    at.run()
    assert not list(at.exception)
    assert "Unsaved prepared revision" in _text(at)
    assert "Finalization is blocked" in _text(at)
    assert not any(button.label == "Finalize immutable ICP review" for button in at.button)
    concise = at.dataframe[0].value
    assert "3.0 mM" in set(concise["Supplied reading"])
    assert "2.0 mM" not in set(concise["Supplied reading"])

    next(item for item in at.text_input if item.label == "Save as researcher").set_value(
        "Synthetic test researcher")
    next(item for item in at.text_input if item.label == "Save reason").set_value(
        "Save exact dirty prepared state")
    at.run()
    next(button for button in at.button if button.label == "Save prepared changes").click().run()
    assert not list(at.exception)
    saved = workspace_store.WorkspaceStore(store.root).get_artifact(draft.artifact_id)
    assert saved.status == "draft"
    assert saved.source_identity["sha256"] == icp_review.source_identity(
        changed_rows, source_bytes=changed_csv)["sha256"]

    class ExactUpload:
        name = "synthetic-icp.csv"

        @staticmethod
        def getvalue():
            return changed_csv

    monkeypatch.setattr(
        phase3_icp.st,
        "file_uploader",
        lambda label, *args, **kwargs: ExactUpload()
        if "exact source" in label.lower() else None,
    )

    # A fresh AppTest and fresh WorkspaceStore model a restart from the saved draft.
    restarted = AppTest.from_string(result_script, default_timeout=60)
    restarted.session_state[state_key] = phase3_icp._load_state(saved)
    restarted.run()
    assert not list(restarted.exception)
    assert "Exact source bytes and parsed rows match" in _text(restarted)
    next(item for item in restarted.text_input
         if item.label.startswith("Type art_")).set_value(saved.artifact_id)
    next(item for item in restarted.text_input if item.label == "Finalized by").set_value(
        "Synthetic test reviewer")
    next(item for item in restarted.text_input
         if item.label == "Finalization reason").set_value("Synthetic durable review complete")
    restarted.run()
    next(button for button in restarted.button
         if button.label == "Finalize immutable ICP review").click().run()
    assert not list(restarted.exception)
    final = workspace_store.WorkspaceStore(store.root).get_artifact(saved.artifact_id)
    assert final.status == "finalized"
    assert final.related_run_id
    assert len(workspace_store.WorkspaceStore(store.root).list_runs(
        project_id=project.project_id, material_id=material.material_id,
        machine_id=icp_review.ICP_MACHINE_ID)) == 1

    gate_script = f"""
from flyash_phreeqc_ml.workspace_store import WorkspaceStore
from ui import phase3_icp
store = WorkspaceStore(r"{store.root}")
phase3_icp.render_validation_gate(store, store.get_active_context())
"""
    gate = AppTest.from_string(gate_script, default_timeout=60).run()
    assert not list(gate.exception)
    rendered = _text(gate)
    assert "Validation-authorized processor output" in rendered
    assert final.artifact_id in rendered
    assert final.source_identity["sha256"] in rendered
    assert len(gate.dataframe) == 2
    assert all(gate.dataframe[0].value["validation_eligible"])
    assert all(gate.dataframe[1].value["validation_eligible"])


def test_validation_page_separates_operational_gate_from_legacy_workflows(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VIRTUAL_LAB_WORKSPACE_DIR", tmp_path / "app-workspace")
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    monkeypatch.setattr(config, "PROCESSED_DIR", tmp_path / "processed")
    (tmp_path / "processed").mkdir()
    store = workspace_store.WorkspaceStore()
    project = store.create_project("Synthetic validation page")
    material = store.create_material(project.project_id, "Synthetic validation material")
    store.set_active_context(project.project_id, material.material_id, None)

    at = AppTest.from_file(APP, default_timeout=180)
    at.session_state["nav_page"] = "Validation & Uncertainty"
    at.run()
    assert not list(at.exception)
    rendered = _text(at)
    assert "Operational durable ICP validation" in rendered
    assert "Legacy validation workflows" in rendered
    assert "They do not finalize a Phase 3 ICP review" in rendered
    assert [tab.label for tab in at.tabs][:5] == [
        "Overview", "Import", "Validate", "Match", "Compare"]
