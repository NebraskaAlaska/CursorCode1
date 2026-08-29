"""Virtual LAB — the **executable machine runner** (safe, limited backend workflows; no UI, no exec).

This is a limited coordination layer on top of the authoritative machine contract
(:mod:`flyash_phreeqc_ml.instruments.virtual_lab_machines`). It runs small, honest workflows over
**user-provided** inputs and returns a standard, self-describing result. It does **not** import
Streamlit, does **not** execute PHREEQC, does **not** call external APIs, and is **not** wired into
the live website.

Every result carries the standard fields ``machine_id, status, output_data_type, result_summary,
results, warnings, missing_inputs, assumptions, provenance, validation_status,
can_be_used_for_validation_claim`` — so a caller can always tell *what kind of data* a result is and
whether it could ever back a validation claim (only a measured comparison can).

Hard safety properties (mirroring the blueprint, never weakening it):

* It never fabricates measured data — ICP/SEM-EDS/TGA/DSC/Mechanical process only the rows you supply;
  XRD never invents peaks; FTIR matches user peaks to *broad advisory* group regions only.
* PHREEQC is capability/routing only here — input authoring, review, confirmation, and execution
  remain exclusively in the Phase 1A-reviewed PHREEQC modules.
* A ``validated_result`` is possible **only** from the Validation machine **with** measured data
  **and** explicit criteria that are met; everything else is advisory / estimate / processed data.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from . import virtual_lab_machines as vlm

# Reuse the blueprint's honest output-data-type vocabulary.
OUT_USER_PROVIDED_ASSUMPTION = vlm.OUT_USER_PROVIDED_ASSUMPTION
OUT_LITERATURE_EVIDENCE = vlm.OUT_LITERATURE_EVIDENCE
OUT_MEASURED_LAB_DATA = vlm.OUT_MEASURED_LAB_DATA
OUT_SIMULATED_MODEL_ESTIMATE = vlm.OUT_SIMULATED_MODEL_ESTIMATE
OUT_ML_PREDICTION = vlm.OUT_ML_PREDICTION
OUT_ADVISORY_INTERPRETATION = vlm.OUT_ADVISORY_INTERPRETATION
OUT_VALIDATED_RESULT = vlm.OUT_VALIDATED_RESULT

# Run statuses (the runner's lifecycle / outcome vocabulary).
STATUS_PROCESSED = "processed"
STATUS_ADVISORY = "advisory"
STATUS_PREVIEW_REQUIRED = "preview_required"
STATUS_AWAITING_CONFIRMATION = "awaiting_confirmation"
STATUS_CONFIRMED_NOT_EXECUTED = "confirmed_not_executed"
STATUS_MISSING_INPUTS = "missing_inputs"
STATUS_TRAINED_MODEL_REQUIRED = "trained_model_required"
STATUS_REFERENCE_DATA_NEEDED = "reference_data_needed"
STATUS_CANDIDATE_EVIDENCE = "candidate_evidence"
STATUS_UNKNOWN_MACHINE = "unknown_machine"

# validation_status vocabulary.
VAL_NOT_APPLICABLE = "not_applicable"
VAL_NO_MEASURED_DATA = "no_measured_data"
VAL_COMPARISON_AVAILABLE = "comparison_available"
VAL_INSUFFICIENT_DATA = "insufficient_data"
VAL_VALIDATED = "validated_against_measured_data"


# --------------------------------------------------------------------------- #
# Request / Result
# --------------------------------------------------------------------------- #
@dataclass
class VirtualLabMachineRequest:
    """A request to run one machine over a user-provided ``payload`` (``confirm`` gates PHREEQC)."""

    machine_id: str
    payload: dict = field(default_factory=dict)
    confirm: bool = False


@dataclass
class VirtualLabMachineResult:
    """A standard, self-describing machine result. ``output_data_type`` is the honest epistemic label;
    ``can_be_used_for_validation_claim`` is only ever True for a measured-backed validated result."""

    machine_id: str | None
    status: str
    output_data_type: str
    result_summary: str
    results: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    missing_inputs: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    validation_status: str = VAL_NOT_APPLICABLE
    can_be_used_for_validation_claim: bool = False

    def to_dict(self) -> dict:
        return {
            "machine_id": self.machine_id,
            "status": self.status,
            "output_data_type": self.output_data_type,
            "result_summary": self.result_summary,
            "results": self.results,
            "warnings": list(self.warnings),
            "missing_inputs": list(self.missing_inputs),
            "assumptions": list(self.assumptions),
            "provenance": dict(self.provenance),
            "validation_status": self.validation_status,
            "can_be_used_for_validation_claim": self.can_be_used_for_validation_claim,
        }


def _result(machine_id, status, output_data_type, result_summary, **kw) -> VirtualLabMachineResult:
    """Build a canonical result and attach the stable backend binding when known."""
    canonical = vlm.canonical_machine_id(machine_id)
    provenance = dict(kw.pop("provenance", {}) or {})
    if canonical is not None:
        spec = vlm.get_virtual_lab_machine(canonical)
        provenance.setdefault("canonical_machine_id", canonical)
        provenance.setdefault("backend_binding", spec.backend_binding if spec else "")
    return VirtualLabMachineResult(machine_id=canonical, status=status,
                                   output_data_type=output_data_type, result_summary=result_summary,
                                   provenance=provenance, **kw)


# --------------------------------------------------------------------------- #
# Input contracts (runner-side) + validation helpers
# --------------------------------------------------------------------------- #
_INPUT_SPEC = {
    vlm.PHREEQC_LEACHING: ("composition", "leachant", "source_term", "database", "temperature",
                           "liquid_solid_ratio"),
    vlm.ICP_PROCESSOR: ("rows",),
    vlm.FTIR_RAMAN: ("peaks",),
    vlm.SEM_EDS: ("rows",),
    vlm.MECHANICAL: ("rows",),
    vlm.LITERATURE_ENGINE: ("rows",),
    vlm.SUSTAINABILITY: ("assumptions",),
    vlm.EXPERIMENTAL_DESIGN: ("goal",),
    vlm.ML_SURROGATE: ("model", "features"),
}
_INPUT_DESCRIPTIONS = {
    "composition": "material composition (oxide or elemental)",
    "leachant": "leachant and its concentration",
    "source_term": "release model / source-term assumption",
    "database": "thermodynamic database name",
    "temperature": "temperature in °C",
    "liquid_solid_ratio": "liquid/solid ratio",
    "rows": "a list of data rows",
    "peaks": "a list of measured peak positions you obtained",
    "phases": "a list of phase names",
    "measured_peaks": "a list of measured 2θ peaks you obtained",
    "predicted_phases": "PHREEQC-predicted phase names",
    "tga": "TGA temperature + mass arrays",
    "dsc": "DSC temperature + heat-flow arrays",
    "assumptions": "your energy / reagent / transport / CO2 / cost assumptions",
    "goal": "your research goal",
    "model": "an approved trained model + feature schema + provenance",
    "measured": "measured values",
    "predicted": "predicted / simulated values",
}


def _missing_inputs(machine_id, payload) -> list:
    """Return the missing required inputs for ``machine_id`` given ``payload`` (empty == complete)."""
    p = payload or {}
    if machine_id == vlm.XRD_ADVISORY:
        if not any(p.get(k) for k in ("phases", "measured_peaks", "predicted_phases")):
            return ["one of: phases, measured_peaks, predicted_phases"]
        return []
    if machine_id == vlm.TGA_DSC:
        if not (p.get("tga") or p.get("dsc")):
            return ["one of: tga, dsc (each with arrays)"]
        return []
    if machine_id == vlm.VALIDATION_UNCERTAINTY:
        return [k for k in ("measured", "predicted") if not p.get(k)]
    return [k for k in _INPUT_SPEC.get(machine_id, ()) if not p.get(k)]


def validate_machine_inputs(machine_id, payload) -> list:
    """Public: the missing required inputs for a machine (``['unknown machine_id']`` if unknown)."""
    canonical = vlm.canonical_machine_id(machine_id)
    if canonical not in _RUNNERS:
        return ["unknown machine_id"]
    return _missing_inputs(canonical, payload)


def explain_missing_inputs(machine_id, payload) -> list:
    """Public: human-readable 'provide X — description' lines for each missing input."""
    out = []
    for m in validate_machine_inputs(machine_id, payload):
        desc = _INPUT_DESCRIPTIONS.get(m)
        out.append(f"Provide {m}" + (f" — {desc}" if desc else ""))
    return out


def get_machine_result_label(machine_id) -> str | None:
    """Public: the machine's primary honest output_data_type (from the blueprint), or ``None``."""
    spec = vlm.get_virtual_lab_machine(machine_id)
    return spec.output_data_type[0] if (spec and spec.output_data_type) else None


