"""Phase 3 product-shell and durable-reopen AppTests.

These tests stay at the UI integration boundary.  Domain behavior is covered by
the focused Phase 3 backend and journey suites; here we pin that every typed
workflow is reachable through the canonical shell and that saved records reopen
through a fresh Streamlit script session.
"""
from __future__ import annotations

import html
from pathlib import Path

import pytest

from flyash_phreeqc_ml import config, phase3_artifacts, workspace_store
from flyash_phreeqc_ml.instruments import virtual_lab_machine_runner as runner
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines
from flyash_phreeqc_ml.literature import evidence_review
from flyash_phreeqc_ml.literature import evidence_schema as E


AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"
PAGES = (
    "Home",
    "Projects",
    "Material Workspace",
    "Machines",
    "Results",
    "Validation & Uncertainty",
    "Evidence",
    "Run History",
    "Settings & Diagnostics",
)
PHASE3_WORKSPACES = (
    (machines.ICP_PROCESSOR, "Review supplied ICP rows"),
    (machines.XRD_ADVISORY, "Import a measured pattern"),
    (machines.EXPERIMENTAL_DESIGN, "Build an advisory plan"),
    (machines.SUSTAINABILITY, "Prepare a transparent screening"),
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VIRTUAL_LAB_WORKSPACE_DIR", tmp_path / "workspace")
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    monkeypatch.setattr(config, "PROCESSED_DIR", tmp_path / "processed")
    (tmp_path / "processed").mkdir()


def _context(name: str = "Synthetic Phase 3 AppTest"):
    store = workspace_store.WorkspaceStore()
    project = store.create_project(name, description="synthetic test-only fixture")
    material = store.create_material(
        project.project_id,
        "Synthetic Phase 3 material",
        material_type="synthetic test fixture",
        composition=[{"component": "CaO", "value": 1.0, "unit": "wt%"}],
        composition_provenance={"source_type": "synthetic_demo"},
        measurement_references=["synthetic-test-only-measurement"],
    )
    store.set_active_context(project.project_id, material.material_id, None)
    return store, project, material


def _nav_radio(at):
    matches = [item for item in at.radio if getattr(item, "key", None) == "nav_page"]
    assert len(matches) == 1
    return matches[0]


def _goto(at, page: str):
    _nav_radio(at).set_value(page).run()
    return at


def _assert_clean(at) -> None:
    assert at.exception is None or len(at.exception) == 0


def _text(at) -> str:
    values = []
    for kind in (
        "caption", "code", "error", "header", "info", "json", "markdown",
        "subheader", "success", "text", "title", "warning",
    ):
        values.extend(str(item.value) for item in getattr(at, kind, ()))
    return html.unescape(" ".join(values))


def _assert_unique_widget_keys(at) -> None:
    """Pin explicit widget keys in main and sidebar, in addition to Streamlit's check."""
    keys = []
    for root in (at.main, at.sidebar):
        for node in root:
            key = getattr(node, "key", None)
            if key is not None:
                keys.append(str(key))
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    assert duplicates == []


def _open_workspace(machine_id: str):
    at = AppTest.from_file(APP, default_timeout=180)
    at.session_state["nav_page"] = "Machines"
    at.session_state["active_machine_id"] = machine_id
    at.run()
    return at


def _saved_plan(store, project, material):
    payload = {
        "mode": "generic_user_defined",
        "material_id": material.material_id,
        "goal": "Synthetic product integration plan",
        "factors": {"temperature_C": [20, 40]},
        "fixed_conditions": {"synthetic_condition": "held"},
        "replicates": 1,
        "max_run_count": 4,
        "controls": [],
        "sample_prefix": "SYN",
        "experiment_date": "2026-08-29",
    }
    result = runner.run_virtual_lab_machine(machines.EXPERIMENTAL_DESIGN, payload)
    saved = phase3_artifacts.save_advisory_artifact_run(
        store,
        project_id=project.project_id,
        material_id=material.material_id,
        record_type=workspace_store.ARTIFACT_EXPERIMENT_PLAN,
        machine_id=machines.EXPERIMENTAL_DESIGN,
        input_snapshot=payload,
        payload=result.results,
        source_identity={
            "source_type": "synthetic_test_form",
            "source_sha256": workspace_store.identity_hash(payload),
        },
        creator="Synthetic AppTest researcher",
        reason="Synthetic durable reopen fixture",
        warnings=result.warnings,
        provenance=result.provenance,
    )
    store.set_active_context(project.project_id, material.material_id, None)
    return saved


def _evidence_payload() -> dict:
    return {
        "schema_kind": E.SCHEMA_LEACHING,
        "provenance": {
            "source": "synthetic manual fixture",
            "title": "Synthetic Phase 3 evidence",
            "authors": ["Synthetic Author"],
            "year": 2026,
            "url": "https://example.invalid/synthetic-evidence",
            "discovery_route": {
                "method": "manual",
                "source": "synthetic manual fixture",
                "executed_queries": [],
                "record_identifier": "SYN-APPTEST-EV-1",
            },
        },
        "source_location": {
            "page": "7",
            "table": "Table S1",
            "section": "Synthetic methods",
            "note": "Synthetic test location",
        },
        "topic": "synthetic evidence topic",
        "claim": "synthetic reported claim",
        "reported_values": {"factor": 3.0},
        "units": {"factor": "synthetic unit"},
        "conditions": {"boundary": "synthetic boundary"},
        "extraction_scope": E.SCOPE_MANUAL,
        "extraction_status": E.STATUS_MANUAL,
        "extraction_confidence": 0.8,
        "field_confidence": {"factor": 0.9},
        "conflicts": [],
        "notes": "Structured synthetic note only",
    }


def _reviewed_evidence_run(store, project, material):
    record = evidence_review.create_manual_evidence(
        store,
        project.project_id,
        material.material_id,
        _evidence_payload(),
        creator="Synthetic AppTest researcher",
    )
    evidence_review.link_material(store, record.artifact_id)
    evidence_review.submit_for_review(
        store, record.artifact_id, submitter="Synthetic AppTest submitter")
    reviewed = evidence_review.mark_reviewed(
        store,
        record.artifact_id,
        reviewer="Synthetic AppTest reviewer",
        reason="Synthetic source location checked",
    )
    run = phase3_artifacts.save_reviewed_evidence_run(
        store, reviewed.artifact_id, machine_id=machines.LITERATURE_ENGINE)
    store.set_active_context(project.project_id, material.material_id, None)
    return reviewed, run


def test_all_primary_pages_render_sequentially_with_unique_widget_keys():
    _context()
    at = AppTest.from_file(APP, default_timeout=180).run()
    assert tuple(_nav_radio(at).options) == PAGES
    for page in PAGES:
        _goto(at, page)
        _assert_clean(at)
        _assert_unique_widget_keys(at)
        assert _nav_radio(at).value == page


@pytest.mark.parametrize(("machine_id", "expected_text"), PHASE3_WORKSPACES)
def test_each_phase3_machine_uses_the_typed_four_area_workspace(machine_id, expected_text):
    _context()
    at = _open_workspace(machine_id)
    _assert_clean(at)
    _assert_unique_widget_keys(at)
    labels = [tab.label for tab in at.tabs]
    outer_indices = [labels.index(label) for label in (
        "Overview", "Prepare", "Results", "History")]
    assert outer_indices == sorted(outer_indices)
    assert expected_text in _text(at)
    assert not any(
        item.key == f"machine_payload_editor__{machine_id}" for item in at.text_area)


def test_manual_evidence_browse_open_and_add_surfaces_render_stably():
    store, project, material = _context("Synthetic evidence AppTest")
    record = evidence_review.create_manual_evidence(
        store,
        project.project_id,
        material.material_id,
        _evidence_payload(),
        creator="Synthetic AppTest researcher",
    )
    evidence_review.link_material(store, record.artifact_id)

    at = AppTest.from_file(APP, default_timeout=180)
    at.session_state["nav_page"] = "Evidence"
    at.run()
    _assert_clean(at)
    assert [tab.label for tab in at.tabs][:3] == [
        "Overview", "Manual evidence & review", "Legacy Evidence Library"]
    assert "Synthetic Phase 3 evidence" in _text(at)
    assert "Material: Synthetic Phase 3 material" in _text(at)
    assert "Confidence: 80%" in _text(at)
    next(button for button in at.button
         if button.key.endswith(f"__{record.artifact_id}")
         and button.label == "Open evidence").click().run()
    _assert_clean(at)
    assert "Edit draft" in _text(at)
    assert "Discovery route and exact source location" in {
        item.label for item in at.expander}

    action = next(item for item in at.radio if item.label == "Evidence action")
    action.set_value("Add evidence manually").run()
    _assert_clean(at)
    _assert_unique_widget_keys(at)
    rendered = _text(at)
    assert "Add evidence manually" in rendered
    assert any(item.label == "Source title" for item in at.text_input)
    assert any(item.label == "Save evidence draft" for item in at.button)


@pytest.mark.parametrize(
    ("page", "key_prefix"),
    (("Results", "results_reopen_"), ("Run History", "history_reopen_")),
)
def test_reviewed_evidence_run_reopens_exact_evidence_from_results_and_history(
        page, key_prefix):
    store, project, material = _context(f"Synthetic evidence {page} reopen AppTest")
    reviewed, run = _reviewed_evidence_run(store, project, material)

    at = AppTest.from_file(APP, default_timeout=180)
    at.session_state["nav_page"] = page
    at.run()
    _assert_clean(at)
    reopen = next(
        button for button in at.button
        if str(getattr(button, "key", "")).startswith(key_prefix)
        and run.run_id in str(button.key)
    )
    reopen.click().run()

    _assert_clean(at)
    _assert_unique_widget_keys(at)
    assert _nav_radio(at).value == "Evidence"
    assert workspace_store.WorkspaceStore().get_active_context()["active_run_id"] == run.run_id
    assert at.session_state[
        f"phase3_evidence_selected__{project.project_id}__new"
    ] == reviewed.artifact_id
    rendered = _text(at)
    assert "Synthetic Phase 3 evidence" in rendered
    assert "Create a needs-review revision" in rendered
    assert "No current result" not in rendered


@pytest.mark.parametrize(
    ("page", "key_prefix"),
    (("Results", "results_reopen_"), ("Run History", "history_reopen_")),
)
def test_saved_phase3_run_reopens_exact_workspace_from_results_and_history(page, key_prefix):
    store, project, material = _context(f"Synthetic {page} reopen AppTest")
    saved = _saved_plan(store, project, material)

    at = AppTest.from_file(APP, default_timeout=180)
    at.session_state["nav_page"] = page
    at.run()
    _assert_clean(at)
    reopen = next(
        button for button in at.button
        if str(getattr(button, "key", "")).startswith(key_prefix)
        and saved.run.run_id in str(button.key)
    )
    reopen.click().run()
    _assert_clean(at)
    _assert_unique_widget_keys(at)
    assert _nav_radio(at).value == "Machines"
    assert at.session_state["active_machine_id"] == machines.EXPERIMENTAL_DESIGN
    context = workspace_store.WorkspaceStore().get_active_context()
    assert context["active_run_id"] == saved.run.run_id
    rendered = _text(at)
    assert "Reopened immutable experiment plan" in rendered
    assert "Reopened from Run History; the saved plan is immutable" in rendered
    assert not any("design_save_button" in str(getattr(button, "key", ""))
                   for button in at.button)
