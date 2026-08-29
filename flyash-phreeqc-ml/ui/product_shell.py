"""Phase 2 product shell and durable-workspace presentation.

The module owns navigation-oriented UI only.  Durable state lives in
``flyash_phreeqc_ml.workspace_store`` and machine metadata/science remains in the
canonical instrument contract and runner.
"""
from __future__ import annotations

import html
import json
from dataclasses import asdict
from typing import Any

import streamlit as st

import app_ui
from flyash_phreeqc_ml import run_manager, workspace_store
from flyash_phreeqc_ml.instruments import icp_processor
from flyash_phreeqc_ml.instruments import virtual_lab_machine_runner as machine_runner
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines
from flyash_phreeqc_ml.simulation import phreeqc_executor

NAV_PAGES = (
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

MACHINE_WORKSPACE_AREAS = ("Overview", "Prepare", "Results", "History")

_FRIENDLY_STATUS = {
    machines.MATURITY_IMPLEMENTED: "Implemented",
    machines.MATURITY_ADVISORY: "Advisory",
    machines.MATURITY_LIMITED: "Limited workflow",
    machines.MATURITY_BLUEPRINT: "Planned",
    "runtime_unavailable": "Not configured",
    "ready_for_gated_preview": "Preview available",
    "approved_model_required": "Needs an approved model",
    "measured_input_required": "Needs measured data",
    "reference_data_required": "Needs reference data",
    "blueprint_only": "Planned",
    "input_required": "Needs input",
    machines.EXEC_PREVIEW_THEN_CONFIRM: "Preview, then confirm",
    machines.EXEC_DATA_PROCESSING: "Processes uploaded data",
    machines.EXEC_ADVISORY_ONLY: "Advisory only",
    machines.EXEC_TRAINED_MODEL_REQUIRED: "Needs an approved model",
    machines.EXEC_EVIDENCE_REQUIRED: "Evidence required",
    machines.EXEC_MEASURED_DATA_REQUIRED: "Measured data required",
    "not_evaluated": "Not evaluated",
    "not_applicable": "Not applicable",
}


def friendly_status(value: Any) -> str:
    """Return plain-language presentation text without changing canonical values."""
    text = str(value or "").strip()
    if not text:
        return "Not available"
    return _FRIENDLY_STATUS.get(text, text.replace("_", " ").strip().capitalize())

_SCIENTIFIC_SESSION_PREFIXES = (
    "asst_state__", "asst_mp__", "asst_release__", "asst_ctx_", "asst_db__",
    "asst_release_mode__", "asst_release_pct__", "sim_", "phase2_prepared_result__",
    "machine_payload__", "lab_icp_", "predmdl_", "evlib_", "assistant_msgs_",
)


def get_store() -> workspace_store.WorkspaceStore:
    return workspace_store.WorkspaceStore()


def _safe_context(store: workspace_store.WorkspaceStore) -> dict:
    context = store.get_active_context()
    project_id = context.get("active_project_id")
    material_id = context.get("active_material_id")
    run_id = context.get("active_run_id")
    try:
        if project_id:
            project = store.get_project(project_id)
            if project.archived:
                raise workspace_store.RecordNotFoundError("archived project")
        if material_id:
            material = store.get_material(material_id)
            if material.project_id != project_id:
                raise workspace_store.RecordNotFoundError("cross-project material")
        if run_id:
            run = store.get_run(run_id)
            if run.project_id != project_id or run.material_id != material_id:
                raise workspace_store.RecordNotFoundError("cross-context run")
    except workspace_store.WorkspaceStoreError:
        return {"schema_version": workspace_store.SCHEMA_VERSION, "active_project_id": None,
                "active_material_id": None, "active_run_id": None, "updated_at": None}
    return context


def _record_names(store, context):
    project = material = run = None
    try:
        if context.get("active_project_id"):
            project = store.get_project(context["active_project_id"])
        if context.get("active_material_id"):
            material = store.get_material(context["active_material_id"])
        if context.get("active_run_id"):
            run = store.get_run(context["active_run_id"])
    except workspace_store.WorkspaceStoreError:
        pass
    return project, material, run


def _bind_scientific_session_to_context(context: dict) -> None:
    """Invalidate unsaved/cached scientific UI state when durable identity changes.

    Persisted legacy/durable artifacts are untouched.  This prevents a PHREEQC result,
    target search, ICP table, model, evidence search, or prepared machine envelope from
    project/material A appearing current after switching to B.
    """
    signature = "|".join(str(context.get(key) or "") for key in (
        "active_project_id", "active_material_id", "active_run_id"))
    previous = st.session_state.get("_vl_context_signature")
    if previous is not None and previous != signature:
        for key in list(st.session_state):
            if str(key).startswith(_SCIENTIFIC_SESSION_PREFIXES):
                del st.session_state[key]
    st.session_state["_vl_context_signature"] = signature


def render_sidebar(store: workspace_store.WorkspaceStore) -> tuple[str, dict]:
    """One navigation system plus persistent project/material/run context."""
    st.sidebar.markdown("### WPI VIRTUAL LAB")
    context = _safe_context(store)
    projects = store.list_projects()
    project_ids = [item.project_id for item in projects]
    project_labels = {item.project_id: item.name for item in projects}
    current_project = context.get("active_project_id")

    if projects:
        chosen_project = st.sidebar.selectbox(
            "Active project", project_ids,
            index=project_ids.index(current_project) if current_project in project_ids else 0,
            format_func=lambda item: project_labels[item], key="shell_active_project")
        materials = store.list_materials(chosen_project)
        material_ids = [item.material_id for item in materials]
        material_labels = {item.material_id: item.name for item in materials}
        current_material = context.get("active_material_id")
        chosen_material = None
        if material_ids:
            chosen_material = st.sidebar.selectbox(
                "Active material", material_ids,
                index=material_ids.index(current_material) if current_material in material_ids else 0,
                format_func=lambda item: material_labels[item], key="shell_active_material")
        else:
            st.sidebar.caption("No material in this project")
        runs = store.list_runs(project_id=chosen_project, material_id=chosen_material) \
            if chosen_material else []
        run_ids = [item.run_id for item in runs]
        run_labels = {}
        for item in runs:
            spec = machines.get_virtual_lab_machine(item.machine_id)
            machine_name = spec.display_name if spec else friendly_status(item.machine_id)
            run_labels[item.run_id] = f"{machine_name} · {item.created_at[:10]}"
        current_run = context.get("active_run_id")
        chosen_run = None
        if run_ids:
            options = [None, *run_ids]
            chosen_run = st.sidebar.selectbox(
                "Active run / context", options,
                index=options.index(current_run) if current_run in options else 0,
                format_func=lambda item: "No active run" if item is None else run_labels[item],
                key="shell_active_run")
        desired = (chosen_project, chosen_material, chosen_run)
        existing = (context.get("active_project_id"), context.get("active_material_id"),
                    context.get("active_run_id"))
        if desired != existing:
            context = store.set_active_context(*desired)
    else:
        st.sidebar.caption("No project selected")

    _bind_scientific_session_to_context(context)

    st.sidebar.divider()
    page = st.sidebar.radio("Navigate", NAV_PAGES, key="nav_page", label_visibility="collapsed")
    return page, context


def render_product_header(store, context) -> None:
    """Compact product identity and the one persistent durable-context summary."""
    project, material, run = _record_names(store, context)
    chips: list[tuple[str, str]] = []
    if project:
        chips.append((f"Project · {project.name}", "info"))
        chips.append((f"Material · {material.name}" if material else "No material selected",
                      "neutral" if material else "warning"))
    if run:
        stale, _ = store.run_staleness(run)
        chips.append(("Historical run" if stale else "Current run",
                      "warning" if stale else "success"))
    chip_html = "".join(app_ui.status_badge(label, status) for label, status in chips)
    st.markdown(
        '<div class="vl-product-header"><div class="vl-product-name">WPI Virtual LAB</div>'
        f'<div class="vl-product-context">{chip_html}</div></div>', unsafe_allow_html=True)


def render_context_bar(store, context, location: str | None = None) -> None:
    """Backward-compatible alias for the compact product/context header."""
    render_product_header(store, context)


def navigate(page: str) -> None:
    if page not in NAV_PAGES:
        return
    st.session_state["nav_page"] = page
    st.rerun()


def _empty(title: str, message: str, action_page: str | None = None,
           action_label: str | None = None, *, key: str = "empty") -> None:
    st.markdown(f'<div class="vl-empty"><strong>{html.escape(title)}</strong><br>'
                f'{html.escape(message)}</div>', unsafe_allow_html=True)
    if action_page and st.button(action_label or f"Open {action_page}", key=f"{key}_action"):
        navigate(action_page)


def _phreeqc_availability():
    return phreeqc_executor.check_availability(run_smoke=False)


def phreeqc_surface(availability) -> dict:
    """Presentation-only PHREEQC summary plus complete collapsed diagnostics."""
    if availability.can_run:
        title = "PHREEQC configured — review still required"
        summary = "Preview the exact input and confirm it before any simulation."
        status = "success"
    else:
        title = "PHREEQC unavailable — preview only"
        summary = "PHREEQC is not configured; you can still prepare and review an input."
        status = "warning"
    return {
        "title": title,
        "summary": summary,
        "status": status,
        "details": (
            f"Executable: {'available' if availability.executable_found else 'unavailable'}.",
            f"Database: {'available' if availability.database_found else 'unavailable'}.",
            "Input preview and review remain available.",
            "Execution remains unavailable." if not availability.can_run
            else "Execution remains gated by exact review and confirmation.",
            "A simulation is not a measurement or experimental validation.",
        ),
    }


def render_home(store, context) -> None:
    app_ui.render_page_header("Home", "Start with the active project, its main blocker, and the next step.")
    project, material, _ = _record_names(store, context)
    availability = _phreeqc_availability()
    if not project:
        st.markdown("Start by creating a project for your materials, runs, and evidence.")
        if st.button("Create project", key="home_no_project_action", type="primary"):
            navigate("Projects")
    else:
        materials = store.list_materials(project.project_id)
        runs = store.list_runs(project_id=project.project_id)
        unresolved = sum(len(item.unresolved_issues) for item in materials)
        completeness = "Add a material" if not materials else (
            "Add composition or provenance" if not material or not material.composition else "Ready to prepare")
        app_ui.render_metric_cards([
            {"label": "Active project", "value": project.name,
             "caption": friendly_status(project.status)},
            {"label": "Material", "value": material.name if material else "Not selected",
             "caption": completeness,
             "status": "success" if completeness == "Ready to prepare" else "warning"},
            {"label": "Recent runs", "value": len(runs)},
            {"label": "Unresolved issues", "value": unresolved,
             "status": "warning" if unresolved else "neutral"},
        ])
        action_label = "Open material workspace" if material else "Add material"
        if st.button(action_label, key="home_primary_action", type="primary"):
            navigate("Material Workspace")

    app_ui.section_header("Needs attention")
    surface = phreeqc_surface(availability)
    app_ui.render_warning_panel(surface["title"], surface["summary"], level=surface["status"])
    st.caption("Simulation output is not measured data or experimental validation.")
    with st.expander("Why is this blocked?", expanded=False):
        for detail in surface["details"]:
            st.markdown(f"- {detail}")
        st.caption("Availability was checked without running a PHREEQC smoke test.")

    app_ui.section_header("Recommended next step")
    if not project:
        st.write("Create a project, then add the material you want to study.")
    elif not material:
        st.write("Add a material and record where its composition came from.")
    else:
        st.write("Choose a machine that matches the evidence and inputs you have.")
        if st.button("Open Machines", key="home_next_machines"):
            navigate("Machines")

    with st.expander("Show capability details", expanded=False):
        st.write("The workspace keeps assumptions, measurements, simulations, model predictions, "
                 "literature evidence, and validation visibly distinct.")
        st.write("Twelve canonical machines remain available from the Machines page.")

    if project:
        recent = list(reversed(store.list_runs(project_id=project.project_id)))[:5]
        app_ui.section_header("Recent runs")
        if not recent:
            _empty("No runs yet", "Prepare a machine result, then save its evidence and provenance.",
                   "Machines", key="home_no_runs")
        else:
            _render_run_rows(store, recent, compact=True)


def render_projects(store, context) -> None:
    app_ui.render_page_header("Projects", "Create or select a project for its materials, runs, and evidence.")
    active_project, _, _ = _record_names(store, context)
    projects = store.list_projects(include_archived=True)
    if active_project:
        materials = store.list_materials(active_project.project_id)
        runs = store.list_runs(project_id=active_project.project_id)
        app_ui.render_metric_cards([
            {"label": "Active project", "value": active_project.name,
             "caption": friendly_status(active_project.status)},
            {"label": "Materials", "value": len(materials)},
            {"label": "Runs", "value": len(runs)},
        ])
    with st.expander("Create project", expanded=not projects):
        name = st.text_input("Project name", key="project_create_name")
        description = st.text_area("Project description", key="project_create_description")
        if st.button("Create project", key="project_create_submit", type="primary"):
            try:
                record = store.create_project(name, description=description)
                store.set_active_context(record.project_id, None, None)
                st.success(f"Created {record.name} · {record.project_id}")
                st.rerun()
            except workspace_store.WorkspaceStoreError as exc:
                st.error(str(exc))

    if not projects:
        _empty("No projects yet", "Create the first project to organize materials and runs.")
        return
    for record in projects:
        materials = store.list_materials(record.project_id)
        runs = store.list_runs(project_id=record.project_id)
        evidence_count = sum(len(item.evidence_references) for item in materials)
        model_count = sum(len(item.associated_model_ids) for item in materials)
        with st.expander(f"{record.name} · {friendly_status(record.status)}", expanded=False):
            st.caption(f"{record.project_id} · created {record.created_at} · updated {record.updated_at}")
            st.write(record.description or "No description supplied.")
            app_ui.render_metric_cards([
                {"label": "Materials", "value": len(materials)},
                {"label": "Runs", "value": len(runs)},
                {"label": "Evidence refs", "value": evidence_count},
                {"label": "Model refs", "value": model_count},
            ])
            if not record.archived and st.button("Make active", key=f"project_activate_{record.project_id}"):
                first_material = materials[0].material_id if materials else None
                store.set_active_context(record.project_id, first_material, None)
                st.rerun()
            if not record.archived:
                with st.form(f"project_edit_{record.project_id}"):
                    edit_name = st.text_input("Name", record.name)
                    edit_description = st.text_area("Description", record.description)
                    edit_status = st.selectbox("Status", ["active", "planning", "on_hold", "complete"],
                                               index=["active", "planning", "on_hold", "complete"].index(record.status)
                                               if record.status in {"active", "planning", "on_hold", "complete"} else 0)
                    if st.form_submit_button("Save project metadata"):
                        store.update_project(record.project_id, name=edit_name,
                                             description=edit_description, status=edit_status)
                        st.success("Project metadata saved.")
                        st.rerun()
                confirmation = st.text_input("Type the exact project ID to archive",
                                             key=f"project_archive_confirm_{record.project_id}")
                if st.button("Archive project", key=f"project_archive_{record.project_id}"):
                    try:
                        store.archive_project(record.project_id, confirmation=confirmation)
                        st.success("Project archived; records were preserved.")
                        st.rerun()
                    except workspace_store.WorkspaceStoreError as exc:
                        st.error(str(exc))


def render_legacy_run_manager() -> str | None:
    """Preserve the Phase 1 per-run selector and creation workflow on Projects."""
    runs = run_manager.list_runs()
    pending = st.session_state.pop("_legacy_run_after_create", None)
    if pending in runs:
        st.session_state.pop("legacy_run_project_selector", None)
    current = pending if pending in runs else st.session_state.get("selected_run")
    options = [None, *runs]
    selected = st.selectbox(
        "Legacy experiment run",
        options,
        index=options.index(current) if current in runs else 0,
        format_func=lambda item: "No legacy run selected" if item is None else item,
        key="legacy_run_project_selector",
    )
    st.session_state["selected_run"] = selected

    with st.expander("Create new legacy run", expanded=not runs):
        new_name = st.text_input(
            "Run name",
            key="new_legacy_run_name",
            placeholder="2026-06-03 fly-ash leaching experiment",
        )
        new_type = st.selectbox(
            "Run type", run_manager.RUN_TYPES, key="new_legacy_run_type"
        )
        st.caption(run_manager.warning_for(new_type))
        new_desc = st.text_area(
            "Description", key="new_legacy_run_desc", height=70
        )
        new_notes = st.text_input(
            "Notes (optional)", key="new_legacy_run_notes"
        )
        if st.button(
            "Create legacy run",
            width="stretch",
            key="create_legacy_run",
        ):
            raw = (new_name or "").strip()
            if not raw:
                st.error("Run name is required.")
            else:
                try:
                    safe = run_manager.safe_run_name(raw)
                    if run_manager.run_exists(safe):
                        st.error(
                            f"A legacy run named '{safe}' already exists — open it instead."
                        )
                    else:
                        run_manager.create_run(
                            raw,
                            new_type,
                            description=new_desc,
                            notes=new_notes,
                        )
                        st.session_state["_legacy_run_after_create"] = safe
                        st.success(f"Created legacy run '{safe}'.")
                        st.rerun()
                except run_manager.RunManagerError as exc:
                    st.error(str(exc))

    if not selected:
        st.caption("No legacy run selected. Create or open one above.")
        return None

    cfg = run_manager.load_run_config(selected)
    st.markdown(f"**Current legacy run:** `{selected}`")
    st.caption(
        f"Type · {cfg.get('run_type')}  |  Source · {cfg.get('data_source')}"
    )
    if cfg.get("description"):
        st.caption(str(cfg["description"]))
    st.warning(run_manager.warning_for(cfg.get("run_type")))
    return selected


def render_material_record(store, context) -> None:
    project, material, _ = _record_names(store, context)
    if not project:
        _empty("Project required", "Select or create a project before adding a material.",
               "Projects", key="material_no_project")
        return
    with st.expander("Create material", expanded=material is None):
        name = st.text_input("Material name", key="material_create_name")
        kind = st.text_input("Material type", key="material_create_type",
                             placeholder="e.g. fly ash, binder, composite")
        if st.button("Create material", key="material_create_submit", type="primary"):
            try:
                record = store.create_material(project.project_id, name, material_type=kind)
                store.set_active_context(project.project_id, record.material_id, None)
                st.success(f"Created {record.name} · {record.material_id}")
                st.rerun()
            except workspace_store.WorkspaceStoreError as exc:
                st.error(str(exc))
    if material is None:
        return

    source_type_current = material.composition_provenance.get("source_type", "unspecified")
    if not material.composition:
        main_missing = "Composition"
    elif source_type_current == "unspecified":
        main_missing = "Composition source"
    elif material.unresolved_issues:
        main_missing = material.unresolved_issues[0]
    else:
        main_missing = "No core gap recorded"
    app_ui.render_metric_cards([
        {"label": "Material", "value": material.name,
         "caption": material.material_type or "Type not set"},
        {"label": "Verification", "value": friendly_status(material.verification_status),
         "status": "success" if material.verification_status == "measured" else "warning"},
        {"label": "Main missing item", "value": main_missing,
         "status": "success" if main_missing == "No core gap recorded" else "warning"},
    ])
    with st.expander("Technical details", expanded=False):
        st.caption(f"Stable material ID: `{material.material_id}` · schema {material.schema_version} · "
                   f"revision {material.revision}")
    composition_rows = material.composition or [
        {"component": "", "value": None, "unit": "wt%", "basis": "", "source": ""}]
    with st.form(f"material_editor_{material.material_id}"):
        name = st.text_input("Name", material.name)
        material_type = st.text_input("Material type", material.material_type)
        description = st.text_area("Description", material.description)
        st.markdown("**Composition** — one row per supplied component; blanks remain missing.")
        composition = st.data_editor(composition_rows, num_rows="dynamic", use_container_width=True,
                                     key=f"material_composition_{material.material_id}")
        with st.expander("Composition provenance and verification", expanded=False):
            verification_options = ["unverified", "user_reviewed", "literature_reported", "measured"]
            verification = st.selectbox(
                "Verification status", verification_options,
                index=verification_options.index(material.verification_status)
                if material.verification_status in verification_options else 0,
                format_func=friendly_status,
                help="Use Measured only for composition supplied by a documented measurement.")
            source_options = ["unspecified", "user_assumption", "measured_laboratory_data",
                              "literature_evidence"]
            source_type = st.selectbox(
                "Composition provenance type", source_options,
                index=source_options.index(source_type_current)
                if source_type_current in source_options else 0,
                format_func=friendly_status)
            source_reference = st.text_input(
                "Composition source / report / DOI",
                material.composition_provenance.get("source_reference", ""))
        with st.expander("Assumptions and process conditions", expanded=False):
            assumptions = st.text_area("Assumptions (one per line)", "\n".join(material.assumptions))
            unresolved = st.text_area("Unresolved issues (one per line)",
                                      "\n".join(material.unresolved_issues))
            col1, col2, col3, col4 = st.columns(4)
            leachant = col1.text_input("Leachant", material.process_conditions.get("leachant", ""))
            concentration = col2.text_input("Concentration + unit",
                                            material.process_conditions.get("concentration", ""))
            ratio = col3.text_input("Liquid / solid ratio",
                                    material.process_conditions.get("liquid_solid_ratio", ""))
            temperature = col4.text_input("Temperature + unit",
                                          material.process_conditions.get("temperature", ""))
        with st.expander("References and approved-model links", expanded=False):
            measurement_refs = st.text_area("Measurement references (one per line)",
                                            "\n".join(material.measurement_references))
            evidence_refs = st.text_area("Evidence references (one per line)",
                                         "\n".join(material.evidence_references))
            model_refs = st.text_area("Associated approved model IDs (one per line)",
                                      "\n".join(material.associated_model_ids))
        submitted = st.form_submit_button("Save durable material", type="primary")
    if submitted:
        def lines(text):
            return [item.strip() for item in text.splitlines() if item.strip()]
        cleaned_composition = []
        for row in composition:
            if any(value not in (None, "") for value in row.values()):
                cleaned_composition.append(dict(row))
        try:
            store.update_material(
                material.material_id, name=name.strip(), description=description.strip(),
                material_type=material_type.strip(), composition=cleaned_composition,
                composition_provenance={"source_type": source_type,
                                        "source_reference": source_reference.strip()},
                verification_status=verification, assumptions=lines(assumptions),
                unresolved_issues=lines(unresolved),
                process_conditions={"leachant": leachant.strip(),
                                    "concentration": concentration.strip(),
                                    "liquid_solid_ratio": ratio.strip(),
                                    "temperature": temperature.strip()},
                measurement_references=lines(measurement_refs),
                evidence_references=lines(evidence_refs),
                associated_model_ids=lines(model_refs),
            )
            store.set_active_context(project.project_id, material.material_id, None)
            st.success("Material saved. Scientifically meaningful changes invalidate current-run status.")
            st.rerun()
        except workspace_store.WorkspaceStoreError as exc:
            st.error(str(exc))


def machine_runtime_state(spec, material=None) -> tuple[str, list[str], bool]:
    if spec.machine_id == machines.PHREEQC_LEACHING:
        availability = _phreeqc_availability()
        return ("ready_for_gated_preview" if availability.can_run else "runtime_unavailable",
                [] if availability.can_run else [availability.message], availability.can_run)
    if spec.needs_trained_model:
        # An association string is provenance, not proof that a model artifact is
        # approved, non-demo, fitted, and usable.  The dedicated model workflow
        # owns that verification boundary, so this generic surface stays closed.
        return (
            "approved_model_required",
            ["Select and verify a usable, approved non-demo model in the approved-model workflow."],
            False,
        )
    if spec.needs_measured_data and not (material and material.measurement_references):
        return "measured_input_required", ["User-supplied measured data are required."], False
    if spec.needs_reference_database:
        return "reference_data_required", list(spec.runtime_requirements), False
    if spec.maturity == machines.MATURITY_BLUEPRINT:
        return "blueprint_only", list(spec.future_backend_dependencies), False
    return "input_required", list(spec.runtime_requirements), True


def _machine_purpose(spec) -> str:
    """Use the canonical short description, omitting its repeated status clause."""
    purpose = spec.short_description.split("—", 1)[0].strip()
    if ":" in purpose:
        purpose = purpose.split(":", 1)[0]
    purpose = purpose.rstrip(".") + "."
    return purpose


def _machine_readiness(runtime: str) -> str:
    return {
        "runtime_unavailable": "PHREEQC is not configured; input preview remains available.",
        "ready_for_gated_preview": "Preview is available; review and confirm before execution.",
        "approved_model_required": "Select an approved model before requesting a prediction.",
        "measured_input_required": "Add measured data before processing or comparison.",
        "reference_data_required": "Add suitable reference data before interpretation.",
        "blueprint_only": "This planned workflow is available for requirements review only.",
        "input_required": "Add the required inputs to begin.",
    }.get(runtime, friendly_status(runtime))


def machine_card_view(spec, runtime, blockers, operable) -> dict:
    """Contract-derived surface and disclosures; no second scientific catalogue."""
    epistemic = [app_ui.EPISTEMIC_LABELS.get(item, friendly_status(item))
                 for item in spec.output_data_type]
    readiness = _machine_readiness(runtime)
    if spec.machine_id == machines.XRD_ADVISORY:
        readiness = "Advisory, not confirmed; reference data may be needed."
    return {
        "display_name": spec.display_name,
        "purpose": _machine_purpose(spec),
        "status_chips": (
            friendly_status(spec.maturity),
            friendly_status(runtime),
            epistemic[0] if epistemic else "Output type unspecified",
        ),
        "readiness": readiness,
        "action_label": "Open workspace",
        "operable": bool(operable),
        "scientific_details": {
            "required_inputs": tuple(spec.required_inputs),
            "optional_inputs": tuple(spec.optional_inputs),
            "honest_outputs": tuple(spec.honest_outputs),
            "output_data_types": tuple(spec.output_data_type),
            "warnings": tuple(spec.safety_notes),
            "blockers": tuple(blockers),
            "must_not_claim": tuple(spec.must_not_claim),
            "validation_requirements": tuple(spec.validation_requirements),
            "verification_required": tuple(spec.verification_required),
            "verification_method": spec.real_world_verification_method,
            "uncertainty": tuple(spec.uncertainty_controls),
            "provenance": tuple(spec.provenance_requirements),
            "example_prompts": tuple(spec.example_user_prompts),
        },
        "technical_details": {
            "canonical_id": spec.machine_id,
            "category": spec.category,
            "mode": spec.mode,
            "maturity": spec.maturity,
            "runtime": runtime,
            "execution_mode": spec.execution_mode,
            "backend": spec.backend_binding,
            "backend_capabilities": tuple(spec.backend_capabilities),
            "runtime_requirements": tuple(spec.runtime_requirements),
            "future_backend_dependencies": tuple(spec.future_backend_dependencies),
            "needs_measured_data": spec.needs_measured_data,
            "needs_trained_model": spec.needs_trained_model,
            "needs_reference_database": spec.needs_reference_database,
            "use_cached_or_precomputed_data": spec.should_use_cached_or_precomputed_data,
        },
    }


def _chip_status(label: str) -> str:
    low = label.lower()
    if any(token in low for token in ("need", "not configured", "planned", "advisory")):
        return "warning"
    if "implemented" in low or "available" in low:
        return "success"
    return "info"


def _machine_card(spec, runtime, blockers, operable):
    view = machine_card_view(spec, runtime, blockers, operable)
    chips = "".join(app_ui.status_badge(label, _chip_status(label))
                    for label in view["status_chips"])
    body = (
        f'<div class="vl-machine-card"><div class="vl-machine-title">'
        f'{html.escape(view["display_name"])}</div><div class="vl-machine-purpose">'
        f'{html.escape(view["purpose"])}</div><div class="vl-machine-chips">{chips}</div>'
        f'<div class="vl-machine-readiness">{html.escape(view["readiness"])}</div></div>')
    st.markdown(body, unsafe_allow_html=True)
    if st.button(view["action_label"], key=f"machine_open_{spec.machine_id}",
                 use_container_width=True):
        st.session_state["active_machine_id"] = spec.machine_id
        st.rerun()


def _render_machine_grid(store, context) -> None:
    specs = machines.list_virtual_lab_machines()
    assert len(specs) == 12
    _, material, _ = _record_names(store, context)
    for row_start in range(0, len(specs), 3):
        cols = st.columns(3)
        for column, spec in zip(cols, specs[row_start:row_start + 3], strict=True):
            with column:
                runtime, blockers, operable = machine_runtime_state(spec, material)
                _machine_card(spec, runtime, blockers, operable)


def render_machine_gallery(store, context) -> str | None:
    """Render the gallery only when nothing is selected; selection is session-stable."""
    stored_selection = st.session_state.get("active_machine_id")
    selected = machines.canonical_machine_id(stored_selection)
    if selected is None:
        st.session_state.pop("active_machine_id", None)
    elif stored_selection != selected:
        # Legacy IDs remain readable, but all new UI state is canonical.
        st.session_state["active_machine_id"] = selected
    app_ui.render_page_header("Machines", "Choose a research machine, then review its readiness before acting.")
    if selected:
        if st.button("Back to all machines", key="machine_back_to_gallery"):
            st.session_state.pop("active_machine_id", None)
            st.rerun()
        return selected
    st.caption("12 research workspaces · scientific and technical details stay one click deeper")
    _render_machine_grid(store, context)
    return selected


def render_machine_picker(store, context) -> None:
    """One collapsed reuse of the canonical gallery below a selected workspace."""
    with st.expander("Choose a different machine", expanded=False):
        _render_machine_grid(store, context)


def _result_to_dict(result) -> dict:
    if hasattr(result, "to_dict"):
        return result.to_dict()
    if hasattr(result, "__dict__"):
        return asdict(result) if hasattr(result, "__dataclass_fields__") else dict(result.__dict__)
    return dict(result)


def _prepared_key(machine_id):
    return f"phase2_prepared_result__{machine_id}"


def _render_items(items, empty: str = "None recorded.") -> None:
    if not items:
        st.caption(empty)
        return
    for item in items:
        st.markdown(f"- {item}")


def _render_icp_qc_summary(qc_summary) -> None:
    """Surface stored Phase 1B row-state counts without exposing row-level detail."""
    if not isinstance(qc_summary, dict):
        return
    qc_items = (
        ("Usable", icp_processor.QC_USABLE, "success"),
        ("Censored", icp_processor.QC_CENSORED, "warning"),
        ("Review needed", icp_processor.QC_REVIEW_REQUIRED, "warning"),
        ("Excluded", icp_processor.QC_EXCLUDED, "warning"),
    )
    app_ui.render_metric_cards([
        {"label": label, "value": qc_summary.get(key, "Not reported"), "status": status}
        for label, key, status in qc_items
    ])
    non_eligible = sum(
        value for key in (
            icp_processor.QC_CENSORED,
            icp_processor.QC_REVIEW_REQUIRED,
            icp_processor.QC_EXCLUDED,
        )
        if isinstance((value := qc_summary.get(key)), int) and value > 0
    )
    if non_eligible:
        st.warning(
            f"{non_eligible} row(s) are not validation-eligible under the recorded QC states; "
            "review the row-level details before comparison."
        )


def _show_specialized_workflow(machine_id: str, label: str) -> None:
    if st.button(label, key=f"machine_specialized_open_{machine_id}", type="primary"):
        st.session_state[f"machine_specialized_visible__{machine_id}"] = True
        st.rerun()


def render_machine_workspace(store, context, machine_id: str) -> None:
    spec = machines.get_virtual_lab_machine(machine_id)
    if spec is None:
        st.error("Unknown machine. Nothing was run.")
        return
    project, material, _ = _record_names(store, context)
    runtime, blockers, operable = machine_runtime_state(spec, material)
    view = machine_card_view(spec, runtime, blockers, operable)
    app_ui.render_page_header(spec.display_name, view["purpose"])
    tabs = st.tabs(list(MACHINE_WORKSPACE_AREAS))
    with tabs[0]:
        chips = "".join(app_ui.status_badge(label, _chip_status(label))
                        for label in view["status_chips"])
        st.markdown(f'<div class="vl-machine-chips">{chips}</div>', unsafe_allow_html=True)
        app_ui.render_warning_panel(
            friendly_status(runtime), view["readiness"],
            level="success" if operable and not blockers else "warning")

        if spec.machine_id == machines.PHREEQC_LEACHING:
            st.caption("Any PHREEQC output is a simulation, not measured data or experimental validation.")
            if st.button("Open PHREEQC planner", key="machine_phreeqc_route", type="primary"):
                navigate("Material Workspace")
            with st.expander("Why is this blocked?", expanded=False):
                availability = _phreeqc_availability()
                for detail in phreeqc_surface(availability)["details"]:
                    st.markdown(f"- {detail}")
                st.caption(availability.message)
        elif spec.machine_id == machines.ICP_PROCESSOR:
            st.caption("Processes supplied concentration data; measured status still depends on row role and QC.")
            _show_specialized_workflow(spec.machine_id, "Open ICP processor")
        elif spec.machine_id == machines.XRD_ADVISORY:
            st.caption("Results are advisory, not confirmed phase identification.")
            _show_specialized_workflow(spec.machine_id, "Open XRD planner")
        elif spec.machine_id == machines.ML_SURROGATE:
            st.caption("A usable approved model is required; no model means no prediction.")
            _show_specialized_workflow(spec.machine_id, "Open approved-model workflow")
        else:
            st.markdown("**Next:** open **Prepare** and add the required inputs.")

        with st.expander("Show scientific details", expanded=False):
            st.write(spec.what_it_can_do)
            st.markdown("**Honest outputs**")
            _render_items(view["scientific_details"]["honest_outputs"])
            st.markdown("**Required inputs**")
            _render_items(view["scientific_details"]["required_inputs"])
            if view["scientific_details"]["optional_inputs"]:
                st.markdown("**Optional inputs**")
                _render_items(view["scientific_details"]["optional_inputs"])
            st.markdown("**Current blockers**")
            _render_items(view["scientific_details"]["blockers"], "No current blocker recorded.")
            st.markdown("**Required provenance**")
            _render_items(view["scientific_details"]["provenance"])
            st.markdown("**Uncertainty notes**")
            _render_items(view["scientific_details"]["uncertainty"])
            st.markdown("**Example requests**")
            _render_items(view["scientific_details"]["example_prompts"])
        with st.expander("Scientific limits", expanded=False):
            st.markdown("**Warnings**")
            _render_items(view["scientific_details"]["warnings"])
            st.markdown("**This machine must not claim**")
            _render_items(view["scientific_details"]["must_not_claim"])
        with st.expander("How can I verify this?", expanded=False):
            st.markdown("**Validation requirements**")
            _render_items(view["scientific_details"]["validation_requirements"])
            st.markdown("**Real-world verification**")
            st.write(view["scientific_details"]["verification_method"])
            _render_items(view["scientific_details"]["verification_required"])
        with st.expander("Technical details", expanded=False):
            st.json(view["technical_details"])

    with tabs[1]:
        st.markdown("**Required inputs**")
        _render_items(spec.required_inputs)
        if blockers:
            st.warning(view["readiness"])
        default_payload = st.session_state.get(f"machine_payload__{spec.machine_id}", "{}")
        payload_text = default_payload
        if spec.machine_id == machines.PHREEQC_LEACHING:
            st.info("Prepare and review PHREEQC input in the planner; this shared workspace has no "
                    "generic input editor or Run button.")
            if st.button("Open PHREEQC planner", key="machine_phreeqc_prepare_route"):
                navigate("Material Workspace")
        else:
            with st.expander("Advanced inputs", expanded=False):
                payload_text = st.text_area(
                    "Input data (JSON; user-supplied values only)", default_payload,
                    key=f"machine_payload_editor__{spec.machine_id}", height=180,
                    help="This delegates to the canonical adapter and does not add missing values.")
            if operable and st.button(
                    "Prepare supplied data", key=f"machine_prepare_{spec.machine_id}", type="primary"):
                try:
                    payload = json.loads(payload_text)
                    if not isinstance(payload, dict):
                        raise ValueError("payload must be a JSON object")
                    result = machine_runner.run_virtual_lab_machine(spec.machine_id, payload)
                    envelope = {
                        "project_id": project.project_id if project else None,
                        "material_id": material.material_id if material else None,
                        "material_revision": material.revision if material else None,
                        "machine_id": spec.machine_id,
                        "input_snapshot": payload,
                        "input_hash": workspace_store.identity_hash(payload),
                        "result": _result_to_dict(result),
                    }
                    st.session_state[_prepared_key(spec.machine_id)] = envelope
                    st.session_state[f"machine_payload__{spec.machine_id}"] = payload_text
                    st.success("Prepared. Review Results before saving.")
                except (ValueError, json.JSONDecodeError, workspace_store.WorkspaceStoreError) as exc:
                    st.error(f"Input refused: {exc}")
            elif not operable:
                st.caption("This machine remains inspectable, but preparation is unavailable until the blocker is resolved.")
            with st.expander("Optional inputs", expanded=False):
                _render_items(spec.optional_inputs)
            with st.expander("Full assumptions and preparation notes", expanded=False):
                _render_items(spec.safety_notes)
                _render_items(spec.runtime_requirements)

    envelope = st.session_state.get(_prepared_key(spec.machine_id))
    context_match = bool(envelope and project and material
                         and envelope.get("project_id") == project.project_id
                         and envelope.get("material_id") == material.material_id
                         and envelope.get("material_revision") == material.revision)
    result = envelope.get("result") if context_match else None
    with tabs[2]:
        if not context_match:
            _empty("No current result", "Prepare this machine for the active project and material.")
        else:
            app_ui.render_epistemic_badge(result.get("output_data_type"))
            st.markdown(f'<div class="vl-result">'
                        f'{html.escape(str(result.get("result_summary", "")))}</div>',
                        unsafe_allow_html=True)
            st.caption(f"{friendly_status(result.get('status'))} · "
                       f"Validation: {friendly_status(result.get('validation_status'))}")
            warnings = list(result.get("warnings") or [])
            if warnings:
                st.warning(str(warnings[0]))
            if spec.machine_id == machines.ICP_PROCESSOR:
                _render_icp_qc_summary((result.get("results") or {}).get("qc_summary"))
            if project and material and st.button("Save immutable durable run",
                                                 key=f"machine_save_{spec.machine_id}"):
                try:
                    saved = store.create_run(
                        project.project_id, material.material_id, spec.machine_id,
                        envelope["input_snapshot"], status=result.get("status", "unknown"),
                        output_type=result.get("output_data_type", "unspecified"),
                        epistemic_type=result.get("output_data_type", "unspecified"),
                        result_data=result.get("results") or {}, warnings=result.get("warnings") or [],
                        validation_state=result.get("validation_status", "not_evaluated"),
                        model_identity=(result.get("provenance") or {}).get("model", {}),
                        environment_identity=(result.get("results") or {}).get(
                            "runtime_availability", {}),
                        evidence_identity=(result.get("provenance") or {}).get("evidence", [])
                        if isinstance((result.get("provenance") or {}).get("evidence", []), list)
                        else [],
                    )
                    store.set_active_context(project.project_id, material.material_id, saved.run_id)
                    st.success(f"Saved {saved.run_id}")
                    st.rerun()
                except workspace_store.WorkspaceStoreError as exc:
                    st.error(str(exc))
            with st.expander("Show all warnings", expanded=False):
                _render_items(warnings)
            with st.expander("Show provenance", expanded=False):
                st.json(result.get("provenance") or {})
                st.caption(f"Input SHA-256: {envelope.get('input_hash')}")
                st.markdown("**Required provenance**")
                _render_items(spec.provenance_requirements)
            with st.expander("Validation details", expanded=False):
                st.write(f"Recorded state: {friendly_status(result.get('validation_status'))}")
                _render_items(spec.validation_requirements)
                st.write(spec.real_world_verification_method)
            with st.expander("Raw result envelope", expanded=False):
                st.json(result)
            with st.expander("Technical execution details", expanded=False):
                st.json({"machine_id": spec.machine_id, "backend": spec.backend_binding,
                         "status": result.get("status"), "input_hash": envelope.get("input_hash")})

    with tabs[3]:
        if not project or not material:
            _empty("Project and material required", "Saved history is tied to its original context.")
        else:
            related = store.list_runs(project_id=project.project_id, material_id=material.material_id,
                                      machine_id=spec.machine_id)
            if related:
                _render_run_rows(store, list(reversed(related)), compact=True,
                                 key_prefix=f"machine_history_{spec.machine_id}")
            else:
                _empty("No related runs", "Saved results for this material will appear here.")


def _run_matches_filters(run, filters, stale):
    if filters.get("material") and run.material_id != filters["material"]:
        return False
    if filters.get("machine") and run.machine_id != filters["machine"]:
        return False
    if filters.get("status") and run.status != filters["status"]:
        return False
    if filters.get("epistemic") and run.epistemic_type != filters["epistemic"]:
        return False
    if filters.get("validation") and run.validation_state != filters["validation"]:
        return False
    if filters.get("freshness") == "current" and stale:
        return False
    if filters.get("freshness") == "historical" and not stale:
        return False
    if filters.get("date_from") and run.created_at[:10] < str(filters["date_from"]):
        return False
    return True


def _result_value_summary(result_data: dict) -> str | None:
    """Return only an explicitly stored scalar value/unit; never infer a result."""
    if not isinstance(result_data, dict):
        return None
    for key in ("value", "estimate", "mean", "result"):
        value = result_data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            unit = result_data.get("unit")
            return f"{friendly_status(key)}: {value}{f' {unit}' if unit else ''}"
    return None


def _render_run_rows(store, records, compact=False, *, key_prefix="run", allow_reopen=False):
    for index, record in enumerate(records):
        stale, reasons = store.run_staleness(record)
        spec = machines.get_virtual_lab_machine(record.machine_id)
        title = spec.display_name if spec else friendly_status(record.machine_id)
        with st.container(border=True):
            st.markdown(f"**{title}**")
            app_ui.render_epistemic_badge(record.epistemic_type)
            app_ui.render_status_badge("Historical / stale" if stale else "Current",
                                       "warning" if stale else "success")
            st.caption(f"Created {record.created_at}")
            value_summary = _result_value_summary(record.result_data)
            if value_summary:
                st.write(value_summary)
            if stale:
                st.warning("Historical result — the material has changed since this run.")
            elif record.warnings:
                st.warning(str(record.warnings[0]))
            if spec and spec.machine_id == machines.ICP_PROCESSOR:
                _render_icp_qc_summary(record.result_data.get("qc_summary"))
            if allow_reopen and st.button(
                    "Reopen this run", key=f"{key_prefix}_reopen_{record.run_id}_{index}"):
                store.set_active_context(record.project_id, record.material_id, record.run_id)
                st.success("Run context reopened without changing its original snapshot.")
                st.rerun()
            with st.expander("Result details", expanded=False):
                st.markdown(f"**Status:** {friendly_status(record.status)}  \n"
                            f"**Output:** {friendly_status(record.output_type)}  \n"
                            f"**Validation:** {friendly_status(record.validation_state)}")
                if record.warnings:
                    st.markdown("**All warnings**")
                    _render_items(record.warnings)
                if stale:
                    st.markdown("**Why this is historical**")
                    _render_items(reasons)
            if not compact:
                with st.expander("Show provenance", expanded=False):
                    st.json({"model": record.model_identity, "environment": record.environment_identity,
                             "evidence": record.evidence_identity,
                             "result_location": record.result_location,
                             "legacy_references": record.legacy_references})
                with st.expander("Validation details", expanded=False):
                    st.write(friendly_status(record.validation_state))
                with st.expander("Raw result data", expanded=False):
                    st.json(record.result_data)
                with st.expander("Technical details", expanded=False):
                    st.json({"model": record.model_identity,
                             "environment": record.environment_identity,
                             "run_id": record.run_id, "project_id": record.project_id,
                             "material_id": record.material_id, "machine_id": record.machine_id,
                             "input_hash": record.input_hash,
                             "input_snapshot": record.input_snapshot,
                             "material_snapshot_hash": record.material_snapshot_hash,
                             "material_revision": record.material_revision,
                             "composition_revision": record.composition_revision,
                             "assumption_revision": record.assumption_revision,
                             "output_type": record.output_type, "status": record.status,
                             "created_at": record.created_at,
                             "schema_version": record.schema_version})


def render_results_index(store, context) -> None:
    app_ui.render_page_header("Results", "Review current and historical results without mixing their evidence types.")
    project, _, _ = _record_names(store, context)
    if not project:
        _empty("Project required", "Select a project to inspect its result index.", "Projects",
               key="results_no_project")
        return
    records = store.list_runs(project_id=project.project_id)
    if not records:
        _empty("No durable results", "Legacy results remain available below; save a machine result "
               "to add it to this unified index.", "Machines", key="results_empty")
        return
    materials = {item.material_id: item.name for item in store.list_materials(project.project_id)}
    col1, col2, col3 = st.columns(3)
    material = col1.selectbox("Material", [None, *materials],
                              format_func=lambda item: "All materials" if item is None else materials[item],
                              key="results_filter_material")
    machine = col2.selectbox("Machine", [None, *machines.machine_ids()],
                             format_func=lambda item: "All machines" if item is None else
                             machines.get_virtual_lab_machine(item).display_name,
                             key="results_filter_machine")
    freshness = col3.selectbox("Freshness", ["all", "current", "historical"],
                               key="results_filter_freshness")
    statuses = sorted({item.status for item in records})
    epistemic = sorted({item.epistemic_type for item in records})
    validations = sorted({item.validation_state for item in records})
    with st.expander("More filters", expanded=False):
        col4, col5, col6 = st.columns(3)
        status = col4.selectbox("Status", [None, *statuses],
                                format_func=lambda item: "All statuses" if item is None
                                else friendly_status(item), key="results_filter_status")
        epistemic_filter = col5.selectbox(
            "Evidence type", [None, *epistemic],
            format_func=lambda item: "All types" if item is None else friendly_status(item),
            key="results_filter_epistemic")
        validation = col6.selectbox(
            "Validation state", [None, *validations],
            format_func=lambda item: "All states" if item is None else friendly_status(item),
            key="results_filter_validation")
        date_from = st.date_input("Created on or after", value=None, key="results_filter_date")
    filters = {"material": material, "machine": machine, "freshness": freshness,
               "status": status, "epistemic": epistemic_filter, "validation": validation,
               "date_from": date_from}
    visible = [item for item in reversed(records)
               if _run_matches_filters(item, filters, store.run_staleness(item)[0])]
    st.caption(f"{len(visible)} of {len(records)} project results")
    if visible:
        _render_run_rows(store, visible, key_prefix="results")
    else:
        _empty("No matching results", "Change filters; missing values are not interpreted as zero.")


def render_validation_header(store, context) -> None:
    app_ui.render_page_header("Validation & Uncertainty",
                              "Check measured data, QC, mapping, criteria, and uncertainty before making a claim.")
    app_ui.render_epistemic_badge("advisory_interpretation")
    app_ui.render_warning_panel(
        "Validation requires measured evidence",
        "A comparison is not validated unless its measured data pass QC, mapping is clear, and explicit criteria are met.",
        level="warning")


def render_validation_overview(store, context, legacy_run: str | None) -> None:
    if legacy_run:
        st.markdown("**Next step:** choose Import, Validate, Match, or Compare for the selected run.")
    else:
        st.markdown("**Next step:** create or select an experiment run before importing measured data.")
        if st.button("Open Projects", key="validation_open_projects", type="primary"):
            navigate("Projects")
    with st.expander("What counts as validation?", expanded=False):
        _render_items((
            "QC-eligible measured evidence is required.",
            "Measured and predicted records need an unambiguous mapping.",
            "The acceptance criterion must be stated before the claim.",
            "The measured comparison must meet that criterion.",
            "Simulation, literature, or a model metric alone does not validate a material.",
        ))


def render_evidence_header(store, context) -> None:
    app_ui.render_page_header("Evidence", "Find and review sources while keeping citations and review status attached.")
    app_ui.render_epistemic_badge("literature_evidence")
    _, material, _ = _record_names(store, context)
    if material and material.evidence_references:
        st.caption(f"{len(material.evidence_references)} reviewed reference(s) attached to the active material")
        with st.expander("Show material references", expanded=False):
            _render_items(material.evidence_references)
    else:
        st.caption("No reviewed reference is attached to the active material yet.")


def render_history(store, context) -> None:
    app_ui.render_page_header("Run History", "Reopen a saved run without changing its original inputs or evidence.")
    project, _, _ = _record_names(store, context)
    if not project:
        _empty("Project required", "Select a project to inspect run history.", "Projects",
               key="history_no_project")
        return
    records = list(reversed(store.list_runs(project_id=project.project_id)))
    if not records:
        _empty("No durable run history", "Prepare and save a supported machine result.", "Machines",
               key="history_empty")
        return
    _render_run_rows(store, records, key_prefix="history", allow_reopen=True)


def render_diagnostics(store, context) -> None:
    app_ui.render_page_header("Settings & Diagnostics", "Check tool availability, then open details only when needed.")
    diag = store.diagnostics()
    availability = _phreeqc_availability()
    app_ui.render_metric_cards([
        {"label": "Projects", "value": diag["projects"]},
        {"label": "Materials", "value": diag["materials"]},
        {"label": "Runs", "value": diag["runs"]},
        {"label": "PHREEQC", "value": "Configured" if availability.can_run else "Unavailable",
         "status": "success" if availability.can_run else "warning"},
    ])
    st.caption("Secret values are never displayed here.")
    if not availability.can_run:
        st.warning("PHREEQC is not configured; input preview remains available.")
    with st.expander("PHREEQC diagnostics", expanded=False):
        st.write(availability.message)
        st.json({"executable_configured": availability.executable_configured,
                 "executable_found": availability.executable_found,
                 "database_configured": availability.database_configured,
                 "database_found": availability.database_found,
                 "smoke_run": "not performed"})
        st.caption("Simulation availability is not experimental validation.")
    with st.expander("Storage and schema details", expanded=False):
        st.markdown(f"**Durable storage:** `{diag['storage_root']}`")
        st.write(f"Workspace schema: {diag['schema_version']}")
    with st.expander("Active durable context", expanded=False):
        st.json({key: value for key, value in diag.items() if key.startswith("active_")})
