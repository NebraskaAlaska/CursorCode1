"""Phase 2 nine-page shell and legacy-workflow integration AppTests."""
from __future__ import annotations

import html
from pathlib import Path

import pytest

from flyash_phreeqc_ml import config, run_manager, workspace_store
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machine_contract

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"
PAGES = [
    "Home", "Projects", "Material Workspace", "Machines", "Results",
    "Validation & Uncertainty", "Evidence", "Run History", "Settings & Diagnostics",
]
MACHINE_AREAS = ["Overview", "Prepare", "Results", "History"]


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VIRTUAL_LAB_WORKSPACE_DIR", tmp_path / "workspace")
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    monkeypatch.setattr(config, "PROCESSED_DIR", tmp_path / "processed")
    (tmp_path / "processed").mkdir()


def _nav_radio(at):
    matches = [item for item in at.radio if getattr(item, "key", None) == "nav_page"]
    assert len(matches) == 1, "exactly one primary navigation radio is required"
    return matches[0]


def _goto(at, page):
    _nav_radio(at).set_value(page).run()
    return at


def _text(at):
    return html.unescape(" ".join([*(str(item.value) for item in at.markdown),
                                   *(str(item.value) for item in at.caption),
                                   *(str(item.value) for item in at.info),
                                   *(str(item.value) for item in at.json),
                                   *(str(item.value) for item in at.warning)]))


def _visible_main_text(at):
    """Return main-body text while omitting collapsed disclosure contents.

    Streamlit's AppTest exposes children of collapsed expanders in its element
    tree.  The ordinary ``_text`` helper is therefore useful for reachability,
    but cannot establish what the user sees before interaction.
    """
    values = []

    def visit(node):
        if getattr(node, "type", None) == "expander":
            return
        children = getattr(node, "children", None)
        if children is not None:
            for child in children.values():
                visit(child)
            return
        if getattr(node, "type", None) in {
                "caption", "error", "header", "info", "markdown", "subheader",
                "success", "text", "title", "warning"}:
            values.append(str(getattr(node, "value", "")))

    for child in at.main.children.values():
        visit(child)
    return html.unescape(" ".join(values))


def _assert_clean(at):
    assert at.exception is None or len(at.exception) == 0


def _durable_context():
    store = workspace_store.WorkspaceStore()
    project = store.create_project("Synthetic AppTest project", description="test-only")
    material = store.create_material(
        project.project_id, "Synthetic AppTest material", material_type="test fixture",
        composition=[{"component": "CaO", "value": 1.0, "unit": "wt%"}],
        composition_provenance={"source_type": "synthetic_demo"},
        measurement_references=["synthetic-test-only-row-set"],
    )
    run = store.create_run(
        project.project_id, material.material_id, "xrd_advisory",
        {"measured_peaks": [20.0], "fixture": "synthetic_test_only"},
        status="advisory", output_type="advisory_interpretation",
        epistemic_type="advisory_interpretation", result_data={"candidate_checklist": []},
        warnings=["tentative only"], validation_state="not_applicable")
    store.set_active_context(project.project_id, material.material_id, run.run_id)
    return store, project, material, run


def test_empty_app_boots_to_home_with_one_nine_page_navigation():
    at = AppTest.from_file(APP, default_timeout=90).run()
    _assert_clean(at)
    assert _nav_radio(at).value == "Home"
    assert list(_nav_radio(at).options) == PAGES
    assert len([item for item in at.radio if item.key == "nav_page"]) == 1
    rendered = _text(at)
    assert "WPI Virtual LAB" in rendered
    assert "No project selected" in rendered
    assert "PHREEQC unavailable" in rendered
    assert "preview" in rendered.lower()
    assert any(button.key == "home_no_project_action" for button in at.button)
    assert "mock result" not in rendered.lower()


@pytest.mark.parametrize("page", PAGES)
def test_every_primary_page_renders_empty_state(page):
    at = AppTest.from_file(APP, default_timeout=120).run()
    _goto(at, page)
    _assert_clean(at)
    assert _nav_radio(at).value == page