# --------------------------------------------------------------------------- #
# Small numeric helpers (pure stdlib)
# --------------------------------------------------------------------------- #
def _num(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _floats(seq):
    return [v for v in (_num(x) for x in (seq or [])) if v is not None]


# --------------------------------------------------------------------------- #
# 1. PHREEQC Leaching Simulator — capability/routing only (never authors or executes here).
# --------------------------------------------------------------------------- #
def run_phreeqc_leaching(payload, confirm: bool = False) -> VirtualLabMachineResult:
    from ..simulation import phreeqc_executor

    p = payload or {}
    missing = _missing_inputs(vlm.PHREEQC_LEACHING, p)
    base_warn = ["A simulation is an estimate under your assumptions, never a measurement.",
                 "PHREEQC results require a MEASURED pH / ICP leachate comparison for validation."]
    availability = phreeqc_executor.check_availability(run_smoke=False)
    runtime = vlm.MachineRuntimeAvailability(
        machine_id=vlm.PHREEQC_LEACHING,
        available=bool(availability.can_run and availability.environment_identity is not None),
        status="available" if availability.can_run else "runtime_configuration_unavailable",
        blockers=() if availability.can_run else (availability.message,),
        checked_by="simulation.phreeqc_executor.check_availability(run_smoke=False)",
    )
    if missing:
        return _result(vlm.PHREEQC_LEACHING, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
                       "Required top-level PHREEQC inputs are missing. Build the authoritative preview "
                       "in the Assistant/Workspace after completing them.",
                       missing_inputs=missing, warnings=base_warn,
                       results={"executed": False, "auto_run": False,
                                "runtime_availability": runtime.to_dict(),
                                "dispatch_target": "Assistant/Workspace PHREEQC workflow"},
                       provenance={"inputs": "user_provided",
                                   "input_builder": "simulation.phreeqc_input_builder",
                                   "run_contract": "simulation.phreeqc_run_contract",
                                   "executor": "simulation.phreeqc_executor (not invoked)"},
                       validation_status=VAL_NO_MEASURED_DATA)

    assumptions = [f"release / source term: {p.get('source_term')}",
                   f"temperature: {p.get('temperature')} °C",
                   f"liquid/solid ratio: {p.get('liquid_solid_ratio')}"]
    if confirm:
        base_warn.append("The generic runner's confirm boolean is not Phase 1A review/confirmation "
                         "evidence and was ignored; no gate was satisfied.")
    if not runtime.available:
        base_warn.append("PHREEQC runtime is unavailable on this computer; no execution was attempted.")
    return _result(
        vlm.PHREEQC_LEACHING, STATUS_PREVIEW_REQUIRED, OUT_ADVISORY_INTERPRETATION,
        "Use the existing Assistant/Workspace PHREEQC workflow to build the authoritative preview, "
        "review it, capture the exact execution environment, and confirm it. This runner authors no "
        "PHREEQC input and executes nothing.",
        results={"executed": False, "auto_run": False, "preview": None,
                 "runtime_availability": runtime.to_dict(),
                 "dispatch_target": "Assistant/Workspace PHREEQC workflow"},
        warnings=base_warn + ["No simulation was run; no results were produced."],
        assumptions=assumptions,
        provenance={"inputs": "user_provided",
                    "input_builder": "simulation.phreeqc_input_builder",
                    "run_contract": "simulation.phreeqc_run_contract",
                    "executor": "simulation.phreeqc_executor (not invoked)"},
        validation_status=VAL_NO_MEASURED_DATA)


# --------------------------------------------------------------------------- #
# 2. XRD Advisory — delegate to the repo's advisory; never invent / identify.
# --------------------------------------------------------------------------- #
def run_xrd_advisory(payload, confirm: bool = False) -> VirtualLabMachineResult:
    from . import xrd_advisory as xrd  # lazy: keep the runner import ultra-light

    p = payload or {}
    missing = _missing_inputs(vlm.XRD_ADVISORY, p)
    if missing:
        return _result(vlm.XRD_ADVISORY, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
                       "Provide phase names, your measured 2θ peaks, or PHREEQC-predicted phases.",
                       missing_inputs=missing, provenance={"inputs": "user_provided"})

    results: dict = {}
    warnings = ["XRD here is advisory / pattern-planning — never a measured phase identification."]

    if p.get("phases"):
        adv = xrd.expected_peaks(list(p["phases"]))
        results["expected_peaks"] = adv.checklist
        results["disclaimer"] = adv.disclaimer
        warnings.extend(adv.warnings)
    if p.get("predicted_phases"):
        adv = xrd.phases_to_check_from_predicted(list(p["predicted_phases"]))
        results["phreeqc_phase_checklist"] = adv.checklist
        warnings.extend(adv.warnings)
    if p.get("measured_peaks"):
        # Accept measured peaks as USER-PROVIDED only — no matching/identification in this phase.
        results["measured_peaks_user_provided"] = _floats(p["measured_peaks"])
        warnings.append("Measured 2θ peaks are recorded as USER-PROVIDED; no matching or "
                        "identification is performed in this phase (it would need reference data).")

    return _result(vlm.XRD_ADVISORY, STATUS_ADVISORY, OUT_ADVISORY_INTERPRETATION,
                   "Advisory XRD planning prepared from the repo's reference data — confirm any phase "
                   "against measured XRD + a reference database.",
                   results=results, warnings=warnings,
                   provenance={"inputs": "user_provided", "engine": "xrd_advisory (repo reference)"})


# --------------------------------------------------------------------------- #
# 3. ICP Data Processor — delegate to the audited ICP processor; no fabrication.
# --------------------------------------------------------------------------- #
def _classify_icp_result(processed, declared_source, icp):
    """Derive the table-level epistemic type from processed row roles, never caller text.

    The Phase 1B processor owns role normalization and QC.  This runner only summarizes that
    authoritative output.  A mixed or role-unresolved table is an advisory interpretation; a table
    with no canonical row role is a user-provided assumption.  The optional top-level source remains
    provenance and can only weaken a contradictory result.
    """
    rows = list(processed.corrected)
    measured_count = sum(row.role == icp.MEASURED for row in rows)
    predicted_count = sum(row.role == icp.PREDICTED for row in rows)
    unresolved = [row for row in rows if row.role not in (icp.MEASURED, icp.PREDICTED)]
    qc_counts = processed.qc_summary()

    if unresolved:
        out_type = (OUT_ADVISORY_INTERPRETATION
                    if measured_count or predicted_count else OUT_USER_PROVIDED_ASSUMPTION)
    elif measured_count and predicted_count:
        out_type = OUT_ADVISORY_INTERPRETATION
    elif measured_count:
        out_type = OUT_MEASURED_LAB_DATA
    elif predicted_count:
        out_type = OUT_SIMULATED_MODEL_ESTIMATE
    else:
        out_type = OUT_USER_PROVIDED_ASSUMPTION

    declared_role = icp.canonical_role(declared_source)
    row_roles = {
        role for role, count in ((icp.MEASURED, measured_count), (icp.PREDICTED, predicted_count))
        if count
    }
    warnings = []
    contradiction = bool(declared_role and row_roles and row_roles != {declared_role})
    if contradiction:
        warnings.append(
            f"Top-level source {declared_source!r} contradicts the processed row roles "
            f"({', '.join(sorted(row_roles))}). Correct measured_or_predicted on the supplied "
            "rows or correct source provenance; source text cannot relabel row evidence."
        )
        out_type = OUT_ADVISORY_INTERPRETATION
    elif declared_source and not declared_role:
        warnings.append(
            f"Top-level source {declared_source!r} is not a recognized measured/predicted role. "
            "It was retained as provenance only; processed row roles determine the result type."
        )

    if unresolved:
        warnings.append(
            f"{len(unresolved)} processed row(s) have a missing or unknown role. Resolve each "
            "measured_or_predicted value through the Phase 1B QC workflow before relying on the "
            "table's epistemic type."
        )

    return out_type, {
        "measured_rows": measured_count,
        "predicted_rows": predicted_count,
        "unresolved_role_rows": len(unresolved),
        "canonical_roles_present": sorted(row_roles),
        "qc_status_counts": qc_counts,
        "declared_source_role": declared_role or None,
        "source_contradiction": contradiction,
        "authority": "processed_row_roles_and_qc",
    }, warnings


def run_icp_processor(payload, confirm: bool = False) -> VirtualLabMachineResult:
    from . import icp_processor as icp  # lazy import

    p = payload or {}
    missing = _missing_inputs(vlm.ICP_PROCESSOR, p)
    if missing:
        return _result(vlm.ICP_PROCESSOR, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
                       "Provide a list of concentration rows (element, value, unit).",
                       missing_inputs=missing,
                       warnings=[icp.PLASMA_EXPLANATION], provenance={"inputs": "user_provided"})

    res = icp.process(
        list(p["rows"]), duplicate_resolutions=p.get("duplicate_resolutions") or [])
    supplied_source = p.get("source")
    source = str(supplied_source or "").strip().lower()
    out_type, role_summary, role_warnings = _classify_icp_result(res, source, icp)
    corrected = res.corrected_table()
    row_type = {
        icp.MEASURED: OUT_MEASURED_LAB_DATA,
        icp.PREDICTED: OUT_SIMULATED_MODEL_ESTIMATE,
    }
    for row in corrected:
        row["row_output_data_type"] = row_type.get(
            row.get("role"), OUT_USER_PROVIDED_ASSUMPTION)

    has_residuals = bool(res.residuals)
    return _result(vlm.ICP_PROCESSOR, STATUS_PROCESSED, out_type,
                   "Processed your concentration rows (unit conversion, blank/dilution correction, "
                   "QC flags). It reduces data only — it does not simulate the plasma.",
                   results={"corrected": corrected, "residuals": res.residual_table(),
                            "qc_summary": res.qc_summary(),
                            "row_role_summary": role_summary,
                            "resolution_provenance": res.resolution_provenance},
                   warnings=[icp.PLASMA_EXPLANATION, *res.warnings, *role_warnings],
                   assumptions=[],
                   provenance={"inputs": "user_provided",
                               "declared_source": supplied_source if supplied_source is not None
                               else "unspecified",
                               "epistemic_authority": "processed_row_roles_and_qc",
                               "processor": "icp_processor"},
                   validation_status=VAL_COMPARISON_AVAILABLE if has_residuals else VAL_NOT_APPLICABLE)


# --------------------------------------------------------------------------- #
# 4. FTIR / Raman Interpreter — match user peaks to BROAD group regions (advisory).
# --------------------------------------------------------------------------- #
# Broad, textbook functional-group regions (approximate cm^-1). Advisory regions, NOT compound IDs;
# nothing here is a fabricated spectrum or a specific reference peak.
_IR_REGIONS = (
    ("O–H stretch (water / hydroxyl)", 3200, 3600),
    ("C–H stretch", 2850, 3000),
    ("C=O stretch (carbonyl)", 1650, 1750),
    ("H–O–H bend (molecular water)", 1620, 1660),
    ("CO3 asymmetric stretch (carbonate)", 1400, 1500),
    ("SO4 stretch (sulfate)", 1080, 1160),
    ("Si–O / Al–O asymmetric stretch", 950, 1100),
    ("CO3 out-of-plane bend (carbonate)", 850, 890),
    ("Si–O bend", 440, 530),
)


def run_ftir_raman(payload, confirm: bool = False) -> VirtualLabMachineResult:
    p = payload or {}
    missing = _missing_inputs(vlm.FTIR_RAMAN, p)
    if missing:
        return _result(vlm.FTIR_RAMAN, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
                       "Provide your measured peak positions (cm⁻¹).", missing_inputs=missing,
                       provenance={"inputs": "user_provided"})

    peaks = _floats(p["peaks"])
    technique = str(p.get("technique") or "ftir").lower()
    assignments, unmatched = [], []
    for pk in peaks:
        regions = [name for (name, lo, hi) in _IR_REGIONS if lo <= pk <= hi]
        if regions:
            assignments.append({"peak_cm1": pk, "candidate_regions": regions,
                                "label": "broad functional-group region (advisory)"})
        else:
            unmatched.append({"peak_cm1": pk, "status": STATUS_REFERENCE_DATA_NEEDED})

    warnings = ["These are BROAD functional-group regions (advisory), NOT a compound identification.",
                "Overlapping regions are ambiguous; confirm with reference bands + complementary "
                "methods (XRD/TGA) and expert review."]
    if technique == "raman":
        warnings.append("Raman shifts differ from these IR regions — treat the assignments as IR-based "
                        "advisory; a Raman reference set is not included.")
    return _result(vlm.FTIR_RAMAN, STATUS_ADVISORY, OUT_ADVISORY_INTERPRETATION,
                   "Advisory functional-group regions for your peaks — not a compound identification.",
                   results={"assignments": assignments, "unmatched_peaks": unmatched,
                            "technique": technique},
                   warnings=warnings, provenance={"inputs": "user_provided", "engine": "broad_ir_regions"})


# --------------------------------------------------------------------------- #
# 5. SEM-EDS Processor — summarise user-supplied elemental data; never infer phases.
# --------------------------------------------------------------------------- #
def run_sem_eds(payload, confirm: bool = False) -> VirtualLabMachineResult:
    p = payload or {}
    missing = _missing_inputs(vlm.SEM_EDS, p)
    if missing:
        return _result(vlm.SEM_EDS, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
                       "Provide measured EDS elemental rows (element, value).", missing_inputs=missing,
                       provenance={"inputs": "user_provided"})

    rows = list(p["rows"])
    elements, values, warnings = [], [], []
    for r in rows:
        el = str((r or {}).get("element") or "").strip()
        if el:
            elements.append(el)
        v = _num((r or {}).get("value"))
        if v is not None:
            values.append(v)
    summary = {"elements_present": sorted(set(e for e in elements if e))}
    if values:
        summary.update({"n_values": len(values), "min": min(values), "max": max(values),
                        "mean": round(statistics.fmean(values), 6)})
    else:
        warnings.append("No numeric EDS values supplied — only the element list is reported.")
    if not p.get("standards"):
        warnings.append("No calibration standards declared — EDS is SEMI-QUANTITATIVE; treat "
                        "quantities cautiously.")
    warnings.append("EDS gives ELEMENTS, not phases — exact mineral phase identification needs XRD + "
                    "standards + expert review.")

    out_type = OUT_MEASURED_LAB_DATA if str(p.get("source") or "").lower() == "measured" \
        else OUT_ADVISORY_INTERPRETATION
    return _result(vlm.SEM_EDS, STATUS_PROCESSED, out_type,
                   "Summarised your EDS elemental data (elements present + simple stats). No phases "
                   "were inferred and no images/maps were generated.",
                   results=summary, warnings=warnings,
                   provenance={"inputs": "user_provided", "declared_source": p.get("source") or "unspecified"})


# --------------------------------------------------------------------------- #
# 6. TGA / DSC Processor — simple metrics from supplied arrays; never fabricate curves.
# --------------------------------------------------------------------------- #
def run_tga_dsc(payload, confirm: bool = False) -> VirtualLabMachineResult:
    p = payload or {}
    missing = _missing_inputs(vlm.TGA_DSC, p)
    if missing:
        return _result(vlm.TGA_DSC, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
                       "Provide measured TGA (temperature + mass) and/or DSC (temperature + heat_flow).",
                       missing_inputs=missing, provenance={"inputs": "user_provided"})

    results: dict = {}
    warnings = ["Metrics are computed from YOUR supplied arrays — no curve is fabricated.",
                "Thermal-event attribution is advisory; confirm with complementary methods (XRD/FTIR)."]

    tga = p.get("tga") or {}
    mass = _floats(tga.get("mass"))
    temp_tga = _floats(tga.get("temperature"))
    if mass:
        total_loss = round(mass[0] - mass[-1], 8)
        results["tga"] = {"total_mass_loss": total_loss,
                          "percent_mass_loss": round(100.0 * total_loss / mass[0], 6) if mass[0] else None,
                          "n_points": len(mass)}
        if temp_tga and len(temp_tga) != len(mass):
            warnings.append("TGA temperature and mass arrays differ in length — temperature ignored.")
    elif tga:
        warnings.append("TGA block supplied without a usable 'mass' array — skipped.")

    dsc = p.get("dsc") or {}
    hf = _floats(dsc.get("heat_flow"))
    temp_dsc = _floats(dsc.get("temperature"))
    if hf:
        i_max, i_min = hf.index(max(hf)), hf.index(min(hf))
        ev = {"max_heat_flow": max(hf), "min_heat_flow": min(hf), "n_points": len(hf)}
        if temp_dsc and len(temp_dsc) == len(hf):
            ev["temp_at_max_heat_flow"] = temp_dsc[i_max]
            ev["temp_at_min_heat_flow"] = temp_dsc[i_min]
        else:
            warnings.append("DSC temperature array missing/mismatched — event temperatures omitted.")
        results["dsc"] = ev
    elif dsc:
        warnings.append("DSC block supplied without a usable 'heat_flow' array — skipped.")

    out_type = OUT_MEASURED_LAB_DATA if str(p.get("source") or "").lower() == "measured" \
        else OUT_ADVISORY_INTERPRETATION
    return _result(vlm.TGA_DSC, STATUS_PROCESSED, out_type,
                   "Computed simple thermal metrics from your supplied curves (no phase/reaction "
                   "assignment).", results=results, warnings=warnings,
                   provenance={"inputs": "user_provided", "declared_source": p.get("source") or "unspecified"})


# --------------------------------------------------------------------------- #
# 7. Mechanical Testing Processor — stats from supplied strengths only.
# --------------------------------------------------------------------------- #
def run_mechanical_testing(payload, confirm: bool = False) -> VirtualLabMachineResult:
    p = payload or {}
    missing = _missing_inputs(vlm.MECHANICAL, p)
    if missing:
        return _result(vlm.MECHANICAL, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
                       "Provide measured strength rows (sample_id, strength, curing_age_days).",
                       missing_inputs=missing, provenance={"inputs": "user_provided"})

    groups: dict = {}
    warnings = ["Statistics are computed ONLY from your supplied measured strengths — none are invented."]
    for r in list(p["rows"]):
        r = r or {}
        s = _num(r.get("strength"))
        if s is None:
            continue
        key = (str(r.get("sample_id") or "(unnamed)"), r.get("curing_age_days"))
        groups.setdefault(key, []).append(s)

    summary = []
    for (sample, age), vals in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        row = {"sample_id": sample, "curing_age_days": age, "n": len(vals),
               "mean_strength": round(statistics.fmean(vals), 6),
               "std_dev": round(statistics.stdev(vals), 6) if len(vals) > 1 else None}
        if len(vals) < 3:
            row["warning"] = "fewer than 3 replicates — spread is not reliable"
        summary.append(row)
    if not summary:
        warnings.append("No numeric strength values found in the supplied rows; no strength result "
                        "was produced.")
        return _result(
            vlm.MECHANICAL, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
            "Provide at least one measured row with a numeric strength value.",
            results={"by_sample_age": []}, warnings=warnings,
            missing_inputs=["rows with numeric strength"],
            provenance={"inputs": "user_provided"})
    if any(s["n"] < 3 for s in summary):
        warnings.append("Some groups have fewer than 3 replicates — add replicates for a meaningful spread.")
    warnings.append("Reports measured statistics only — it never claims code/standard compliance.")

    return _result(vlm.MECHANICAL, STATUS_PROCESSED, OUT_MEASURED_LAB_DATA,
                   "Computed mean / std-dev / count from your measured strength data.",
                   results={"by_sample_age": summary}, warnings=warnings,
                   provenance={"inputs": "user_provided_measured"})


# --------------------------------------------------------------------------- #
# 8. ML Surrogate Predictor — delegate only an approved trained model to ml_models.predict.
# --------------------------------------------------------------------------- #
def _ml_prediction_outputs_are_finite(prediction) -> bool:
    """Defense in depth: require a finite mean and finite optional uncertainty outputs."""
    value = getattr(prediction, "value", None)
    optional = (getattr(prediction, name, None) for name in ("lower", "upper", "sigma"))
    return _num(value) is not None and all(item is None or _num(item) is not None
                                            for item in optional)


def _invalid_ml_artifact_result(prediction_provenance, *, refusal_reason=None):
    """Return the common advisory envelope for an unusable approved-status artifact."""
    results = {}
    if refusal_reason:
        results = {"refusal_code": "invalid_model_output", "refusal_reason": refusal_reason}
    return _result(
        vlm.ML_SURROGATE, STATUS_TRAINED_MODEL_REQUIRED, OUT_ADVISORY_INTERPRETATION,
        "The model status appeared approved, but its persisted artifact was unusable or "
        "incompatible. No ML prediction was produced.",
        results=results,
        missing_inputs=["a usable fitted model artifact compatible with its feature schema"],
        warnings=["Prediction delegation failed closed. Re-train or re-select the model and "
                  "verify its model card and feature schema.",
                  "No exception detail or stack trace is included in this result."],
        provenance={**prediction_provenance, "model_artifact_usable": False,
                    "prediction_delegated": True},
        validation_status=VAL_NO_MEASURED_DATA)


def run_ml_surrogate(payload, confirm: bool = False) -> VirtualLabMachineResult:
    from ..ml_models import model_schema
    from ..ml_models import predict as prediction_backend

    p = payload or {}
    model = p.get("model")
    features = p.get("features")
    approved = bool(
        isinstance(model, model_schema.TrainedModel)
        and model.training_status == model_schema.TRAINING_DATA_APPROVED
        and model.validation_status != model_schema.VALIDATION_DEMO
        and not model.is_demo
    )
    if not approved:
        return _result(
            vlm.ML_SURROGATE, STATUS_TRAINED_MODEL_REQUIRED, OUT_ADVISORY_INTERPRETATION,
            "An approved trained model with recorded training provenance is required before any ML "
            "prediction — none was produced.",
            missing_inputs=["an approved trained model + feature schema + provenance"],
            warnings=["Demo, exploratory, legacy/unknown, missing, or malformed model artifacts cannot "
                      "produce an ordinary machine prediction.",
                      "A surrogate prediction is an experimental estimate, never accuracy or validation."],
            provenance={"inputs": "user_provided", "model_loaded": model is not None,
                        "model_approved": False, "model_status_appeared_approved": False,
                        "model_artifact_usable": None,
                        "prediction_delegated": False}, validation_status=VAL_NO_MEASURED_DATA)
    if not isinstance(features, dict) or not features:
        return _result(
            vlm.ML_SURROGATE, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
            "Provide model-compatible input features before prediction.",
            missing_inputs=["features"],
            warnings=["No prediction was produced."],
            provenance={"inputs": "user_provided", "model_loaded": True,
                        "model_approved": True, "model_status_appeared_approved": True,
                        "model_artifact_usable": None, "prediction_delegated": False,
                        "model_name": model.name, "model_version": model.version,
                        "training_data_status": model.training_status,
                        "source_of_model": model.source_type,
                        "n_training_rows": model.n_train}, validation_status=VAL_NO_MEASURED_DATA)

    prediction_provenance = {
        "inputs": "user_provided", "model_loaded": True, "model_approved": True,
        "model_status_appeared_approved": True, "model_artifact_usable": None,
        "model_name": model.name, "model_version": model.version,
        "training_data_status": model.training_status,
        "source_of_model": model.source_type, "n_training_rows": model.n_train,
    }
    try:
        prediction = prediction_backend.predict(model, features)
    except Exception:  # noqa: BLE001 - fail closed at the specialized artifact boundary
        return _invalid_ml_artifact_result(prediction_provenance)
    if (not prediction.refused and not _ml_prediction_outputs_are_finite(prediction)):
        return _invalid_ml_artifact_result(
            prediction_provenance,
            refusal_reason="The fitted model produced invalid non-finite prediction output.")
    if prediction.refused:
        if getattr(prediction, "refusal_code", None) == "invalid_model_output":
            return _invalid_ml_artifact_result(
                prediction_provenance, refusal_reason=prediction.refusal_reason)
        return _result(
            vlm.ML_SURROGATE, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
            "The approved model refused this incomplete or unsupported prediction request.",
            results={"refusal_code": getattr(prediction, "refusal_code", None),
                     "refusal_reason": prediction.refusal_reason},
            missing_inputs=list(prediction.missing_features), warnings=list(prediction.warnings),
            provenance={**prediction_provenance, "prediction_delegated": True},
            validation_status=VAL_NO_MEASURED_DATA)

    return _result(
        vlm.ML_SURROGATE, STATUS_PROCESSED, OUT_ML_PREDICTION,
        "Approved trained-model prediction produced with uncertainty and applicability warnings; "
        "it is an experimental estimate, not a measurement or validated result.",
        results={"target": prediction.target, "value": prediction.value,
                 "lower": prediction.lower, "upper": prediction.upper,
                 "sigma": prediction.sigma, "interval_method": prediction.interval_method,
                 "used_features": prediction.used_features,
                 "missing_features": prediction.missing_features,
                 "out_of_domain": prediction.out_of_domain,
                 "unseen_categories": prediction.unseen_categories},
        warnings=list(prediction.warnings),
        provenance={**prediction_provenance, "model_artifact_usable": True,
                    "prediction_delegated": True},
        validation_status=VAL_NO_MEASURED_DATA)


# --------------------------------------------------------------------------- #
# 9. Literature Evidence Engine — user metadata only; candidate / unreviewed.
# --------------------------------------------------------------------------- #
def run_literature_evidence(payload, confirm: bool = False) -> VirtualLabMachineResult:
    p = payload or {}
    missing = _missing_inputs(vlm.LITERATURE_ENGINE, p)
    if missing:
        return _result(vlm.LITERATURE_ENGINE, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
                       "Provide user-supplied evidence rows (title, DOI/URL, source_location, "
                       "extracted_value).", missing_inputs=missing, provenance={"inputs": "user_provided"})

    rows, warnings = [], ["No web scraping is performed — only your supplied metadata is recorded.",
                          "Candidate evidence is UNREVIEWED until a human checks it against the source.",
                          "Literature evidence is context, not validation of your sample.",
                          "Never scrapes restricted sources such as Google Scholar."]
    all_have_provenance = True
    for r in list(p["rows"]):
        r = r or {}
        provenance = r.get("doi") or r.get("url") or r.get("source_location")
        has_prov = bool(provenance)
        all_have_provenance = all_have_provenance and has_prov
        rows.append({"title": r.get("title"), "authors": r.get("authors"), "year": r.get("year"),
                     "provenance": provenance, "extracted_value": r.get("extracted_value"),
                     "confidence": r.get("confidence"), "has_provenance": has_prov,
                     "human_review_required": True, "review_status": "candidate_unreviewed"})
        if not has_prov:
            warnings.append(f"Row {r.get('title') or '(untitled)'!r} lacks provenance (DOI/URL/source).")

    out_type = OUT_LITERATURE_EVIDENCE if (rows and all_have_provenance) else OUT_ADVISORY_INTERPRETATION
    return _result(vlm.LITERATURE_ENGINE, STATUS_CANDIDATE_EVIDENCE, out_type,
                   "Recorded your candidate evidence with provenance — UNREVIEWED until human review.",
                   results={"candidates": rows, "human_review_required": True,
                            "all_rows_have_provenance": all_have_provenance},
                   warnings=warnings, provenance={"inputs": "user_provided", "scraping": "none"})


# --------------------------------------------------------------------------- #
# 10. Sustainability / Cost Screening — order-of-magnitude from user assumptions.
# --------------------------------------------------------------------------- #
def run_sustainability_screening(payload, confirm: bool = False) -> VirtualLabMachineResult:
    p = payload or {}
    missing = _missing_inputs(vlm.SUSTAINABILITY, p)
    if missing:
        return _result(vlm.SUSTAINABILITY, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
                       "Provide your assumptions (e.g. amounts + CO2 / cost factors).",
                       missing_inputs=missing, provenance={"inputs": "user_provided"})

    a = dict(p["assumptions"] or {})
    results: dict = {}
    # Order-of-magnitude products ONLY from user-provided amount+factor pairs; no factor is invented.
    co2 = _pairwise_total(a, "co2")
    cost = _pairwise_total(a, "cost")
    if co2 is not None:
        results["co2_estimate_order_of_magnitude"] = round(co2, 6)
    if cost is not None:
        results["cost_estimate_order_of_magnitude"] = round(cost, 6)
    if not results:
        results["note"] = "No amount×factor pairs found to multiply — provide e.g. energy + co2_factor."

    return _result(vlm.SUSTAINABILITY, STATUS_ADVISORY, OUT_ADVISORY_INTERPRETATION,
                   "ORDER-OF-MAGNITUDE screening from YOUR assumptions — not a final LCA/TEA.",
                   results=results, assumptions=[f"{k}: {v}" for k, v in a.items()],
                   warnings=["Order-of-magnitude only — NOT a quantified LCA/TEA or certified carbon "
                             "savings.",
                             "Every number is derived from YOUR assumptions; no emission/cost factor "
                             "is invented."],
                   provenance={"inputs": "user_assumptions"})


def _pairwise_total(assumptions: dict, kind: str):
    """Sum amount × factor for keys like ``energy`` + ``energy_<kind>_factor`` (user-provided only)."""
    total, found = 0.0, False
    for key, val in assumptions.items():
        if key.endswith(f"_{kind}_factor"):
            base = key[: -len(f"_{kind}_factor")]
            amount = _num(assumptions.get(base))
            factor = _num(val)
            if amount is not None and factor is not None:
                total += amount * factor
                found = True
    return total if found else None


# --------------------------------------------------------------------------- #
# 11. Experimental Design Assistant — deterministic plan; never results.
# --------------------------------------------------------------------------- #
def run_experimental_design(payload, confirm: bool = False) -> VirtualLabMachineResult:
    p = payload or {}
    missing = _missing_inputs(vlm.EXPERIMENTAL_DESIGN, p)
    if missing:
        return _result(vlm.EXPERIMENTAL_DESIGN, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
                       "Provide your research goal (and optional factors).", missing_inputs=missing,
                       provenance={"inputs": "user_provided"})

    goal = str(p.get("goal"))
    factors = p.get("factors") or {}          # {factor_name: [levels]}
    matrix = _factor_matrix(factors)
    plan = {
        "goal": goal,
        "controls": ["a no-treatment / blank control", "a known reference material",
                     "a process-blank to catch contamination"],
        "recommended_replicates": 3,
        "variable_matrix": matrix,
        "matrix_size": len(matrix),
        "measurement_plan": ["define the measured response(s) up front",
                             "measure with the relevant Virtual LAB machine (ICP / XRD / TGA / "
                             "mechanical) on PHYSICAL samples",
                             "compare measured vs any model estimate with the Validation assistant"],
        "missing_measurements_to_consider": _suggest_missing_measurements(goal),
    }
    return _result(vlm.EXPERIMENTAL_DESIGN, STATUS_ADVISORY, OUT_ADVISORY_INTERPRETATION,
                   "A suggested experiment plan (advisory) — run the experiments to obtain results.",
                   results=plan,
                   warnings=["This is a PLAN only — it contains no experimental results.",
                             "It helps prioritise which FEW physical experiments are worth doing."],
                   provenance={"inputs": "user_provided", "engine": "deterministic_planner"})


def _factor_matrix(factors) -> list:
    """Deterministic full-factorial matrix from ``{factor: [levels]}`` (sorted; capped for safety)."""
    if not isinstance(factors, dict) or not factors:
        return []
    names = sorted(factors)
    combos = [{}]
    for name in names:
        levels = list(factors[name]) or [None]
        combos = [{**c, name: lv} for c in combos for lv in levels]
        if len(combos) > 256:                 # safety cap; advisory planning only
            combos = combos[:256]
            break
    return combos


def _suggest_missing_measurements(goal: str) -> list:
    low = (goal or "").lower()
    out = []
    if any(w in low for w in ("leach", "release", "ph", "icp", "concentration")):
        out.append("measured leachate pH + ICP concentrations (to compare with PHREEQC)")
    if any(w in low for w in ("phase", "xrd", "crystall", "mineral")):
        out.append("measured XRD pattern + reference comparison")
    if any(w in low for w in ("strength", "mechanical", "compress", "flexural")):
        out.append("measured compressive/flexural strength with ≥3 replicates")
    if not out:
        out.append("define the measured response and its acceptance criterion")
    return out


# --------------------------------------------------------------------------- #
# 12. Validation & Uncertainty Assistant — measured vs predicted; gated validated_result.
# --------------------------------------------------------------------------- #
def run_validation_uncertainty(payload, confirm: bool = False) -> VirtualLabMachineResult:
    p = payload or {}
    measured, measured_qc = _as_map_with_qc(p.get("measured"), role="measured")
    predicted, predicted_qc = _as_map_with_qc(p.get("predicted"), role="predicted")
    qc_warnings = [*measured_qc, *predicted_qc]
    criteria = p.get("criteria") or {}

    if not measured:
        return _result(vlm.VALIDATION_UNCERTAINTY, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
                       "No QC-eligible measured data — validation is impossible.",
                       missing_inputs=["measured"],
                       results={"qc_exclusions": qc_warnings},
                       warnings=["Validation REQUIRES QC-eligible measured data — "
                                 "simulation/ML/literature alone are never validated.",
                                 *qc_warnings],
                       provenance={"inputs": "user_provided"}, validation_status=VAL_NO_MEASURED_DATA)
    if not predicted:
        return _result(vlm.VALIDATION_UNCERTAINTY, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
                       "Provide QC-eligible predicted/simulated values to compare.",
                       missing_inputs=["predicted"], results={"qc_exclusions": qc_warnings},
                       warnings=qc_warnings, provenance={"inputs": "user_provided"},
                       validation_status=VAL_INSUFFICIENT_DATA)

    keys = sorted(set(measured) & set(predicted))
    residuals = []
    for k in keys:
        m, pr = measured[k], predicted[k]
        residual = m - pr
        residuals.append({"key": k, "measured": m, "predicted": pr,
                          "residual": round(residual, 8), "abs_error": round(abs(residual), 8),
                          "percent_error": round(100.0 * abs(residual) / abs(m), 6) if m else None})
    if not residuals:
        return _result(vlm.VALIDATION_UNCERTAINTY, STATUS_MISSING_INPUTS, OUT_ADVISORY_INTERPRETATION,
                       "Measured and predicted share no comparable keys — cannot compute residuals.",
                       missing_inputs=["matching measured/predicted keys"],
                       results={"qc_exclusions": qc_warnings}, warnings=qc_warnings,
                       provenance={"inputs": "user_provided"}, validation_status=VAL_INSUFFICIENT_DATA)

    has_criteria = bool(criteria)
    criteria_met = _criteria_met(residuals, criteria) if has_criteria else None
    results = {"residuals": residuals, "criteria": criteria, "criteria_met": criteria_met,
               "qc_exclusions": qc_warnings}

    if has_criteria and criteria_met:
        return _result(vlm.VALIDATION_UNCERTAINTY, STATUS_PROCESSED, OUT_VALIDATED_RESULT,
                       "Measured-vs-predicted comparison MEETS your validation criteria.",
                       results=results,
                       warnings=["'validated_result' means it met YOUR criteria against QC-eligible "
                                 "measured data — not a universal guarantee.", *qc_warnings],
                       provenance={"inputs": "user_provided"},
                       validation_status=VAL_VALIDATED, can_be_used_for_validation_claim=True)

    summary = ("Comparison computed; criteria NOT met — not a validated result."
               if has_criteria else
               "Comparison computed; no validation criteria supplied — advisory, not validated.")
    return _result(vlm.VALIDATION_UNCERTAINTY, STATUS_PROCESSED, OUT_ADVISORY_INTERPRETATION, summary,
                   results=results,
                   warnings=["Validation needs QC-eligible measured data AND explicit criteria that "
                             "are MET; otherwise this is advisory, not a validated result.",
                             *qc_warnings],
                   provenance={"inputs": "user_provided"}, validation_status=VAL_COMPARISON_AVAILABLE)


def _as_map(obj) -> dict:
    """Normalise measured/predicted to ``{key: float}`` from a dict or a list of {key,value} rows."""
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            fv = _num(v)
            if fv is not None:
                out[str(k)] = fv
    elif isinstance(obj, (list, tuple)):
        for row in obj:
            row = row or {}
            k = row.get("key") or row.get("element") or row.get("name")
            fv = _num(row.get("value"))
            if k is not None and fv is not None:
                out[str(k)] = fv
    return out


def _as_map_with_qc(obj, *, role: str) -> tuple[dict, list[str]]:
    """Normalize validation inputs without duplicate overwrite or ICP-QC bypass."""
    from . import icp_processor as icp

    entries: list[tuple[str, object]] = []
    if isinstance(obj, dict):
        entries = [(str(k), v) for k, v in obj.items()]
    elif isinstance(obj, (list, tuple)):
        for raw in obj:
            row = dict(raw or {})
            key = row.get("key") or row.get("element") or row.get("name")
            if key is not None:
                entries.append((str(key), row.get("value")))

    counts: dict[str, int] = {}
    for key, _ in entries:
        counts[key] = counts.get(key, 0) + 1

    values: dict[str, float] = {}
    warnings: list[str] = []
    for key, value in entries:
        if counts[key] > 1:
            warning = f"duplicate {role} key {key!r}; explicit selection required"
            if warning not in warnings:
                warnings.append(warning)
            continue
        element = icp.canonical_element(key)
        if element:
            decision = icp.assess_final_concentration(
                value, role=role, element=element, unit="mM")
            if decision.validation_eligible:
                values[key] = float(decision.numeric_value)
            else:
                warnings.append(f"{role} {key}: " + "; ".join(decision.qc_reasons))
        else:
            numeric = _num(value)
            if numeric is not None:
                values[key] = numeric
            else:
                warnings.append(f"{role} {key}: missing/non-numeric/non-finite value excluded")
    return values, warnings


def _criteria_met(residuals, criteria) -> bool:
    """All residuals satisfy the supplied criteria (max_abs_error / max_percent_error)."""
    max_abs = _num(criteria.get("max_abs_error"))
    max_pct = _num(criteria.get("max_percent_error"))
    if max_abs is None and max_pct is None:
        return False
    for r in residuals:
        if max_abs is not None and r["abs_error"] > max_abs:
            return False
        if max_pct is not None and (r["percent_error"] is None or r["percent_error"] > max_pct):
            return False
    return True


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
_RUNNERS = {
    vlm.PHREEQC_LEACHING: run_phreeqc_leaching,
    vlm.XRD_ADVISORY: run_xrd_advisory,
    vlm.ICP_PROCESSOR: run_icp_processor,
    vlm.FTIR_RAMAN: run_ftir_raman,
    vlm.SEM_EDS: run_sem_eds,
    vlm.TGA_DSC: run_tga_dsc,
    vlm.MECHANICAL: run_mechanical_testing,
    vlm.ML_SURROGATE: run_ml_surrogate,
    vlm.LITERATURE_ENGINE: run_literature_evidence,
    vlm.SUSTAINABILITY: run_sustainability_screening,
    vlm.EXPERIMENTAL_DESIGN: run_experimental_design,
    vlm.VALIDATION_UNCERTAINTY: run_validation_uncertainty,
}


def runner_machine_ids() -> tuple[str, ...]:
    """Canonical dispatch IDs in authoritative catalogue order."""
    return tuple(machine_id for machine_id in vlm.machine_ids() if machine_id in _RUNNERS)


def run_virtual_lab_machine(machine_id, payload=None, confirm: bool = False) -> VirtualLabMachineResult:
    """Run one Virtual LAB machine over ``payload``. Unknown ids are rejected (status unknown_machine).

    PHREEQC is capability/routing only here (never authored or executed); its actual review and
    confirmation evidence must go through the Phase 1A contract. Every result is a
    :class:`VirtualLabMachineResult` with the full standard field set.
    """
    input_machine_id = str(machine_id).strip() if machine_id is not None else ""
    canonical = vlm.canonical_machine_id(input_machine_id)
    handler = _RUNNERS.get(canonical)
    if handler is None:
        return _result(None, STATUS_UNKNOWN_MACHINE, OUT_ADVISORY_INTERPRETATION,
                       f"Unknown machine_id {machine_id!r}; nothing was run.",
                       warnings=[f"unknown machine_id {machine_id!r}",
                                 "known ids: " + ", ".join(sorted(_RUNNERS))],
                       provenance={"inputs": "user_provided",
                                   "input_machine_id": input_machine_id or None,
                                   "canonical_machine_id": None})
    result = handler(payload or {}, confirm)
    result.machine_id = canonical
    result.provenance["canonical_machine_id"] = canonical
    if input_machine_id != canonical:
        result.provenance["input_machine_id"] = input_machine_id
    return result
