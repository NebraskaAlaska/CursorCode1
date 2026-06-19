"""Deterministic **tool registry** — binds each agent action to a real backend function.

This is the agent's "doing" layer. Every tool is a deterministic wrapper over an existing,
tested module (the scenario merge, the input-preview builder, the database-compatibility
report, the gated executor, the batch executor, the ranking/target-matching layers, the run
registry). The LLM **never** runs these — it only *names* an action; the orchestrator runs
the bound tool **after** the policy layer allows it (and, for execution/save, after explicit
user confirmation).

Boundaries (pinned by ``tests/test_ai_boundary.py``):

* It imports **no AI module** — AI never writes PHREEQC input, never runs anything, and never
  decides a composition / release fraction.
* The PHREEQC input is built by :mod:`phreeqc_input_builder` from the *structured scenario*
  (never from model-supplied text), and run **verbatim** by :mod:`phreeqc_executor`.
* It writes only through the existing safe paths (the executor's ``outputs/simulations/``
  workspace and the run registry's ``outputs/simulation_runs/``) — never measured data, never
  the result path, never the comparison CSVs.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from .. import config
from ..simulation import batch_executor, database_compatibility
from ..simulation import matrix as sim_matrix
from ..simulation import phreeqc_executor, phreeqc_input_builder, run_registry
from ..simulation import scenario_schema as S
from ..simulation import source_terms, strategy, target_matching
from ..simulation.scenario_schema import SimulationScenario
from . import agent_actions as A
from . import agent_state, domains

# Label for the assistant-built single scenario.
ASSISTANT_SCENARIO_ID = "ASSISTANT-001"
RANGEABLE = set(sim_matrix.RANGEABLE_FIELDS)


# --------------------------------------------------------------------------- #
# Tool outcome
# --------------------------------------------------------------------------- #
@dataclass
class ToolOutcome:
    """Structured result of running one deterministic tool (never raises out of :func:`run`)."""

    ok: bool = True
    status: str = "done"
    summary: str = ""
    warnings: list = field(default_factory=list)
    data: dict = field(default_factory=dict)


def _f(value, nd: int = 3):
    try:
        return round(float(value), nd)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Safe (immediate) tools
# --------------------------------------------------------------------------- #
def _tool_ask_user(state, args) -> ToolOutcome:
    return ToolOutcome(status="asked", summary="Asked the user for more detail.")


def _tool_update_scenario(state, args) -> ToolOutcome:
    """Apply the model's *typed scalar* scenario hints on top of the deterministic merge.

    The orchestrator already merged the raw user text deterministically; this applies any
    additional clamped scalar fields the model named in ``arguments`` (never composition,
    never a release fraction, never input text — those keys are stripped upstream).
    """
    flat = state.scenario.to_flat_dict()
    applied: dict = {}
    # Recipe / process scalars only — NEVER a composition, release fraction, or a pH/result
    # (pH is a forbidden key and the builder fixes the SOLUTION pH deterministically anyway).
    allowed = {
        "material_name", "material_type", "solid_mass_g", "liquid_volume_mL",
        "leachant_type", "leachant_concentration_M", "time_min",
        "temperature_C", "CO2_condition", "cover_condition", "centrifuge_used",
        "filtration_used", "filter_size_um", "target_elements", "desired_outputs",
    }
    for key, value in (args or {}).items():
        if key not in allowed or value is None:
            continue
        if key in ("target_elements", "desired_outputs"):
            merged = list(dict.fromkeys(list(flat.get(key) or []) + list(S.as_str_list(value))))
            if merged != list(flat.get(key) or []):
                flat[key] = merged
                applied[key] = merged
        elif value != flat.get(key):
            flat[key] = value
            applied[key] = value
    if applied:
        state.scenario = SimulationScenario.from_flat_dict(flat)
        state.scenario.liquid_solid_ratio = state.scenario.computed_ls_ratio()
    state.recompute_safety()
    return ToolOutcome(status="updated",
                       summary=("Updated: " + ", ".join(applied)) if applied
                       else "No additional fields to update.",
                       data={"applied": applied})


def _tool_classify_domain(state, args) -> ToolOutcome:
    hint = (args or {}).get("domain")
    # The orchestrator already classified on NORMALIZED text and set state.domain; preserve it
    # rather than re-deriving from the raw (typo-laden) experiment_text, which can regress a
    # correct domain to `unknown`. Only (re)classify here when no domain is known yet.
    if not getattr(state, "domain", None) or state.domain == domains.UNKNOWN:
        state.domain = domains.classify(state.experiment_text, hint=hint)
    state.engine = domains.engine_for(state.domain)
    executable = domains.is_executable(state.domain)
    state.phase = (agent_state.PLANNING if executable
                   else agent_state.UNSUPPORTED_DOMAIN_PLANNING_ONLY)
    return ToolOutcome(
        status="classified",
        summary=(f"Domain: {domains.label(state.domain)} "
                 + ("(PHREEQC engine available)" if executable else "(planning-only)")),
        data={"domain": state.domain, "executable": executable, "engine": state.engine})


def _tool_plan_experiment(state, args) -> ToolOutcome:
    state.recompute_safety()
    # Planning-only (non-executable) domain → structure the experiment + offer next actions;
    # never simulate. The response/input variables come from the domain's planning support.
    if not domains.is_executable(state.domain):
        support = domains.planning_support(state.domain)
        state.phase = agent_state.UNSUPPORTED_DOMAIN_PLANNING_ONLY
        return ToolOutcome(
            status="planning_only",
            summary=domains.planning_only_message(state.domain),
            data={"planning_support": support, "executable": False})
    if state.phase in (agent_state.IDLE, agent_state.COLLECTING_CONTEXT):
        state.phase = agent_state.PLANNING
    missing = [m.label for m in state.missing_fields]
    return ToolOutcome(
        status="planned",
        summary=("Plan structured. " + ("Missing: " + ", ".join(missing) if missing
                                        else "All core fields are present.")),
        data={"missing": missing, "n_warnings": len(state.warnings), "executable": True})


def _tool_request_material_profile(state, args) -> ToolOutcome:
    return ToolOutcome(
        status="requested",
        summary=("A confirmed material composition is required before a meaningful run. "
                 "Provide it under **Advanced details → Material composition** (paste an oxide / "
                 "element assay) and confirm it — composition is never invented."),
        warnings=["No usable material composition yet — predicted material totals would be ~0."])


def _tool_request_release_model(state, args) -> ToolOutcome:
    return ToolOutcome(
        status="requested",
        summary=("Choose a material release model (e.g. a global % release, per-element "
                 "fractions, or measured liquid). Release fractions are USER ASSUMPTIONS, not "
                 "measured truth — they control the predicted dissolved totals."))


def _tool_check_database(state, args) -> ToolOutcome:
    expected = []
    if state.phase_template is not None:
        expected = list(getattr(state.phase_template, "phase_names", lambda: [])())
    report = database_compatibility.build_report(state.database_path, expected_phases=expected)
    state.database_report = report
    state.database_status = agent_state.DB_CHECKED
    return ToolOutcome(
        status="checked",
        summary=(f"Database: {report.database_label} (family {report.detected_family}; "
                 f"present: {report.database_exists}). "
                 f"Compatibility: {report.compatibility_level}."),
        warnings=list(report.warnings),
        data={"family": report.detected_family, "exists": report.database_exists,
              "available_phases": list(report.available_phases)})


def _tool_build_preview(state, args) -> ToolOutcome:
    """Deterministically build the PHREEQC input preview from the structured scenario.

    The preview text is produced entirely by :func:`phreeqc_input_builder.build_phreeqc_input_preview`
    from ``state.scenario`` + the user's confirmed material profile / release model — never
    from anything the model supplied. ``args`` is ignored for the input text.
    """
    preview = phreeqc_input_builder.build_phreeqc_input_preview(
        state.scenario, scenario_id=ASSISTANT_SCENARIO_ID,
        material_profile=state.material_profile, dissolution_model=state.release_model,
        phase_template=state.phase_template, database_path=state.database_path)
    state.preview = preview
    state.preview_status = preview.status
    state.preview_signature = state.scenario_preview_signature()
    state.database_report = preview.database_report or state.database_report
    state.phase = agent_state.PREVIEW_READY
    return ToolOutcome(
        status=preview.status,
        summary=(f"Built a draft PHREEQC input preview (status: {preview.status}). "
                 f"{_preview_status_note(preview.status)} "
                 "PHREEQC has NOT been run — this is a reviewable draft only."),
        warnings=list(preview.warnings),
        data={"status": preview.status, "template_type": preview.template_type,
              "unsupported_features": list(preview.unsupported_features)})


def _wrap_single_run(execution, parsed) -> object:
    """Wrap one execution+parse into a one-scenario BatchResult (for table/save reuse)."""
    sid = getattr(execution, "scenario_id", ASSISTANT_SCENARIO_ID)
    bsr = batch_executor.BatchScenarioResult(sid, execution, parsed)
    return batch_executor.BatchResult(results=[bsr], requested=1, max_scenarios=1)


def _tool_run_single(state, args) -> ToolOutcome:
    """Execute the confirmed preview verbatim (deterministic; reached only after confirmation).

    Runs the **parked snapshot** (``pending_preview``) when present — the exact reviewed input —
    falling back to the live ``preview``. It never rebuilds from the (possibly-changed) scenario.
    """
    preview = state.pending_preview if state.pending_preview is not None else state.preview
    if preview is None:
        return ToolOutcome(ok=False, status="no_preview",
                           summary="No input preview to run — build one first.")
    execution, parsed = phreeqc_executor.run_and_parse(preview)
    state.execution_result = execution
    state.parsed_result = parsed
    state.batch_result = _wrap_single_run(execution, parsed)
    state.result_table = batch_executor.build_result_table(state.batch_result, None)
    state.execution_status = agent_state.EXEC_DONE
    state.phase = (agent_state.RESULTS_READY if execution.status == phreeqc_executor.STATUS_SUCCESS
                   else state.phase)
    if execution.status != phreeqc_executor.STATUS_SUCCESS:
        return ToolOutcome(
            ok=False, status=execution.status,
            summary=_execution_status_message(execution.status, execution.error_message),
            warnings=[execution.error_message] if execution.error_message else [])
    return ToolOutcome(
        status=execution.status,
        summary=("PHREEQC run complete. " + _parsed_brief(parsed)
                 + " These are model estimates, not validated."),
        data={"pH": _f(getattr(parsed, "pH", None), 2),
              "element_totals_mM": {k: _f(v) for k, v in
                                    getattr(parsed, "element_totals_mM", {}).items()}})


def _tool_build_sweep_matrix(state, args) -> ToolOutcome:
    field_name = str((args or {}).get("field") or (args or {}).get("axis") or "").strip()
    values = (args or {}).get("values")
    ranges = {}
    if field_name in RANGEABLE and isinstance(values, (list, tuple)):
        nums = [v for v in (S.as_float(x) for x in values) if v is not None]
        if nums:
            ranges[field_name] = nums[:batch_executor.DEFAULT_MAX_SCENARIOS]
    matrix = sim_matrix.build_simulation_matrix(state.scenario, ranges=ranges)
    previews = phreeqc_input_builder.build_previews_for_matrix(
        state.scenario, matrix, material_profile=state.material_profile,
        dissolution_model=state.release_model, phase_template=state.phase_template,
        database_path=state.database_path)
    state.sweep_matrix = matrix
    state.sweep_previews = previews
    if previews:
        state.preview = previews[0]
        state.preview_status = previews[0].status
        state.preview_signature = state.scenario_preview_signature()
    note = (f"over {field_name}" if ranges else "(single scenario — no sweep range given)")
    return ToolOutcome(
        status="matrix_built",
        summary=(f"Built a plan-only simulation matrix {note}: {len(previews)} scenario(s). "
                 "Nothing has run."),
        data={"n_scenarios": len(previews), "swept_field": field_name if ranges else None})


def _tool_run_sweep(state, args) -> ToolOutcome:
    previews = state.sweep_previews or ([state.preview] if state.preview is not None else [])
    if not previews:
        return ToolOutcome(ok=False, status="no_previews",
                           summary="No sweep previews to run — build the matrix first.")
    batch = batch_executor.run_batch(previews, max_scenarios=batch_executor.DEFAULT_MAX_SCENARIOS)
    state.batch_result = batch
    state.result_table = batch_executor.build_result_table(batch, state.sweep_matrix)
    state.execution_status = agent_state.EXEC_DONE
    if batch.n_success:
        state.phase = agent_state.RESULTS_READY
    counts = batch.status_counts()
    return ToolOutcome(
        ok=bool(batch.n_success),
        status="swept",
        summary=(f"Sweep complete: {batch.n_success}/{batch.executed} succeeded "
                 f"({counts}). Results are model estimates, not validated."),
        data={"status_counts": counts, "n_success": batch.n_success})


def _tool_rank_results(state, args) -> ToolOutcome:
    if not state.has_result_table:
        return ToolOutcome(ok=False, status="no_results",
                           summary="No executed results to rank yet.")
    text = str((args or {}).get("objective") or state.desired_outputs_text or "")
    objective = strategy.parse_objective(text, target_elements=state.scenario.outputs.target_elements)
    axis_col, _ = batch_executor.detect_sweep_axis(state.sweep_matrix)
    ranking = strategy.rank_results(state.result_table, objective, axis_col=axis_col)
    state.ranking = ranking
    return ToolOutcome(
        ok=ranking.ok, status=ranking.status,
        summary=(f"Ranked {len(ranking.ranked)} result(s). Top: {ranking.top_scenario_id} "
                 f"(driver: {ranking.driving_metric}). {strategy.RANKING_NOT_VALIDATION}"
                 if ranking.ok else "Could not rank: " + "; ".join(ranking.warnings)),
        warnings=list(ranking.warnings),
        data={"top": ranking.top_scenario_id, "objective": objective.display()})


def _tool_target_match(state, args) -> ToolOutcome:
    """Parse the desired target (interpretation only) and rank ANY already-executed results
    against it. The agent never supplies the inverse-search grid's numeric values — those are
    user-reviewed ranges (and release fractions), chosen in the Advanced Mode Target-matching
    step, never invented by the model."""
    text = str((args or {}).get("target") or state.desired_outputs_text or "")
    spec = target_matching.parse_target_spec(text)
    state.target_spec = spec
    scored = None
    if state.has_result_table and spec.is_defined:
        scored = target_matching.score_results(spec, state.result_table)
        state.target_match_result = scored
    if scored is not None and scored.best:
        tail = (f" Best match so far: {scored.best.get('scenario_id')} "
                f"(score {scored.best.get('objective_score')}).")
    elif state.has_result_table:
        tail = " Run results exist but none could be scored against this target yet."
    else:
        tail = (" Choose the search ranges and run a sweep (Advanced Mode) — then I can rank "
                "candidates against this target. I won't invent the ranges or release fractions.")
    return ToolOutcome(
        status="target_built",
        summary=f"Target: {spec.display()}.{tail} {target_matching.NOT_VALIDATION}",
        warnings=(list(scored.warnings) if scored else []),
        data={"target": spec.display()})


def _tool_save_run(state, args) -> ToolOutcome:
    if state.batch_result is None:
        return ToolOutcome(ok=False, status="no_results",
                           summary="No executed run to save yet.")
    now = _dt.datetime.now()
    label = str((args or {}).get("label") or "assistant run").strip()
    run_id = run_registry.generate_run_id(now, label)
    record = run_registry.build_run_record(
        run_id=run_id, created_at=now.isoformat(timespec="seconds"),
        batch=state.batch_result, matrix=state.sweep_matrix, scenario=state.scenario,
        material_profile=state.material_profile, previews=list(state.sweep_previews
                                                               or ([state.preview]
                                                                   if state.preview else [])),
        experiment_text=state.experiment_text,
        desired_outputs_text=state.desired_outputs_text, label=label,
        notes="Saved from the AI assistant.",
        agent_provenance=state.to_provenance_dict())
    registry = run_registry.SimulationRunRegistry()
    path = registry.save_run(record)
    state.last_run_id = run_id
    return ToolOutcome(
        status="saved",
        summary=(f"Saved simulation run '{run_id}' (with the agent transcript + action trace "
                 "+ confirmed assumptions + the not-validated label). It is a simulation "
                 "record, not validation."),
        data={"run_id": run_id, "path": str(path)})


def _tool_create_validation_template(state, args) -> ToolOutcome:
    # Planning-only domain → a materials-experiment data-collection template (input + response
    # variables), so a dataset can be built for a future model. Not a leaching-release template.
    if not domains.is_executable(state.domain):
        support = domains.planning_support(state.domain)
        cols, labels = domains.data_template_columns(state.domain)
        return ToolOutcome(
            status="template",
            summary=(f"Here's a data-collection template for a {support['domain_label']} study — "
                     "fill it in across your specimens to build a dataset for a future model. "
                     "(No simulation engine exists for this domain yet; this is a dataset, not a "
                     "prediction.)"),
            data={"template_columns": cols, "template_labels": labels,
                  "csv_header": ",".join(cols), "planning_only": True})
    # Leaching/geochemistry → the measured-release validation template.
    cols = list(config.EXPERIMENTAL_RELEASE_COLUMNS)
    return ToolOutcome(
        status="template",
        summary=("To validate these estimates, collect measured ICP / pH data into the "
                 "measured-release template, then use the Validate / Compare workflow. "
                 "The template columns are listed below."),
        data={"template_columns": cols, "csv_header": ",".join(cols), "planning_only": False})


def _tool_explain_results(state, args) -> ToolOutcome:
    """Assemble a grounded explanation — numbers from the tools, never from the model.

    Always carries the not-validated caveat, the assumptions controlling the result, and the
    database/phase limitation, plus what measured data are needed for validation and a
    recommended next step.
    """
    explanation = build_result_explanation(state)
    state.last_explanation = explanation
    state.validation_status = "recommended"
    if state.phase == agent_state.RESULTS_READY:
        state.phase = agent_state.VALIDATION_RECOMMENDED
    return ToolOutcome(
        status="explained",
        summary=explanation.get("headline", "Explained the current estimate."),
        warnings=[agent_state.NOT_VALIDATED_WARNING],
        data=explanation)


def _tool_open_advanced(state, args) -> ToolOutcome:
    target = str((args or {}).get("workflow") or "").strip().lower()
    tab = {"validate": "Validate", "compare": "Compare Results", "match": "Match",
           "import": "Import Data", "simulate": "Simulate"}.get(target, "Simulate")
    return ToolOutcome(
        status="pointer",
        summary=f"Open the **{tab}** tab for that — the assistant focuses on planning + running "
                "simulations; the advanced tabs hold the full controls.",
        data={"tab": tab})


# --------------------------------------------------------------------------- #
# Grounded result explanation (deterministic; numbers from state, not the model)
# --------------------------------------------------------------------------- #
def build_result_explanation(state) -> dict:
    """Build the plain-language result explanation facts from deterministic state.

    Returns a dict the UI renders and the orchestrator weaves into prose. Every number comes
    from ``state.parsed_result`` / ``state.result_table`` — the model never supplies one.
    """
    parsed = state.parsed_result
    pH = _f(getattr(parsed, "pH", None), 2) if parsed is not None else None
    totals = ({k: _f(v) for k, v in getattr(parsed, "element_totals_mM", {}).items()}
              if parsed is not None else {})

    assumptions = [a.reason or a.field for a in state.assumptions]
    if state.release_model is not None and getattr(state.release_model, "mode", None) not in (
            None, source_terms.MODE_NONE):
        assumptions.append("a user-chosen material release model controls the dissolved totals "
                           "(release fractions are assumptions, not measured truth)")

    db_limits = []
    rep = state.database_report
    if rep is not None and not getattr(rep, "database_exists", False):
        db_limits.append("no thermodynamic database is configured — saturation / precipitation "
                         "predictions are unverified (high-pH cement/fly-ash phases need CEMDATA18)")
    elif rep is not None:
        db_limits.append(f"phases are limited to what {getattr(rep, 'database_label', 'the database')} "
                        "defines; kinetics are not modelled (equilibrium only)")

    headline = (f"Under your reviewed assumptions, the model estimates pH ≈ {pH}."
                if pH is not None else "The model produced an estimate under your assumptions.")
    if totals:
        shown = ", ".join(f"{k} ≈ {v} mM" for k, v in list(totals.items())[:6])
        headline += f" Dissolved totals: {shown}."

    next_steps = ["Compare these estimates against measured ICP / pH data (the Validate / "
                  "Compare workflow) — that is the strongest next step."]
    if state.has_result_table and (state.sweep_matrix is not None):
        next_steps.append("Refine the parameter sweep around the best region to sharpen the trend.")

    return {
        "headline": headline,
        "estimated_pH": pH,
        "element_totals_mM": totals,
        "assumptions": assumptions,
        "database_phase_limitations": db_limits,
        "validation_needed": ("Measured pH and ICP element concentrations for this exact "
                              "condition are needed to validate (or correct) these estimates."),
        "recommended_next_steps": next_steps,
        "not_validated": agent_state.NOT_VALIDATED_WARNING,
    }


def _parsed_brief(parsed) -> str:
    if parsed is None:
        return "No parsed outputs."
    pH = _f(getattr(parsed, "pH", None), 2)
    n = len(getattr(parsed, "element_totals_mM", {}) or {})
    bits = []
    if pH is not None:
        bits.append(f"pH ≈ {pH}")
    if n:
        bits.append(f"{n} element total(s)")
    return ("Estimated " + ", ".join(bits) + ".") if bits else "Run parsed (no pH/totals found)."


def _preview_status_note(status: str) -> str:
    """A short, honest note on what a preview status means for running it."""
    if status == phreeqc_input_builder.STATUS_READY:
        return "It's ready for review — say *run it* (you'll confirm first)."
    if status == phreeqc_input_builder.STATUS_NEEDS_COMPOSITION:
        return ("It still needs a **confirmed material composition** before a meaningful run — "
                "add one under **Advanced details → Material composition** and confirm it (the "
                "release model alone is not enough).")
    if status == phreeqc_input_builder.STATUS_MISSING_FIELD:
        return "Some required set-up fields are still missing — see the warnings."
    if status == phreeqc_input_builder.STATUS_UNSUPPORTED_LEACHANT:
        return "This leachant has no preview template (supported: water, NaOH, HCl)."
    if status == phreeqc_input_builder.STATUS_TEMPLATE_WARNING:
        return "It's reviewable, but is a preview-only template (the runner templates NaOH only)."
    return "Review it before running."


def _execution_status_message(status: str, error: str | None) -> str:
    if status == phreeqc_executor.STATUS_MISSING:
        # Use the SAME availability reasoning Settings shows, so they always agree, and make the
        # local-vs-container distinction explicit (rather than a flat "not configured").
        return ("I couldn't run PHREEQC. " + phreeqc_executor.availability_hint()
                + " You can still review and download the input preview.")
    if status == phreeqc_executor.STATUS_TIMEOUT:
        return "The PHREEQC run timed out. Try a simpler scenario or raise the timeout."
    return f"The PHREEQC run did not succeed ({status}): {error or 'see the run log'}."


# --------------------------------------------------------------------------- #
# Registry + dispatcher
# --------------------------------------------------------------------------- #
TOOLS = {
    A.ASK_USER: _tool_ask_user,
    A.UPDATE_SCENARIO: _tool_update_scenario,
    A.CLASSIFY_DOMAIN: _tool_classify_domain,
    A.PLAN_EXPERIMENT: _tool_plan_experiment,
    A.REQUEST_MATERIAL_PROFILE: _tool_request_material_profile,
    A.REQUEST_RELEASE_MODEL: _tool_request_release_model,
    A.CHECK_DATABASE: _tool_check_database,
    A.BUILD_PHREEQC_PREVIEW: _tool_build_preview,
    A.RUN_SINGLE_SIMULATION: _tool_run_single,
    A.BUILD_SWEEP_MATRIX: _tool_build_sweep_matrix,
    A.RUN_SWEEP: _tool_run_sweep,
    A.RANK_RESULTS: _tool_rank_results,
    A.TARGET_MATCH: _tool_target_match,
    A.SAVE_SIMULATION_RUN: _tool_save_run,
    A.CREATE_VALIDATION_TEMPLATE: _tool_create_validation_template,
    A.EXPLAIN_RESULTS: _tool_explain_results,
    A.OPEN_ADVANCED_WORKFLOW: _tool_open_advanced,
}


def has_tool(action_name: str) -> bool:
    return action_name in TOOLS


def run(action, state) -> ToolOutcome:
    """Run the deterministic tool bound to ``action`` against ``state`` (never raises).

    ``REQUEST_RUN_CONFIRMATION`` / ``REQUEST_SWEEP_CONFIRMATION`` have no side effect here —
    they only *propose* an execution (the orchestrator parks the corresponding RUN action for
    explicit confirmation), so they resolve to a no-op acknowledgement.
    """
    name = getattr(action, "action_name", None)
    if name in (A.REQUEST_RUN_CONFIRMATION, A.REQUEST_SWEEP_CONFIRMATION):
        return ToolOutcome(status="awaiting_confirmation",
                           summary="Ready when you are — confirm to run.")
    fn = TOOLS.get(name)
    if fn is None:
        return ToolOutcome(ok=False, status="unknown_action",
                           summary=f"No tool is bound to '{name}'.")
    try:
        return fn(state, getattr(action, "arguments", {}) or {})
    except Exception as exc:                                   # noqa: BLE001 — never crash the chat
        return ToolOutcome(ok=False, status="tool_error",
                           summary=f"That step failed ({type(exc).__name__}). "
                                   "Nothing was changed.",
                           warnings=[f"{type(exc).__name__}: {exc}"])
