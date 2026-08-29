"""Focused Phase 2 progressive-disclosure and plain-language regressions.

The pure view-model tests distinguish default card copy from retained scientific
and technical detail.  AppTests then pin the controls that expose those levels
without treating collapsed expander content as default-visible text.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import app_ui
from flyash_phreeqc_ml import config, workspace_store
from flyash_phreeqc_ml.instruments import icp_processor
from flyash_phreeqc_ml.instruments import virtual_lab_machine_runner as machine_runner
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines
from flyash_phreeqc_ml.simulation.phreeqc_executor import PhreeqcAvailability
from ui import product_shell

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
EXPECTED_CARD_KEYS = {
    "display_name",
    "purpose",
    "status_chips",
    "readiness",
    "action_label",
    "operable",
    "scientific_details",
    "technical_details",
}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VIRTUAL_LAB_WORKSPACE_DIR", tmp_path / "workspace")
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    monkeypatch.setattr(config, "PROCESSED_DIR", tmp_path / "processed")
    (tmp_path / "processed").mkdir()


def _nav_radio(at):
    matches = [item for item in at.radio if getattr(item, "key", None) == "nav_page"]
    assert len(matches) == 1
    return matches[0]


def _goto(at, page):
    _nav_radio(at).set_value(page).run()
    return at


def _assert_clean(at):
    assert at.exception is None or len(at.exception) == 0


def _block_text(block):
    values = []
    for kind in ("caption", "code", "error", "info", "json", "markdown", "success", "warning"):
        values.extend(str(item.value) for item in getattr(block, kind, ()))
    return html.unescape(" ".join(values))


def _visible_main_text(at):
    """Collect text in the main body, excluding collapsed expander children."""
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


def _flatten_strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _flatten_strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _flatten_strings(item)
    elif value is not None:
        yield str(value)


def _detail_blob(value):
    return " ".join(_flatten_strings(value))


def _chip_labels(chips):
    labels = []
    for chip in chips:
        if isinstance(chip, dict):
            labels.append(str(chip.get("label", "")))
        elif isinstance(chip, (tuple, list)):
            labels.append(str(chip[0]) if chip else "")
        else:
            labels.append(str(chip))
    return labels


def _surface_blob(view):
    return " ".join([
        str(view["display_name"]),
        str(view["purpose"]),
        *_chip_labels(view["status_chips"]),
        str(view["readiness"]),
        str(view["action_label"]),
    ])


def _word_count(value):
    return len(re.findall(r"\b[\w/-]+\b", str(value)))


def _context_with_run():
    store = workspace_store.WorkspaceStore()
    project = store.create_project("Synthetic UX project", description="test-only")
    material = store.create_material(
        project.project_id,
        "Synthetic UX material",
        material_type="test fixture",
        composition=[{"component": "CaO", "value": 1.0, "unit": "wt%"}],
        composition_provenance={"source_type": "synthetic_demo"},
        measurement_references=["synthetic-test-only-row-set"],
    )
    run = store.create_run(
        project.project_id,
        material.material_id,
        machines.XRD_ADVISORY,
        {"measured_peaks": [20.0], "fixture": "synthetic_test_only"},
        status="advisory",
        output_type=machines.OUT_ADVISORY_INTERPRETATION,
        epistemic_type=machines.OUT_ADVISORY_INTERPRETATION,
        result_data={"candidate_checklist": []},
        warnings=["Tentative only; confirm against measured XRD and reference data."],
        validation_state="not_applicable",
    )
    store.set_active_context(project.project_id, material.material_id, None)
    return store, project, material, run


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        ("implemented_backend", "Implemented"),
        ("advisory_backend", "Advisory"),
        ("limited_workflow", "Limited workflow"),
        ("blueprint_only", "Planned"),
        ("runtime_unavailable", "Not configured"),
        ("input_required", "Needs input"),
        ("reference_data_required", "Needs reference data"),
        ("trained_model_required", "Needs an approved model"),
        ("approved_model_required", "Needs an approved model"),
        ("measured_input_required", "Needs measured data"),
        ("preview_then_confirm", "Preview, then confirm"),
        ("data_processing", "Processes uploaded data"),
        ("advisory_only", "Advisory only"),
    ],
)
def test_friendly_status_uses_plain_language(raw, label):
    assert product_shell.friendly_status(raw) == label
    assert "_" not in product_shell.friendly_status(raw)


def test_unknown_status_fallback_does_not_leak_underscore_text():
    assert product_shell.friendly_status("unexpected_internal_state") == "Unexpected internal state"


def test_shared_machine_workspace_has_exactly_four_primary_areas():
    assert product_shell.MACHINE_WORKSPACE_AREAS == (
        "Overview", "Prepare", "Results", "History")


def test_all_twelve_card_views_are_concise_contract_derived_and_plain_language():
    specs = machines.list_virtual_lab_machines()
    assert len(specs) == 12
    views = [
        product_shell.machine_card_view(
            spec,
            "input_required",
            ["Provide the required user input before preparing this machine."],
            True,
        )
        for spec in specs
    ]
    assert [view["display_name"] for view in views] == [spec.display_name for spec in specs]

    for spec, view in zip(specs, views, strict=True):
        assert set(view) == EXPECTED_CARD_KEYS
        assert view["display_name"] == spec.display_name
        assert view["purpose"].strip()
        assert _word_count(view["purpose"]) <= 18
        assert 2 <= len(view["status_chips"]) <= 3
        assert all(label and "_" not in label for label in _chip_labels(view["status_chips"]))
        assert _word_count(_surface_blob(view)) <= 45
        assert view["action_label"] == "Open workspace"
        assert view["operable"] is True

        surface = _surface_blob(view)
        for internal in (
                spec.machine_id, spec.mode, spec.maturity, spec.execution_mode,
                spec.backend_binding):
            assert internal not in surface


def test_machine_card_views_preserve_complete_scientific_and_technical_contract_details():
    for spec in machines.list_virtual_lab_machines():
        view = product_shell.machine_card_view(
            spec, "runtime_unavailable", ["Runtime is unavailable for this test."], False)
        scientific = _detail_blob(view["scientific_details"])
        technical = _detail_blob(view["technical_details"])
        retained = f"{scientific} {technical}"

        for value in (
                *spec.required_inputs,
                *spec.optional_inputs,
                *spec.honest_outputs,
                *spec.verification_required,
                spec.real_world_verification_method,
                *spec.must_not_claim,
                *spec.provenance_requirements,
                *spec.validation_requirements,
                *spec.uncertainty_controls,
                *spec.safety_notes):
            assert str(value) in retained, f"{spec.machine_id}: detail lost: {value}"

        for value in (
                spec.machine_id, spec.category, spec.mode, spec.maturity,
                spec.execution_mode, spec.backend_binding):
            assert str(value) in technical, f"{spec.machine_id}: technical detail lost: {value}"


def test_machine_cards_release_fixed_height_at_the_required_tablet_viewport():
    css = re.sub(r"\s+", "", app_ui._PHASE2_CSS)
    tablet_rule = css.split("@media(max-width:900px)", 1)[1]
    assert ".vl-machine-card{height:auto;min-height:auto;}" in tablet_rule
    assert ':has(.vl-machine-card){flex-direction:column;' in tablet_rule
    assert 'width:100%!important;flex:11' in tablet_rule


def test_blocked_card_remains_inspectable_without_looking_runnable():
    spec = machines.get_virtual_lab_machine(machines.ML_SURROGATE)
    view = product_shell.machine_card_view(
        spec,
        "approved_model_required",
        ["No approved model is associated with the active material."],
        False,
    )
    assert view["operable"] is False
    assert view["action_label"] == "Open workspace"
    assert "Needs an approved model" in _surface_blob(view)
    assert "approved_model_required" not in _surface_blob(view)


def test_all_nine_pages_render_sequentially_without_widget_key_collisions():
    at = AppTest.from_file(APP, default_timeout=180).run()
    _assert_clean(at)
    assert tuple(_nav_radio(at).options) == PAGES
    for page in PAGES:
        _goto(at, page)
        _assert_clean(at)
        assert _nav_radio(at).value == page


def test_home_keeps_primary_action_and_phreeqc_diagnostics_one_click_deeper(monkeypatch):
    availability = PhreeqcAvailability(
        executable_configured=True,
        database_configured=False,
        executable_found=False,
        database_found=False,
        executable_path="/internal/test/phreeqc",
        database_path=None,
        message=("PHREEQC execution is unavailable because the executable and database "
                 "were not both found."),
        smoke_ok=None,
        environment_identity=None,
    )
    monkeypatch.setattr(product_shell, "_phreeqc_availability", lambda: availability)
    at = AppTest.from_file(APP, default_timeout=120).run()
    _assert_clean(at)
    visible = _visible_main_text(at)

    assert any(button.key == "home_no_project_action" for button in at.button)
    assert "PHREEQC unavailable" in visible
    assert "preview" in visible.lower()
    assert availability.message not in visible

    why = next(item for item in at.expander if "Why is this blocked?" in item.label)
    assert why.proto.expanded is False
    detail = _block_text(why).lower()
    for concept in ("executable", "database", "preview", "execution", "validation"):
        assert concept in detail


def test_gallery_has_exactly_twelve_open_actions_and_no_default_internal_values():
    at = _goto(AppTest.from_file(APP, default_timeout=150).run(), "Machines")
    _assert_clean(at)
    visible = _visible_main_text(at)
    open_buttons = [button for button in at.button
                    if str(getattr(button, "key", "")).startswith("machine_open_")]
    assert [button.key for button in open_buttons] == [
        f"machine_open_{machine_id}" for machine_id in machines.machine_ids()]

    for spec in machines.list_virtual_lab_machines():
        assert visible.count(spec.display_name) == 1
        assert spec.machine_id not in visible
        assert spec.maturity not in visible
        assert spec.execution_mode not in visible
        assert spec.backend_binding not in visible


def test_legacy_machine_selection_is_normalized_to_canonical_session_state():
    at = AppTest.from_file(APP, default_timeout=150)
    at.session_state["nav_page"] = "Machines"
    at.session_state["active_machine_id"] = "xrd_advisory_module"
    at.run()
    _assert_clean(at)

    assert at.session_state["active_machine_id"] == machines.XRD_ADVISORY
    assert machines.get_virtual_lab_machine(machines.XRD_ADVISORY).display_name in _visible_main_text(at)


def test_selected_workspace_precedes_collapsed_machine_chooser_and_exposes_disclosures():
    _context_with_run()
    at = _goto(AppTest.from_file(APP, default_timeout=180).run(), "Machines")
    next(button for button in at.button
         if button.key == f"machine_open_{machines.ICP_PROCESSOR}").click().run()
    _assert_clean(at)

    assert [tab.label for tab in at.tabs][:4] == list(product_shell.MACHINE_WORKSPACE_AREAS)
    chooser = next(item for item in at.expander if item.label == "Choose a different machine")
    assert chooser.proto.expanded is False
    assert {button.key for button in chooser.button
            if str(getattr(button, "key", "")).startswith("machine_open_")}

    nodes = list(at.main)
    overview_index = next(index for index, node in enumerate(nodes)
                          if getattr(node, "type", None) == "tab"
                          and getattr(node, "label", None) == "Overview")
    chooser_index = next(index for index, node in enumerate(nodes) if node is chooser)
    assert overview_index < chooser_index

    labels = {item.label for item in at.expander}
    assert {"Show scientific details", "Scientific limits", "How can I verify this?",
            "Technical details"} <= labels
    scientific = next(item for item in at.expander if item.label == "Show scientific details")
    limits = next(item for item in at.expander if item.label == "Scientific limits")
    verification = next(item for item in at.expander if item.label == "How can I verify this?")
    scientific_text = _block_text(scientific)
    verification_text = _block_text(verification)
    assert "provenance" in scientific_text.lower()
    assert "current blockers" in scientific_text.lower()
    assert "warning" in _block_text(limits).lower()
    assert "validation requirements" in verification_text.lower()
    spec = machines.get_virtual_lab_machine(machines.ICP_PROCESSOR)
    assert all(item in verification_text for item in spec.validation_requirements)


def test_blocked_ml_workspace_is_inspectable_but_has_no_prepare_action():
    _context_with_run()
    at = _goto(AppTest.from_file(APP, default_timeout=180).run(), "Machines")
    next(button for button in at.button
         if button.key == f"machine_open_{machines.ML_SURROGATE}").click().run()
    _assert_clean(at)
    assert "Needs an approved model" in _visible_main_text(at)
    assert not any(button.key == f"machine_prepare_{machines.ML_SURROGATE}"
                   for button in at.button)
    assert any(button.key == f"machine_open_{machines.ML_SURROGATE}"
               for button in at.button)


def test_bogus_associated_model_id_does_not_open_the_ml_prepare_gate():
    spec = machines.get_virtual_lab_machine(machines.ML_SURROGATE)
    material = SimpleNamespace(
        associated_model_ids=["fabricated-model-id"],
        measurement_references=[],
    )

    runtime, blockers, operable = product_shell.machine_runtime_state(spec, material)

    assert runtime == "approved_model_required"
    assert operable is False
    assert "verify" in " ".join(blockers).lower()


def test_phreeqc_workspace_is_preview_only_and_has_no_generic_run(monkeypatch):
    availability = PhreeqcAvailability(
        executable_configured=True,
        database_configured=False,
        executable_found=False,
        database_found=False,
        executable_path="/internal/test/phreeqc",
        database_path=None,
        message="PHREEQC executable and database are unavailable in this test.",
        smoke_ok=None,
        environment_identity=None,
    )
    monkeypatch.setattr(product_shell, "_phreeqc_availability", lambda: availability)
    _context_with_run()
    at = _goto(AppTest.from_file(APP, default_timeout=180).run(), "Machines")
    next(button for button in at.button
         if button.key == f"machine_open_{machines.PHREEQC_LEACHING}").click().run()
    _assert_clean(at)

    visible = _visible_main_text(at)
    assert "PHREEQC is not configured" in visible
    assert "preview" in visible.lower()
    assert "prepare" in visible.lower() and "review" in visible.lower()
    assert any(button.key == "machine_phreeqc_route" for button in at.button)
    assert not any(button.key == f"machine_prepare_{machines.PHREEQC_LEACHING}"
                   for button in at.button)
    assert not any(button.label.strip().lower() == "run" for button in at.button)
    assert "runtime_unavailable" not in visible
    technical = [item for item in at.expander if item.label == "Technical details"]
    assert any("runtime_unavailable" in _block_text(item) for item in technical)


def test_result_rows_keep_epistemic_state_visible_and_provenance_one_click_deeper():
    _store, _project, _material, run = _context_with_run()
    at = _goto(AppTest.from_file(APP, default_timeout=150).run(), "Results")
    _assert_clean(at)
    visible = _visible_main_text(at)
    assert "Advisory interpretation" in visible
    assert run.run_id not in visible

    labels = {item.label for item in at.expander}
    assert {"Show provenance", "Validation details", "Raw result data",
            "Technical details"} <= labels
    technical = next(item for item in at.expander
                     if item.label == "Technical details" and run.run_id in _block_text(item))
    assert run.run_id in _block_text(technical)
    provenance = next(item for item in at.expander if item.label == "Show provenance")
    provenance_text = _block_text(provenance).lower()
    assert all(key in provenance_text for key in ("model", "environment", "evidence"))


def test_icp_machine_results_surface_qc_counts_and_retain_the_complete_raw_envelope():
    _store, project, material, _run = _context_with_run()
    rows = [
        {"row_id": "usable", "sample_id": "S1", "element": "Ca",
         "concentration": 40.078, "unit": "mg/L", "dilution_factor": 1.0,
         "measured_or_predicted": "measured"},
        {"row_id": "censored", "sample_id": "S1", "element": "Sc",
         "concentration": 0.02, "unit": "ppb", "dilution_factor": 1.0,
         "detection_limit": 0.05, "measured_or_predicted": "measured"},
        {"row_id": "review", "sample_id": "S1", "element": "Si",
         "concentration": 1.0, "unit": "mg/L", "dilution_factor": None,
         "measured_or_predicted": "measured"},
        {"row_id": "excluded", "sample_id": "S2", "element": "Ca",
         "concentration": -1.0, "unit": "mg/L", "dilution_factor": 1.0,
         "measured_or_predicted": "measured"},
    ]
    result = machine_runner.run_virtual_lab_machine(
        machines.ICP_PROCESSOR, {"source": "measured", "rows": rows})
    at = _goto(AppTest.from_file(APP, default_timeout=180).run(), "Machines")
    next(button for button in at.button
         if button.key == f"machine_open_{machines.ICP_PROCESSOR}").click().run()
    at.session_state[f"phase2_prepared_result__{machines.ICP_PROCESSOR}"] = {
        "project_id": project.project_id,
        "material_id": material.material_id,
        "material_revision": material.revision,
        "input_snapshot": {"source": "measured", "rows": rows},
        "input_hash": "synthetic-test-hash",
        "result": result.to_dict(),
    }
    at.run()
    _assert_clean(at)

    visible = _visible_main_text(at)
    for label in ("Usable", "Censored", "Review needed", "Excluded"):
        assert label in visible
    assert "3 row(s) are not validation-eligible" in visible

    raw = next(item for item in at.expander if item.label == "Raw result envelope")
    raw_text = _block_text(raw)
    for field in (
        "machine_id", "status", "output_data_type", "result_summary", "results",
        "warnings", "missing_inputs", "assumptions", "provenance", "validation_status",
        "can_be_used_for_validation_claim",
    ):
        assert field in raw_text


def test_durable_icp_result_and_history_rows_keep_qc_block_state_visible():
    store, project, material, _run = _context_with_run()
    qc_summary = {"usable": 1, "censored": 1, "review_required": 1, "excluded": 1}
    store.create_run(
        project.project_id,
        material.material_id,
        machines.ICP_PROCESSOR,
        {"fixture": "synthetic-test-only"},
        status=machine_runner.STATUS_PROCESSED,
        output_type=machines.OUT_MEASURED_LAB_DATA,
        epistemic_type=machines.OUT_MEASURED_LAB_DATA,
        result_data={"qc_summary": qc_summary},
        warnings=["Generic ICP plasma caveat."],
        validation_state=machine_runner.VAL_NOT_APPLICABLE,
    )

    at = _goto(AppTest.from_file(APP, default_timeout=180).run(), "Results")
    _assert_clean(at)
    visible = _visible_main_text(at)
    for label in ("Usable", "Censored", "Review needed", "Excluded"):
        assert label in visible
    assert "3 row(s) are not validation-eligible" in visible

    _goto(at, "Run History")
    _assert_clean(at)
    history = _visible_main_text(at)
    assert "Review needed" in history
    assert "3 row(s) are not validation-eligible" in history


def test_icp_runner_still_delegates_to_phase1b_processor(monkeypatch):
    called = []
    real_process = icp_processor.process

    def recording_process(rows, *args, **kwargs):
        called.append((list(rows), kwargs))
        return real_process(rows, *args, **kwargs)

    monkeypatch.setattr(icp_processor, "process", recording_process)
    rows = [{
        "sample_id": "synthetic-S1",
        "element": "Ca",
        "concentration": 40.078,
        "unit": "mg/L",
        "dilution_factor": 1.0,
        "measured_or_predicted": "measured",
    }]
    result = machine_runner.run_virtual_lab_machine(
        machines.ICP_PROCESSOR, {"rows": rows, "source": "measured"})
    assert called and called[0][0] == rows
    assert result.provenance["processor"] == "icp_processor"
    assert "qc_summary" in result.results


def test_xrd_surface_and_result_remain_advisory_not_confirmed_identification():
    spec = machines.get_virtual_lab_machine(machines.XRD_ADVISORY)
    view = product_shell.machine_card_view(
        spec, "reference_data_required", ["Reference data are required."], False)
    surface = _surface_blob(view).lower()
    assert "advisory" in surface
    assert "not confirmed" in surface

    result = machine_runner.run_virtual_lab_machine(
        machines.XRD_ADVISORY, {"measured_peaks": [20.0, 29.4]})
    blob = " ".join([result.result_summary, *result.warnings]).lower()
    assert result.output_data_type == machines.OUT_ADVISORY_INTERPRETATION
    assert result.can_be_used_for_validation_claim is False
    assert "identified" not in blob and "confirmed identification" not in blob


def test_ml_surface_and_runner_require_an_approved_usable_model():
    spec = machines.get_virtual_lab_machine(machines.ML_SURROGATE)
    view = product_shell.machine_card_view(
        spec,
        "approved_model_required",
        ["No approved model is associated with the active material."],
        False,
    )
    assert "Needs an approved model" in _surface_blob(view)

    result = machine_runner.run_virtual_lab_machine(
        machines.ML_SURROGATE, {"features": {"curing_age_days": 28}, "model": None})
    assert result.status == machine_runner.STATUS_TRAINED_MODEL_REQUIRED
    assert result.output_data_type != machines.OUT_ML_PREDICTION
    assert result.results == {}


def test_machine_view_models_introduce_no_result_payload_or_mock_scientific_value():
    for spec in machines.list_virtual_lab_machines():
        view = product_shell.machine_card_view(spec, "input_required", [], True)
        assert set(view) == EXPECTED_CARD_KEYS
        assert "mock result" not in json.dumps(view, sort_keys=True).lower()
        assert not ({"result", "result_data", "prediction", "measurement"} & set(view))