@pytest.mark.parametrize("page", PAGES)
def test_every_primary_page_renders_with_durable_context(page):
    _durable_context()
    run_manager.create_run("synthetic_legacy", "synthetic_demo")
    at = AppTest.from_file(APP, default_timeout=150)
    at.session_state["selected_run"] = "synthetic_legacy"
    at.run()
    _goto(at, page)
    _assert_clean(at)
    rendered = _text(at)
    assert "Synthetic AppTest project" in rendered
    assert "Synthetic AppTest material" in rendered


def test_material_workspace_retains_assistant_phreeqc_and_import_workflows():
    _durable_context()
    at = _goto(AppTest.from_file(APP, default_timeout=150).run(), "Material Workspace")
    _assert_clean(at)
    labels = [item.label for item in at.tabs]
    assert labels[:4] == ["Material record", "Assistant", "PHREEQC planning",
                          "Import measured data"]
    rendered = _text(at)
    assert "Authoritative Phase 1A path" in rendered
    assert "What can I help with?" in rendered


def test_validation_page_opens_with_overview_before_technical_workflows():
    at = _goto(AppTest.from_file(APP, default_timeout=150).run(),
               "Validation & Uncertainty")
    _assert_clean(at)
    assert [item.label for item in at.tabs][:5] == [
        "Overview", "Import", "Validate", "Match", "Compare"]
    assert "Validation requires measured evidence" in _text(at)


def test_projects_page_preserves_legacy_run_selection_and_creation():
    at = _goto(AppTest.from_file(APP, default_timeout=150).run(), "Projects")
    _assert_clean(at)
    next(item for item in at.button if item.key == "projects_show_legacy").click().run()
    _assert_clean(at)
    assert any(item.label == "Legacy experiment run" for item in at.selectbox)
    assert any(item.label == "Create new legacy run" for item in at.expander)
    next(item for item in at.text_input
         if item.key == "new_legacy_run_name").set_value("phase2 legacy fixture")
    next(item for item in at.button
         if item.key == "create_legacy_run").click().run()
    _assert_clean(at)
    assert run_manager.run_exists("phase2_legacy_fixture")
    assert at.session_state["selected_run"] == "phase2_legacy_fixture"


def test_machine_gallery_is_exactly_the_canonical_twelve_with_no_second_catalogue():
    _durable_context()
    at = _goto(AppTest.from_file(APP, default_timeout=150).run(), "Machines")
    _assert_clean(at)
    rendered = _visible_main_text(at)
    open_buttons = [button for button in at.button
                    if str(getattr(button, "key", "")).startswith("machine_open_")]
    assert {button.key for button in open_buttons} == {
        f"machine_open_{machine_id}" for machine_id in machine_contract.machine_ids()}
    for machine in machine_contract.list_virtual_lab_machines():
        assert rendered.count(machine.display_name) == 1
        assert machine.machine_id not in rendered
        assert machine.maturity not in rendered
        assert machine.execution_mode not in rendered
        assert machine.backend_binding not in rendered
    assert len(machine_contract.machine_ids()) == 12
    assert "Instrument registry" not in rendered


@pytest.mark.parametrize("machine_id", machine_contract.machine_ids())
def test_each_canonical_machine_card_opens_shared_workspace(machine_id):
    _durable_context()
    at = _goto(AppTest.from_file(APP, default_timeout=180).run(), "Machines")
    button = next(item for item in at.button if item.key == f"machine_open_{machine_id}")
    button.click().run()
    _assert_clean(at)
    labels = [item.label for item in at.tabs]
    assert labels[:4] == MACHINE_AREAS
    spec = machine_contract.get_virtual_lab_machine(machine_id)
    assert spec.display_name in _text(at)


