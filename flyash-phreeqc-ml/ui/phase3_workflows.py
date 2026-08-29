"""Concise Phase 3 workflow UI over typed backend records.

Scientific calculations remain in package modules. This file only validates user-facing form
syntax, calls those authorities, presents summaries, and coordinates durable save/reopen actions.
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

import app_ui
from flyash_phreeqc_ml import phase3_artifacts, workspace_store
from flyash_phreeqc_ml.experiments import plan_generator, sustainability_score
from flyash_phreeqc_ml.instruments import virtual_lab_machine_runner as machine_runner
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines
from flyash_phreeqc_ml.literature import evidence_review, evidence_schema
from ui import phase3_icp, phase3_xrd


def _records(store, context):
    project = material = None
    try:
        if context.get("active_project_id"):
            project = store.get_project(context["active_project_id"])
        if context.get("active_material_id"):
            material = store.get_material(context["active_material_id"])
    except workspace_store.WorkspaceStoreError:
        return None, None
    return project, material


def _key(prefix: str, project, material) -> str:
    return f"phase3_{prefix}__{getattr(project, 'project_id', 'none')}__{getattr(material, 'material_id', 'none')}"


def _json_object(text: str, label: str) -> dict:
    value = json.loads(text or "{}")
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _json_list(text: str, label: str) -> list:
    value = json.loads(text or "[]")
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON list")
    return value


def _actor_reason(prefix: str) -> tuple[str, str]:
    actor = st.text_input("Researcher / reviewer", key=f"{prefix}_actor")
    reason = st.text_input("Save reason", key=f"{prefix}_reason")
    return actor, reason


def _artifact_source_identity(input_snapshot: dict) -> dict:
    return {
        "source_type": "user_supplied_form",
        "source_sha256": workspace_store.identity_hash(input_snapshot),
    }


def _reviewed_factor_evidence(
    store, evidence_id: str, *, project_id: str, material_id: str,
):
    """Load one literature-factor dependency through the strict evidence authority."""
    try:
        record = evidence_review.get_evidence(
            store,
            evidence_id,
            project_id=project_id,
            material_id=material_id,
        )
    except evidence_review.EvidenceReviewError as exc:
        raise workspace_store.WorkspaceStoreError(
            f"literature factor evidence failed strict domain/envelope validation: {exc}") \
            from exc
    if record.status != evidence_schema.REVIEW_REVIEWED:
        raise workspace_store.WorkspaceStoreError(
            "literature factor evidence must be explicitly reviewed for the active context")
    return record


def _reopened_envelope(store, context, machine_id: str, summary: str) -> dict | None:
    run_id = context.get("active_run_id")
    if not run_id:
        return None
    try:
        run = store.get_run(run_id)
        if run.machine_id != machine_id:
            return None
        record_types = {
            machines.EXPERIMENTAL_DESIGN: workspace_store.ARTIFACT_EXPERIMENT_PLAN,
            machines.SUSTAINABILITY: workspace_store.ARTIFACT_SUSTAINABILITY_SCREEN,
        }
        saved = phase3_artifacts.require_exact_advisory_run(
            store,
            run.run_id,
            project_id=str(context.get("active_project_id") or ""),
            material_id=str(context.get("active_material_id") or ""),
            machine_id=machine_id,
            record_type=record_types[machine_id],
        )
    except workspace_store.WorkspaceStoreError as exc:
        st.error(f"Saved artifact cannot be reopened safely: {exc}")
        return None
    run = saved.run
    input_snapshot = dict(run.input_snapshot)
    input_snapshot.pop("artifact_identities", None)
    return {
        "input_snapshot": input_snapshot,
        "reopened_run_id": run.run_id,
        "result": {
            "result_summary": summary,
            "results": dict(run.result_data.get("artifact_payload") or {}),
            "warnings": list(run.warnings),
            "assumptions": list((run.result_data.get("artifact_payload") or {}).get(
                "assumptions") or []),
            "provenance": {"reopened_run_id": run.run_id,
                           "artifact_identity": run.result_data.get("artifact_identity")},
        },
    }


def render_design_prepare(store, context) -> None:
    project, material = _records(store, context)
    if not project or not material:
        st.warning("Select an active project and material before planning experiments.")
        return
    st.markdown("**Build an advisory plan**")
    st.caption("Choose the CFA preset explicitly, or define every factor for another material.")
    mode_label = st.radio(
        "Plan mode",
        ("Generic user-defined factors", "Existing CFA leaching preset"),
        key=_key("design_mode", project, material),
        horizontal=True,
    )
    if mode_label == "Existing CFA leaching preset":
        st.warning("This preset assumes Class C fly ash and its documented NaOH leaching matrix.")
        date = st.text_input("Optional experiment date", key=_key("design_cfa_date", project, material))
        cap = st.number_input("Maximum runs", min_value=1, value=20, step=1,
                              key=_key("design_cfa_cap", project, material))
        payload = {
            "mode": "cfa_leaching_preset",
            "experiment_date": date or None,
            "max_run_count": int(cap),
        }
    else:
        goal = st.text_input("Research goal", key=_key("design_goal", project, material))
        col1, col2 = st.columns(2)
        replicates = col1.number_input("Replicates", min_value=1, value=1, step=1,
                                       key=_key("design_replicates", project, material))
        cap = col2.number_input("Maximum runs", min_value=1, value=24, step=1,
                                key=_key("design_cap", project, material))
        factors_text = st.text_area(
            "Factors and levels (JSON)", '{"temperature_C": [20, 40]}',
            key=_key("design_factors", project, material),
            help="Every factor needs a non-empty list. No level is invented.",
        )
        with st.expander("Fixed conditions, controls, and identifiers", expanded=False):
            fixed_text = st.text_area(
                "Fixed conditions (JSON)", "{}",
                key=_key("design_fixed", project, material))
            controls_text = st.text_area(
                "Optional controls (JSON list)", "[]",
                key=_key("design_controls", project, material),
                help='Example: [{"name":"blank","conditions":{"treatment":"none"}}]')
            prefix = st.text_input("Optional sample prefix", key=_key("design_prefix", project, material))
            date = st.text_input("Optional experiment date", key=_key("design_date", project, material))
        try:
            payload = {
                "mode": "generic_user_defined",
                "material_id": material.material_id,
                "goal": goal,
                "factors": _json_object(factors_text, "Factors"),
                "fixed_conditions": _json_object(fixed_text, "Fixed conditions"),
                "replicates": int(replicates),
                "max_run_count": int(cap),
                "controls": _json_list(controls_text, "Controls"),
                "sample_prefix": prefix or None,
                "experiment_date": date or None,
            }
        except (ValueError, json.JSONDecodeError) as exc:
            st.error(f"Plan input is not ready: {exc}")
            payload = None
    if st.button("Generate advisory plan", key=_key("design_generate", project, material),
                 type="primary"):
        if payload is None:
            return
        result = machine_runner.run_virtual_lab_machine(machines.EXPERIMENTAL_DESIGN, payload)
        if result.status == machine_runner.STATUS_MISSING_INPUTS:
            st.error(result.result_summary)
            return
        st.session_state[_key("design_result", project, material)] = {
            "input_snapshot": payload,
            "result": result.to_dict(),
        }
        st.success("Plan prepared. Review it under Results before saving.")


def render_design_results(store, context) -> None:
    project, material = _records(store, context)
    if not project or not material:
        st.warning("Select an active project and material.")
        return
    envelope = st.session_state.get(_key("design_result", project, material)) or _reopened_envelope(
        store, context, machines.EXPERIMENTAL_DESIGN,
        "Reopened immutable experiment plan with its exact saved inputs.")
    if not envelope:
        st.info("No current plan. Build one under Prepare.")
        return
    result = envelope["result"]
    app_ui.render_epistemic_badge("advisory_interpretation")
    st.write(result["result_summary"])
    rows = list((result.get("results") or {}).get("rows") or [])
    stats = result.get("results") or {}
    app_ui.render_metric_cards([
        {"label": "Runs", "value": stats.get("run_count", 0)},
        {"label": "Duplicate conditions removed", "value": stats.get("duplicate_conditions_removed", 0)},
        {"label": "Replicates", "value": stats.get("replicates", "Preset")},
    ])
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        frame = pd.DataFrame(rows)
        st.download_button("Download plan CSV", plan_generator.plan_to_csv(frame),
                           "experiment_plan.csv", "text/csv",
                           key=_key("design_csv", project, material))
        export_result = dict(stats)
        export_result["plan"] = frame
        st.download_button("Download plan JSON", plan_generator.plan_to_json(export_result),
                           "experiment_plan.json", "application/json",
                           key=_key("design_json", project, material))
    st.warning("Advisory plan only; measurement fields stay blank until physical experiments run.")
    with st.expander("Assumptions and technical identity", expanded=False):
        st.json({"assumptions": result.get("assumptions"),
                 "warnings": result.get("warnings"),
                 "input_hash": workspace_store.identity_hash(envelope["input_snapshot"])})
    if envelope.get("reopened_run_id"):
        st.success("Reopened from Run History; the saved plan is immutable.")
        return
    actor, reason = _actor_reason(_key("design_save", project, material))
    if st.button("Save immutable plan", key=_key("design_save_button", project, material)):
        try:
            saved = phase3_artifacts.save_advisory_artifact_run(
                store,
                project_id=project.project_id,
                material_id=material.material_id,
                record_type=workspace_store.ARTIFACT_EXPERIMENT_PLAN,
                machine_id=machines.EXPERIMENTAL_DESIGN,
                input_snapshot=envelope["input_snapshot"],
                payload=result["results"],
                source_identity=_artifact_source_identity(envelope["input_snapshot"]),
                creator=actor,
                reason=reason,
                warnings=result.get("warnings") or [],
                provenance=result.get("provenance") or {},
            )
            store.set_active_context(project.project_id, material.material_id, saved.run.run_id)
            st.success("Plan saved with exact input and artifact identities.")
        except workspace_store.WorkspaceStoreError as exc:
            st.error(str(exc))


def render_sustainability_prepare(store, context) -> None:
    project, material = _records(store, context)
    if not project or not material:
        st.warning("Select an active project and material before screening.")
        return
    st.markdown("**Prepare a transparent screening**")
    mode_label = st.radio(
        "Screen mode",
        ("User-supplied inventory", "Compatible experimental-condition proxy"),
        key=_key("sustain_mode", project, material),
        horizontal=True,
    )
    if mode_label == "User-supplied inventory":
        default = json.dumps([{
            "item": "Synthetic test process",
            "amount": 1,
            "amount_unit": "kg",
            "factor": None,
            "factor_unit": "kg CO2e/kg",
            "factor_source": "",
            "source_type": "user_assumption",
            "boundary": "",
        }], indent=2)
        rows_text = st.text_area(
            "Inventory rows (JSON)", default, height=260,
            key=_key("sustain_inventory", project, material),
            help="Missing factors remain missing. Every calculated factor needs units, source, and boundary.",
        )
        try:
            payload = {"mode": "user_inventory_screen",
                       "inventory_rows": _json_list(rows_text, "Inventory")}
        except (ValueError, json.JSONDecodeError) as exc:
            st.error(f"Inventory is not ready: {exc}")
            payload = None
    else:
        st.info("Use only supplied experimental rows with an explicit per-row eligibility field.")
        rows_text = st.text_area("Experimental rows (JSON)", "[]", height=220,
                                 key=_key("sustain_proxy", project, material))
        eligibility = st.text_input("Eligibility column", "validation_eligible",
                                    key=_key("sustain_eligibility", project, material))
        try:
            payload = {"mode": "experimental_condition_screening_proxy",
                       "rows": _json_list(rows_text, "Experimental rows"),
                       "eligibility_column": eligibility}
        except (ValueError, json.JSONDecodeError) as exc:
            st.error(f"Proxy input is not ready: {exc}")
            payload = None
    if st.button("Calculate screening", key=_key("sustain_calculate", project, material),
                 type="primary"):
        if payload is None:
            return
        result = machine_runner.run_virtual_lab_machine(machines.SUSTAINABILITY, payload)
        if result.status == machine_runner.STATUS_MISSING_INPUTS:
            st.error(result.result_summary)
            return
        st.session_state[_key("sustain_result", project, material)] = {
            "input_snapshot": payload,
            "result": result.to_dict(),
        }
        st.success("Screen prepared. Review included and missing rows under Results.")


def render_sustainability_results(store, context) -> None:
    project, material = _records(store, context)
    if not project or not material:
        st.warning("Select an active project and material.")
        return
    envelope = st.session_state.get(_key("sustain_result", project, material)) or _reopened_envelope(
        store, context, machines.SUSTAINABILITY,
        "Reopened immutable screening with its exact saved inventory and factors.")
    if not envelope:
        st.info("No current screen. Prepare one under Prepare.")
        return
    result = envelope["result"]
    data = result.get("results") or {}
    app_ui.render_epistemic_badge("advisory_interpretation")
    st.write(result["result_summary"])
    app_ui.render_metric_cards([
        {"label": "Included", "value": data.get("included_count", data.get("included_rows", 0))},
        {"label": "Missing factors", "value": len(data.get("missing_factors") or [])},
        {"label": "Blocked / excluded", "value": data.get("excluded_count", data.get("excluded_rows", 0))},
    ])
    rows = list(data.get("contributions") or data.get("scores") or [])
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    if data.get("missing_factors"):
        st.warning("Missing factors remain missing and are excluded from totals.")
        with st.expander("Missing-factor list", expanded=False):
            st.dataframe(pd.DataFrame(data["missing_factors"]), use_container_width=True,
                         hide_index=True)
    if data.get("totals"):
        st.markdown("**Totals by boundary and compatible unit**")
        st.dataframe(pd.DataFrame(data["totals"]), use_container_width=True, hide_index=True)
    if data.get("mode") == "user_inventory_screen":
        st.download_button("Download screen CSV", sustainability_score.inventory_to_csv(data),
                           "sustainability_screen.csv", "text/csv",
                           key=_key("sustain_csv", project, material))
        st.download_button("Download screen JSON", sustainability_score.inventory_to_json(data),
                           "sustainability_screen.json", "application/json",
                           key=_key("sustain_json", project, material))
    st.warning("Screening only; boundaries, missing items, and supplied sources determine the result.")
    with st.expander("Assumptions, sources, and technical identity", expanded=False):
        st.json({"assumptions": data.get("assumptions"), "limitations": data.get("limitations"),
                 "warnings": result.get("warnings"),
                 "input_hash": workspace_store.identity_hash(envelope["input_snapshot"])})
    if envelope.get("reopened_run_id"):
        st.success("Reopened from Run History; the saved screen is immutable.")
        return
    actor, reason = _actor_reason(_key("sustain_save", project, material))
    if st.button("Save immutable screen", key=_key("sustain_save_button", project, material)):
        try:
            evidence_records = []
            for evidence_id in data.get("evidence_ids") or []:
                evidence_records.append(_reviewed_factor_evidence(
                    store,
                    evidence_id,
                    project_id=project.project_id,
                    material_id=material.material_id,
                ))
            evidence = [phase3_artifacts.artifact_dependency(record)
                        for record in evidence_records]
            saved = phase3_artifacts.save_advisory_artifact_run(
                store,
                project_id=project.project_id,
                material_id=material.material_id,
                record_type=workspace_store.ARTIFACT_SUSTAINABILITY_SCREEN,
                machine_id=machines.SUSTAINABILITY,
                input_snapshot=envelope["input_snapshot"],
                payload=data,
                source_identity=_artifact_source_identity(envelope["input_snapshot"]),
                creator=actor,
                reason=reason,
                warnings=result.get("warnings") or [],
                provenance=result.get("provenance") or {},
                evidence_identity=evidence,
                dependency_artifacts=evidence_records,
            )
            store.set_active_context(project.project_id, material.material_id, saved.run.run_id)
            st.success("Screen saved with exact inventory, source, and boundary identities.")
        except workspace_store.WorkspaceStoreError as exc:
            st.error(str(exc))


def render_machine_prepare(store, context, machine_id: str) -> bool:
    """Render a typed Phase 3 Prepare surface; return True when the machine is handled."""
    if machine_id == machines.ICP_PROCESSOR:
        phase3_icp.render_prepare(store, context)
        return True
    if machine_id == machines.XRD_ADVISORY:
        phase3_xrd.render_prepare(store, context)
        return True
    if machine_id == machines.EXPERIMENTAL_DESIGN:
        render_design_prepare(store, context)
        return True
    if machine_id == machines.SUSTAINABILITY:
        render_sustainability_prepare(store, context)
        return True
    return False


def render_machine_results(store, context, machine_id: str) -> bool:
    """Render a typed Phase 3 Results surface; return True when the machine is handled."""
    if machine_id == machines.ICP_PROCESSOR:
        return phase3_icp.render_results(store, context)
    if machine_id == machines.XRD_ADVISORY:
        return phase3_xrd.render_results(store, context)
    if machine_id == machines.EXPERIMENTAL_DESIGN:
        render_design_results(store, context)
        return True
    if machine_id == machines.SUSTAINABILITY:
        render_sustainability_results(store, context)
        return True
    return False


def render_icp_validation_gate(store, context) -> None:
    phase3_icp.render_validation_gate(store, context)
