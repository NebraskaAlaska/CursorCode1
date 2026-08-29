"""Digital Lab / Virtual Instruments — the section that lists the instruments and runs the two
new safe modules (ICP data reduction, XRD advisory).

UI only. It renders the twelve-machine canonical contract through the deprecated instrument-registry
compatibility view (status · what each can do · required inputs ·
limitations), exposes the three cross-cutting **mode toggles** (validation / uncertainty / evidence)
on the shared per-run agent state, and provides hands-on demos for the ICP Data Processor and the
XRD Advisory module. It runs **no PHREEQC** and never executes anything — the simulation engine
stays on its existing confirmation-gated path; this section only reduces data the user provides and
plans measurements. Robust with no run selected.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import app_ui
from flyash_phreeqc_ml import units
from flyash_phreeqc_ml.instruments import (icp_processor as icp, instrument_registry as reg,
                                           lab_modes, virtual_lab_machines as machines,
                                           xrd_advisory as xrd)
from flyash_phreeqc_ml.simulation import phreeqc_executor

from .common import _render_next_step
from .state import get_agent_state

# A representative demo ICP table: measured (with dilution/blank/detection-limit) + predicted, so
# the residual table and the QC flags are both exercised. Synthetic demo values, clearly labelled.
_ICP_DEMO_ROWS = [
    {"row_id": "valid-ca", "sample_id": "L1", "element": "Ca", "concentration": 84, "unit": "mg/L",
     "dilution_factor": 10, "blank_value": 0.5, "detection_limit": 0.05,
     "measured_or_predicted": "measured"},
    {"row_id": "valid-si", "sample_id": "L1", "element": "Si", "concentration": 28, "unit": "mg/L",
     "dilution_factor": 10, "measured_or_predicted": "measured"},
    {"row_id": "pred-ca", "sample_id": "L1", "element": "Ca", "concentration": 21, "unit": "mM",
     "dilution_factor": 1.0,
     "measured_or_predicted": "predicted"},
    {"row_id": "pred-si", "sample_id": "L1", "element": "Si", "concentration": 9.5, "unit": "mM",
     "dilution_factor": 1.0,
     "measured_or_predicted": "predicted"},
    {"row_id": "below-dl", "sample_id": "L1", "element": "Sc", "concentration": 0.02, "unit": "ppb",
     "dilution_factor": 1.0, "detection_limit": 0.05,
     "measured_or_predicted": "measured"},
    {"row_id": "hard-invalid", "sample_id": "L2", "element": "Ca", "concentration": -1.0,
     "unit": "mg/L", "dilution_factor": 1.0, "measured_or_predicted": "measured"},
]

_CORRECTED_COLS = [
    "row_id", "sample_id", "element", "role", "supplied_concentration", "supplied_unit",
    "supplied_dilution_factor", "supplied_blank_value", "blank_correction_applied",
    "blank_corrected_value", "corrected_value", "value_mM", "supplied_detection_limit",
    "below_detection_limit", "below_blank", "conversion_id", "conversion_authority",
    "qc_display", "qc_status", "qc_codes", "qc_reasons", "validation_eligible", "resolutions",
]


# --------------------------------------------------------------------------- #
# Section
# --------------------------------------------------------------------------- #
def _render_digital_lab(selected_run: str | None, dev_mode: bool = False) -> None:
    app_ui.render_page_header(
        "Digital Lab / Virtual Instruments",
        "Scientifically-safe modules for simulation, data processing, interpretation, and "
        "measurement planning. A virtual instrument is a real simulation engine, a data-reduction "
        "workflow, a signal/pattern advisory, or a planning helper — never a faked lab readout.",
        eyebrow="Registry · ICP data processor · XRD advisory · lab modes")
    _render_next_step(selected_run)
    st.info("These modules process the data **you** provide and plan measurements. They do not "
            "simulate instrument physics, fabricate measured data, or run PHREEQC — the simulation "
            "engine stays behind its confirmation gate in the Assistant / Workspace.")

    state = get_agent_state(selected_run)
    _render_mode_toggles(state)
    _render_registry()
    st.divider()
    _render_icp_module()
    st.divider()
    _render_xrd_module()


# --------------------------------------------------------------------------- #
# Cross-cutting mode toggles (write the shared agent state)
# --------------------------------------------------------------------------- #
def _render_mode_toggles(state) -> None:
    with st.container(border=True):
        app_ui.section_header("Lab modes", "design + state support — never fake certainty")
        c1, c2, c3 = st.columns(3)
        state.validation_mode = c1.checkbox(
            "Validation mode", value=bool(getattr(state, "validation_mode", False)),
            key="lab_validation_mode",
            help="Only a comparison against MEASURED data counts as validation; a simulation alone "
                 "is never labelled 'validated'.")
        state.uncertainty_mode = c2.checkbox(
            "Uncertainty / sensitivity mode", value=bool(getattr(state, "uncertainty_mode", False)),
            key="lab_uncertainty_mode",
            help="Suggests which variables to vary and re-run — no fabricated statistical certainty.")
        state.evidence_mode = c3.checkbox(
            "Evidence support", value=bool(getattr(state, "evidence_mode", False)),
            key="lab_evidence_mode",
            help="Prefer sourced literature / measured benchmarks (the Evidence Library).")

        if state.validation_mode:
            verdict = lab_modes.assess_validation(
                has_measured=False, has_simulation=bool(getattr(state, "has_results", False)))
            st.caption("🔬 Validation: " + verdict.note)
        if state.uncertainty_mode:
            variables = ", ".join(lab_modes.sensitivity_variables(getattr(state, "domain", None)))
            st.caption("🎚️ Sensitivity variables to vary: " + variables)
            st.caption(lab_modes.UNCERTAINTY_DISCLAIMER)
        if state.evidence_mode:
            st.caption("📚 " + lab_modes.evidence_note())


# --------------------------------------------------------------------------- #
# Instrument registry
# --------------------------------------------------------------------------- #
def _render_registry() -> None:
    app_ui.section_header("Instrument registry",
                          "12 canonical machines · one metadata authority · current layout")
    instruments = reg.all_instruments()
    cols = st.columns(2)
    for i, spec in enumerate(instruments):
        with cols[i % 2]:
            with st.container(border=True):
                st.markdown(f"**{spec.display_name}**")
                app_ui.render_status_badge(spec.readiness_label(), spec.readiness_badge())
                runtime = _runtime_availability(spec)
                if runtime is not None and not runtime.available:
                    app_ui.render_status_badge("Runtime configuration unavailable", "warning")
                st.caption(spec.short_description)
                st.markdown("**Needs:** " + ", ".join(spec.required_inputs))
                if spec.output_types:
                    st.caption("Outputs: " + ", ".join(spec.output_types))
                st.caption("Epistemic type(s): " + ", ".join(spec.output_data_type))
                with st.expander("Limitations & safety"):
                    for lim in spec.limitations:
                        st.markdown(f"- {lim}")
                    for note in spec.safety_notes:
                        st.caption("🛡️ " + note)
                    st.caption(f"Machine ID: `{spec.instrument_id}`")
                    st.caption(f"Maturity: `{spec.maturity}` · Mode: `{spec.mode}` · "
                               f"Execution: `{spec.execution_mode}`")
                    st.caption(f"Backend: `{spec.backend_binding}`")
                    st.caption("Runtime requirements: " + "; ".join(spec.runtime_requirements))
                    if runtime is not None:
                        for blocker in runtime.blockers:
                            st.caption("⚠️ " + blocker)


def _runtime_availability(spec) -> machines.MachineRuntimeAvailability | None:
    """Current-computer checks stay outside immutable catalogue metadata."""
    if spec.instrument_id != machines.PHREEQC_LEACHING:
        return None
    availability = phreeqc_executor.check_availability(run_smoke=False)
    available = bool(availability.can_run and availability.environment_identity is not None)
    return machines.MachineRuntimeAvailability(
        machine_id=machines.PHREEQC_LEACHING,
        available=available,
        status="available" if available else "runtime_configuration_unavailable",
        blockers=() if available else (availability.message,),
        checked_by="simulation.phreeqc_executor.check_availability(run_smoke=False)",
    )


# --------------------------------------------------------------------------- #
# ICP Data Processor
# --------------------------------------------------------------------------- #
def _render_icp_module() -> None:
    app_ui.section_header("ICP Data Processor", "measured / predicted concentration data — not the plasma")
    st.caption("ℹ️ " + icp.PLASMA_EXPLANATION)

    st.markdown("**Quick convert** — one value to mM (dilution-corrected)")
    q1, q2, q3, q4 = st.columns([1.2, 1, 1, 1])
    element = q1.selectbox("Element", icp.SUPPORTED_ELEMENTS, key="lab_icp_el")
    value = q2.number_input("Value", min_value=0.0, value=84.0, step=1.0, key="lab_icp_val")
    unit = q3.selectbox("Unit", [units.UNIT_MGL, units.UNIT_PPM, units.UNIT_PPB, units.UNIT_MM],
                        key="lab_icp_unit")
    dil = q4.number_input("Dilution ×", min_value=0.0, value=1.0, step=1.0, key="lab_icp_dil")
    quick = icp.process([{"sample_id": "quick", "element": element, "concentration": value,
                          "unit": unit, "dilution_factor": dil,
                          "measured_or_predicted": "measured"}])
    qrow = quick.corrected[0]
    if qrow.value_mM is not None:
        st.success(f"{value:g} {unit} {element} (×{dil:g}) = **{qrow.value_mM:.4g} mM** "
                   f"· conversion `{qrow.conversion_id}`")
    else:
        st.warning("; ".join(qrow.warnings) or "could not convert this value.")

    st.markdown("**QC work table** — editable synthetic examples. Invalid and censored rows stay "
                "visible but cannot enter a residual:")
    base_rows = st.session_state.get("lab_icp_resolved_rows", _ICP_DEMO_ROWS)
    edited = st.data_editor(
        pd.DataFrame(base_rows), num_rows="dynamic", use_container_width=True,
        hide_index=True, key="lab_icp_rows_editor")
    rows = edited.to_dict("records")

    with st.expander("Resolve a reviewable metadata issue", expanded=False):
        st.caption("Only unit, dilution, role, element, or sample ID can be corrected here. "
                   "Negative/non-finite concentrations, invalid blanks, and invalid detection "
                   "limits cannot be approved anyway.")
        row_ids = [str(r.get("row_id") or "") for r in rows if str(r.get("row_id") or "")]
        if row_ids:
            c1, c2 = st.columns(2)
            selected_id = c1.selectbox("Row ID", row_ids, key="lab_icp_resolution_row")
            field = c2.selectbox("Reviewable field",
                                 ["dilution_factor", "unit", "role", "element", "sample_id"],
                                 key="lab_icp_resolution_field")
            replacement = c1.text_input("Corrected/replacement value",
                                        key="lab_icp_resolution_value")
            resolved_by = c2.text_input("Resolved by", value="user",
                                        key="lab_icp_resolution_by")
            reason = st.text_input("Reason / evidence", key="lab_icp_resolution_reason")
            if st.button("Apply explicit QC correction", key="lab_icp_resolution_apply"):
                try:
                    rows = [
                        icp.resolve_reviewable_issue(
                            row, field=field, replacement_value=replacement,
                            resolved_by=resolved_by, reason=reason)
                        if str(row.get("row_id")) == selected_id else row
                        for row in rows
                    ]
                    st.session_state["lab_icp_resolved_rows"] = rows
                    st.success("Correction recorded with original and replacement provenance.")
                except icp.QcResolutionError as exc:
                    st.error(str(exc))
        else:
            st.caption("Add a stable row_id before recording a correction.")

    result = icp.process(rows)
    _render_icp_result(result)


def _render_icp_result(result) -> None:
    st.markdown("**Processed concentrations and QC eligibility**")
    rows = []
    for corrected in result.corrected:
        record = corrected.to_dict()
        record["qc_display"] = icp.qc_display_label(corrected.qc_status)
        record["qc_codes"] = " | ".join(corrected.qc_codes)
        record["qc_reasons"] = " | ".join(corrected.qc_reasons)
        rows.append({c: record.get(c) for c in _CORRECTED_COLS})
    st.dataframe(pd.DataFrame(rows, columns=_CORRECTED_COLS), use_container_width=True,
                 hide_index=True)
    st.download_button(
        "Download processed ICP QC table (CSV)",
        data=pd.DataFrame(result.corrected_export_table()).to_csv(index=False).encode("utf-8"),
        file_name="processed_icp_qc.csv", mime="text/csv", key="lab_icp_qc_download")
    summary = result.qc_summary()
    st.caption("QC status — " + " · ".join(
        f"{icp.qc_display_label(status)}: {summary[status]}" for status in icp.QC_STATUSES))
    if result.residuals:
        st.markdown("**Validation residuals** (measured − predicted)")
        st.dataframe(pd.DataFrame(result.residual_table()), use_container_width=True,
                     hide_index=True)
    else:
        st.caption("No measured + predicted pairs → no residual table (this is correct, not an error).")
    if result.warnings:
        with st.expander(f"QC warnings ({len(result.warnings)})"):
            for w in result.warnings:
                st.caption("⚠️ " + w)


# --------------------------------------------------------------------------- #
# XRD Advisory v2 — four user-facing modes (all advisory; UI calls the backend, owns no science).
# --------------------------------------------------------------------------- #
_XRD_MODES = {
    "Expected Peaks": xrd.MODE_EXPECTED_PEAKS,
    "Match Measured Peaks": xrd.MODE_MATCH_MEASURED,
    "PHREEQC Phase Checklist": xrd.MODE_PHREEQC_CHECKLIST,
    "Reference Data Notes": xrd.MODE_REFERENCE_NOTES,
}


def _render_xrd_module() -> None:
    app_ui.section_header("XRD Advisory / Pattern Planning",
                          "advisory & pattern-planning — never a measured identification")
    st.caption("ℹ️ " + xrd.EXPLANATION)
    # A flat radio (no nested expanders — the app previously had nested-expander issues).
    label = st.radio("XRD mode", list(_XRD_MODES), horizontal=True, key="lab_xrd_mode")
    mode = _XRD_MODES[label]
    if mode == xrd.MODE_EXPECTED_PEAKS:
        _render_xrd_expected()
    elif mode == xrd.MODE_MATCH_MEASURED:
        _render_xrd_match()
    elif mode == xrd.MODE_PHREEQC_CHECKLIST:
        _render_xrd_phreeqc_checklist()
    else:
        _render_xrd_reference_notes()


def _render_xrd_expected() -> None:
    names = xrd.reference_phase_names()
    selected = st.multiselect("Expected phases (reference dictionary)", names,
                              default=names[:3], key="lab_xrd_phases")
    extra = st.text_input("Other phases to check (comma-separated; flagged if no reference data)",
                          key="lab_xrd_extra")
    phases = list(selected) + [p.strip() for p in str(extra or "").split(",") if p.strip()]
    if not phases:
        st.caption("Pick one or more expected phases to see approximate reference peaks.")
        return
    _render_xrd_advisory(xrd.expected_peaks(phases))


def _render_xrd_match() -> None:
    st.caption("Enter MEASURED 2θ peaks to get TENTATIVE possible phases — never an identification.")
    c1, c2 = st.columns([3, 1])
    peaks_text = c1.text_input("Measured 2θ peaks (°, comma-separated)", value="26.6, 29.4, 34.1",
                               key="lab_xrd_meas")
    tol = c2.number_input("Tolerance ±° 2θ", min_value=0.05, max_value=1.0,
                          value=float(xrd.DEFAULT_MATCH_TOLERANCE_DEG), step=0.05, key="lab_xrd_tol")
    measured = [p.strip() for p in str(peaks_text or "").split(",") if p.strip()]
    result = xrd.match_measured_peaks(measured, tolerance=tol)
    if result.candidates:
        st.markdown("**Tentative candidate phases** (possible matches only — not identifications)")
        st.dataframe(pd.DataFrame(result.candidate_table()), use_container_width=True,
                     hide_index=True)
    else:
        st.warning("No tentative candidates in the internal reference set for these peaks.")
    if result.unmatched_measured:
        st.caption("Measured peaks with no internal candidate: "
                   + ", ".join(f"{x:g}" for x in result.unmatched_measured))
    with st.container(border=True):
        st.markdown("**" + result.wording_note + "**")
        for w in result.warnings:
            st.caption("⚠️ " + w)
        st.caption(result.disclaimer)


def _render_xrd_phreeqc_checklist() -> None:
    st.caption("PHREEQC suggests phases that MAY be worth checking by XRD — a saturation prediction "
               "is not XRD validation.")
    phases_text = st.text_input("PHREEQC-predicted / saturated phases (comma-separated)",
                                value="Calcite, Portlandite, Ettringite", key="lab_xrd_pqphases")
    names = [p.strip() for p in str(phases_text or "").split(",") if p.strip()]
    if not names:
        st.caption("Enter the phases PHREEQC predicted (or that reached saturation).")
        return
    _render_xrd_advisory(xrd.phases_to_check_from_predicted(names))


def _render_xrd_reference_notes() -> None:
    notes = xrd.reference_data_notes()
    st.caption("📐 " + notes["note"])
    st.markdown(f"**Covered phases ({notes['covered_count']})** — internal approximate references:")
    st.dataframe(pd.DataFrame(notes["covered_phases"]), use_container_width=True, hide_index=True)
    st.markdown("**Needs external reference data** (CIF / ICDD PDF / a library such as pymatgen, later):")
    st.dataframe(pd.DataFrame({"phase (no internal peaks)": notes["needs_external_reference"]}),
                 use_container_width=True, hide_index=True)
    st.caption("🔭 " + notes["future_work"])


def _render_xrd_advisory(advisory) -> None:
    table = [{"phase": e["phase"], "formula": e["formula"], "status": e["status"],
              "approx 2θ (°)": ", ".join(str(x) for x in e["approx_2theta"]) or "—",
              "label": e["label"], "note": e.get("note", "")} for e in advisory.checklist]
    st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)
    st.caption("📐 " + advisory.peak_basis)
    # Bordered container (NOT an expander) to avoid nested-expander incompatibility.
    with st.container(border=True):
        st.caption("Warnings · disclaimer (overlap · amorphous · preferred orientation · confirm "
                   "with reference)")
        for w in advisory.warnings:
            st.caption("⚠️ " + w)
        st.markdown("**" + advisory.disclaimer + "**")


render = _render_digital_lab