def test_phreeqc_workspace_is_unavailable_and_has_no_generic_prepare_button():
    _durable_context()
    at = _goto(AppTest.from_file(APP, default_timeout=180).run(), "Machines")
    next(item for item in at.button
         if item.key == "machine_open_phreeqc_leaching_simulator").click().run()
    _assert_clean(at)
    rendered = _visible_main_text(at)
    assert "PHREEQC is not configured" in rendered
    assert "preview" in rendered.lower()
    assert "prepare" in rendered.lower() and "review" in rendered.lower()
    assert any(item.key == "machine_phreeqc_route" for item in at.button)
    assert not any(item.key == "machine_prepare_phreeqc_leaching_simulator" for item in at.button)
    assert "runtime_unavailable" not in rendered
    technical = [item for item in at.expander if item.label == "Technical details"]
    assert technical
    assert any("runtime_unavailable" in _text(item) for item in technical)


def test_results_and_history_mark_changed_material_run_historical():
    store, project, material, run = _durable_context()
    store.update_material(material.material_id, assumptions=["changed synthetic test assumption"])
    store.set_active_context(project.project_id, material.material_id, None)
    for page in ("Results", "Run History"):
        at = _goto(AppTest.from_file(APP, default_timeout=150).run(), page)
        _assert_clean(at)
        visible = _visible_main_text(at)
        assert run.run_id not in visible
        assert "Historical" in visible or "historical" in visible
        assert "material has changed" in visible
        details = [item for item in at.expander if item.label == "Result details"]
        assert any("material revision changed" in _text(item) for item in details)
        technical = [item for item in at.expander if item.label == "Technical details"]
        assert any(run.run_id in _text(item) for item in technical)


def test_settings_diagnostics_show_paths_and_blockers_but_no_secret_values(monkeypatch):
    _durable_context()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SENSITIVE_TEST_VALUE_DO_NOT_RENDER")
    at = _goto(AppTest.from_file(APP, default_timeout=150).run(), "Settings & Diagnostics")
    _assert_clean(at)
    rendered = _text(at)
    assert "Durable storage" in rendered
    assert "PHREEQC" in rendered
    assert "SENSITIVE_TEST_VALUE_DO_NOT_RENDER" not in rendered


def test_project_create_material_reload_journey_uses_persistent_ids():
    store = workspace_store.WorkspaceStore()
    project = store.create_project("Persistence journey")
    material = store.create_material(
        project.project_id, "Persistent material", material_type="synthetic test fixture",
        composition=[{"component": "SiO2", "value": 2.0, "unit": "wt%"}],
        composition_provenance={"source_type": "synthetic_demo", "source_reference": "fixture"})
    store.set_active_context(project.project_id, material.material_id, None)
    # AppTest starts a new script session; durable IDs and values still reopen.
    at = _goto(AppTest.from_file(APP, default_timeout=150).run(), "Material Workspace")
    _assert_clean(at)
    rendered = _text(at)
    assert project.name in rendered
    assert material.name in rendered
    assert material.material_id not in _visible_main_text(at)
    technical = [item for item in at.expander if item.label == "Technical details"]
    assert any(material.material_id in _text(item) for item in technical)


def test_switching_durable_context_invalidates_unsaved_scientific_session_state():
    store = workspace_store.WorkspaceStore()
    p1 = store.create_project("Context A")
    m1 = store.create_material(p1.project_id, "Material A")
    p2 = store.create_project("Context B")
    store.create_material(p2.project_id, "Material B")
    store.set_active_context(p1.project_id, m1.material_id, None)
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["sim_batch_result"] = {"synthetic": "must not leak"}
    at.session_state["phase2_prepared_result__xrd_advisory"] = {
        "project_id": p1.project_id, "material_id": m1.material_id}
    at.run()
    project_select = next(item for item in at.sidebar.selectbox
                          if item.key == "shell_active_project")
    project_select.set_value(p2.project_id).run()
    _assert_clean(at)
    assert "sim_batch_result" not in at.session_state
    assert "phase2_prepared_result__xrd_advisory" not in at.session_state
