"""Streamlit interface for the flyash-phreeqc-ml project.

A thin GUI on top of the existing Phase 1 / Phase 2 code — it does **not**
reimplement any pipeline logic. It presents an AI-assisted geochemical simulation &
validation platform: a run-management sidebar drives a guided seven-tab workflow
**Start → Simulate → Import Data → Validate → Match → Compare Results → Export**.
**Simulate** is the forward-looking planning core (describe an experiment → structured
scenario → simulation plan; no model is executed yet); the measured-vs-model mapping +
comparison is the current strongest **validation module**. Each tab reuses the package
functions; this file adds no chemistry or ML on the result path. It lets you:

* see the three product modes (Simulate / Validate / Learn) + run status at a glance,
* plan a simulation from a plain-language description (Simulate, planning only),
* enter measured / literature / demo data into per-run save files,
* map measured samples to model rows and run the existing scripts,
* read an honest measured-vs-model summary, and browse model outputs.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import io
import json
import subprocess
import zipfile
import sys
from pathlib import Path

# Make the package importable when Streamlit runs this file directly.
_PROJECT_ROOT = Path(__file__).resolve().parent


def _rel(path: Path) -> Path:
    """Display a path relative to the project root, or as-is if it lives elsewhere.

    Presentation-only: runs are normally under the repo, but a deployment may point
    ``EXPERIMENT_RUNS_DIR`` elsewhere — a caption must never crash on that.
    """
    try:
        return Path(path).relative_to(_PROJECT_ROOT)
    except ValueError:
        return Path(path)
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

import app_ui  # noqa: E402  (presentation-only UI helper layer)
# Pure helpers extracted to the ui/ package (refactor prep; see docs/refactor_plan.md).
# Re-imported under their historical underscore names so call sites are unchanged.
from ui.formatters import (  # noqa: E402
    has_numeric as _has_numeric,
    is_present as _is_present,
    nearest_manifest_row as _nearest_manifest_row,
)
from flyash_phreeqc_ml import audit  # noqa: E402  (append-only audit log)
from flyash_phreeqc_ml import calculations  # noqa: E402
from flyash_phreeqc_ml import config  # noqa: E402
from flyash_phreeqc_ml import dissolution_workbook  # noqa: E402
from flyash_phreeqc_ml import import_mapping  # noqa: E402
from flyash_phreeqc_ml import mapping_table  # noqa: E402
from flyash_phreeqc_ml import phreeqc_runner  # noqa: E402  (on-demand PHREEQC, Prompt 11)
from flyash_phreeqc_ml import profiles  # noqa: E402
from flyash_phreeqc_ml import attribution  # noqa: E402  (PHREEQC gap attribution)
from flyash_phreeqc_ml import mass_balance  # noqa: E402  (deterministic element closure)
from flyash_phreeqc_ml import replicates  # noqa: E402
from flyash_phreeqc_ml import report  # noqa: E402  (one-click validation report)
from flyash_phreeqc_ml import run_manager  # noqa: E402
from flyash_phreeqc_ml import scenarios  # noqa: E402
from flyash_phreeqc_ml import units  # noqa: E402  (single conversion authority)
from flyash_phreeqc_ml.ai import config as ai_config  # noqa: E402  (AI settings/status authority)
from flyash_phreeqc_ml.ai import import_assist  # noqa: E402  (optional AI helpers)
from flyash_phreeqc_ml.ai import assistant as ai_assistant  # noqa: E402  (grounded Q&A)
from flyash_phreeqc_ml.ai import literature as ai_literature  # noqa: E402  (sourced lit values)
from flyash_phreeqc_ml.ai import scenario_parser as ai_scenario_parser  # noqa: E402  (NL simulation planner)
from flyash_phreeqc_ml.simulation import scenario_schema as sim_schema  # noqa: E402
from flyash_phreeqc_ml.simulation import matrix as sim_matrix  # noqa: E402
from flyash_phreeqc_ml.simulation import phreeqc_input_builder  # noqa: E402  (deterministic .pqi preview)
from flyash_phreeqc_ml.experiments import validate_experimental_df  # noqa: E402
from flyash_phreeqc_ml.parsers import (  # noqa: E402
    has_measured_data,
    load_experimental_release,
)
from flyash_phreeqc_ml.compare import comparison_inclusion  # noqa: E402
from flyash_phreeqc_ml.compare import inclusion as compare_inclusion  # noqa: E402
from flyash_phreeqc_ml.ml import residual_stats  # noqa: E402  (descriptive bias stats)
from flyash_phreeqc_ml.viz import compare_plots  # noqa: E402
from flyash_phreeqc_ml.viz import measured_overview  # noqa: E402

# Active model profile — its display name drives generic UI strings ("needs new
# {MODEL_NAME} simulation"). PHREEQC for this project; swappable via ModelProfile.
MODEL_NAME = profiles.PHREEQC_PROFILE.name

# The form appends here. Kept out of git (see .gitignore) so manually-entered
# measured data is never committed by accident.
MANUAL_ENTRY_FILENAME = "experimental_release_manual_entry.csv"
MANUAL_ENTRY_PATH = config.EXPERIMENTAL_ICP_DIR / MANUAL_ENTRY_FILENAME

# Processed CSVs surfaced first in the data viewer.
PREFERRED_PROCESSED = [
    config.MASTER_DATASET_CSV,
    config.PHREEQC_RESULTS_CSV,
    config.PHREEQC_SI_CSV,
    config.PHREEQC_ASSEMBLAGE_CSV,
]

# Free-text columns get plain text inputs; a couple get friendly dropdowns.
# CO2 labels come straight from config so the form, validator, and plan agree.
_CO2_OPTIONS = [""] + list(config.CO2_CONDITION_ALLOWED)
_YESNO_OPTIONS = ["", "yes", "no"]

# The platform's forward arc (describe → simulate → validate → learn), shown as a neutral
# orientation stepper on Start. "Run model" is future work (no execution from Simulate yet).
WORKFLOW_STEPS = ["Describe experiment", "Extract scenario", "Simulation plan",
                  "Run model (future)", "Validate vs measured", "Learn & improve"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _run_script(relative_path: str, *args: str) -> subprocess.CompletedProcess:
    """Run a project script with the current interpreter, capturing output.

    Extra ``*args`` are passed through on the command line (e.g. ``--run NAME``).
    """
    return subprocess.run(
        [sys.executable, relative_path, *args],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
    )


def _show_process_result(label: str, proc: subprocess.CompletedProcess) -> None:
    if proc.returncode == 0:
        st.success(f"{label} finished (exit code 0).")
    else:
        st.error(f"{label} failed (exit code {proc.returncode}).")
    if proc.stdout:
        st.text_area("stdout", proc.stdout, height=220)
    if proc.stderr:
        st.text_area("stderr", proc.stderr, height=160)


@st.cache_data(show_spinner=False)
def _read_csv(path_str: str, mtime: float) -> pd.DataFrame:
    """Read a CSV, cache-keyed on path + mtime so edits invalidate the cache."""
    return pd.read_csv(path_str)


def _load_measured_safe() -> pd.DataFrame:
    try:
        # Non-strict so a partially-filled manual file still loads in the UI.
        return load_experimental_release(strict=False)
    except Exception as exc:  # pragma: no cover - defensive UI guard
        st.warning(f"Could not load experimental data: {exc}")
        return pd.DataFrame()


def _figure_dirs() -> list[Path]:
    """Where plots may live. Pipeline writes to reports/figures; outputs/figures
    is checked too since the task referred to it."""
    return [config.FIGURES_DIR, _PROJECT_ROOT / "outputs" / "figures"]


# --------------------------------------------------------------------------- #
# Experiment-run sidebar + workspace (the "save files" layer)
# --------------------------------------------------------------------------- #
def _run_type_warning(run_type: str) -> None:
    """Render the run-type warning with severity matching its meaning."""
    msg = run_manager.warning_for(run_type)
    if run_type == "lab_experiment":
        st.info(f"🧪 {msg}")
    elif run_type == "literature_benchmark":
        st.warning(f"📚 {msg}")
    elif run_type == "synthetic_demo":
        st.error(f"🧩 {msg}")
    else:  # plastic_composite
        st.warning(f"♻️ {msg}")


def _render_run_sidebar() -> str | None:
    """Sidebar 'Experiment runs' section: select or create a run.

    Returns the selected run's safe-name (or None). The selection persists across
    reruns via st.session_state['selected_run'].
    """
    st.sidebar.header("Experiment runs")
    runs = run_manager.list_runs()

    # --- select existing -------------------------------------------------- #
    current = st.session_state.get("selected_run")
    options = ["— none —"] + runs
    index = options.index(current) if current in runs else 0
    chosen = st.sidebar.selectbox("Open a run", options, index=index)
    st.session_state["selected_run"] = None if chosen == "— none —" else chosen

    # --- create new ------------------------------------------------------- #
    with st.sidebar.expander("➕ Create new run", expanded=not runs):
        new_name = st.text_input("Run name", key="new_run_name",
                                 placeholder="2026-06-03 fly-ash leaching experiment")
        new_type = st.selectbox("Run type", run_manager.RUN_TYPES, key="new_run_type")
        st.caption(run_manager.warning_for(new_type))
        new_desc = st.text_area("Description", key="new_run_desc", height=70)
        new_notes = st.text_input("Notes (optional)", key="new_run_notes")
        if st.button("Create run", use_container_width=True):
            raw = (new_name or "").strip()
            if not raw:
                st.error("Run name is required.")
            else:
                try:
                    safe = run_manager.safe_run_name(raw)
                    if run_manager.run_exists(safe):
                        st.error(f"A run named '{safe}' already exists — open it instead.")
                    else:
                        run_manager.create_run(
                            raw, new_type, description=new_desc, notes=new_notes
                        )
                        st.session_state["selected_run"] = safe
                        st.success(f"Created run '{safe}'.")
                        st.rerun()
                except run_manager.RunManagerError as exc:
                    st.error(str(exc))

    # --- show current ----------------------------------------------------- #
    selected = st.session_state.get("selected_run")
    st.sidebar.divider()
    if not selected:
        st.sidebar.caption("No run selected. Create or open one above.")
        return None

    cfg = run_manager.load_run_config(selected)
    st.sidebar.markdown(f"**Current run:** `{selected}`")
    st.sidebar.markdown(f"**Type:** `{cfg.get('run_type')}`")
    st.sidebar.markdown(f"**Source:** `{cfg.get('data_source')}`")
    st.sidebar.caption(f"📁 {_rel(run_manager.run_dir(selected))}")
    if cfg.get("description"):
        st.sidebar.caption(f"📝 {cfg['description']}")
    st.sidebar.caption(f"⚠️ {run_manager.warning_for(cfg.get('run_type'))}")
    st.sidebar.info("➡️ Use **Simulate** to plan, or **Compare Results** to validate against measured data.")
    return selected


def _render_ai_settings_panel() -> None:
    """Sidebar 'AI settings' panel — status + provider/model selection.

    Read-only with respect to the science: it shows whether the optional AI layer is
    enabled and lets the user pick the provider/model used for *suggestions*. It never
    affects mapping, residuals, validation status, or the comparison data, and it never
    shows or accepts the API key (the key comes only from the environment or Streamlit
    secrets — see :mod:`flyash_phreeqc_ml.ai.config`).
    """
    with st.sidebar.expander("🤖 AI settings", expanded=False):
        # The base model from env / secrets / default, ignoring any prior UI override —
        # so the picker defaults to it and a no-op selection never clobbers ANTHROPIC_MODEL.
        ai_config.clear_runtime_overrides()
        base_model = ai_config.resolve_config().model

        provider = st.selectbox(
            "Provider", list(ai_config.SUPPORTED_PROVIDERS), index=0,
            key="ai_provider_choice", help="Only Anthropic is supported today.")

        options = list(dict.fromkeys([base_model, *ai_config.SUGGESTED_MODELS]))
        picked = st.selectbox(
            "Model (suggested)", options, index=0, key="ai_model_pick",
            help="Used for AI suggestions only. Overrides ANTHROPIC_MODEL for this session.")
        custom = st.text_input(
            "…or enter a model id", key="ai_model_custom",
            help="Leave blank to use the selected model above.").strip()
        effective_model = custom or picked

        # Apply the choice for this process so the AI helpers + the status below use it.
        ai_config.set_runtime_overrides(provider=provider, model=effective_model)
        cfg = ai_config.resolve_config()

        st.markdown(f"**Status:** {'🟢 enabled' if cfg.enabled else '⚪ disabled'}")
        st.markdown(f"- Provider: `{cfg.provider}`")
        st.markdown(f"- Model: `{cfg.model}`")
        st.markdown(
            f"- API key detected: **{'yes' if cfg.key_present else 'no'}**"
            + (f" · {cfg.key_source}" if cfg.key_present else ""))
        st.markdown(f"- SDK available: **{'yes' if cfg.sdk_available else 'no'}**")
        st.caption(f"Role: {ai_config.AI_ROLE_LINE}.")
        if not cfg.enabled:
            st.caption(f"Disabled — {cfg.disabled_reason()}.")
        st.caption(
            "The API key is read only from the `ANTHROPIC_API_KEY` environment variable "
            "or a Streamlit secret — it is never entered or shown here.")
        st.warning(ai_config.AI_EXPERIMENTAL_WARNING)


# --------------------------------------------------------------------------- #
# Simulate tab — natural-language simulation planner (no PHREEQC execution)
# --------------------------------------------------------------------------- #
def _sim_num_text(label: str, value, key: str):
    """A text input for a numeric field that preserves 'missing' (blank → None)."""
    raw = st.text_input(label, value=("" if value is None else str(value)), key=key)
    return sim_schema.as_float(raw)


def _simulate_edit_form(flat: dict) -> dict:
    """Editable widgets for the key scenario fields; returns an edited flat dict.

    Numeric fields use text inputs so a blank stays 'missing' (None) rather than 0.
    """
    edited = dict(flat)
    c1, c2 = st.columns(2)
    with c1:
        edited["material_name"] = (st.text_input(
            "Material", value=flat.get("material_name") or "", key="sim_e_mat") or None)
        edited["solid_mass_g"] = _sim_num_text("Solid mass (g)", flat.get("solid_mass_g"), "sim_e_mass")
        edited["liquid_volume_mL"] = _sim_num_text(
            "Liquid volume (mL)", flat.get("liquid_volume_mL"), "sim_e_vol")
        edited["leachant_type"] = (st.text_input(
            "Leachant", value=flat.get("leachant_type") or "", key="sim_e_lea") or None)
        edited["leachant_concentration_M"] = _sim_num_text(
            "Leachant concentration (M)", flat.get("leachant_concentration_M"), "sim_e_conc")
    with c2:
        edited["time_min"] = _sim_num_text("Time (min)", flat.get("time_min"), "sim_e_time")
        edited["temperature_C"] = _sim_num_text(
            "Temperature (°C)", flat.get("temperature_C"), "sim_e_temp")
        co2_opts = ["(unknown)"] + list(sim_schema.CO2_CONDITION_ALLOWED)
        cur = flat.get("CO2_condition") or "(unknown)"
        cur = cur if cur in co2_opts else "(unknown)"
        co2 = st.selectbox("CO2 condition", co2_opts, index=co2_opts.index(cur), key="sim_e_co2")
        edited["CO2_condition"] = None if co2 == "(unknown)" else co2
        edited["target_elements"] = st.multiselect(
            "Target elements", list(sim_schema.RECOGNIZED_ELEMENTS),
            default=[e for e in (flat.get("target_elements") or [])
                     if e in sim_schema.RECOGNIZED_ELEMENTS], key="sim_e_els")
        edited["desired_outputs"] = st.multiselect(
            "Desired outputs", list(sim_schema.DESIRED_OUTPUTS_VOCAB),
            default=[o for o in (flat.get("desired_outputs") or [])
                     if o in sim_schema.DESIRED_OUTPUTS_VOCAB], key="sim_e_outs")
    # L/S is recomputed from the (possibly edited) mass + volume when the scenario rebuilds.
    edited["liquid_solid_ratio"] = None
    return edited


_PREVIEW_STATUS_LEVEL = {
    phreeqc_input_builder.STATUS_READY: "exact",
    phreeqc_input_builder.STATUS_TEMPLATE_WARNING: "scenario-level",
    phreeqc_input_builder.STATUS_NEEDS_COMPOSITION: "preliminary",
    phreeqc_input_builder.STATUS_DRAFT: "preliminary",
    phreeqc_input_builder.STATUS_MISSING_FIELD: "unsafe",
    phreeqc_input_builder.STATUS_UNSUPPORTED_LEACHANT: "unsafe",
}


def _render_phreeqc_input_preview(scenario, matrix) -> None:
    """Deterministic PHREEQC input preview from a confirmed plan — in-memory, download-only.

    No PHREEQC is run, no file is written to a run folder, and AI does not write the input
    (the LLM only extracted the scenario; this `.pqi` text is templated by deterministic code
    in `simulation/phreeqc_input_builder.py`).
    """
    st.markdown("#### Step 7 — PHREEQC input preview (draft)")
    st.caption(
        f"**{phreeqc_input_builder.PREVIEW_HEADER_LABEL}** Deterministic, rule-based `.pqi` "
        "text — AI does not write PHREEQC input. Nothing is run and no file is written to a "
        "run folder; the preview is in-memory and downloadable only.")
    if scenario is None:
        st.info("Re-generate the plan (Step 6) to enable the input preview.")
        return
    if st.button("Generate PHREEQC input preview", key="sim_pqi_btn"):
        with st.spinner("Templating PHREEQC input…"):
            st.session_state["sim_previews"] = phreeqc_input_builder.build_previews_for_matrix(
                scenario, matrix)
    previews = st.session_state.get("sim_previews")
    if not previews:
        st.info("Click **Generate PHREEQC input preview** to template a draft `.pqi` per scenario.")
        return

    ids = [p.scenario_id for p in previews]
    chosen_id = st.selectbox("Scenario", ids, key="sim_pqi_choice") if len(ids) > 1 else ids[0]
    pv = next(p for p in previews if p.scenario_id == chosen_id)

    st.markdown(
        f"**{pv.scenario_id}** · template `{pv.template_type}` · "
        + app_ui.status_badge(pv.status.replace("_", " "),
                              _PREVIEW_STATUS_LEVEL.get(pv.status, "neutral")),
        unsafe_allow_html=True)
    st.warning("⚠️ " + phreeqc_input_builder.PREVIEW_HEADER_LABEL)
    st.code(pv.phreeqc_input_text, language="text")
    st.download_button(
        "Download .pqi", pv.phreeqc_input_text,
        file_name=f"{pv.scenario_id}_preview.pqi", mime="text/plain",
        key=f"sim_pqi_dl_{pv.scenario_id}")
    if pv.warnings:
        st.markdown("**Warnings**")
        for w in pv.warnings:
            st.warning(w)
    if pv.assumptions:
        st.markdown("**Assumptions**")
        for a in pv.assumptions:
            st.markdown(f"- {a}")
    if pv.unsupported_features:
        st.markdown("**Unsupported / not yet modeled**")
        for u in pv.unsupported_features:
            st.markdown(f"- {u}")


def _render_simulate_tab(selected_run, dev_mode: bool) -> None:
    """Plan a simulation scenario from a plain-language description (planning layer only).

    Flow: describe → desired outputs → parse (AI if consented, else rule-based) → review
    what was understood + missing/assumptions/warnings → edit/confirm → choose a strategy →
    generate a plan matrix. **No deterministic simulation is run, no measured data is
    touched, nothing becomes verified.**
    """
    st.subheader("Simulate — describe an experiment, plan a simulation")
    st.caption(
        "This is the **planning layer**. It converts experiment descriptions into "
        "structured scenarios and simulation matrices. It does **not** yet prove scientific "
        "predictions until deterministic model execution and validation are performed. "
        "Describe a batch reaction / leaching experiment below — **no deterministic "
        "simulation (e.g. PHREEQC) is run here.**")

    cfg = ai_config.resolve_config()
    if cfg.enabled:
        st.caption(f"AI extraction is available (model `{cfg.model}`). Tick consent below to use "
                   "it, or parse with rule-based extraction.")
    else:
        st.caption("AI is disabled — the planner will use **rule-based** extraction (low "
                   "confidence). Enable AI in the sidebar **🤖 AI settings** for better extraction.")

    desc = st.text_area(
        "Step 1 — Describe your experiment", key="sim_desc", height=130,
        placeholder=("e.g. I have 2 g of Class C fly ash. I add 10 mL of 0.5 M HCl for 60 "
                     "minutes at room temperature. I centrifuge, filter the liquid, and measure "
                     "pH, Ca, Si, Al and Fe."))
    outputs = st.text_area(
        "Step 2 — Desired variables / outputs", key="sim_outputs", height=70,
        placeholder="e.g. simulate what should be in the liquid and what may have precipitated")

    use_ai = False
    if cfg.enabled:
        st.caption(ai_scenario_parser.SCENARIO_DATA_NOTICE)
        use_ai = st.checkbox(ai_scenario_parser.SCENARIO_CONSENT_LABEL, key="sim_ai_consent")

    if st.button("Step 3 — Parse scenario", key="sim_parse_btn", disabled=not desc.strip()):
        with st.spinner("Extracting scenario…"):
            st.session_state["sim_parse_result"] = ai_scenario_parser.parse_scenario(
                desc, outputs, prefer_ai=use_ai)
        st.session_state.pop("sim_matrix", None)

    res = st.session_state.get("sim_parse_result")
    if res is None:
        st.info("Enter a description and click **Parse scenario** to begin.")
        return

    # -- Step 4: review ---------------------------------------------------- #
    st.markdown(f"#### Step 4 — Review&nbsp;&nbsp;·&nbsp;&nbsp;parsed by **{res.source_label()}** "
                f"·&nbsp;&nbsp;confidence **{res.confidence:.0%}**")
    if res.used_ai:
        st.caption("Extracted by AI — review every value. AI output is a suggestion, never verified data.")
    else:
        st.caption("Rule-based extraction (no AI) — low confidence; check every value.")
    if res.error:
        st.warning(f"Parser note: {res.error}")

    flat = res.scenario.to_flat_dict()

    def _disp(v):
        # A single string column keeps Streamlit's Arrow serialization happy (mixed
        # float/bool/None/str in one object column otherwise fails to render).
        if v is None:
            return ""
        if isinstance(v, list):
            return ", ".join(str(x) for x in v)
        return str(v)

    overview = pd.DataFrame(
        [{"field": k, "value": _disp(v)}
         for k, v in flat.items() if k not in ("warnings", "confidence")])
    st.markdown("**What the planner understood**")
    st.dataframe(overview, use_container_width=True, height=300, hide_index=True)

    if res.missing:
        st.markdown("**Missing information**")
        for m in res.missing:
            icon = {"error": "🔴", "warning": "🟠"}.get(m.severity, "ℹ️")
            st.markdown(f"- {icon} **{m.label}** — {m.message}")
    if res.assumptions:
        st.markdown("**Assumptions**")
        for a in res.assumptions:
            st.markdown(f"- `{a.field}` = **{a.assumed_value}** — {a.reason} _(source: {a.source})_")
    if res.scenario.warnings:
        st.markdown("**Warnings**")
        for w in res.scenario.warnings:
            st.warning(w)

    st.info(sim_schema.NON_PREDICTION_NOTE)
    st.caption(
        "When this plan is eventually run, results will also depend on the chosen "
        "thermodynamic database (e.g. CEMDATA18) and the candidate-phase list — those model "
        "assumptions affect and limit what a simulation can predict.")

    if dev_mode and res.raw_response:
        with st.expander("Raw AI response (debug — not saved anywhere)"):
            st.code(res.raw_response)

    # -- Step 5: edit / confirm ------------------------------------------- #
    st.markdown("#### Step 5 — Edit / confirm")
    with st.expander("Edit extracted values", expanded=True):
        edited = _simulate_edit_form(flat)
    confirmed = st.checkbox(
        "I have reviewed the extracted scenario, assumptions, and warnings.",
        key="sim_confirm_chk")

    # -- Step 6: choose a simulation strategy, then generate the plan ------ #
    st.markdown("#### Step 6 — Simulation strategy")
    strategy = st.radio(
        "How should the plan be generated?",
        options=[
            "Single scenario (one plan row)",
            "Small parameter sweep",
            "Large batch / design-of-experiments — future",
            "Adaptive (active-learning) search — future",
            "Surrogate-assisted fast search — future",
        ],
        index=0, key="sim_strategy",
        help="Single scenario and small parameter sweeps are supported now. Large-batch, "
             "adaptive, and surrogate-assisted strategies are planned and disabled here.")
    is_future = strategy.endswith("— future")
    ranges = None
    if strategy.startswith("Small parameter sweep"):
        st.caption("Sweep one parameter over a few values (a small Cartesian plan; no "
                   "execution — still plan-only).")
        sweep_field = st.selectbox(
            "Parameter to sweep", list(sim_matrix.RANGEABLE_FIELDS), key="sim_sweep_field")
        sweep_raw = st.text_input(
            "Values (comma-separated)", key="sim_sweep_vals", placeholder="e.g. 0.1, 0.5, 1.0")
        vals = [v for v in (sim_schema.as_float(x) for x in sweep_raw.split(",")) if v is not None]
        if vals:
            ranges = {sweep_field: vals}
            st.caption(f"Plan will have {len(vals)} row(s).")
    if is_future:
        st.info("This strategy is planned for a future version and is disabled here. Use "
                "**Single scenario** or **Small parameter sweep** for now.")

    gen_disabled = (not confirmed) or is_future or (
        strategy.startswith("Small parameter sweep") and not ranges)
    if st.button("Generate simulation plan", key="sim_gen_btn", disabled=gen_disabled):
        sc = sim_schema.SimulationScenario.from_flat_dict(edited)
        sc.liquid_solid_ratio = sc.computed_ls_ratio()
        st.session_state["sim_matrix"] = sim_matrix.build_simulation_matrix(sc, ranges=ranges)
        st.session_state["sim_scenario"] = sc      # confirmed scenario → drives the .pqi preview
        st.session_state.pop("sim_previews", None)
    if not confirmed:
        st.caption("Confirm the reviewed scenario (Step 5) to enable plan generation.")

    mtx = st.session_state.get("sim_matrix")
    if mtx is not None:
        st.success(sim_schema.PLAN_ONLY_LABEL)
        st.info(
            "ℹ️ **Changing simulation-plan values does not update result graphs** (pH, "
            "residuals, measured-vs-model) until a deterministic model is executed. The "
            "Simulate tab produces a **plan table only** — it runs no model, writes no output "
            "file, and draws no result graph. The pH/residual graphs in **Validate** and "
            "**Compare Results** are driven by measured data + model results, not by this plan.")
        st.dataframe(mtx, use_container_width=True, height=160, hide_index=True)
        st.download_button(
            "Download plan (CSV)", mtx.to_csv(index=False), file_name="simulation_plan.csv",
            mime="text/csv", key="sim_dl_btn")
        st.caption("This plan is **not** a simulation result — deterministic execution is a "
                   "separate, deliberate step the planner never runs for you. (In the current "
                   "fly-ash + PHREEQC workflow, model generation lives in the **Match** tab; "
                   "future backends will run from here.)")
        st.divider()
        _render_phreeqc_input_preview(st.session_state.get("sim_scenario"), mtx)


def _import_raw_frame(run_name: str, up) -> tuple[pd.DataFrame | None, str, str]:
    """Read an uploaded CSV/Excel into a raw frame (handles sheet selection).

    Returns ``(raw_df_or_None, kind, sheet_name)``. Renders the file-type / sheet
    widgets and any read error inline. ``raw_df`` is None when the file can't be
    read yet (bad type, unreadable, or empty).
    """
    try:
        kind = import_mapping.file_kind(up.name)
    except import_mapping.ImportMappingError as exc:
        st.error(str(exc))
        return None, "", ""

    data = up.getvalue()
    sheet_name = ""
    if kind == "excel":
        try:
            sheets = import_mapping.list_excel_sheets(io.BytesIO(data))
        except import_mapping.ImportMappingError as exc:
            st.error(str(exc))
            return None, kind, ""
        sheet_name = st.selectbox(
            "Select sheet", sheets, key=f"lab_import_sheet_{run_name}",
            help="Excel workbooks can hold several sheets — pick the one to import.",
        )

    try:
        raw = import_mapping.read_tabular(io.BytesIO(data), kind=kind, sheet=sheet_name or None)
    except import_mapping.ImportMappingError as exc:
        st.error(str(exc))
        return None, kind, sheet_name
    except Exception as exc:  # pragma: no cover - UI guard
        st.error(f"Could not read file: {exc}")
        return None, kind, sheet_name

    if raw.empty:
        st.warning("The selected file / sheet has no rows.")
        return None, kind, sheet_name
    return raw, kind, sheet_name


def _import_render_report(report: dict) -> None:
    """Render the pre-save validation summary (Feature 7) inline."""
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows to save", report["n_rows"])
    c2.metric("Rows missing required", report["rows_missing_required"])
    c3.metric("Blank sample IDs", report["blank_sample_ids"])
    c4.metric("Rows with no values", report["rows_no_measured_values"])

    if report["missing_required_columns"]:
        st.error("Missing required column(s): "
                 + ", ".join(f"`{c}`" for c in report["missing_required_columns"]))
    if report["rows_missing_required"]:
        st.warning(f"⚠️ {report['rows_missing_required']} row(s) are missing one or more "
                   "required metadata fields (sample_id, date, fly_ash_type, NaOH_M, "
                   "time, temperature, L/S, CO2, initial/final pH).")
    if report["ph_out_of_range"]:
        st.warning("⚠️ pH outside 0–14: "
                   + "; ".join(f"row {p['row']} {p['column']}={p['value']:g}"
                               for p in report["ph_out_of_range"][:8]))
    if report["duplicate_sample_ids"]:
        st.warning("⚠️ Duplicate sample_id(s): "
                   + ", ".join(f"`{s}`" for s in report["duplicate_sample_ids"][:10]))
    if report["rows_no_measured_values"]:
        st.warning(f"⚠️ {report['rows_no_measured_values']} row(s) have no measured values "
                   "(no pH and no chemistry).")
    if report["converted_columns"]:
        st.info("Converted to mM: "
                + ", ".join(f"`{c}` (from {u})" for c, u in report["converted_columns"].items()))
    if report["classifications"]:
        st.caption("Row classification — "
                   + ", ".join(f"{k}: {v}" for k, v in report["classifications"].items()))


def _lab_data_import(run_name: str) -> None:
    """Data-tab import for a lab run — pick the import mode, then dispatch.

    Two modes share this run's ``experimental_release.csv``:
    a **generic** rectangular CSV/Excel importer, and a **special-case** parser for
    the Class C fly ash dissolution workbook (stacked ICP OES element blocks + a
    pH sheet). The generic importer is never removed.
    """
    st.markdown("**Upload experimental data file**")
    mode = st.radio(
        "Import mode",
        ["Generic table (CSV / Excel)", "Class C fly ash dissolution workbook"],
        key=f"lab_import_mode_{run_name}",
        help="Use the dissolution-workbook parser for the multi-block lab workbook "
             "(ICP OES element blocks + pH sheet); use the generic importer for a "
             "normal single-table CSV/Excel.",
    )
    if mode.startswith("Class C"):
        _dissolution_import(run_name)
    else:
        _generic_table_import(run_name)

    _render_model_predictions_import(run_name)


def _render_model_predictions_import(run_name: str) -> None:
    """"Import model predictions (CSV)" — a non-PHREEQC model via the generic contract.

    Same review-before-save pattern as the measured import. UI strings say *model
    predictions*; the model name is read from the file. Saved predictions become the
    manifest source (precedence over PHREEQC), so the same mapping/comparison applies.
    """
    from flyash_phreeqc_ml.parsers import generic_prediction_parser as gpp

    with st.expander("Import model predictions (CSV) — a model other than PHREEQC",
                     expanded=False):
        st.caption(
            "A non-PHREEQC model can supply predictions via the documented CSV contract "
            "(`docs/model_prediction_format.md`): required `record_key` + `model_name`, "
            "prediction columns `pred_pH` / `pred_Ca_mM` … in the profile's target units. "
            "These flow through the **same** manifest → mapping → comparison as PHREEQC."
        )
        up = st.file_uploader("Model predictions CSV", type=["csv"],
                              key=f"mp_up_{run_name}")
        if up is None:
            return
        try:
            raw = pd.read_csv(up)
        except Exception as exc:  # noqa: BLE001 - surface any read error to the user
            st.error(f"Could not read CSV: {exc}")
            return
        st.markdown("**Uploaded preview**")
        st.dataframe(raw.head(20), use_container_width=True, height=180)

        try:
            parsed = gpp.parse_predictions(raw)
        except gpp.PredictionContractError as exc:
            st.error(f"Contract error — fix the file and re-upload: {exc}")
            return

        model_names = sorted({str(m) for m in parsed["model_name"].unique()})
        st.success(f"Parsed {len(parsed)} prediction row(s) · model: "
                   f"{', '.join(model_names) or '(unnamed)'}.")
        manifest = scenarios.build_scenario_manifest(parsed)
        show_cols = [c for c in ("phreeqc_record_key", "scenario_label", "predicted_pH",
                                 "predicted_Ca_mM", "predicted_Si_mM", "predicted_Al_mM",
                                 "predicted_Fe_mM") if c in manifest.columns]
        st.markdown(f"**Model predictions to use ({', '.join(model_names) or 'model'}) — "
                    "review before save**")
        st.dataframe(manifest[show_cols], use_container_width=True, height=200,
                     hide_index=True)

        confirmed = st.checkbox(
            "I reviewed these model predictions and want to use them for mapping + "
            "comparison (they take precedence over PHREEQC).",
            key=f"mp_confirm_{run_name}")
        if st.button("Save model predictions", key=f"mp_save_{run_name}",
                     disabled=not confirmed):
            dest = config.PROCESSED_DIR / config.MODEL_PREDICTIONS_CSV
            dest.parent.mkdir(parents=True, exist_ok=True)
            parsed.to_csv(dest, index=False)
            audit.log_event(run_name, "model_predictions_import", {
                "file_name": getattr(up, "name", None),
                "model_names": model_names, "n_rows": int(len(parsed))})
            _scenario_manifest.clear()
            st.success(f"Saved {len(parsed)} model prediction(s) → "
                       f"`{_rel(dest)}`. The Match tab will use them.")
            st.rerun()


# --------------------------------------------------------------------------- #
# Optional AI import-assist (Data tab, generic importer) — suggestion-only.
# Everything it proposes flows into the existing review/confirm UI below; nothing
# AI-touched is saved without the explicit confirm-gated save.
# --------------------------------------------------------------------------- #
def _ai_sheet_previews(up, kind: str, raw: pd.DataFrame) -> list[dict]:
    """Build minimal per-sheet previews (headers + first rows) for classify_sheets."""
    n = import_assist.MAX_SAMPLE_ROWS
    if kind == "excel":
        data = up.getvalue()
        previews: list[dict] = []
        try:
            sheets = import_mapping.list_excel_sheets(io.BytesIO(data))
        except Exception:  # pragma: no cover - UI guard
            sheets = []
        for s in sheets:
            try:
                df = import_mapping.read_tabular(io.BytesIO(data), kind="excel", sheet=s)
            except Exception:
                continue
            previews.append({"sheet": s, "headers": [str(c) for c in df.columns],
                             "rows": df.head(n).astype(str).values.tolist()})
        return previews
    return [{"sheet": "csv", "headers": [str(c) for c in raw.columns],
             "rows": raw.head(n).astype(str).values.tolist()}]


def _ai_sample_id_source_column(run_name: str, raw: pd.DataFrame) -> str | None:
    """Which raw column feeds sample_id — AI suggestion first, else the fuzzy guess."""
    ai_colmap = st.session_state.get(f"ai_colmap_{run_name}") or []
    for c in ai_colmap:
        if c.get("target_col") == "sample_id" and c.get("source_col") in raw.columns:
            return c["source_col"]
    src = import_mapping.suggest_column_mapping(raw.columns).get("sample_id")
    return src if (src is not None and src in raw.columns) else None


def _render_ai_names_table(names: list[dict]) -> None:
    """Editable metadata-extraction table; each row badged rule / ai-suggested / unparsed."""
    badge = {import_assist.SOURCE_RULE: "rule", import_assist.SOURCE_AI: "ai-suggested"}
    rows = []
    for r in names:
        f = r.get("fields", {})
        rows.append({
            "sample_id": r.get("sample_id", ""),
            "provenance": badge.get(r.get("source"), "unparsed"),
            "confidence": r.get("confidence", 0.0),
            "leachant": f.get("leachant"), "concentration": f.get("concentration"),
            "condition_code": f.get("condition_code"), "time_min": f.get("time_min"),
            "replicate": f.get("replicate"), "note": r.get("note", ""),
        })
    st.markdown(
        app_ui.status_badge("rule", "exact") + " &nbsp; "
        + app_ui.status_badge("ai-suggested", "scenario-level") + " &nbsp; "
        + app_ui.status_badge("unparsed", "neutral"),
        unsafe_allow_html=True,
    )
    st.data_editor(pd.DataFrame(rows), use_container_width=True, height=240,
                   hide_index=True, key="ai_names_editor")
    st.caption("Suggestions only — saved rows keep their mapped values; the saved CSV "
               "records each row's provenance (`rule` / `ai-confirmed` / `manual`).")


def _render_ai_import_assist(run_name: str, up, kind: str, raw: pd.DataFrame) -> None:
    """The 'AI assist (optional)' expander: consent gate + a button per AI function.

    Suggestions land in session_state and are consumed by the existing column-mapping
    editor and the metadata-extraction table — nothing here saves or maps anything.
    """
    with st.expander("🤖 AI assist (optional) — propose interpretations of messy files"):
        if not import_assist.is_enabled():
            st.caption(
                "AI assist is disabled. Set the `ANTHROPIC_API_KEY` environment variable "
                "and `pip install anthropic` to enable optional suggestions. The importer "
                "works fully without it."
            )
            return

        st.caption(import_assist.DATA_LEAVES_MACHINE_NOTICE)
        consent = st.checkbox(import_assist.CONSENT_LABEL, key="ai_consent")
        if not consent:
            st.info("Tick the box above to allow sending headers + a small preview to the "
                    "API for these optional suggestions.")
            return

        headers = [str(c) for c in raw.columns]
        sample_rows = raw.head(import_assist.MAX_SAMPLE_ROWS).astype(str).values.tolist()
        b1, b2, b3 = st.columns(3)
        if b1.button("Classify sheets", key=f"ai_sheets_btn_{run_name}"):
            with st.spinner("Asking the model to classify sheets…"):
                st.session_state[f"ai_sheets_{run_name}"] = import_assist.classify_sheets(
                    _ai_sheet_previews(up, kind, raw))
        if b2.button("Suggest column mapping", key=f"ai_colmap_btn_{run_name}"):
            with st.spinner("Asking the model to map columns…"):
                st.session_state[f"ai_colmap_{run_name}"] = import_assist.propose_column_mapping(
                    headers, sample_rows, import_assist.default_target_schema())
            st.rerun()  # so the mapping editor below picks up the new defaults
        if b3.button("Extract sample-name fields", key=f"ai_names_btn_{run_name}"):
            src = _ai_sample_id_source_column(run_name, raw)
            ids = (raw[src].astype(str).tolist() if src else [])
            with st.spinner("Parsing sample names (rules first, AI for the rest)…"):
                st.session_state[f"ai_names_{run_name}"] = import_assist.parse_sample_names(
                    ids, profiles.default_dataset_profile())

        sheets = st.session_state.get(f"ai_sheets_{run_name}")
        if sheets:
            st.markdown("**Sheet classification (suggested)**")
            st.dataframe(pd.DataFrame(sheets), use_container_width=True, height=160,
                         hide_index=True)
        colmap = st.session_state.get(f"ai_colmap_{run_name}")
        if colmap:
            mapped = [c for c in colmap if c.get("target_col")]
            st.caption(f"🤖 {len(mapped)} column suggestion(s) applied as defaults in the "
                       "mapping editor below (badged *ai-suggested*).")
        names = st.session_state.get(f"ai_names_{run_name}")
        if names:
            st.markdown("**Sample-name fields (suggested)**")
            _render_ai_names_table(names)
        if st.button("Clear AI suggestions", key=f"ai_clear_{run_name}"):
            for k in (f"ai_sheets_{run_name}", f"ai_colmap_{run_name}", f"ai_names_{run_name}"):
                st.session_state.pop(k, None)
            st.rerun()


def _generic_table_import(run_name: str) -> None:
    """Generic CSV/Excel import into a lab run's experimental_release.csv.

    Reads .csv/.xlsx/.xls, previews the raw file, lets the user pick the Excel
    sheet, suggests a column mapping onto the app schema, converts chemistry units
    to mM, records leachant/provenance, validates, and only saves after explicit
    confirmation. Acid (HCl) rows are never forced into NaOH_M. Only ever writes to
    this lab run's own ``data/experimental_release.csv``.
    """
    st.caption(
        "Accepts `.csv`, `.xlsx`, or `.xls`. Preview → pick sheet → map columns → "
        "check units → confirm before saving to this run's `experimental_release.csv` "
        "(lab data only, never literature or synthetic)."
    )
    up = st.file_uploader(
        "Upload experimental data file", type=["csv", "xlsx", "xls"],
        key=f"lab_import_up_{run_name}",
    )
    if up is None:
        return

    raw, kind, sheet_name = _import_raw_frame(run_name, up)
    if raw is None:
        return

    # Feature 2 — raw preview before saving.
    st.markdown("**1 · Raw preview (nothing is saved yet)**")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("File", up.name)
    m2.metric("Type", kind)
    m3.metric("Sheet", sheet_name or "—")
    m4.metric("Raw shape", f"{raw.shape[0]} × {raw.shape[1]}")
    st.dataframe(raw, use_container_width=True, height=220)
    st.caption("Detected columns: " + ", ".join(f"`{c}`" for c in map(str, raw.columns)))

    # Optional AI assist — sets suggestion state consumed by the editors below.
    _render_ai_import_assist(run_name, up, kind, raw)

    # AI column-mapping suggestions (if any) become the editor defaults, layered
    # over the fuzzy guess. They are defaults only — the user still confirms.
    ai_colmap = st.session_state.get(f"ai_colmap_{run_name}") or []
    ai_target_to_source = {c["target_col"]: c["source_col"]
                           for c in ai_colmap if c.get("target_col")}
    ai_units = {c["source_col"]: c.get("unit_guess")
                for c in ai_colmap if c.get("unit_guess")}

    # Feature 3 — column mapping interface.
    st.markdown("**2 · Map uploaded columns → app schema**")
    st.caption("Suggestions are pre-filled from column names; adjust any of them. "
               "AI-suggested columns are badged below their selector.")
    suggestion = import_mapping.suggest_column_mapping(raw.columns)
    options = ["(leave blank)"] + [str(c) for c in raw.columns]
    mapping: dict[str, str | None] = {}
    mcols = st.columns(3)
    for i, target in enumerate(import_mapping.MAPPING_TARGETS):
        widget_col = mcols[i % 3]
        ai_default = ai_target_to_source.get(target)
        default = ai_default if (ai_default in options) else suggestion.get(target)
        idx = options.index(str(default)) if (default is not None and str(default) in options) else 0
        choice = widget_col.selectbox(target, options, index=idx, key=f"lab_map_{run_name}_{target}")
        if ai_default and ai_default in options:
            widget_col.caption("🤖 ai-suggested")
        mapping[target] = None if choice == "(leave blank)" else choice

    # Feature 4 — unit handling for mapped chemistry columns.
    st.markdown("**3 · Units for chemistry columns**")
    st.caption("mM = mg/L / atomic_mass. mg/L and ppm are equivalent; ppb = µg/L. "
               "Sc and total REE stay in ppb.")
    units: dict[str, str] = {}
    mapped_chem = [c for c in import_mapping.CHEM_VALUE_COLUMNS if mapping.get(c)]
    if mapped_chem:
        ucols = st.columns(3)
        for i, chem in enumerate(mapped_chem):
            ai_unit = ai_units.get(mapping.get(chem))
            uidx = (import_mapping.UNIT_OPTIONS.index(ai_unit)
                    if ai_unit in import_mapping.UNIT_OPTIONS else 0)
            units[chem] = ucols[i % 3].selectbox(
                f"{chem} unit", import_mapping.UNIT_OPTIONS, index=uidx,
                key=f"lab_unit_{run_name}_{chem}",
            )
    else:
        st.caption("No chemistry columns mapped — nothing to convert.")

    # Feature 5 — acid/base (leachant) support.
    st.markdown("**4 · Leachant (acid / base)**")
    default_leachant = st.selectbox(
        "Default leachant when not given in a column", ["NaOH", "HCl", "other"],
        key=f"lab_leachant_{run_name}",
    )
    st.caption(
        "A mapped `leachant` column overrides this per row. Rows that look like acid "
        "leaching get `NaOH_M` blanked, `leachant`/`acid_M` recorded, and a warning note."
    )

    # Feature 6/7 — build the transformed (schema-aligned) frame + validation.
    transformed = import_mapping.build_schema_frame(
        raw, mapping, units, filename=up.name, sheet_name=sheet_name,
        default_leachant=default_leachant,
    )
    st.markdown("**5 · Transformed preview & validation**")
    _import_render_report(import_mapping.summarize_import(transformed, units))
    st.dataframe(transformed, use_container_width=True, height=240)
    _render_unit_conversions_applied(transformed)

    if "sample_id" in transformed.columns:
        sid = transformed["sample_id"].astype(str).str.upper()
        flagged = int(sid.str.contains("TEST|SYNTH|MOCK|DEMO", na=False, regex=True).sum())
        if flagged:
            st.warning(f"⚠️ {flagged} sample_id(s) look like placeholders (TEST/SYNTH/MOCK/DEMO). "
                       "Only save real measured lab data into a lab run.")

    # Per-row metadata provenance: rule / ai-confirmed (AI-proposed and saved here) /
    # manual. Sourced from the sample-name extraction table; defaults to manual.
    names = st.session_state.get(f"ai_names_{run_name}") or []
    source_by_id = {str(r.get("sample_id")): r.get("source") for r in names}
    if "sample_id" in transformed.columns:
        sources = [source_by_id.get(str(sid), import_assist.SOURCE_MANUAL)
                   for sid in transformed["sample_id"].astype(str)]
    else:
        sources = [import_assist.SOURCE_MANUAL] * len(transformed)
    transformed[import_assist.METADATA_PROVENANCE_COLUMN] = (
        import_assist.build_provenance_column(sources))

    # Feature 8 — save options (replace / append), gated on confirmation.
    st.markdown("**6 · Save**")
    confirmed = st.checkbox(
        "I reviewed the imported data and understand these values will be saved to this "
        "experiment run.", key=f"lab_import_confirm_{run_name}",
    )
    existing = run_manager.read_data_file(run_name)

    def _do_save(mode: str) -> None:
        ctx = {"file_name": getattr(up, "name", None), "sheet": sheet_name or None,
               "column_mapping": {k: v for k, v in mapping.items() if v},
               "mapping_confirmed": True}
        dest = run_manager.save_lab_dataframe(run_name, transformed, mode=mode,
                                              audit_context=ctx)
        saved = run_manager.read_data_file(run_name)
        _read_csv.clear()
        st.success(f"Saved {len(transformed)} imported row(s) ({mode}) → "
                   f"`{_rel(dest)}`. Run now has {len(saved)} row(s).")
        st.dataframe(saved, use_container_width=True, height=240)

    if not existing.empty:
        st.info(f"This run already has {len(existing)} row(s). Choose how to save:")
        rc, ac = st.columns(2)
        if rc.button("Replace current run data", key=f"lab_import_replace_{run_name}",
                     disabled=not confirmed):
            _do_save("replace")
        if ac.button("Append to current run data", key=f"lab_import_append_{run_name}",
                     disabled=not confirmed):
            _do_save("append")
    else:
        if st.button("Save imported data to this run", key=f"lab_import_save_{run_name}",
                     disabled=not confirmed):
            _do_save("replace")


def _save_transformed(run_name: str, transformed: pd.DataFrame, key_prefix: str,
                      *, audit_context: dict | None = None) -> None:
    """Confirm-gated replace/append save of a transformed frame (shared by importers)."""
    confirmed = st.checkbox(
        "I reviewed the imported data and understand these values will be saved to this "
        "experiment run.", key=f"{key_prefix}_confirm_{run_name}",
    )
    existing = run_manager.read_data_file(run_name)

    def _do_save(mode: str) -> None:
        dest = run_manager.save_lab_dataframe(run_name, transformed, mode=mode,
                                              audit_context=audit_context)
        saved = run_manager.read_data_file(run_name)
        _read_csv.clear()
        st.success(f"Saved {len(transformed)} imported row(s) ({mode}) → "
                   f"`{_rel(dest)}`. Run now has {len(saved)} row(s).")
        st.dataframe(saved, use_container_width=True, height=240)

    if not existing.empty:
        st.info(f"This run already has {len(existing)} row(s). Choose how to save:")
        rc, ac = st.columns(2)
        if rc.button("Replace current run data", key=f"{key_prefix}_replace_{run_name}",
                     disabled=not confirmed):
            _do_save("replace")
        if ac.button("Append to current run data", key=f"{key_prefix}_append_{run_name}",
                     disabled=not confirmed):
            _do_save("append")
    else:
        if st.button("Save imported data to this run", key=f"{key_prefix}_save_{run_name}",
                     disabled=not confirmed):
            _do_save("replace")


def _dissolution_import(run_name: str) -> None:
    """Special-case importer for the Class C fly ash dissolution workbook.

    Parses the stacked ICP OES element blocks + pH sheet via
    :mod:`dissolution_workbook`, lets the user set shared metadata defaults and the
    NaOH/HCl scope, shows a normalised preview with parse counts and warnings, and
    saves (confirm-gated) into this lab run's ``experimental_release.csv``. The
    generic importer remains available via the mode selector above.
    """
    st.caption(
        "For the multi-block lab workbook: an **ICP OES** sheet (Calcium / Silicon / "
        "Aluminum blocks, columns NaOH-OA/PF/GS, mmol/l + mg/L) and a **pH** sheet "
        "(`0.5M NaOH-OA-10`-style rows). mmol/l is preferred; mg/L is converted to mM."
    )
    up = st.file_uploader(
        "Upload dissolution workbook (.xlsx / .xls)", type=["xlsx", "xls"],
        key=f"diss_up_{run_name}",
    )
    if up is None:
        return

    # Feature 6 — shared metadata the user sets once for every imported row.
    st.markdown("**1 · Default metadata for all imported rows**")
    st.caption("These are not in the workbook — set them once and they fill every row. "
               "Rows are not rejected just because these are blank.")
    d1, d2, d3 = st.columns(3)
    defaults = {
        "experiment_date": d1.text_input("experiment_date", key=f"diss_date_{run_name}"),
        "temperature_C": d2.text_input("temperature_C", key=f"diss_temp_{run_name}"),
        "liquid_solid_ratio": d3.text_input("liquid_solid_ratio", key=f"diss_ls_{run_name}"),
        "CO2_condition": d1.selectbox("CO2_condition", _CO2_OPTIONS, key=f"diss_co2_{run_name}"),
        "initial_pH": d2.text_input("initial_pH", key=f"diss_iph_{run_name}"),
        "fly_ash_type": d3.text_input("fly_ash_type", value=dissolution_workbook.FLY_ASH_DEFAULT,
                                      key=f"diss_fat_{run_name}"),
    }

    # Feature 10 — NaOH-only vs NaOH+HCl.
    st.markdown("**2 · Rows to import**")
    scope = st.radio(
        "Scope", ["Import only NaOH rows", "Import NaOH + HCl rows"],
        index=1, key=f"diss_scope_{run_name}",
    )
    include_hcl = scope.endswith("HCl rows")

    try:
        transformed, report = dissolution_workbook.normalize_dissolution_workbook(
            io.BytesIO(up.getvalue()), defaults=defaults, include_hcl=include_hcl,
            filename=up.name,
        )
    except dissolution_workbook.DissolutionWorkbookError as exc:
        st.error(str(exc))
        return
    except Exception as exc:  # pragma: no cover - UI guard
        st.error(f"Could not parse workbook: {exc}")
        return

    if transformed.empty:
        st.warning("No NaOH/HCl sample rows were parsed from the pH sheet — check the workbook "
                   "matches the expected structure.")
        return

    # Feature 8 — normalised preview + counts.
    st.markdown("**3 · Normalised preview (nothing is saved yet)**")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("NaOH rows", report["n_naoh"])
    c2.metric("HCl rows", report["n_hcl"])
    c3.metric("Rows with pH", report["n_with_ph"])
    c4.metric("Rows with Ca/Si/Al", report["n_with_chem"])
    c5.metric("Rows missing metadata", report["rows_missing_metadata"])

    # Feature 9 — warnings.
    for msg in report["warnings"]:
        st.warning("⚠️ " + msg)

    st.dataframe(transformed, use_container_width=True, height=300)

    # Feature 8 — debug view: detected ICP tables + joined chemistry.
    with st.expander("Debug — detected ICP mmol/l tables & joined chemistry", expanded=False):
        debug = report.get("icp_debug", {})
        if not debug:
            st.info("No ICP chemistry was detected. Check the ICP OES sheet has element block "
                    "labels (Calcium/Silicon/Aluminum), a `mg/L` and `mmol/l` header row, and "
                    "`NaOH-OA/PF/GS` columns.")
        else:
            st.caption("Each table is the value used per (time, condition) — mmol/l preferred, "
                       "mg/L converted to mM as a fallback.")
            for col, label in (("Ca_mM", "Calcium"), ("Si_mM", "Silicon"), ("Al_mM", "Aluminum")):
                if col in debug:
                    st.markdown(f"**{label} → `{col}`**")
                    st.dataframe(debug[col], use_container_width=True, height=180)
        st.markdown("**Joined normalised rows (chemistry columns)**")
        st.dataframe(
            transformed[["sample_id", "time_min", "extra__condition_code", "final_pH",
                         "Ca_mM", "Si_mM", "Al_mM"]],
            use_container_width=True, height=240,
        )

    # Save (confirm-gated).
    st.markdown("**4 · Save**")
    _save_transformed(run_name, transformed, key_prefix="diss",
                      audit_context={"file_name": getattr(up, "name", None),
                                     "sheet": "dissolution_workbook"})


def _lab_entry_form(run_name: str) -> None:
    """Measured-release entry form for a lab-type run (pH-only or full ICP)."""
    st.write(
        "Enter a measured-release row. **Leave any chemistry field blank if not "
        "measured** — pH-only rows are fine; add ICP numbers later."
    )
    with st.form(f"lab_entry_{run_name}", clear_on_submit=True):
        inputs: dict[str, str] = {}
        cols = st.columns(3)
        for i, column in enumerate(config.EXPERIMENTAL_RELEASE_COLUMNS):
            widget_col = cols[i % 3]
            numeric = column in config.EXPERIMENTAL_NUMERIC_COLUMNS
            label = f"{column} (number)" if numeric else column
            if column == "CO2_condition":
                inputs[column] = widget_col.selectbox(column, _CO2_OPTIONS, key=f"{run_name}_{column}")
            elif column == "precipitate_observed":
                inputs[column] = widget_col.selectbox(column, _YESNO_OPTIONS, key=f"{run_name}_{column}")
            else:
                inputs[column] = widget_col.text_input(label, value="", key=f"{run_name}_{column}")
        submitted = st.form_submit_button("Save row to this run")

    if submitted:
        errors: list[str] = []
        for column in config.EXPERIMENTAL_NUMERIC_COLUMNS:
            raw = (inputs.get(column) or "").strip()
            if raw and _is_not_number(raw):
                errors.append(f"'{column}' must be a number (got '{raw}').")
        if not (inputs.get("sample_id") or "").strip():
            errors.append("'sample_id' is required.")
        if errors:
            for e in errors:
                st.error(e)
        else:
            row = {c: (inputs.get(c) or "").strip() for c in config.EXPERIMENTAL_RELEASE_COLUMNS}
            path = run_manager.append_lab_row(run_name, row)
            st.success(f"Saved sample '{row['sample_id']}' to {_rel(path)}.")
            _read_csv.clear()


def _literature_entry(run_name: str) -> None:
    """Manual row entry + CSV upload for a literature-benchmark run."""
    st.write(
        "**Literature benchmark data** — values reported by other papers, for "
        "comparison only. This is kept separate from our measured experiment and is "
        "never written to a lab run's `experimental_release.csv`."
    )
    up = st.file_uploader("Upload a literature CSV", type=["csv"], key=f"lit_up_{run_name}")
    if up is not None:
        try:
            df = pd.read_csv(up)
            path = run_manager.save_literature_dataframe(run_name, df)
            st.success(f"Saved {len(df)} row(s) to {_rel(path)}.")
            _read_csv.clear()
        except Exception as exc:  # pragma: no cover - UI guard
            st.error(f"Could not read CSV: {exc}")

    with st.form(f"lit_entry_{run_name}", clear_on_submit=True):
        inputs: dict[str, str] = {}
        cols = st.columns(3)
        for i, column in enumerate(run_manager.LITERATURE_BENCHMARK_COLUMNS):
            widget_col = cols[i % 3]
            if column == "CO2_condition":
                inputs[column] = widget_col.selectbox(column, _CO2_OPTIONS, key=f"lit_{run_name}_{column}")
            else:
                inputs[column] = widget_col.text_input(column, value="", key=f"lit_{run_name}_{column}")
        submitted = st.form_submit_button("Add literature row")
    if submitted:
        if not (inputs.get("source_id") or "").strip():
            st.error("'source_id' is required for a literature row.")
        else:
            row = {c: (inputs.get(c) or "").strip() for c in run_manager.LITERATURE_BENCHMARK_COLUMNS}
            path = run_manager.append_literature_row(run_name, row)
            st.success(f"Added literature row '{row['source_id']}' to {_rel(path)}.")
            _read_csv.clear()


def _demo_entry(run_name: str) -> None:
    """Add synthetic demo rows (every row tagged source_type=synthetic_demo)."""
    st.error(
        "🧩 This is **synthetic / demo data only** — for testing the code, not for "
        "scientific conclusions. Every row is tagged `source_type=synthetic_demo`."
    )
    with st.form(f"demo_entry_{run_name}", clear_on_submit=True):
        inputs: dict[str, str] = {}
        cols = st.columns(3)
        for i, column in enumerate(config.EXPERIMENTAL_RELEASE_COLUMNS):
            widget_col = cols[i % 3]
            if column == "CO2_condition":
                inputs[column] = widget_col.selectbox(column, _CO2_OPTIONS, key=f"demo_{run_name}_{column}")
            else:
                inputs[column] = widget_col.text_input(column, value="", key=f"demo_{run_name}_{column}")
        submitted = st.form_submit_button("Add demo row")
    if submitted:
        if not (inputs.get("sample_id") or "").strip():
            st.error("'sample_id' is required.")
        else:
            row = {c: (inputs.get(c) or "").strip() for c in config.EXPERIMENTAL_RELEASE_COLUMNS}
            path = run_manager.append_demo_row(run_name, row)
            st.success(f"Added demo row '{row['sample_id']}' to {_rel(path)}.")
            _read_csv.clear()


def _is_not_number(raw: str) -> bool:
    try:
        float(raw)
        return False
    except ValueError:
        return True


def _run_lab_workflow(run_name: str) -> None:
    """Export a lab run to the pipeline, then run the relevant scripts in order.

    Stops at the first failing step (non-zero exit), showing the command, stdout,
    stderr and a pass/fail status for each. Only touches the pipeline's
    manual-entry file via the explicit export; other runs are unaffected.
    """
    # Step 1 — export this run's CSV into the pipeline's manual-entry location.
    st.markdown("**Step 1 — Export run data → pipeline**")
    try:
        dest = run_manager.export_lab_run_to_pipeline(run_name)
        st.success(f"Exported to `{_rel(dest)}`.")
    except run_manager.RunManagerError as exc:
        st.error(f"Export failed: {exc}")
        st.error("⛔ Workflow stopped — no scripts were run.")
        return

    # Mapping — needed for measured-vs-PHREEQC residuals. Export it if present,
    # otherwise warn (the workflow still runs, just without residuals).
    if run_manager.has_mapping(run_name):
        map_dest = run_manager.export_mapping_to_pipeline(run_name)
        st.success(f"Sample→PHREEQC mapping exported to `{_rel(map_dest)}`.")
    else:
        st.warning(
            "No measured-data → model mapping found. The workflow can still run, but "
            "measured-vs-model residuals will not be calculated. Add a mapping in "
            "the **Match** tab."
        )

    # Steps 2..N — run each script, halting on the first failure. The comparison
    # step gets ``--run`` so it also writes this run's own outputs + provenance
    # stamp under experiments/<run>/outputs/ (what the Results tab reads).
    steps = [
        ("Phase 1 pipeline", "scripts/run_phase1.py", ()),
        ("Validate experimental data", "scripts/07_validate_experimental_data.py", ()),
        ("Compare measured vs PHREEQC", "scripts/05_compare_experimental.py",
         ("--run", run_name)),
        ("Sustainability score", "scripts/08_sustainability_score.py", ()),
    ]
    for i, (label, script, args) in enumerate(steps, start=2):
        st.markdown(f"**Step {i} — {label}**")
        st.code(" ".join(["python", script, *args]), language="bash")
        with st.spinner(f"Running {label}…"):
            proc = _run_script(script, *args)
        _show_process_result(label, proc)
        audit.log_script_run(run_name, script=script, exit_status=int(proc.returncode))
        if proc.returncode != 0:
            st.error(f"⛔ Workflow stopped at step {i} ({label}) — see stderr above.")
            return

    st.success("✅ Workflow complete — all steps succeeded.")
    st.info(
        "Outputs written to:\n"
        f"- `experiments/{run_manager.safe_run_name(run_name)}/outputs/` — this run's "
        "comparison + figures (provenance-stamped)\n"
        "- `data/processed/` — parsed tables, master dataset, comparison\n"
        "- `outputs/tables/` — validation report, sustainability score\n"
        "- `reports/figures/` — plots"
    )
    _read_csv.clear()  # processed CSVs changed; refresh the viewers below


def _render_mapping_quality(mapping: pd.DataFrame) -> None:
    """Small mapping-quality summary + collision warning for the Mapping tab."""
    summary = run_manager.summarize_mapping(mapping)
    if summary["n_samples"] == 0:
        return

    st.markdown("**Mapping quality**")
    m1, m2 = st.columns(2)
    m1.metric("Mapped samples", summary["n_samples"])
    m2.metric("Unique model results used", summary["n_unique_rows"])

    if summary["samples_per_row"]:
        per_row = pd.DataFrame(
            [{"model_result": k, "samples_mapped": v}
             for k, v in summary["samples_per_row"].items()]
        )
        st.caption("Samples per model result:")
        st.dataframe(per_row, use_container_width=True, height=170)

    if summary["has_collisions"]:
        st.warning(
            "Multiple samples are mapped to the same model result. Scatter plots may "
            "appear as vertical lines because the model prediction is identical for "
            "those samples.\n\n"
            "Your graph may form a vertical line because several samples share the "
            "same model prediction."
        )
    if summary["n_samples"] > summary["n_unique_rows"]:
        st.warning(
            "There are more samples than distinct model results, so the comparison may "
            "not represent distinct model conditions."
        )


@st.cache_data(show_spinner=False)
def _scenario_manifest(results_path_str: str, mtime: float) -> pd.DataFrame:
    """Build (and persist) the PHREEQC scenario manifest, cached on results mtime."""
    manifest = scenarios.build_scenario_manifest(pd.read_csv(results_path_str))
    dest = scenarios.scenario_manifest_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    manifest.to_csv(dest, index=False)  # data/processed/ is gitignored
    return manifest


# Columns the Scenario Explorer table shows (readable subset of the manifest).
_EXPLORER_COLUMNS = [
    "scenario_label", "source_file", "state", "solution_number",
    "predicted_pH", "predicted_Ca_mM", "predicted_Si_mM", "predicted_Al_mM",
    "predicted_Fe_mM", "liquid_solid_ratio", "CO2_condition", "metadata_quality",
]


def _render_scenario_explorer(run_name: str, manifest: pd.DataFrame) -> None:
    """Feature 2 — filterable, readable table of PHREEQC scenarios."""
    st.markdown("#### PHREEQC Scenario Explorer")
    st.caption(
        "Each PHREEQC result row described in plain terms, with metadata inferred "
        "from the source filename where safe (anything uncertain is `unknown`)."
    )
    if manifest.empty:
        st.info("No PHREEQC scenarios yet — run Phase 1 to generate results.")
        return

    f1, f2, f3, f4 = st.columns(4)
    state_choice = f1.selectbox(
        "state", ["batch only", "initial only", "all"], key=f"exp_state_{run_name}")
    co2_opts = ["all"] + sorted(manifest["CO2_condition"].dropna().astype(str).unique())
    co2_choice = f2.selectbox("CO2_condition", co2_opts, key=f"exp_co2_{run_name}")
    ls_vals = sorted(
        {f"{v:g}" for v in manifest["liquid_solid_ratio"].dropna().tolist()})
    ls_choice = f3.selectbox("liquid_solid_ratio", ["all"] + ls_vals, key=f"exp_ls_{run_name}")
    src_opts = ["all"] + sorted(manifest["source_file"].dropna().astype(str).unique())
    src_choice = f4.selectbox("source_file", src_opts, key=f"exp_src_{run_name}")

    view = manifest
    if state_choice == "batch only":
        view = view[view["state"].astype(str).str.lower() == "batch"]
    elif state_choice == "initial only":
        view = view[view["state"].astype(str).str.lower() == "initial"]
    if co2_choice != "all":
        view = view[view["CO2_condition"].astype(str) == co2_choice]
    if ls_choice != "all":
        view = view[view["liquid_solid_ratio"].map(lambda v: f"{v:g}") == ls_choice]
    if src_choice != "all":
        view = view[view["source_file"].astype(str) == src_choice]

    cols = [c for c in _EXPLORER_COLUMNS if c in view.columns]
    st.dataframe(view[cols], use_container_width=True, height=280)
    st.caption(f"{len(view)} of {len(manifest)} scenarios shown.")

    st.info(
        "**sol1, sol2, sol3 are PHREEQC solution numbers / repeated solution outputs.** They "
        "should not be interpreted as experimental time points unless the PHREEQC input "
        "explicitly defines them that way. **In this project, sol1/sol2/sol3 should be treated "
        "as replicate/batch outputs for the same broad scenario, not as 10/20/60 min time points.**"
    )
    descriptions = scenarios.load_solution_descriptions()
    with st.expander("What do sol1 / sol2 / sol3 represent? (from the PHREEQC input files)"):
        if descriptions.empty:
            st.caption(
                "No parsed PHREEQC input solutions found yet (run Phase 1). PHREEQC `.pqi` "
                "files label each `SOLUTION n`; those labels — not time or replicate — are "
                "all the app can attribute to a solution number."
            )
        else:
            st.caption(
                "Parsed from the `.pqi` input files (`SOLUTION n <label>`). A blank/generic "
                "label means the input did not describe that solution as a time point or replicate."
            )
            st.dataframe(descriptions, use_container_width=True, height=200)


_ASSISTANT_META_FIELDS = [
    "NaOH_M", "time_min", "liquid_solid_ratio", "CO2_condition",
    "temperature_C", "final_pH", "Ca_mM", "Si_mM", "Al_mM", "Fe_mM",
]


def _render_mapping_assistant(run_name: str, data: pd.DataFrame,
                              sample_ids: list[str], manifest: pd.DataFrame) -> None:
    """Features 3/4/5 — sample metadata, top-3 suggestions, approve, no-match warning."""
    st.markdown("#### Mapping Assistant")
    st.caption(
        "Pick a measured sample; the assistant scores PHREEQC scenarios with simple, "
        "transparent rules (no ML) and suggests the best matches."
    )
    sel_sample = st.selectbox("Experimental sample_id", sample_ids,
                              key=f"assist_sample_{run_name}")
    sample_row = data[data["sample_id"].astype(str).str.strip() == sel_sample]
    sample = sample_row.iloc[0].to_dict() if not sample_row.empty else {"sample_id": sel_sample}

    meta = {f: sample.get(f, "") for f in _ASSISTANT_META_FIELDS if f in sample}
    exp_code = scenarios.sample_condition_code(sample)
    if exp_code:
        meta["condition_code"] = exp_code
        meta["cover_condition"] = scenarios.cover_condition(exp_code) or ""
        meta["CO2_exposure_level"] = scenarios.co2_exposure_level(exp_code) or ""
    if meta:
        st.markdown("**Measured metadata (known):**")
        st.dataframe(pd.DataFrame([meta]), use_container_width=True, height=80)
        st.caption(
            "The model (currently PHREEQC) knows L/S, CO2, state, solution number and "
            "predicted pH/Ca/Si/Al — but often **not** fine-grained measured metadata such "
            "as exact time, condition code, or concentration, so an exact match can't always "
            "be confirmed (see notes below)."
        )

    if manifest.empty:
        st.info("No PHREEQC scenarios available to suggest yet — run Phase 1 first.")
        return

    suggestions = scenarios.suggest_mappings(sample, manifest, top_n=3)
    if not suggestions or suggestions[0]["confidence"] == "low":
        # Feature 5 — best match is weak.
        st.error(
            "No strong PHREEQC match exists for this sample. The comparison may be "
            "misleading. Consider generating a new PHREEQC simulation for this "
            "experimental condition."
        )

    for i, sug in enumerate(suggestions):
        conf = sug["confidence"]
        badge = {"high": "🟢 high", "medium": "🟡 medium", "low": "🔴 low"}.get(conf, conf)
        with st.container(border=True):
            st.markdown(f"**{sug['scenario_label']}** · score {sug['score']} · {badge} confidence")
            st.caption(f"`{sug['suggested_phreeqc_record_key']}`")
            if sug.get("base_confidence") and sug["base_confidence"] != conf:
                st.caption(
                    f"Capped from {sug['base_confidence']} → {conf}: PHREEQC does not specify "
                    f"{', '.join(sug.get('phreeqc_missing', [])) or 'some experimental metadata'}."
                )
            st.markdown(
                f"- **Reason:** {sug['reason']}\n"
                f"- **Matched:** {', '.join(sug['matched_fields']) or '—'}\n"
                f"- **Mismatched:** {', '.join(sug['mismatched_fields']) or '—'}\n"
                f"- **Missing PHREEQC metadata:** {', '.join(sug.get('phreeqc_missing', [])) or '—'}"
            )
            for note in sug.get("metadata_notes", []):
                st.warning("ℹ️ " + note)
            if st.button("Use this mapping", key=f"assist_use_{run_name}_{i}"):
                try:
                    run_manager.add_mapping(
                        run_name, sel_sample, sug["suggested_phreeqc_record_key"])
                    st.success(
                        f"Mapped `{sel_sample}` → `{sug['suggested_phreeqc_record_key']}`.")
                    _read_csv.clear()
                    st.rerun()
                except run_manager.RunManagerError as exc:
                    st.error(str(exc))


def _render_generate_simulation(run_name: str, data: pd.DataFrame,
                                manifest: pd.DataFrame, needs_new: pd.DataFrame) -> None:
    """Generate (and optionally run) a PHREEQC simulation for a needs-new condition.

    Preview-first: the templated ``.pqi`` and its chemistry-assumptions banner are
    always shown (no PHREEQC needed to read them). Running + ingesting requires a
    configured PHREEQC; afterwards the suggestion table refreshes and the new
    scenario can be mapped (OA → exact; PF/GS → scenario-level by design).
    """
    app_ui.section_header("Generate a PHREEQC simulation",
                          "make a needs-new condition actionable")
    configured = phreeqc_runner.is_configured()
    if not configured:
        st.info("PHREEQC is not configured, so simulations can be **previewed** here but not "
                "run. Set `PHREEQC_EXE` + `PHREEQC_DATABASE` to enable running. The generated "
                "input below is still fully readable.")

    profile = profiles.default_dataset_profile()
    ck = st.selectbox("Condition needing a new simulation",
                      needs_new["condition_key"].astype(str).tolist(),
                      key=f"gen_ck_{run_name}")
    if st.button("Generate simulation input", key=f"gen_btn_{run_name}"):
        sample = mapping_table.condition_representative_sample(data, ck, profile)
        reason = phreeqc_runner.generation_blocked_reason(sample, profile)
        if reason:
            st.warning("⚠️ " + reason)
            st.session_state.pop(f"gen_inputs_{run_name}", None)
        else:
            st.session_state[f"gen_inputs_{run_name}"] = {
                "ck": ck, "inputs": phreeqc_runner.build_input(sample, profile)}

    payload = st.session_state.get(f"gen_inputs_{run_name}")
    if not payload or payload.get("ck") != ck:
        return
    inputs = payload["inputs"]
    if not inputs:
        st.info("No simulation can be templated for this condition.")
        return

    for gi in inputs:
        with st.expander(f"Generated input — {gi.model_label} ({gi.condition_code})",
                         expanded=True):
            app_ui.render_warning_panel(
                "Chemistry assumptions — read before running",
                "; ".join(gi.assumptions), level="warning")
            st.code(gi.pqi_text, language="text")

    if not configured:
        app_ui.render_warning_panel("PHREEQC not configured", phreeqc_runner._SETUP_HELP,
                                    level="error")
        return

    if st.button(f"▶️ Run PHREEQC for {len(inputs)} variant(s) & ingest",
                 key=f"gen_run_{run_name}", type="primary"):
        workdir = run_manager.generated_simulations_dir(run_name)
        ok = 0
        for gi in inputs:
            try:
                out = phreeqc_runner.run(gi.pqi_text, workdir, basename=gi.basename)
                keys = phreeqc_runner.ingest(out, run_name,
                                             condition_key=gi.source_condition_key,
                                             metadata=gi.metadata)
                st.write(f"✅ **{gi.model_label}** — {len(keys)} record(s) ingested")
                ok += 1
            except phreeqc_runner.PhreeqcRunnerError as exc:
                st.write(f"❌ **{gi.model_label}** — {str(exc).splitlines()[0]}")
        if ok:
            st.success(f"Ingested {ok} generated scenario(s) into the shared results — "
                       "the suggestion table will refresh with the new candidate(s).")
            _read_csv.clear()
            _scenario_manifest.clear()
            st.session_state.pop(f"gen_inputs_{run_name}", None)
            st.rerun()


def _render_sim_needed(run_name: str, data: pd.DataFrame, mapping: pd.DataFrame,
                       manifest: pd.DataFrame, table: pd.DataFrame) -> None:
    """Conditions whose best candidate is *needs new simulation*.

    Driven from the **same** suggestion ``table`` shown above, so the count here
    always agrees with the table's status column.
    """
    st.markdown(f"#### Conditions needing new {MODEL_NAME} simulations")
    needs_new = mapping_table.needs_new_simulation(table)
    st.caption(
        "Conditions with no usable model/simulation candidate (status **needs new "
        f"simulation** in the suggestion table). Generate matching {MODEL_NAME} scenarios for "
        "these. Conditions that are *scenario-level only* or *unsafe* appear in the table "
        "above with that status, not here."
    )
    if needs_new.empty:
        st.success("Every measured condition has at least one candidate model scenario.")
    else:
        st.dataframe(needs_new[["condition_key", "n_replicates", "confidence", "reason"]],
                     use_container_width=True, height=240)
        # Make it actionable: generate a PHREEQC input for one of these conditions.
        _render_generate_simulation(run_name, data, manifest, needs_new)

    # Keep the older per-sample view available but out of the way.
    with st.expander("Per-sample detail (no mapping / low confidence / collisions)"):
        needed = scenarios.samples_needing_simulation(data, mapping, manifest)
        if needed.empty:
            st.success("Every sample has a confident, non-colliding PHREEQC mapping.")
        else:
            st.dataframe(needed, use_container_width=True, height=240)


def _render_manual_mapping(run_name: str, phreeqc: pd.DataFrame,
                           sample_ids: list[str]) -> None:
    """Feature 7 — the original manual dropdown, kept as advanced mode."""
    st.warning(
        "Use manual mapping only if you understand what the PHREEQC row represents."
    )
    only_batch = st.checkbox(
        "Only show PHREEQC 'batch' rows (post-equilibration)",
        value=True, key=f"map_batch_{run_name}",
    )
    view = phreeqc
    if only_batch and "state" in phreeqc.columns:
        batch = phreeqc[phreeqc["state"] == "batch"]
        view = batch if not batch.empty else phreeqc

    label_cols = [
        c for c in ["record_key", "source_file", "simulation", "state",
                    "solution_number", "pH", "mol_Ca", "mol_Si", "mol_Al", "mol_Na"]
        if c in view.columns
    ]

    def _phreeqc_label(pos) -> str:
        row = view.loc[pos]
        return " | ".join(f"{c}={row[c]}" for c in label_cols)

    c1, c2 = st.columns(2)
    with c1:
        sel_sample = st.selectbox("sample_id", sample_ids, key=f"map_sample_{run_name}")
    with c2:
        sel_pos = st.selectbox(
            "PHREEQC result row", list(view.index),
            format_func=_phreeqc_label, key=f"map_pheq_{run_name}",
        )

    if st.button("Save mapping", key=f"map_save_{run_name}"):
        record_key = str(view.loc[sel_pos, "record_key"]).strip()
        try:
            run_manager.add_mapping(run_name, sel_sample, record_key)
            st.success(f"Mapped `{sel_sample}` → `{record_key}`.")
            _read_csv.clear()
            st.rerun()
        except run_manager.RunManagerError as exc:
            st.error(str(exc))


def _render_replicate_summary(data: pd.DataFrame) -> None:
    """Feature 2 — replicate count + mean/std per experimental condition."""
    st.markdown("#### Replicate summary (grouped by condition)")
    st.caption(
        "Rows are grouped by `condition_key` (leachant + molarity + OA/PF/GS + time + "
        "L/S + CO2). `replicate_id` is read from the sample_id (R1/R2/R3, rep…, batch…). "
        "std needs ≥2 replicates."
    )
    summary = replicates.replicate_summary(data)
    if summary.empty:
        st.info("No rows to summarise yet.")
        return
    st.dataframe(summary, use_container_width=True, height=220)
    singles = summary[summary["number_of_replicates"] < 2]
    if not singles.empty:
        st.warning(f"⚠️ {len(singles)} condition(s) have a single replicate — no standard "
                   "deviation can be estimated for them.")


def _scenario_record_key_picker(run_name: str, manifest: pd.DataFrame, key: str) -> str | None:
    """Selectbox over batch-first manifest scenarios -> a PHREEQC record_key."""
    if manifest.empty:
        st.info("No PHREEQC scenarios yet — run Phase 1 first.")
        return None
    view = manifest.copy()
    view["_batch"] = view["state"].astype(str).str.lower().eq("batch")
    view = view.sort_values("_batch", ascending=False)
    keys = view["phreeqc_record_key"].astype(str).tolist()
    labels = {
        r["phreeqc_record_key"]: f"{r.get('scenario_label', '')}  ·  {r['phreeqc_record_key']}"
        for _, r in view.iterrows()
    }
    return st.selectbox("Model / simulation result", keys,
                        format_func=lambda k: labels.get(k, k), key=key,
                        help="Currently PHREEQC scenario rows. Each label includes its "
                             "PHREEQC file/source and solution number.")


# Status → (emoji, Streamlit alert level) for the simple mapping-status line.
_MAPPING_STATUS_BADGE = {
    replicates.MAPPING_STATUS_EXACT: ("✅", "success"),
    replicates.MAPPING_STATUS_SCENARIO: ("🟡", "warning"),
    replicates.MAPPING_STATUS_UNSAFE: ("🔴", "error"),
    replicates.MAPPING_STATUS_NEEDS_NEW: ("⚠️", "warning"),
}


def _manifest_row(manifest: pd.DataFrame, record_key: str | None) -> dict | None:
    """The manifest row for a PHREEQC ``record_key`` as a dict (or ``None``)."""
    if not record_key or manifest.empty or "phreeqc_record_key" not in manifest.columns:
        return None
    hit = manifest[manifest["phreeqc_record_key"].astype(str) == str(record_key)]
    return hit.iloc[0].to_dict() if not hit.empty else None


def _condition_representative_sample(data: pd.DataFrame, condition_key: str) -> dict:
    """First measured row belonging to ``condition_key`` (for metadata/status)."""
    ann = replicates.annotate(data)
    sub = ann[ann[replicates.CONDITION_KEY_COLUMN].astype(str) == str(condition_key)]
    return sub.iloc[0].to_dict() if not sub.empty else {}


# Project-specific (fly ash) explanation — only shown when the dataset actually
# uses OA/PF/GS condition codes, so the app stays generic for other experiments.
def _condition_code_descriptions_md(codes) -> str:
    """Markdown describing the given condition codes, sourced from the config dict.

    ``config.CONDITION_CODE_DESCRIPTIONS`` is the single source of truth — the UI
    never hard-codes the wording or the not-confirmed-sealed caution.
    """
    lines = []
    for code in sorted(codes):
        info = config.CONDITION_CODE_DESCRIPTIONS.get(str(code).strip().upper())
        if not info:
            continue
        caution = f" — ⚠️ {info['caution']}" if info.get("caution") else ""
        lines.append(f"- **{code} = {info['label']}**: {info['description']}{caution}")
    return "\n".join(lines)


def _dataset_condition_codes(data: pd.DataFrame) -> set[str]:
    """OA/PF/GS codes actually present in this run's measured rows (may be empty)."""
    codes: set[str] = set()
    if data is None or data.empty:
        return codes
    for _, r in data.iterrows():
        code = scenarios.sample_condition_code(r.to_dict())
        if code:
            codes.add(code)
    return codes


def _render_condition_code_help(data: pd.DataFrame) -> None:
    """Show the cup-cover condition descriptions *only* if this dataset uses the codes."""
    codes = _dataset_condition_codes(data)
    if not codes:
        return
    with st.expander(f"What do the condition codes ({', '.join(sorted(codes))}) mean? (this dataset)"):
        st.markdown(_condition_code_descriptions_md(codes))
        st.caption("CO₂-exposure cup covers. Source: config.CONDITION_CODE_DESCRIPTIONS.")


def _render_mapping_status_line(sample: dict, scenario: dict | None) -> str:
    """Show the four-state mapping status as a compact, color-coded line."""
    status = replicates.mapping_status(sample, scenario)
    st.markdown("**Mapping status:** " + app_ui.status_badge(status, status),
                unsafe_allow_html=True)
    st.caption(replicates.MAPPING_STATUS_DEFINITIONS.get(status, ""))
    return status


# Generic validation-metadata columns surfaced when present (project-agnostic).
_GENERIC_META_FIELDS = [
    "leachant", "acid_M", "NaOH_M", "time_min", "liquid_solid_ratio",
    "temperature_C", "CO2_condition",
]


def _render_advanced_validation_metadata(sample: dict, scenario: dict | None) -> None:
    """Expander body — measured-side metadata (dynamic) + model-side PHREEQC metadata.

    Metadata is never discarded (it stays on every saved row); this view only
    *surfaces* whatever columns the current dataset actually has, so the app does
    not assume every experiment carries OA/PF/GS, CO₂ cover, or leachant fields.
    """
    # --- Measured-side metadata, shown dynamically ---
    derived: dict = {}
    exp_code = scenarios.sample_condition_code(sample)
    if exp_code:  # project-specific (fly ash) derived fields, only when codes exist
        derived["condition_code"] = exp_code
        derived["cover_condition"] = scenarios.cover_condition(exp_code) or "—"
        derived["CO2_exposure_level"] = scenarios.co2_exposure_level(exp_code) or "—"
    for f in _GENERIC_META_FIELDS:
        if _is_present(sample.get(f)):
            derived[f] = sample.get(f)
    # any other non-canonical metadata the importer preserved (extra__* columns)
    for k, v in sample.items():
        if str(k).startswith("extra__") and _is_present(v) and k not in (
                "extra__condition_code", "extra__cover_condition", "extra__CO2_exposure_level"):
            derived[k] = v

    if exp_code:
        st.caption(
            "Condition codes, cover type and CO₂ exposure are **auto-derived** from the "
            "sample/condition name for this dataset — no manual selection needed."
        )
    else:
        st.caption("Validation metadata available for this dataset (varies by experiment).")
    if derived:
        st.dataframe(pd.DataFrame([derived]), use_container_width=True, height=80)
    else:
        st.info("No extra validation metadata columns found for this record.")
    # Cup-cover description + not-confirmed-sealed caution, from config (single source).
    if exp_code and str(exp_code).upper() in config.CONDITION_CODE_DESCRIPTIONS:
        st.markdown(_condition_code_descriptions_md({exp_code}))

    if not scenario:
        st.info("Pick a model / simulation result above to see matched / missing / conflicting fields.")
        return

    # --- Model-side metadata (currently PHREEQC) ---
    st.markdown("**Model prediction source (PHREEQC)**")
    pheq_source = {
        "file/source": scenario.get("source_file", "—"),
        "solution number": scenario.get("solution_number", "—"),
        "state": scenario.get("state", "—"),
        "scenario": scenario.get("scenario_label", "—"),
    }
    st.dataframe(pd.DataFrame([pheq_source]), use_container_width=True, height=80)

    pheq_fields = ["liquid_solid_ratio", "CO2_condition", "time_min",
                   "condition_code", "NaOH_M", "temperature_C"]
    avail = {f: ("present" if _is_present(scenario.get(f)) else "not specified")
             for f in pheq_fields}
    st.markdown("**Model metadata availability**")
    st.dataframe(pd.DataFrame([avail]), use_container_width=True, height=80)

    score = scenarios.score_scenario(sample, scenario)
    st.markdown(
        f"- **Matched fields:** {', '.join(score['matched_fields']) or '—'}\n"
        f"- **Missing model fields:** {', '.join(score.get('phreeqc_missing', [])) or '—'}\n"
        f"- **Conflicting fields:** {', '.join(score['mismatched_fields']) or '—'}"
    )
    for note in score.get("metadata_notes", []):
        st.caption("ℹ️ " + note)


# Status → a badged display string for the table's status column / detail headers.
def _status_badge(status: str) -> str:
    emoji = _MAPPING_STATUS_BADGE.get(status, ("•", "info"))[0]
    return f"{emoji} {status}"


def _map_flash(run_name: str, level: str, msg: str) -> None:
    """Queue a message that should survive the post-accept rerun (shown atop the table)."""
    st.session_state.setdefault(f"map_flash_{run_name}", []).append((level, msg))


def _render_map_flash(run_name: str) -> None:
    for level, msg in st.session_state.pop(f"map_flash_{run_name}", []):
        getattr(st, level, st.info)(msg)


# Metadata fields compared side-by-side in the row-detail alignment table.
_ALIGNMENT_FIELDS = ["liquid_solid_ratio", "CO2_condition", "temperature_C",
                     "time_min", "NaOH_M"]


def _fmt_meta(value) -> str:
    return str(value) if _is_present(value) else "—"


def _alignment_value_table(sample: dict, scenario: dict) -> pd.DataFrame:
    """Measured vs model values for each comparison field (both sides, with values)."""
    rows = [{"field": f, "measured": _fmt_meta(sample.get(f)),
             "model (PHREEQC)": _fmt_meta(scenario.get(f))} for f in _ALIGNMENT_FIELDS]
    rows.append({
        "field": "condition_code",
        "measured": scenarios.sample_condition_code(sample) or "—",
        "model (PHREEQC)": _fmt_meta(scenario.get("condition_code")),
    })
    return pd.DataFrame(rows)


def _accept_condition_mappings(run_name: str, rows: list[dict], *,
                               override: bool = False) -> int:
    """Save the chosen suggestion rows as condition mappings, then apply to per-sample.

    Each row needs a ``condition_key`` + ``phreeqc_record_key``. ``override=True`` tags
    the saved mapping (used for the confirmed unsafe manual-override path). Replicates
    inherit the link via :func:`run_manager.apply_condition_mapping`.
    """
    accepted = 0
    for row in rows:
        ck = str(row.get("condition_key", "")).strip()
        key = str(row.get("phreeqc_record_key", "")).strip()
        if not ck or not key:
            continue
        status = row.get("mapping_status", "")
        note = (f"override of {status} mapping" if override
                else f"accepted from suggestion table ({status})")
        try:
            run_manager.add_condition_mapping(run_name, ck, key, notes=note,
                                              override=override, mapping_status=status)
            accepted += 1
        except run_manager.RunManagerError as exc:
            st.error(f"{ck}: {exc}")
    if accepted:
        try:
            run_manager.apply_condition_mapping(run_name)
        except run_manager.RunManagerError as exc:
            st.error(str(exc))
    return accepted


# Columns shown in the editable suggestion table (status is badged for display).
_SUGGESTION_DISPLAY_COLUMNS = [
    "accept", "condition_key", "n_replicates", "scenario_label",
    "phreeqc_record_key", "status", "score", "confidence", "already_mapped", "reason",
]


def _runner_up_delta(best_score: int, candidate: dict) -> str:
    """One-line 'lost N points: …' for a runner-up, generated from its trace."""
    lost = int(best_score) - int(candidate.get("score") or 0)
    # Prefer the explicit conflicts; fall back to the model-missing metadata.
    why = list(candidate.get("mismatched_fields") or [])
    if not why:
        why = [f"model lacks {m}" for m in candidate.get("phreeqc_missing", [])]
    detail = "; ".join(why) if why else "lower-scoring metadata match"
    if lost <= 0:
        return f"tied with best · {detail}"
    return f"lost {lost} point(s) vs best: {detail}"


def _render_condition_detail(run_name: str, data: pd.DataFrame, manifest: pd.DataFrame,
                             condition_key: str) -> None:
    """Row-level detail: structured alignment + runner-up candidates + score breakdown."""
    sample, candidates = mapping_table.condition_candidates(data, condition_key, manifest, top_n=3)
    # Cup-cover condition description (with the not-confirmed-sealed caution), from
    # config — shown only when this condition actually carries an OA/PF/GS code.
    _det_code = scenarios.sample_condition_code(sample)
    if _det_code and str(_det_code).upper() in config.CONDITION_CODE_DESCRIPTIONS:
        st.caption(_condition_code_descriptions_md({_det_code}))
    if not candidates:
        st.info("No model/simulation candidate exists for this condition — it needs a new simulation.")
        return
    best_score = candidates[0].get("score") or 0
    for i, c in enumerate(candidates):
        scenario = mapping_table._manifest_row(manifest, c["suggested_phreeqc_record_key"])
        status = replicates.mapping_status(sample, scenario)
        header = "Best candidate" if i == 0 else f"Runner-up {i}"
        with st.container(border=True):
            st.markdown(
                f"**{header}: {c.get('scenario_label', '') or '—'}**  ·  score "
                f"{c['score']}  ·  {c['confidence']} confidence  ·  {_status_badge(status)}"
            )
            st.caption(f"`{c['suggested_phreeqc_record_key']}`")
            # Runner-up one-line delta vs the best candidate (generated from the trace).
            if i > 0:
                st.caption("↳ " + _runner_up_delta(best_score, c))
            if c.get("confidence_explanation"):
                st.caption(f"Confidence: {c['confidence_explanation']}")

            st.caption("Field alignment (measured vs model, with values):")
            st.dataframe(_alignment_value_table(sample, scenario or {}),
                         use_container_width=True, hide_index=True, height=250)
            st.markdown(
                f"- **Matched:** {', '.join(c['matched_fields']) or '—'}\n"
                f"- **Missing model fields:** {', '.join(c.get('phreeqc_missing', [])) or '—'}\n"
                f"- **Conflicting (both values):** {', '.join(c['mismatched_fields']) or '—'}"
            )
            breakdown = c.get("score_breakdown", [])
            if breakdown:
                st.caption("Score breakdown (which rule added/subtracted points):")
                st.dataframe(pd.DataFrame(breakdown), use_container_width=True,
                             hide_index=True, height=min(40 + 35 * len(breakdown), 220))
            for note in c.get("metadata_notes", []):
                st.caption("ℹ️ " + note)


def _render_suggestion_table(run_name: str, data: pd.DataFrame, manifest: pd.DataFrame,
                             table: pd.DataFrame) -> None:
    """Automatic-first suggestion table (no button) + review detail + accept actions."""
    _render_map_flash(run_name)
    st.markdown("#### Suggested mappings")
    st.caption(
        "One row per measured condition, generated automatically with transparent "
        "rule-based scoring (no ML). Replicates of a condition are mapped together. "
        "Tick **accept** then **Accept selected**; **Accept all exact** bulk-accepts only "
        "exact matches. Unsafe rows cannot be accepted here — use Manual override below."
    )
    if table.empty:
        st.info("No measured data groups to map yet.")
        return

    EXACT = replicates.MAPPING_STATUS_EXACT
    UNSAFE = replicates.MAPPING_STATUS_UNSAFE
    NEEDS_NEW = replicates.MAPPING_STATUS_NEEDS_NEW

    disp = table.copy()
    # Pre-tick exact, not-yet-mapped rows; never pre-tick unsafe/needs-new.
    disp.insert(0, "accept", [
        (r["mapping_status"] == EXACT and not bool(r["already_mapped"]))
        for _, r in disp.iterrows()
    ])
    disp["status"] = disp["mapping_status"].map(_status_badge)
    edited = st.data_editor(
        disp[_SUGGESTION_DISPLAY_COLUMNS],
        column_config={
            "accept": st.column_config.CheckboxColumn("accept", help="Tick to accept this mapping"),
            "reason": st.column_config.TextColumn("reason", width="large"),
        },
        disabled=[c for c in _SUGGESTION_DISPLAY_COLUMNS if c != "accept"],
        hide_index=True, use_container_width=True, height=320,
        key=f"sug_editor_{run_name}",
    )

    # Re-attach the raw status to each row (the editor only edits `accept`).
    records = table.to_dict("records")
    for rec, acc in zip(records, edited["accept"].tolist()):
        rec["accept"] = bool(acc)

    a1, a2, a3 = st.columns(3)
    if a1.button("✅ Accept all exact suggestions", key=f"acc_exact_{run_name}"):
        rows = mapping_table.exact_suggestions(table).to_dict("records")
        n = _accept_condition_mappings(run_name, rows)
        _map_flash(run_name, "success", f"Accepted {n} exact mapping(s).")
        _read_csv.clear()
        st.rerun()
    if a2.button("✅ Accept selected", key=f"acc_sel_{run_name}"):
        selected = [r for r in records if r["accept"]]
        acceptable = [r for r in selected
                      if r["mapping_status"] in mapping_table.SELECTABLE_STATUSES
                      and str(r["phreeqc_record_key"]).strip()]
        unsafe_sel = [r for r in selected if r["mapping_status"] == UNSAFE]
        new_sel = [r for r in selected if r["mapping_status"] == NEEDS_NEW]
        n = _accept_condition_mappings(run_name, acceptable)
        _map_flash(run_name, "success", f"Accepted {n} selected mapping(s).")
        if any(r["mapping_status"] == replicates.MAPPING_STATUS_SCENARIO for r in acceptable):
            _map_flash(run_name, "info",
                       "Some accepted mappings are **scenario-level only** — comparison graphs "
                       "for them are a preliminary / workflow check, not final validation.")
        if unsafe_sel:
            _map_flash(run_name, "error",
                       f"{len(unsafe_sel)} **unsafe** row(s) were NOT accepted. Unsafe mappings "
                       "(e.g. acid leachant on a NaOH/CO₂ scenario) can only be saved via "
                       "**Manual override / advanced mapping**, where a confirmation is required "
                       "and the mapping is tagged `override=true`.")
        if new_sel:
            _map_flash(run_name, "warning",
                       f"{len(new_sel)} row(s) are **needs new simulation** — no candidate to accept.")
        _read_csv.clear()
        st.rerun()
    if a3.button("➡️ Export mapping to pipeline", key=f"exp_sug_{run_name}"):
        try:
            dest = run_manager.export_mapping_to_pipeline(run_name)
            _map_flash(run_name, "success",
                       f"Copied mapping to {_rel(dest)} — step 05 reads it.")
            _read_csv.clear()
        except run_manager.RunManagerError as exc:
            _map_flash(run_name, "error", str(exc))
        st.rerun()

    st.markdown("**Inspect a condition** — alignment, runner-up candidates & score breakdown")
    sel = st.selectbox("Condition to inspect", table["condition_key"].astype(str).tolist(),
                       key=f"detail_sel_{run_name}")
    _render_condition_detail(run_name, data, manifest, sel)


def _render_saved_condition_mappings(run_name: str) -> None:
    """Review of accepted/saved condition mappings (+ advanced apply/delete)."""
    cond_map = run_manager.read_condition_mapping(run_name)
    st.markdown(f"**Accepted / saved mappings** ({len(cond_map)})")
    if cond_map.empty:
        st.caption("No mappings accepted yet — use auto-suggest above, or manual override below.")
        return
    st.dataframe(cond_map, use_container_width=True, height=150)
    _render_condition_mapping_advanced(run_name, cond_map)


def _render_condition_mapping(run_name: str, data: pd.DataFrame, manifest: pd.DataFrame) -> None:
    """Manual override view: condition + scenario + status + notes + save/apply."""
    summary = replicates.replicate_summary(data)
    if summary.empty:
        st.info("No conditions to map yet — enter data first.")
        return

    condition_keys = summary["condition_key"].tolist()
    c1, c2 = st.columns([2, 3])
    with c1:
        sel_condition = st.selectbox("Measured data group", condition_keys,
                                     key=f"cond_sel_{run_name}",
                                     help="A group of measured records sharing one experimental "
                                          "condition (its replicates are mapped together).")
    with c2:
        chosen_key = _scenario_record_key_picker(run_name, manifest, key=f"cond_scn_{run_name}")

    sample = _condition_representative_sample(data, sel_condition)
    scenario = _manifest_row(manifest, chosen_key)
    status = _render_mapping_status_line(sample, scenario)

    notes = st.text_input(
        "Notes (optional)", key=f"cond_notes_{run_name}",
        help="Free-text validation context. Stored with the mapping; never sent to "
             "the comparison step.",
    )

    # Unsafe mappings can ONLY be saved here, and only with explicit confirmation;
    # the saved mapping is tagged override=true in the condition-mapping CSV.
    is_unsafe = (status == replicates.MAPPING_STATUS_UNSAFE)
    override_confirm = False
    if is_unsafe:
        st.error(
            "This mapping is **unsafe** — a known metadata conflict (e.g. an acid leachant "
            "mapped to a NaOH/CO₂ scenario, or opposite CO₂ families). It cannot be accepted "
            "from the suggestion table; saving it here records `override=true`."
        )
        override_confirm = st.checkbox(
            "I understand this mapping is unsafe and want to override and save it anyway.",
            key=f"cond_override_{run_name}",
        )

    if st.button("Save mapping", key=f"cond_map_{run_name}", type="primary"):
        if not chosen_key:
            st.warning("Pick a model / simulation result first.")
        elif is_unsafe and not override_confirm:
            st.warning("Tick the override confirmation to save an unsafe mapping.")
        else:
            try:
                run_manager.add_condition_mapping(
                    run_name, sel_condition, chosen_key, notes=notes, override=is_unsafe)
                run_manager.apply_condition_mapping(run_name)
                n = len(run_manager.read_mapping(run_name))
                st.success(
                    f"Saved & applied — condition `{sel_condition}` → `{chosen_key}` "
                    f"({n} sample row(s) mapped)."
                    + (" Tagged `override=true`." if is_unsafe else "")
                )
                if status != replicates.MAPPING_STATUS_EXACT:
                    st.info(
                        "This mapping is not *exact*, so any comparison graph is a "
                        "**preliminary / workflow check only**."
                    )
                _read_csv.clear()
                st.rerun()
            except run_manager.RunManagerError as exc:
                st.error(str(exc))

    with st.expander("Advanced validation metadata"):
        _render_advanced_validation_metadata(sample, scenario)


def _render_condition_mapping_advanced(run_name: str, cond_map: pd.DataFrame) -> None:
    """Advanced apply-options (replicate→solution) + delete, kept out of the main view."""
    with st.expander("Advanced apply options & replicate→solution mapping"):
        st.warning(
            "⚠️ Only use replicate→solution mapping if sol1/sol2/sol3 represent **replicate "
            "batches**, not time points or unrelated solutions."
        )
        st.caption("Map each replicate id to a PHREEQC solution number (e.g. R1→1, R2→2), "
                   "then re-apply to point each replicate at its own solution.")
        rc1, rc2, rc3 = st.columns(3)
        rep_in = rc1.text_input("replicate_id (e.g. R1)", key=f"repsol_rid_{run_name}")
        sol_in = rc2.text_input("solution_number (e.g. 1)", key=f"repsol_sol_{run_name}")
        if rc3.button("Add R→sol", key=f"repsol_add_{run_name}"):
            try:
                run_manager.add_replicate_solution(run_name, rep_in, sol_in)
                _read_csv.clear()
                st.rerun()
            except run_manager.RunManagerError as exc:
                st.error(str(exc))
        rs_map = run_manager.read_replicate_solution_map(run_name)
        if not rs_map.empty:
            st.dataframe(rs_map, use_container_width=True, height=120)
        use_rep_sol = st.checkbox(
            "Re-apply using replicate→solution mapping (instead of one scenario for all replicates)",
            key=f"repsol_use_{run_name}",
        )
        if st.button("Re-apply condition mapping → per-sample map", key=f"cond_apply_{run_name}"):
            try:
                path = run_manager.apply_condition_mapping(run_name, use_replicate_solution=use_rep_sol)
                n = len(run_manager.read_mapping(run_name))
                st.success(f"Applied — {n} sample row(s) written to `{_rel(path)}`.")
                _read_csv.clear()
                st.rerun()
            except run_manager.RunManagerError as exc:
                st.error(str(exc))

    with st.expander("Delete condition mappings"):
        to_del = st.multiselect(
            "Rows to delete", options=list(range(len(cond_map))),
            format_func=lambda i: f"{cond_map.iloc[i]['condition_key']} → "
                                  f"{cond_map.iloc[i]['phreeqc_record_key']}",
            key=f"cond_del_{run_name}",
        )
        if st.button("Delete selected condition mappings", key=f"cond_delbtn_{run_name}") and to_del:
            run_manager.delete_condition_mapping_rows(run_name, to_del)
            _read_csv.clear()
            st.rerun()


def _render_replicate_collision_warnings(data: pd.DataFrame, mapping: pd.DataFrame,
                                         manifest: pd.DataFrame) -> None:
    """Feature 7 — replicate-aware mapping safety warnings."""
    warns = replicates.collision_report(data, mapping, manifest)
    if not warns:
        st.success("No replicate-aware mapping problems. (Replicates of one condition sharing a "
                   "PHREEQC scenario is expected, not a collision.)")
        return
    # Keep the (often repeated) detail tidy for presentation.
    st.warning(f"⚠️ {len(warns)} replicate-aware mapping warning(s) — expand for detail.")
    with st.expander("Mapping warning detail"):
        seen: set[str] = set()
        for w in warns:
            if w["message"] in seen:
                continue
            seen.add(w["message"])
            st.markdown(f"- {w['message']}")


def _render_mapping_section(run_name: str) -> None:
    """Measured-data → model-prediction mapping UI for a lab-like run.

    Automatic-first: a single suggestion table is generated as soon as run data +
    model results exist (no button). Workflow order: **suggestion table → accept →
    existing mappings → conditions needing simulation → advanced tools**. Manual
    dropdown mapping, the scenario explorer and the per-sample assistant are kept
    under expanders. Saves to the run's own ``data/sample_phreeqc_map.csv``.
    """
    st.markdown("---")
    st.subheader("Match measured data to model predictions")
    st.caption(
        "Automatic-first: a **suggestion table** is built as soon as data + model results "
        "exist — review, accept, then graph. Transparent rule-based scoring (no ML); "
        "manual mapping, scenario explorer and per-sample assistant are under advanced."
    )

    data = run_manager.read_data_file(run_name)

    sample_ids: list[str] = []
    if "sample_id" in data.columns:
        for s in data["sample_id"].astype(str).map(str.strip).tolist():
            if s and s.lower() != "nan":
                sample_ids.append(s)
    sample_ids = list(dict.fromkeys(sample_ids))  # unique, order-preserving

    # Item 1 — no measured data: a clear message, and no stale suggestions/state.
    if not sample_ids:
        for k in (f"map_flash_{run_name}", f"sug_editor_{run_name}", f"detail_sel_{run_name}"):
            st.session_state.pop(k, None)
        st.info(
            "No measured data found for this run. Upload or import a dataset in the "
            "**Data** tab first."
        )
        return

    graph_note = (
        "Graphs only require measured values, model-predicted values, and a saved "
        "mapping between them. Extra metadata is retained for scientific validation "
        "and interpretation."
    )
    if _dataset_condition_codes(data):
        graph_note += (
            "\n\nFor this dataset, OA/PF/GS and CO₂ exposure are treated as "
            "validation metadata."
        )
    st.info(graph_note)

    results_path = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    if not results_path.exists():
        st.info(
            "`data/processed/phreeqc_results.csv` not found — run Phase 1 first "
            "(the Compare Results tab) to generate model (PHREEQC) results."
        )
        return
    phreeqc = _read_csv(str(results_path), results_path.stat().st_mtime)
    if "record_key" not in phreeqc.columns:
        st.warning("`phreeqc_results.csv` has no `record_key` column — cannot map.")
        return

    manifest = _scenario_manifest(str(results_path), results_path.stat().st_mtime)
    mapping = run_manager.read_mapping(run_name)
    cond_map = run_manager.read_condition_mapping(run_name)

    # Build the suggestion table ONCE; the table view and the "needs new simulation"
    # section are both driven from it, so their counts always agree.
    table = mapping_table.build_suggestion_table(data, manifest, cond_map)
    if not table.empty:
        _sc = table["mapping_status"].value_counts().to_dict()
        _audit_once(
            run_name, "suggestion:" + ":".join(f"{k}={v}" for k, v in sorted(_sc.items())),
            lambda: audit.log_suggestion_table(
                run_name, status_counts=_sc, n_conditions=int(len(table))))

    # Compact mapping summary — accepted + the status distribution across the
    # detected measured conditions (same source as the suggestion table below).
    if not table.empty:
        vc = table["mapping_status"].value_counts().to_dict()
        _ms = replicates
        app_ui.render_metric_cards([
            {"label": "Accepted mappings", "value": len(cond_map),
             "status": "good" if len(cond_map) else "neutral"},
            {"label": "Exact", "value": vc.get(_ms.MAPPING_STATUS_EXACT, 0),
             "status": "exact" if vc.get(_ms.MAPPING_STATUS_EXACT, 0) else "neutral"},
            {"label": "Scenario-level", "value": vc.get(_ms.MAPPING_STATUS_SCENARIO, 0),
             "status": "scenario-level" if vc.get(_ms.MAPPING_STATUS_SCENARIO, 0) else "neutral"},
            {"label": "Unsafe", "value": vc.get(_ms.MAPPING_STATUS_UNSAFE, 0),
             "status": "unsafe" if vc.get(_ms.MAPPING_STATUS_UNSAFE, 0) else "neutral"},
            {"label": "Needs new sim", "value": vc.get(_ms.MAPPING_STATUS_NEEDS_NEW, 0),
             "status": "needs new simulation" if vc.get(_ms.MAPPING_STATUS_NEEDS_NEW, 0) else "neutral"},
        ])

    # ---- 1) Suggestion table (auto-generated) + accept actions ----
    _render_suggestion_table(run_name, data, manifest, table)

    # ---- 2) Existing/accepted mappings + overall status ----
    st.markdown("---")
    _render_saved_condition_mappings(run_name)
    overall = replicates.overall_mapping_status(data, mapping, manifest)
    if not mapping.empty:
        line = " · ".join(f"{k}: {v}" for k, v in overall.get("counts", {}).items())
        if overall.get("all_exact"):
            st.success(f"Overall mapping status: **{overall.get('overall')}** — {line}")
        else:
            st.warning(
                f"Overall mapping status: **{overall.get('overall')}** — {line}. "
                "Comparison graphs are a **preliminary / workflow check only** until "
                "mappings are exact."
            )

    # ---- 3) Conditions needing new simulations (same table source) ----
    st.markdown("---")
    _render_sim_needed(run_name, data, mapping, manifest, table)

    _render_condition_code_help(data)

    # ---- 4) Advanced tools (all functionality kept, just demoted) ----
    with st.expander("Manual override / advanced mapping"):
        st.caption(
            "Pick a measured data group and model result by hand if a suggestion is wrong. "
            "**Unsafe** mappings can only be saved here, with a confirmation that records "
            "`override=true`."
        )
        _render_condition_mapping(run_name, data, manifest)
        st.markdown("---")
        st.markdown("**Per-sample manual mapping**")
        _render_manual_mapping(run_name, phreeqc, sample_ids)

    with st.expander("Validation context: replicate summary & mapping warnings"):
        _render_replicate_summary(data)
        st.markdown("---")
        _render_mapping_quality(mapping)
        _render_replicate_collision_warnings(data, mapping, manifest)

    with st.expander("Explore PHREEQC scenarios"):
        _render_scenario_explorer(run_name, manifest)

    with st.expander("Per-sample assistant (advanced)"):
        _render_mapping_assistant(run_name, data, sample_ids, manifest)

    with st.expander("Per-sample mappings: view, delete & export to pipeline"):
        st.markdown(f"**Existing per-sample mappings** ({len(mapping)}):")
        st.dataframe(mapping, use_container_width=True, height=170)
        if not mapping.empty:
            def _map_label(i: int) -> str:
                r = mapping.iloc[i]
                return f"Row {i} — {r.get('sample_id', '')} → {r.get('phreeqc_record_key', '')}"
            to_del = st.multiselect(
                "Select mapping rows to delete", options=list(range(len(mapping))),
                format_func=_map_label, key=f"map_del_{run_name}",
            )
            confirm = st.checkbox(
                "I understand this will delete the selected mapping rows.",
                key=f"map_delc_{run_name}",
            )
            if st.button("Delete selected mappings", key=f"map_delbtn_{run_name}"):
                if not to_del:
                    st.warning("No mapping rows selected — nothing was deleted.")
                elif not confirm:
                    st.warning("Tick the confirmation checkbox before deleting.")
                else:
                    n = run_manager.delete_mapping_rows(run_name, to_del)
                    st.success(f"Deleted {n} mapping row(s).")
                    st.rerun()

            if st.button("➡️ Export mapping to pipeline", key=f"map_export_{run_name}"):
                try:
                    dest = run_manager.export_mapping_to_pipeline(run_name)
                    st.success(
                        f"Copied mapping to {_rel(dest)} — step 05 "
                        "will use it to compute residuals."
                    )
                    _read_csv.clear()
                except run_manager.RunManagerError as exc:
                    st.error(str(exc))


# --------------------------------------------------------------------------- #
# Results summary + comparison preview (presentation-friendly, honest)
# --------------------------------------------------------------------------- #
# (measured_col, display_name) — measured columns are unprefixed in the
# comparison CSV; renamed here so the preview reads "measured_ vs phreeqc_".
_COMPARISON_PREVIEW_SPEC = [
    ("sample_id", "sample_id"),
    ("phreeqc_record_key", "phreeqc_record_key"),
    ("final_pH", "measured_final_pH"),
    ("phreeqc_pH", "phreeqc_pH"),
    ("residual_pH", "residual_pH"),
    ("Ca_mM", "measured_Ca_mM"), ("phreeqc_Ca_mM", "phreeqc_Ca_mM"), ("residual_Ca", "residual_Ca"),
    ("Si_mM", "measured_Si_mM"), ("phreeqc_Si_mM", "phreeqc_Si_mM"), ("residual_Si", "residual_Si"),
    ("Al_mM", "measured_Al_mM"), ("phreeqc_Al_mM", "phreeqc_Al_mM"), ("residual_Al", "residual_Al"),
    ("Fe_mM", "measured_Fe_mM"), ("phreeqc_Fe_mM", "phreeqc_Fe_mM"), ("residual_Fe", "residual_Fe"),
]

_ICP_MEASURED_COLS = ["Ca_mM", "Si_mM", "Al_mM", "Fe_mM", "Na_mM", "K_mM", "Sc_ppb", "total_REE_ppb"]


def _looks_like_test(comp: pd.DataFrame) -> bool:
    if "sample_id" not in comp.columns:
        return False
    sids = comp["sample_id"].astype(str)
    return bool(sids.str.upper().str.contains("TEST").any())


def _looks_like_run_test(data: pd.DataFrame) -> bool:
    """True if any sample_id in a run's data frame looks like mock/test data."""
    if data.empty or "sample_id" not in data.columns:
        return False
    sids = data["sample_id"].astype(str).str.upper()
    return bool(sids.str.contains("TEST|SYNTH|DEMO|MOCK", na=False, regex=True).any())


def _run_comparison_path(run_name: str | None) -> Path | None:
    """The selected run's per-run comparison CSV path, or None.

    Returns None when no run is selected or the run is not lab-like (so a
    literature/synthetic run can never surface another run's comparison).
    """
    if not run_name:
        return None
    try:
        return run_manager.comparison_path(run_name)
    except run_manager.RunManagerError:  # non-lab run_type, or no config
        return None


def _run_comparison_figures_dir(run_name: str | None) -> Path | None:
    """The selected run's per-run comparison figures directory, or None."""
    if not run_name:
        return None
    try:
        return run_manager.comparison_figures_dir(run_name)
    except run_manager.RunManagerError:
        return None


def _render_stale_results_warning(run_name: str) -> None:
    """Prominent banner when a run's stored comparison no longer matches its inputs."""
    if not run_manager.has_comparison(run_name):
        return
    current, reasons = run_manager.comparison_is_current(run_name)
    if current:
        return
    st.warning(
        "⚠️ **These results were generated from older data/mappings for this run — "
        "re-run the workflow.** What changed:\n\n"
        + "\n".join(f"- {r}" for r in reasons)
    )


def _comparison_has_residuals(run_name: str | None) -> bool:
    """True if the run's comparison CSV exists and has at least one numeric residual."""
    comp_path = _run_comparison_path(run_name)
    if comp_path is None or not comp_path.exists():
        return False
    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    return any(_has_numeric(comp, f"residual_{el}")
               for el in ["pH", "Ca", "Si", "Al", "Fe"])


def _render_results_summary(run_name: str | None) -> None:
    """Honest, presentation-friendly summary of this run's comparison."""
    if not run_name:
        st.info("Select a lab run in the **Experiment runs** sidebar (left) to see its results.")
        return
    comp_path = _run_comparison_path(run_name)
    if comp_path is None:
        st.info(
            "The selected run is not a lab run, so it has no measured-vs-model comparison."
        )
        return
    if not comp_path.exists():
        st.info(
            "No comparison results yet for this run. Run the workflow (above) for a lab run "
            "that has a measured-data → model mapping to generate this run's comparison."
        )
        return

    _render_stale_results_warning(run_name)

    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    n_rows = len(comp)
    if "phreeqc_record_key" in comp.columns:
        mapped = int(comp["phreeqc_record_key"].apply(
            lambda v: not (pd.isna(v) or str(v).strip() == "")).sum())
    else:
        mapped = 0
    ph_ok = _has_numeric(comp, "residual_pH")
    icp_resid_ok = any(_has_numeric(comp, f"residual_{el}") for el in ["Ca", "Si", "Al", "Fe"])
    icp_missing = not any(_has_numeric(comp, c) for c in _ICP_MEASURED_COLS)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Experimental rows", n_rows)
    m2.metric("Mapped samples", mapped)
    m3.metric("pH residuals", "yes" if ph_ok else "no")
    m4.metric("Ca/Si/Al/Fe residuals", "yes" if icp_resid_ok else "no")

    st.markdown(
        f"- **ICP chemistry:** {'missing (pH-only)' if icp_missing else 'present'}\n"
        f"- **Comparison CSV:** `{_rel(comp_path)}`"
    )

    # Test/demo guard.
    if _looks_like_test(comp):
        st.error(
            "This appears to be a test/demo row (sample_id contains \"TEST\"). "
            "Do not interpret it as scientific evidence."
        )

    # pH-only mode.
    if ph_ok and not icp_resid_ok:
        st.warning(
            "Only pH residuals are available because ICP chemistry values are blank. "
            "Ca/Si/Al/Fe/REE validation requires ICP-OES/ICP-MS data."
        )

    # Single-sample honesty + pH residual cards.
    if mapped == 1 and ph_ok:
        st.warning(
            "This is a single-sample comparison, not a trend. It only checks one "
            "mapped condition."
        )
        row = comp[pd.to_numeric(comp["residual_pH"], errors="coerce").notna()].iloc[0]
        meas = pd.to_numeric(pd.Series([row.get("final_pH")]), errors="coerce").iloc[0]
        pred = pd.to_numeric(pd.Series([row.get("phreeqc_pH")]), errors="coerce").iloc[0]
        resid = pd.to_numeric(pd.Series([row.get("residual_pH")]), errors="coerce").iloc[0]
        p1, p2, p3 = st.columns(3)
        p1.metric("Measured pH", f"{meas:.2f}" if pd.notna(meas) else "—")
        p2.metric("Model pH", f"{pred:.2f}" if pd.notna(pred) else "—")
        p3.metric("Residual pH (measured − model)", f"{resid:+.2f}" if pd.notna(resid) else "—")

    # Comparison table preview — only columns that exist.
    present = [(src, disp) for src, disp in _COMPARISON_PREVIEW_SPEC if src in comp.columns]
    if present:
        preview = comp[[src for src, _ in present]].rename(columns=dict(present))
        st.markdown("**Comparison table** (measured vs model, existing columns only):")
        st.dataframe(preview, use_container_width=True, height=200)


# Literature-summary columns (only those present are shown). Both reported_final_pH
# (the literature schema name) and final_pH are listed so whichever exists is used.
_LIT_SUMMARY_COLS = [
    "source_id", "paper_title", "year", "fly_ash_class",
    "reported_final_pH", "final_pH",
    "reported_Ca_mM", "reported_Al_mM", "reported_Fe_mM",
    "comparability_to_our_experiment",
]


def _render_literature_summary(run_name: str) -> None:
    """Literature-benchmark read-out — never the lab measured-vs-PHREEQC residual."""
    st.info(
        "This is a literature benchmark run. Literature data are stored separately "
        "and are not treated as measured lab data."
    )
    lit = run_manager.read_data_file(run_name)
    st.metric("Literature rows", len(lit))

    present = [c for c in _LIT_SUMMARY_COLS if c in lit.columns]
    if lit.empty:
        st.info("No literature rows entered yet for this run.")
    elif present:
        st.markdown("**Literature benchmark summary** (existing columns only):")
        st.dataframe(lit[present], use_container_width=True, height=200)
    # No lab-comparison read-out here: comparisons are per-run (stored under the lab
    # run's own outputs/), so a literature run never displays another run's results.


# Comparison figures get specific captions; everything else is a PHREEQC-only plot.
_FIGURE_CAPTIONS = {
    "measured_vs_phreeqc.png": (
        "This plot compares measured values against PHREEQC predictions. Points on "
        "the dashed 1:1 line would indicate perfect agreement. Points far from the "
        "line indicate model/experiment mismatch or incorrect mapping. Proximity to "
        "the 1:1 line indicates agreement only if the mapping is scientifically valid."
    ),
    "residuals_by_sample.png": (
        "This plot shows measured − PHREEQC. Positive values mean the measured value "
        "is higher than the PHREEQC prediction. Near-zero residuals indicate agreement "
        "only if the mapping is scientifically valid."
    ),
}
_COMPARISON_FIGURES = set(_FIGURE_CAPTIONS)


def _single_sample_comparison(run_name: str | None) -> bool:
    """True if the run's comparison has exactly one mapped sample (not a trend)."""
    comp_path = _run_comparison_path(run_name)
    if comp_path is None or not comp_path.exists():
        return False
    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    if "phreeqc_record_key" not in comp.columns:
        return False
    mapped = comp["phreeqc_record_key"].apply(
        lambda v: not (pd.isna(v) or str(v).strip() == "")).sum()
    return int(mapped) == 1


# Provenance labels for result graphs (UI labels only — no science change). They let a user
# see a figure's source + age and make explicit that the Simulate tab is plan-only and does
# NOT regenerate any result graph.
_LIVE_COMPARE_NOTE = ("Live measured-vs-model figure — drawn fresh from this run's data + "
                      "comparison each render; **not affected by the Simulate tab** (plan-only).")
_LIVE_MEASURED_NOTE = ("Live measured-data-only figure — drawn fresh from this run's measured "
                       "data each render; **not affected by the Simulate tab** (plan-only).")


def _png_provenance_caption(path: Path, kind: str) -> str:
    """Provenance line for a static PNG result figure: source path + generated time + type +
    the fact that the Simulate tab does not regenerate it. (UI label only.)"""
    try:
        ts = pd.Timestamp.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except Exception:
        ts = "unknown"
    return (f"Source figure: `{_rel(path)}` · generated {ts} · {kind} · static image — "
            "regenerated only by re-running the workflow, **not** by the Simulate tab.")


def _render_comparison_figures(run_name: str | None) -> None:
    """Measured-vs-PHREEQC + residual plots for the selected run (per-run figures)."""
    fig_dir = _run_comparison_figures_dir(run_name)
    if fig_dir is None or not fig_dir.exists():
        return
    comparison = [p for p in sorted(fig_dir.glob("*.png")) if p.name in _COMPARISON_FIGURES]
    if not comparison:
        return
    app_ui.section_header("Residual analysis",
                          "residual = measured − model predicted")
    # The single-sample caveat is folded into the validity line of the inclusion
    # section above, so it is not repeated here.
    for png in comparison:
        kind = ("measured-vs-model comparison (Ca/Si/Al/Fe/**pH**)"
                if png.name == "measured_vs_phreeqc.png"
                else "residual plot, measured − model (Ca/Si/Al/Fe/**pH**)")
        st.image(str(png), use_container_width=True)
        st.caption(_FIGURE_CAPTIONS.get(png.name, png.name))
        st.caption(_png_provenance_caption(png, kind))


def _render_phreeqc_only_figures() -> None:
    """PHREEQC model-output plots only (pH, element molality, saturation indices,
    …). Excludes the measured-vs-PHREEQC comparison/residual figures."""
    pngs = [p for d in _figure_dirs() if d.exists() for p in sorted(d.glob("*.png"))]
    phreeqc_only = [p for p in pngs if p.name not in _COMPARISON_FIGURES]
    if not phreeqc_only:
        st.warning("No PHREEQC figures yet — run Phase 1 to generate them.")
        return
    st.info(
        "These are **PHREEQC model outputs, not measured experimental data.** "
        "Crowded axis labels come from the many PHREEQC solution states plotted "
        "together — use the selector to view one figure at a time."
    )
    names = [p.name for p in phreeqc_only]
    choice = st.selectbox("Choose a PHREEQC figure", names, key="phreeqc_fig_choice")
    chosen = next(p for p in phreeqc_only if p.name == choice)
    st.image(str(chosen), use_container_width=True)
    st.caption(f"{choice} — PHREEQC model output, not a measurement.")
    st.caption(_png_provenance_caption(
        chosen, "existing PHREEQC-only model output (e.g. `pH.png` = pH by solution state)"))


# --------------------------------------------------------------------------- #
# Shared script-runner button (so Run Workflow + Tools reuse one code path;
# distinct key prefixes keep Streamlit widget identities unique)
# --------------------------------------------------------------------------- #
def _script_button(label: str, script: str, result_label: str, key: str,
                   refresh_csv: bool = False) -> None:
    if st.button(label, use_container_width=True, key=key):
        with st.spinner(f"Running {result_label}…"):
            proc = _run_script(script)
        _show_process_result(result_label, proc)
        if refresh_csv:
            _read_csv.clear()


# --------------------------------------------------------------------------- #
# Presentation-status wording (validation workflow, not an overclaimed model)
# --------------------------------------------------------------------------- #
_PRELIMINARY_CAVEAT = (
    "Current measured-vs-model comparison should be treated as preliminary / workflow "
    "check only unless mappings are exact."
)
_VALID_NOW = [
    "Real Excel workbook import",
    "pH extraction",
    "Ca/Si/Al extraction",
    "Data validation",
    "Formula audit",
    "PHREEQC parsing",
    "Preliminary workflow comparison",
]
_NOT_VALID_YET = [
    "Time-resolved PHREEQC validation",
    "HCl comparison (until HCl PHREEQC scenarios are generated)",
    "CO₂-resolved PHREEQC validation of OA vs PF/GS cover conditions",
    "ML training",
]
_OA_PF_GS_CAVEAT = (
    "OA, PF, and GS represent CO₂ exposure / cup-cover conditions: OA = open air, "
    "PF = plastic flap cover, GS = glass cover. OA is directly exposed to atmospheric CO₂; "
    "PF and GS are covered conditions that likely reduce CO₂ exchange. They are kept as "
    "distinct experimental conditions because cover material and CO₂ exposure can affect pH "
    "and carbonate formation. Do not treat PF or GS as fully sealed unless airtight sealing "
    "is confirmed."
)


def _manifest_if_available() -> pd.DataFrame:
    # A non-PHREEQC model's predictions (generic CSV) take precedence when present —
    # the manifest (and everything downstream) is model-agnostic.
    mp = config.PROCESSED_DIR / config.MODEL_PREDICTIONS_CSV
    if mp.exists():
        return _scenario_manifest(str(mp), mp.stat().st_mtime)
    rp = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    if rp.exists():
        return _scenario_manifest(str(rp), rp.stat().st_mtime)
    return pd.DataFrame(columns=scenarios.MANIFEST_COLUMNS)


def _render_mapping_status_definitions() -> None:
    """Feature 2 — the four mapping statuses and what they mean."""
    rows = [{"status": k, "meaning": v}
            for k, v in replicates.MAPPING_STATUS_DEFINITIONS.items()]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=180, hide_index=True)


def _render_valid_now_section() -> None:
    """Feature 5 — what is scientifically valid now vs not fully valid yet."""
    a, b = st.columns(2)
    with a:
        with st.container(border=True):
            app_ui.render_status_badge("Valid now", "valid")
            st.markdown("\n".join(f"- {x}" for x in _VALID_NOW))
    with b:
        with st.container(border=True):
            app_ui.render_status_badge("Not fully valid yet", "preliminary")
            st.markdown("\n".join(f"- {x}" for x in _NOT_VALID_YET))


def _rows_with_any_numeric(data: pd.DataFrame, cols: list[str]) -> int:
    present = [c for c in cols if c in data.columns]
    if not present or data.empty:
        return 0
    num = data[present].apply(pd.to_numeric, errors="coerce")
    return int(num.notna().any(axis=1).sum())


def _render_presentation_summary(selected_run: str | None) -> None:
    """Feature 1 — a single honest status panel near the top of the Start tab."""
    if not selected_run:
        st.info("Select or create a run in the **Experiment runs** sidebar (left) for a summary.")
        return
    cfg = run_manager.load_run_config(selected_run)
    rt = cfg.get("run_type")
    lab_like = rt in run_manager.LAB_LIKE_RUN_TYPES
    data = run_manager.read_data_file(selected_run)

    n_rows = len(data)
    rows_with_ph = _rows_with_any_numeric(data, ["final_pH"])
    rows_with_chem = _rows_with_any_numeric(data, ["Ca_mM", "Si_mM", "Al_mM"])

    issues = (validate_experimental_df(data, source=selected_run)
              if lab_like and not data.empty else [])
    n_err = sum(1 for i in issues if i.get("severity") == "error")
    n_warn = sum(1 for i in issues if i.get("severity") == "warning")

    mapping = run_manager.read_mapping(selected_run) if lab_like else pd.DataFrame()
    msum = run_manager.summarize_mapping(mapping)
    manifest = _manifest_if_available()
    status = (replicates.overall_mapping_status(data, mapping, manifest)
              if lab_like else {"overall": "n/a", "all_exact": False,
                                "counts": {}, "n_mapped": 0, "n_unmapped": 0})
    comp_exists = lab_like and run_manager.has_comparison(selected_run)

    _counts = status["counts"]
    n_exact = _counts.get(replicates.MAPPING_STATUS_EXACT, 0)
    n_scn = _counts.get(replicates.MAPPING_STATUS_SCENARIO, 0)
    n_unsafe = _counts.get(replicates.MAPPING_STATUS_UNSAFE, 0)
    n_needs = _counts.get(replicates.MAPPING_STATUS_NEEDS_NEW, 0)

    # The six headline cards (data + mapping coverage), status-coloured.
    app_ui.render_metric_cards([
        {"label": "Data loaded", "value": "Yes" if n_rows else "No",
         "caption": rt, "status": "good" if n_rows else "neutral"},
        {"label": "Measured rows", "value": n_rows, "status": "info" if n_rows else "neutral"},
        {"label": "Mapped samples", "value": msum["n_samples"] if lab_like else "n/a",
         "caption": f'{msum["n_unique_rows"]} unique model result(s)' if lab_like else "",
         "status": "good" if (lab_like and msum["n_samples"]) else "neutral"},
        {"label": "Exact mappings", "value": n_exact if lab_like else "n/a",
         "status": "exact" if (lab_like and n_exact) else "neutral"},
        {"label": "Unsafe mappings", "value": n_unsafe if lab_like else "n/a",
         "status": "unsafe" if (lab_like and n_unsafe) else "neutral"},
        {"label": "Needs new sim", "value": n_needs if lab_like else "n/a",
         "status": "needs new simulation" if (lab_like and n_needs) else "neutral"},
    ])
    # Secondary cards — measured coverage + validation.
    app_ui.render_metric_cards([
        {"label": "Rows with pH", "value": rows_with_ph,
         "status": "good" if rows_with_ph else "neutral"},
        {"label": "Rows with Ca/Si/Al", "value": rows_with_chem,
         "status": "good" if rows_with_chem else "neutral"},
        {"label": "Scenario-level", "value": n_scn if lab_like else "n/a",
         "status": "scenario-level" if (lab_like and n_scn) else "neutral"},
        {"label": "Validation errors", "value": n_err,
         "status": "error" if n_err else "good"},
        {"label": "Validation warnings", "value": n_warn,
         "status": "warning" if n_warn else "good"},
    ])

    overall = status["overall"]
    comp_label = "Available (preliminary)" if comp_exists else "Not run yet"
    comp_status = "preliminary" if comp_exists else "neutral"
    st.markdown(
        "**Overall mapping status:** "
        + app_ui.status_badge(overall, overall if lab_like else "neutral")
        + " &nbsp; **Comparison:** " + app_ui.status_badge(comp_label, comp_status),
        unsafe_allow_html=True,
    )

    # Recommended next *scientific* step.
    if not n_rows:
        nxt = ("Describe an experiment in the **Simulate** tab to plan a simulation, or "
               "import measured data in the **Import Data** tab to validate against predictions.")
    elif n_err:
        nxt = f"Fix {n_err} validation error(s) in the **Import Data** / **Validate** tabs before mapping."
    elif lab_like and msum["n_samples"] == 0:
        nxt = "Map conditions to model scenarios in the **Match** tab."
    elif status["counts"].get("unsafe", 0):
        nxt = ("Resolve unsafe mapping(s) (e.g. HCl mapped to a NaOH/CO2 scenario) — generate "
               "matching acid PHREEQC scenarios.")
    elif lab_like and not status["all_exact"]:
        nxt = ("Generate time- and condition-resolved PHREEQC scenarios so mappings can become "
               "exact; the current comparison is a workflow check, not final validation.")
    elif not comp_exists:
        nxt = "Run the workflow in the **Compare Results** tab to generate the comparison."
    else:
        nxt = "Review the preliminary comparison in the **Compare Results** tab."
    st.success(f"**Recommended next scientific step:** {nxt}")

    if not (lab_like and status["all_exact"]):
        st.warning("⚠️ " + _PRELIMINARY_CAVEAT)

    with st.expander("Mapping status definitions"):
        _render_mapping_status_definitions()
    with st.expander("What is valid now vs not fully valid yet"):
        _render_valid_now_section()


# --------------------------------------------------------------------------- #
# Tab renderers — each is a self-contained view; all reuse the helpers above.
# --------------------------------------------------------------------------- #
def _next_step_hint(selected_run: str | None) -> str:
    """One recommended next action for the selected run (the Start checklist logic).

    Shared by Start and surfaced as a "next step" hint at the top of every tab, so a
    user who didn't build the app always knows where to go next. References the new
    tab names (Import / Match / Compare / Export).
    """
    if not selected_run:
        return "Create or open a run in the **Experiment runs** sidebar (left)."
    try:
        cfg = run_manager.load_run_config(selected_run)
    except run_manager.RunManagerError:
        return "Create or open a run in the **Experiment runs** sidebar (left)."
    rt = cfg.get("run_type")
    data = run_manager.read_data_file(selected_run)
    lab_like = rt in run_manager.LAB_LIKE_RUN_TYPES
    has_map = run_manager.has_mapping(selected_run) if lab_like else False
    map_summary = (run_manager.summarize_mapping(run_manager.read_mapping(selected_run))
                   if lab_like else {"has_collisions": False})
    icp_present = lab_like and any(_has_numeric(data, c) for c in _ICP_MEASURED_COLS)
    is_mock = _looks_like_run_test(data)
    comp_exists = lab_like and run_manager.has_comparison(selected_run)

    if data.empty:
        return ("Describe an experiment in the **Simulate** tab, or import measured data "
                "in the **Import Data** tab to validate against predictions.")
    if is_mock:
        return "Mock/test data — for code checking only, not scientific conclusions."
    if rt == "literature_benchmark":
        return ("Review the literature table in the **Import Data** tab — literature data are "
                "kept separate from lab data and are not run through the pipeline.")
    if rt == "synthetic_demo":
        return "This is a synthetic/demo run — for testing only, not scientific output."
    if lab_like and not has_map:
        return "Map measured data to model results in the **Match** tab."
    if lab_like and map_summary["has_collisions"]:
        return ("Review mapping in the **Match** tab — several samples share one model "
                "result, so graphs may be misleading.")
    if not comp_exists:
        return "Run the workflow in the **Compare Results** tab to generate results."
    if lab_like and not icp_present:
        return "Only pH comparison is meaningful until ICP data are added."
    return "Read the comparison in the **Compare Results** tab, then build a report in **Export**."


def _render_next_step(selected_run: str | None) -> None:
    """Render the one-line "next step" hint at the top of a tab."""
    st.info(f"➡️ **Next step:** {_next_step_hint(selected_run)}")


def _render_modes_panel() -> None:
    """The three product modes — the platform's front-door explanation."""
    st.markdown("#### Three ways to use this platform")
    m1, m2, m3 = st.columns(3)
    m1.markdown(
        "**1 · Simulate**  \nDescribe an experiment and the variables you care about. AI "
        "extracts a structured scenario, flags missing info and assumptions, and builds a "
        "simulation plan/matrix.  \n_Planning layer — deterministic model execution is "
        "future work._")
    m2.markdown(
        "**2 · Validate**  \nCompare measured data against model predictions using "
        "transparent mapping status and residuals, with an honest validity status.  \n"
        f"_Current strongest workflow: Class C fly ash + {MODEL_NAME}._")
    m3.markdown(
        "**3 · Learn & Improve**  \nUse literature, measured data, residuals, and "
        "(experimental) surrogate/ML tools to improve predictions and recommend "
        "experiments.  \n_Experimental, display-only — never on the result path._")
    st.caption(
        "AI extracts and organizes scenario information; **deterministic code and "
        "simulation engines generate the scientific outputs**, and AI output always "
        "requires your review. A simulation plan is not a validated prediction — exact "
        "mapping and measured data are required for any validation claim.")
    st.divider()


def _render_overview(selected_run: str | None) -> None:
    """Three-mode product panel + run status + a recommended next step."""
    app_ui.render_page_header(
        "Start — what this platform does",
        "An AI-assisted platform for geochemical / material-leaching simulation and "
        "validation. Describe an experiment to plan a simulation, and — where you have "
        "measured data — validate and correct the predictions against it.",
        eyebrow="Overview",
    )
    _render_modes_panel()
    app_ui.render_workflow_steps(WORKFLOW_STEPS, current=None)
    st.write("")

    app_ui.section_header("Validation module — status",
                          "measured-data, mapping & comparison status for the current run")
    _render_presentation_summary(selected_run)
    st.divider()

    master_path = config.PROCESSED_DIR / config.MASTER_DATASET_CSV
    template_path = config.EXPERIMENTAL_ICP_DIR / config.EXPERIMENTAL_TEMPLATE_CSV
    measured = _load_measured_safe()
    measured_exists = has_measured_data(measured)

    app_ui.section_header("Workspace status", "shared pipeline artifacts (validation module)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("master_dataset.csv", "present" if master_path.exists() else "missing")
    n_rows = (
        len(_read_csv(str(master_path), master_path.stat().st_mtime))
        if master_path.exists() else 0
    )
    c2.metric("master rows", n_rows)
    c3.metric("release template", "present" if template_path.exists() else "missing")
    c4.metric("measured data", "yes" if measured_exists else "not yet")
    if not measured_exists:
        st.info(
            "No measured data yet — the **Validate** module stays dormant until measured "
            "data is added. You can still use the **Simulate** tab to plan a simulation "
            "before any measurement. (For the fly-ash workflow this is the Phase-2 release "
            "data; comparison and any future ML stay dormant until it is entered.)"
        )

    st.divider()
    app_ui.section_header("Selected run", "the active save file")
    if not selected_run:
        st.info("No run selected. Create or open one in the **Experiment runs** sidebar (left).")
        return

    cfg = run_manager.load_run_config(selected_run)
    rt = cfg.get("run_type")
    data = run_manager.read_data_file(selected_run)
    lab_like = rt in run_manager.LAB_LIKE_RUN_TYPES
    has_map = run_manager.has_mapping(selected_run) if lab_like else False
    map_summary = (
        run_manager.summarize_mapping(run_manager.read_mapping(selected_run))
        if lab_like else {"n_samples": 0, "n_unique_rows": 0, "has_collisions": False}
    )
    icp_present = lab_like and any(_has_numeric(data, c) for c in _ICP_MEASURED_COLS)
    is_mock = _looks_like_run_test(data)
    comp_exists = lab_like and run_manager.has_comparison(selected_run)

    st.markdown(f"**`{selected_run}`**")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Run type", rt)
    s2.metric("Data rows", len(data))
    s3.metric("Mapped samples", map_summary["n_samples"] if lab_like else "n/a")
    s4.metric("Unique model results used",
              map_summary["n_unique_rows"] if lab_like else "n/a")
    st.caption(f"📁 {_rel(run_manager.run_dir(selected_run))} · source `{cfg.get('data_source')}`")

    # Data quality status (one honest line).
    if data.empty:
        quality = "⚪ No data entered yet."
    elif is_mock:
        quality = "🔴 Mock/test data detected — for code checking only, not evidence."
    elif lab_like and not icp_present:
        quality = "🟡 pH-only — ICP chemistry (Ca/Si/Al/Fe/REE) not yet entered."
    elif lab_like and icp_present:
        quality = "🟢 Full ICP chemistry present."
    else:
        quality = f"🟢 {len(data)} row(s) entered."
    st.markdown(f"**Data quality:** {quality}")

    # What's missing.
    missing: list[str] = []
    if data.empty:
        missing.append("no data rows entered yet")
    if lab_like:
        if not icp_present:
            missing.append("ICP chemistry (Ca/Si/Al/Fe/REE) — pH-only so far")
        if not has_map:
            missing.append("measured → model mapping (needed for residuals)")
    if missing:
        st.markdown("**Missing / not yet present:**")
        for m in missing:
            st.markdown(f"- {m}")

    # Recommended next action (shared logic, also surfaced on every tab).
    st.success(f"**Recommended next action:** {_next_step_hint(selected_run)}")

    # Workflow checklist — rendered as a progression stepper (first incomplete
    # step is highlighted; completed steps read as done).
    st.divider()
    app_ui.section_header("Workflow checklist", "progress for this run")
    data_uploaded = not data.empty
    data_checked = (config.TABLES_DIR / config.EXPERIMENTAL_VALIDATION_REPORT_CSV).exists()
    mapping_complete = lab_like and has_map
    workflow_run = comp_exists
    results_available = comp_exists and _comparison_has_residuals(selected_run)

    checks = [data_uploaded, data_checked, mapping_complete, workflow_run, results_available]
    labels = ["Data uploaded", "Data checked", "Mapping complete",
              "Workflow run", "Results available"]
    current = next((i for i, c in enumerate(checks) if not c), len(checks))
    app_ui.render_workflow_steps(labels, current=current)
    st.markdown(
        " &nbsp;·&nbsp; ".join(
            app_ui.status_badge(lbl, "good" if done else "neutral")
            for lbl, done in zip(labels, checks)
        ),
        unsafe_allow_html=True,
    )


def _render_unit_conversions_applied(df: pd.DataFrame) -> None:
    """"Unit conversions applied" expander: original→target, formula, molar mass, examples.

    Reads the conversion-provenance companions an import wrote, so every converted
    value is traceable to its original value + unit + the registry conversion id.
    Renders nothing when the frame carries no converted columns.
    """
    rows = import_mapping.conversion_provenance_summary(df)
    if not rows:
        return
    with st.expander("Unit conversions applied", expanded=False):
        st.caption("Every converted column kept its original value, original unit, and the "
                   "registry `conversion_id`, so the conversion is auditable later. "
                   f"Molar masses: {units.MOLAR_MASS_SOURCE}.")
        for r in rows:
            mm = r["molar_mass_g_mol"]
            head = (f"**{r['column']}** — {r['from_unit']} → {r['to_unit']}  ·  "
                    f"`{r['conversion_id']}`")
            if mm is not None:
                head += f"  ·  M_{r['element']} = {mm:g} g/mol"
            st.markdown(head)
            st.caption(f"formula: `{r['formula']}`")
            if r["examples"]:
                st.dataframe(pd.DataFrame(r["examples"]), use_container_width=True,
                             hide_index=True, height=min(40 + 35 * len(r["examples"]), 160))


def _render_run_data_and_edit(run_name: str, rt: str) -> None:
    """This run's data table + row deletion + CSV/pipeline export (no mapping)."""
    data = run_manager.read_data_file(run_name)
    st.markdown(f"**This run's data** ({len(data)} row(s)):")
    st.dataframe(data, use_container_width=True, height=300)
    _render_unit_conversions_applied(data)

    # --- Delete / clean rows --- only affects THIS run's CSV.
    if not data.empty:
        with st.expander("🗑️ Delete rows", expanded=False):
            id_col = run_manager.id_column_for(run_name)

            def _row_label(i: int) -> str:
                if id_col in data.columns:
                    val = data.iloc[i][id_col]
                    shown = "" if pd.isna(val) else str(val).strip()
                    return f"Row {i} — {id_col}={shown or '(blank)'}"
                return f"Row {i}"

            to_delete = st.multiselect(
                "Select row numbers to delete",
                options=list(range(len(data))),
                format_func=_row_label,
                key=f"del_rows_{run_name}",
            )
            confirm = st.checkbox(
                "I understand this will delete the selected rows from this run's CSV.",
                key=f"del_confirm_{run_name}",
            )
            if st.button("Delete selected rows", key=f"del_btn_{run_name}"):
                if not to_delete:
                    st.warning("No rows selected — nothing was deleted.")
                elif not confirm:
                    st.warning("Tick the confirmation checkbox before deleting.")
                else:
                    n = run_manager.delete_data_rows(run_name, to_delete)
                    st.success(f"Deleted {n} row(s) from this run's CSV.")
                    _read_csv.clear()
                    st.rerun()

            st.divider()
            st.caption(f"Remove rows with a blank `{id_col}` or where every value is empty.")
            if st.button("Remove blank rows", key=f"del_blank_{run_name}"):
                n = run_manager.remove_blank_data_rows(run_name)
                if n:
                    st.success(f"Removed {n} blank row(s).")
                    _read_csv.clear()
                    st.rerun()
                else:
                    st.info("No blank rows found.")

    # Export this run's CSV.
    ec1, ec2 = st.columns(2)
    with ec1:
        if not data.empty:
            export_name = f"{run_name}_{run_manager.spec_for(rt).data_filename}"
            if st.download_button(
                "⬇️ Export this run's CSV",
                data=data.to_csv(index=False).encode("utf-8"),
                file_name=export_name,
                mime="text/csv",
                use_container_width=True,
            ):
                audit.log_export(run_name, kind="run_csv", file_name=export_name,
                                 n_rows=int(len(data)))
    with ec2:
        if rt in run_manager.LAB_LIKE_RUN_TYPES:
            if st.button("➡️ Export to pipeline (manual-entry CSV)", use_container_width=True,
                         key=f"export_pipe_{run_name}"):
                try:
                    dest = run_manager.export_lab_run_to_pipeline(run_name)
                    audit.log_export(run_name, kind="pipeline_manual_entry",
                                     file_name=dest.name, n_rows=int(len(data)))
                    st.success(
                        f"Copied to {_rel(dest)} — the existing "
                        "scripts (05/07) will pick it up."
                    )
                    _read_csv.clear()
                except run_manager.RunManagerError as exc:
                    st.error(str(exc))


def _audit_once(run_name: str, dedupe_key: str, log_fn) -> None:
    """Log a render-time event at most once per (run, key) per session (no spam).

    Render functions run on every interaction; ``dedupe_key`` should encode the
    salient state (e.g. the counts) so a *genuine* change logs again but a re-render
    of the same state does not.
    """
    if not run_name:
        return
    seen = st.session_state.setdefault("_audit_seen", set())
    token = (run_name, dedupe_key)
    if token in seen:
        return
    seen.add(token)
    try:
        log_fn()
    except Exception:  # pragma: no cover - logging must never crash a render
        pass


def _render_basic_validation_summary(run_name: str) -> None:
    """Quick error/warning count over this lab run's data (reuses the validator)."""
    data = run_manager.read_data_file(run_name)
    if data.empty:
        return
    issues = validate_experimental_df(data, source=run_name)
    real = [i for i in issues if i.get("severity") in ("error", "warning")]
    errors = [i for i in real if i["severity"] == "error"]
    warnings = [i for i in real if i["severity"] == "warning"]
    _audit_once(
        run_name, f"validation:{len(errors)}:{len(warnings)}",
        lambda: audit.log_validation(
            run_name, severity_counts={"error": len(errors), "warning": len(warnings)},
            source=run_name))

    st.markdown("**Basic data validation**")
    v1, v2 = st.columns(2)
    v1.metric("Errors", len(errors))
    v2.metric("Warnings", len(warnings))
    if not real:
        st.success("No validation errors or warnings on the entered rows.")
    else:
        report = pd.DataFrame(real)[["severity", "check", "column", "message"]]
        st.dataframe(report, use_container_width=True, height=200)


def _render_import_tab(selected_run: str | None) -> None:
    app_ui.render_page_header(
        "Import — add experimental data",
        "Import or enter your experimental (measured) data for the selected run, review the "
        "detected records and unit conversions, then save to the run. Measured data is used "
        "later to validate and correct simulated predictions.",
        eyebrow="Validation module · Import",
    )
    _render_next_step(selected_run)
    app_ui.render_workflow_steps(
        ["Upload / import data", "Review detected records", "Review units", "Save to run"],
        current=0,
    )
    if not selected_run:
        st.info("Select or create a run in the **Experiment runs** sidebar (left) to import "
                "data. Use **lab_experiment** for measured ICP/pH data, **literature_benchmark** "
                "for reported values, or **synthetic_demo** for testing.")
        return
    cfg = run_manager.load_run_config(selected_run)
    rt = cfg.get("run_type")
    app_ui.section_header(f"Run · {selected_run}", rt)
    _run_type_warning(rt)

    if rt in run_manager.LAB_LIKE_RUN_TYPES:
        _lab_data_import(selected_run)
        st.divider()
        _lab_entry_form(selected_run)
    elif rt == "literature_benchmark":
        _literature_entry(selected_run)
    elif rt == "synthetic_demo":
        _demo_entry(selected_run)

    st.divider()
    _render_run_data_and_edit(selected_run, rt)
    st.caption("Data-quality validation moved to the **Validate** tab.")

    st.divider()
    with st.expander("Legacy global data entry — not recommended", expanded=False):
        st.caption(
            "This form predates per-run save files and writes to one shared "
            "pipeline file. Prefer the run-specific entry above."
        )
        _render_legacy_global_form()


def _render_match_tab(selected_run: str | None) -> None:
    app_ui.render_page_header(
        "Match — link measured data to model predictions",
        f"Automatic-first: records are detected and model-prediction mappings are suggested "
        f"for you (current model: {MODEL_NAME}). Review, then accept. Manual mapping stays "
        "available under advanced.",
        eyebrow="Validation module · Match",
    )
    _render_next_step(selected_run)
    app_ui.render_workflow_steps(
        ["Auto-detect measured records", "Auto-suggest model mappings",
         "Review suggestions", "Accept mappings"],
        current=1,
    )
    if not selected_run:
        st.info(
            "Select or create a **lab_experiment** (or **plastic_composite**) run in the "
            "sidebar to add a measured → model mapping."
        )
        return
    rt = run_manager.load_run_config(selected_run).get("run_type")
    if rt == "literature_benchmark":
        st.info(
            "Literature benchmark runs do not use sample-to-model mapping as measured "
            "lab data."
        )
        return
    if rt not in run_manager.LAB_LIKE_RUN_TYPES:
        st.info(
            "Mapping is only available for **lab_experiment** or **plastic_composite** "
            "runs. The current run is a synthetic/demo run (testing only)."
        )
        return
    _render_mapping_section(selected_run)


def _render_run_workflow_tab(selected_run: str | None) -> None:
    st.write(
        "Run all the relevant scripts in order for the selected run and see their output. "
        "For a lab run this exports the run's data (and mapping) to the pipeline, then "
        "runs Phase 1 → validate → compare → sustainability, stopping at the first failure."
    )
    if not selected_run:
        st.info(
            "Select or create a run in the **Experiment runs** sidebar (left) first, then "
            "this button will run the workflow for it."
        )
    else:
        rt = run_manager.load_run_config(selected_run).get("run_type")
        st.caption(f"Selected run: `{selected_run}` — **{rt}**")
        if st.button("▶️ Run selected experiment workflow", type="primary", key="wf_run_btn"):
            if rt in run_manager.LAB_LIKE_RUN_TYPES:
                _run_lab_workflow(selected_run)
            elif rt == "literature_benchmark":
                st.warning(
                    "📚 This is a **literature-benchmark** run. Literature data are kept "
                    "separate from our measured lab data and are **not** run through the "
                    "measured-vs-PHREEQC pipeline. Nothing was exported."
                )
                _lit = run_manager.read_data_file(selected_run)
                if not _lit.empty:
                    st.markdown("**Literature benchmark data:**")
                    st.dataframe(_lit, use_container_width=True, height=300)
                else:
                    st.info("No literature rows entered yet.")
            elif rt == "synthetic_demo":
                st.warning(
                    "🧩 This is a **synthetic/demo** run. Synthetic data are only for "
                    "testing the code — they are not real experimental data and are not "
                    "run through the pipeline."
                )

    with st.expander("Advanced individual script controls", expanded=False):
        st.caption("Low-level: run a single script and view its raw output.")
        a1, a2 = st.columns(2)
        with a1:
            _script_button("Run Phase 1 pipeline", "scripts/run_phase1.py", "Phase 1",
                           "adv_phase1", refresh_csv=True)
        with a2:
            _script_button("Run Phase 2 comparison", "scripts/05_compare_experimental.py",
                           "Phase 2", "adv_phase2", refresh_csv=True)
        b1, b2 = st.columns(2)
        with b1:
            _script_button("Validate experimental CSVs",
                           "scripts/07_validate_experimental_data.py", "Validation", "adv_validate")
        with b2:
            _script_button("Run sustainability score", "scripts/08_sustainability_score.py",
                           "Sustainability score", "adv_sustain")


def _render_condition_results(selected_run: str | None) -> None:
    """Feature 6 — replicate-aware results: condition mean ± std vs PHREEQC."""
    if not selected_run:
        return
    data = run_manager.read_data_file(selected_run)
    if data.empty or "sample_id" not in data.columns:
        return

    st.markdown("#### Condition-level replicate comparison")
    results_path = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    if results_path.exists():
        manifest = _scenario_manifest(str(results_path), results_path.stat().st_mtime)
    else:
        manifest = pd.DataFrame(columns=scenarios.MANIFEST_COLUMNS)
    cond_map = run_manager.read_condition_mapping(selected_run)

    mode = st.radio(
        "Comparison mode", ["Replicate mean comparison", "Individual replicate comparison"],
        key=f"cmp_mode_{selected_run}",
        help="Default compares each condition's replicate mean ± std to PHREEQC; the other "
             "compares each replicate row to its mapped PHREEQC solution.",
    )

    if mode.startswith("Replicate mean"):
        comp = replicates.condition_mean_comparison(data, cond_map, manifest)
        if comp.empty:
            st.info("No conditions to compare yet.")
            return
        st.dataframe(comp, use_container_width=True, height=260)
        n_lt2 = int((comp["n_replicates"] < 2).sum())
        if n_lt2:
            st.warning(f"⚠️ {n_lt2} condition(s) have fewer than 2 replicates — no standard "
                       "deviation, so the mean is a single measurement.")
        mc1, mc2 = st.columns([3, 2])
        metric = mc1.selectbox("Metric to plot", replicates.VALUE_COLUMNS,
                               key=f"cmp_metric_{selected_run}")
        err_label = mc2.radio("Error bars", ["std", "SEM"], horizontal=True,
                              key=f"cmp_err_{selected_run}",
                              help="std = replicate spread; SEM = std/√n = uncertainty of "
                                   "the mean. n=1 conditions show no bar.")
        _render_condition_errorbar(comp, metric, "sem" if err_label == "SEM" else "std")
        st.caption("Measured error bars vs the model point: **a residual smaller than the "
                   "replicate spread is indistinguishable from experimental noise** (see "
                   "`within_meas_std_*` in the table).")
        with st.expander("Advanced: individual replicate scatter"):
            ind = replicates.individual_replicate_comparison(
                data, run_manager.read_mapping(selected_run), manifest)
            st.dataframe(ind, use_container_width=True, height=240)
    else:
        ind = replicates.individual_replicate_comparison(
            data, run_manager.read_mapping(selected_run), manifest)
        if ind.empty:
            st.info("No replicate rows to compare yet.")
            return
        st.dataframe(ind, use_container_width=True, height=300)
        st.caption("Each replicate vs its mapped PHREEQC solution (residual = measured − PHREEQC).")
    st.markdown("---")


def _render_condition_errorbar(comp: pd.DataFrame, metric: str, err_kind: str = "std") -> None:
    """Measured mean ± (std|SEM) vs PHREEQC prediction, one point per condition.

    A condition with no spread (n=1, error is NaN) is drawn as a mean marker with **no
    error bar** — never a fake zero — and is listed below the plot.
    """
    label = replicates.RESIDUAL_LABEL[metric]
    sub = comp.dropna(subset=[f"mean_{metric}"]).reset_index(drop=True)
    if sub.empty:
        st.caption(f"No measured `{metric}` values to plot.")
        return
    import matplotlib.pyplot as plt  # lazy: only when a figure is drawn

    err = pd.to_numeric(sub[f"{err_kind}_{metric}"], errors="coerce")
    has_err = err.notna()
    x = list(range(len(sub)))
    fig, ax = plt.subplots(figsize=(7, 3.6))
    err_name = "SEM" if err_kind == "sem" else "std"
    # Points with a spread get an error bar; n=1 points are plain markers (no fake 0).
    if has_err.any():
        idx = [i for i in x if has_err.iloc[i]]
        ax.errorbar([x[i] for i in idx], sub[f"mean_{metric}"].iloc[idx], yerr=err.iloc[idx],
                    fmt="o", capsize=4, label=f"measured mean ± {err_name}")
    if (~has_err).any():
        idx = [i for i in x if not has_err.iloc[i]]
        ax.scatter([x[i] for i in idx], sub[f"mean_{metric}"].iloc[idx],
                   marker="o", facecolors="none", edgecolors="C0",
                   label="measured mean (n=1, no spread)")
    pheq = pd.to_numeric(sub[f"phreeqc_{label}"], errors="coerce")
    if pheq.notna().any():
        ax.scatter(x, pheq, marker="x", color="red", s=60, label="PHREEQC")
    ax.set_xticks(x)
    ax.set_xticklabels(sub["condition_key"], rotation=45, ha="right", fontsize=7)
    ax.set_ylabel(metric)
    ax.legend(fontsize=8)
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)
    st.caption(_LIVE_COMPARE_NOTE)
    n1 = sub.loc[~has_err, "condition_key"].astype(str).tolist()
    if n1:
        st.caption(f"n=1 (no error bar): {', '.join(f'`{c}`' for c in n1)}.")


def _render_overview_plot(ov: dict, variable: str, overlay: bool,
                          err_kind: str = "std") -> list[str]:
    """Measured-data overview plot. Returns the list of n=1 conditions (no error bar).

    Error bars use ``err_kind`` (``"std"`` or ``"sem"``). A single-replicate condition
    has no spread, so its mean is drawn **without** an error bar (never a fake zero).
    """
    import matplotlib.pyplot as plt  # lazy: only when a figure is drawn

    plot = ov["plot"]
    tcol = measured_overview.TIME_COLUMN
    conditions = sorted(plot["condition_key"].astype(str).unique())
    cmap = plt.get_cmap("tab10" if len(conditions) <= 10 else "tab20")
    color = {c: cmap(i % cmap.N) for i, c in enumerate(conditions)}
    stats = {str(r["condition_key"]): r for _, r in ov["group_stats"].iterrows()}
    n1: list[str] = []

    def _bar(x, c, ecolor):
        g = stats[c]
        err = g.get(err_kind)
        if err is None or pd.isna(err):     # n=1 → omit the bar, just mark the mean
            ax.errorbar(x, g["mean"], yerr=None, fmt="_", color=ecolor,
                        elinewidth=1.6, zorder=2)
            if str(c) not in n1:
                n1.append(str(c))
        else:
            ax.errorbar(x, g["mean"], yerr=err, fmt="_", color=ecolor,
                        capsize=4, elinewidth=1.6, zorder=2)

    fig, ax = plt.subplots(figsize=(7.5, 4))
    use_time = ov["has_time"] and tcol in plot.columns and plot[tcol].notna().any()

    if use_time:
        for c in conditions:
            sub = plot[plot["condition_key"].astype(str) == c]
            ax.scatter(pd.to_numeric(sub[tcol], errors="coerce"), sub["value"],
                       color=color[c], label=c, edgecolor="black", linewidth=0.3, zorder=3)
            if overlay and c in stats:
                _bar(pd.to_numeric(sub[tcol], errors="coerce").mean(), c, color[c])
        ax.set_xlabel(tcol)
    else:
        pos = {c: i for i, c in enumerate(conditions)}
        for c in conditions:
            sub = plot[plot["condition_key"].astype(str) == c]
            ax.scatter([pos[c]] * len(sub), sub["value"], color=color[c], label=c,
                       edgecolor="black", linewidth=0.3, zorder=3)
            if overlay and c in stats:
                _bar(pos[c], c, "black")
        ax.set_xticks(range(len(conditions)))
        ax.set_xticklabels(conditions, rotation=45, ha="right", fontsize=7)

    ax.set_ylabel(variable)
    ax.set_title(f"{variable} — measured data only")
    if len(conditions) <= 12:
        ax.legend(fontsize=7, title="condition", loc="best")
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)
    st.caption(_LIVE_MEASURED_NOTE)
    return n1


def _render_measured_overview(selected_run: str) -> None:
    """First plot family — measured data only, from the run's own rows.

    Renders fully without a sample→PHREEQC mapping and without
    ``data/processed/phreeqc_results.csv`` (it reads nothing but the run's data).
    """
    app_ui.section_header("Measured data overview", "measured data only — no model comparison")

    data = run_manager.read_data_file(selected_run)
    variables = measured_overview.available_variables(data)
    if not variables:
        st.info(
            "No numeric measured variables in this run yet — enter pH or ICP values in "
            "the **Import Data** tab to see the overview."
        )
        return

    c1, c2, c3 = st.columns([3, 2, 2])
    variable = c1.selectbox("Measured variable", variables, key=f"overview_var_{selected_run}")
    overlay = c2.checkbox("Overlay condition mean ± error", value=True,
                          key=f"overview_overlay_{selected_run}")
    err_label = c3.radio("Error bars", ["std", "SEM"], horizontal=True,
                         key=f"overview_err_{selected_run}",
                         help="std = spread of the replicates; SEM = std/√n = uncertainty "
                              "of the mean. Single-replicate conditions show no bar.")
    err_kind = "sem" if err_label == "SEM" else "std"

    ov = measured_overview.prepare_overview(data, variable)
    rep_counts = ov["replicate_counts"]
    rc_txt = ", ".join(f"{k}: {v}" for k, v in sorted(rep_counts.items())) if rep_counts else "—"
    st.markdown(
        f"- **Rows shown:** {ov['n_shown']}  ·  **Distinct conditions:** {ov['n_conditions']}  "
        f"·  **Rows excluded:** {ov['n_excluded']}"
    )
    st.caption(f"Replicate counts per condition — {rc_txt}")

    if ov["n_excluded"]:
        with st.expander(f"Excluded rows ({ov['n_excluded']}) — blank or non-numeric values"):
            st.dataframe(ov["excluded"], use_container_width=True, height=200)

    if ov["plot"].empty:
        st.info(f"No numeric `{variable}` values to plot.")
        return
    n1 = _render_overview_plot(ov, variable, overlay, err_kind)
    if overlay:
        which = "standard error of the mean (SEM = std/√n)" if err_kind == "sem" else \
            "standard deviation (std, ddof=1)"
        st.caption(f"Error bars show the **{which}** per condition; replicate counts (n) "
                   f"per condition are listed above.")
        if n1:
            st.caption(f"⚠️ n=1 (no error bar): {', '.join(f'`{c}`' for c in sorted(n1))} — "
                       "a single measurement has no spread.")


def _inclusion_variables(comp: pd.DataFrame) -> list[str]:
    """Variables with a measured column (numeric) + a model-prediction column."""
    out = []
    for v, (mcol, pcol) in compare_inclusion.VARIABLE_SPEC.items():
        if mcol in comp.columns and pcol in comp.columns and _has_numeric(comp, mcol):
            out.append(v)
    return out


def _render_comparison_inclusion(selected_run: str) -> None:
    """Explicit inclusion view: counts, exclusion reasons, status-aware scatter, validity.

    All filtering comes from the single `compare.comparison_inclusion` function — this
    renderer only displays its output (the plots never re-derive the filter).
    """
    comp_path = _run_comparison_path(selected_run)
    if comp_path is None or not comp_path.exists():
        return  # the results summary already explains "no comparison yet"
    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    variables = _inclusion_variables(comp)
    if not variables:
        return  # nothing comparable; residual figures / summary cover the messaging

    app_ui.section_header("Model comparison — coverage & inclusion",
                          "measured vs model prediction")
    data = run_manager.read_data_file(selected_run)
    mapping = run_manager.read_mapping(selected_run)
    manifest = _manifest_if_available()

    c1, c2 = st.columns([3, 2])
    variable = c1.selectbox("Variable", variables, key=f"incl_var_{selected_run}")
    include_unsafe = c2.checkbox(
        "Advanced: include unsafe mappings (flagged red)", value=False,
        key=f"incl_unsafe_{selected_run}",
        help="Unsafe mappings are excluded from the comparison by default. Toggling this "
             "plots them flagged in red — they are known metadata conflicts, not valid "
             "model comparisons.",
    )

    inc = comparison_inclusion(data, mapping, comp, variable,
                               manifest=manifest, include_unsafe=include_unsafe)

    # Coverage cards — straight from the inclusion dict (consistent with the table).
    n_excluded = inc["n_total"] - inc["rows_plotted"]
    app_ui.render_metric_cards([
        {"label": "Measured rows", "value": inc["measured_rows_available"]},
        {"label": "With mapping", "value": inc["rows_with_mapping"]},
        {"label": "Prediction available", "value": inc["rows_prediction_available"]},
        {"label": "Rows plotted", "value": inc["rows_plotted"],
         "status": "good" if inc["rows_plotted"] else "neutral"},
        {"label": "Unique predictions", "value": inc["unique_predictions_used"]},
        {"label": "Excluded rows", "value": n_excluded,
         "status": "warning" if n_excluded else "good"},
    ])

    if include_unsafe and inc["n_unsafe_plotted"]:
        st.error(
            f"⚠️ {inc['n_unsafe_plotted']} unsafe mapping(s) are plotted (red) — known "
            "metadata conflicts, shown for inspection only, not valid model comparison."
        )

    with st.expander(f"Rows excluded from model comparison ({inc['n_total'] - inc['rows_plotted']})"):
        st.caption("Each excluded row has exactly one reason; plotted + excluded = all rows.")
        if inc["excluded"].empty:
            st.success("No rows excluded — every measured row is plotted.")
        else:
            st.dataframe(inc["excluded"], use_container_width=True, height=240)
            rc = pd.DataFrame([{"reason": k, "rows": v} for k, v in inc["reason_counts"].items()])
            st.dataframe(rc, use_container_width=True, hide_index=True, height=180)

    if not inc["plotted"].empty:
        st.caption("**Measured vs model prediction** — points near the dashed 1:1 line "
                   "indicate agreement *only if the mapping is scientifically valid*.")
        st.pyplot(compare_plots.comparison_scatter_figure(inc["plotted"], variable))
        st.caption(_LIVE_COMPARE_NOTE)

    if inc["collapse_warning"]:
        st.warning(
            "🔁 " + inc["collapse_message"]
            + " See the **Match** tab → *Conditions needing new PHREEQC "
            "simulations* for the list."
        )

    # One overall validity panel, chosen by the documented rules. Only `valid` implies
    # the model was validated; everything else reads as preliminary / workflow check.
    app_ui.render_warning_panel(
        inc["validity"].upper(), inc["validity_message"],
        level=inc["validity"] if inc["validity"] in app_ui.STATUS_STYLES else "info",
    )


def _bias_band_figure(points: pd.DataFrame, element: str, unit: str, band: dict | None):
    """Per-condition residual scatter with a shaded mean±std band (where sufficient)."""
    import matplotlib.pyplot as plt  # lazy: only when a figure is drawn

    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    conditions = sorted(points["condition_key"].unique())
    xpos = {ck: i for i, ck in enumerate(conditions)}
    ax.axhline(0.0, color="#888", lw=1, ls="--", zorder=1)
    if band is not None:
        mean, std = band["mean"], band["std"]
        ax.axhspan(mean - std, mean + std, color="#3b82f6", alpha=0.12, zorder=0,
                   label=f"pooled mean ± std (n={band['n']})")
        ax.axhline(mean, color="#3b82f6", lw=1.4, zorder=2)
    ax.scatter([xpos[c] for c in points["condition_key"]], points["residual"],
               color="#1f77b4", s=42, zorder=3, edgecolor="white", linewidth=0.5)
    ax.set_xticks(range(len(conditions)))
    ax.set_xticklabels(conditions, rotation=30, ha="right", fontsize=8)
    ax.set_ylabel(f"residual ({unit}) = measured − model")
    ax.set_title(f"{element}: exact-mapped residuals by condition")
    if band is not None:
        ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    return fig


def _render_systematic_bias(selected_run: str) -> None:
    """"Systematic bias (exact mappings only)" — descriptive residual statistics.

    Reads this run's comparison, takes the inclusion module's status join (single
    source), and shows the per-element/per-condition mean±std bias over **exact**
    mappings only. Always carries the explicit non-claim line; below ``min_n`` it
    shows "insufficient exact pairs", never a number.
    """
    comp_path = _run_comparison_path(selected_run)
    if comp_path is None or not comp_path.exists():
        return
    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    if not any(_has_numeric(comp, f"residual_{el}")
               for el in (*config.RESIDUAL_ELEMENTS, "pH")):
        return  # no residuals at all → nothing to summarise

    data = run_manager.read_data_file(selected_run)
    mapping = run_manager.read_mapping(selected_run)
    manifest = _manifest_if_available()
    statuses = residual_stats.collect_sample_statuses(
        data, mapping, comp, manifest=manifest)
    table = residual_stats.bias_table(comp, statuses)

    with st.expander("Systematic bias (exact mappings only)"):
        st.caption(
            "Mean residual (`measured − model`) over exact-mapped pairs only. "
            "Positive → the model underpredicts; negative → it overpredicts."
        )
        # The explicit non-claim, shown every time the table renders.
        app_ui.render_warning_panel(
            "Descriptive only — not a correction model",
            residual_stats.NON_CLAIM_LINE, level="preliminary",
        )
        if table.empty:
            st.info(
                "No exact-mapped comparisons yet, so no bias can be estimated. "
                "Bias is computed only over mappings classified **exact** in the "
                "Match tab — scenario-level, unsafe and unmapped rows are excluded."
            )
            return

        # Display frame: hide the mean for insufficient rows (no over-claiming).
        disp = table.copy()
        disp["mean_residual_shown"] = [
            (f"{r.mean_residual:+.3g}" if r.sufficient
             else f"insufficient ({int(r.n_exact_pairs)} of {residual_stats.DEFAULT_MIN_N})")
            for r in table.itertuples()
        ]
        disp = disp[["element", "condition_key", "n_exact_pairs",
                     "mean_residual_shown", "std", "sem", "unit", "sufficient"]]
        st.dataframe(disp, use_container_width=True, hide_index=True, height=260)

        # Plain-language captions for the rows that meet the threshold.
        good = table[table["sufficient"]]
        if not good.empty:
            for r in good.itertuples():
                st.markdown(f"- {residual_stats.describe_bias_row(r._asdict())}")
        else:
            st.caption(
                f"No condition yet reaches {residual_stats.DEFAULT_MIN_N} exact pairs — "
                "estimates are shown as counts, not means."
            )

        # Per-element residual band (shaded mean±std where the pooled estimate is sufficient).
        bands = residual_stats.sufficient_bias_bands(table)
        plot_elements = [el for el in (*config.RESIDUAL_ELEMENTS, "pH")
                         if not residual_stats.exact_residuals(comp, statuses, el).empty]
        if plot_elements:
            element = st.selectbox("Residual band — element", plot_elements,
                                   key=f"bias_elem_{selected_run}")
            pts = residual_stats.exact_residuals(comp, statuses, element)
            unit = dict((e, u) for e, _c, u in residual_stats.element_specs()).get(element, "mM")
            band = bands.get(element)
            st.pyplot(_bias_band_figure(pts, element, unit, band))
            st.caption(_LIVE_COMPARE_NOTE)
            if band is None:
                st.caption(
                    "No shaded band drawn — the pooled estimate for this element has "
                    f"fewer than {residual_stats.DEFAULT_MIN_N} exact pairs."
                )


def _residual_elements_with_data(comp: pd.DataFrame) -> list[str]:
    """Elements (Ca/Si/Al/Fe/pH) that actually carry a numeric residual column."""
    return [el for el in (*config.RESIDUAL_ELEMENTS, "pH")
            if _has_numeric(comp, f"residual_{el}")]


def _render_residual_correction(selected_run: str) -> None:
    """"Residual correction (experimental)" — hard-gated GP, raw-vs-corrected only.

    Shows gate progress per element (a train button only when the gate is met),
    LOCO-vs-baseline honesty for trained models, and an **off-by-default** corrected
    overlay that always draws raw PHREEQC + correction + measured together. Corrected
    values never feed mapping/validity status or the comparison CSV.
    """
    comp_path = _run_comparison_path(selected_run)
    if comp_path is None or not comp_path.exists():
        return
    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    elements = _residual_elements_with_data(comp)
    if not elements:
        return

    with app_ui.advanced_expander("Residual correction (experimental) — GP, raw-vs-corrected"):
        try:
            from flyash_phreeqc_ml.ml import residual_model as _rm  # lazy: optional sklearn
        except Exception as exc:  # pragma: no cover - optional dependency
            st.info(f"scikit-learn not available ({exc}). Install requirements to train "
                    "a residual-correction model.")
            return

        st.caption(
            "A Gaussian-process model of the residual (`measured − model`) from condition "
            "metadata. **Experimental — not for scientific claims until enough exact "
            "measured–model pairs exist.** It is gated on data sufficiency, validated "
            "leave-one-condition-out against the constant-bias baseline, and shown only as a "
            "raw-vs-corrected overlay. " + _rm.NON_CLAIM_LINE
        )

        data = run_manager.read_data_file(selected_run)
        mapping = run_manager.read_mapping(selected_run)
        manifest = _manifest_if_available()
        statuses = residual_stats.collect_sample_statuses(
            data, mapping, comp, manifest=manifest)
        try:
            model_dir = run_manager.residual_model_dir(selected_run)
        except run_manager.RunManagerError:
            st.info("This run type has no residual-correction model.")
            return
        trained = _rm.load_residual_models(model_dir)

        for element in elements:
            gate = _rm.gate_status(comp, statuses, element)
            st.markdown(f"**{element}** — {gate.progress_message()}")
            if not gate.meets:
                st.caption(
                    f"Not enough exact-mapped data yet. Need ≥{gate.min_pairs} exact pairs "
                    f"across ≥{gate.min_conditions} conditions before a model can be trained — "
                    "use the systematic-bias bands above in the meantime."
                )
                continue

            if st.button(f"Train residual-correction model — {element}",
                         key=f"train_resid_{selected_run}_{element}"):
                try:
                    model = _rm.train_element_model(
                        comp, statuses, element, run_name=selected_run)
                    _rm.save_residual_model(model, model_dir)
                    st.success(f"Trained + saved residual model for {element}.")
                    trained = _rm.load_residual_models(model_dir)
                except _rm.ResidualModelGateError as exc:  # pragma: no cover - gated above
                    st.error(str(exc))
                except Exception as exc:  # pragma: no cover - defensive
                    st.error(f"Training failed: {exc}")

            model = trained.get(element)
            if model is None:
                st.caption("Gate met — train a model to see LOCO validation and the overlay.")
                continue

            loco = model.card.get("loco") or {}
            if "model_loco_rmse" in loco:
                beats = bool(loco.get("beats_baseline"))
                msg = (f"LOCO RMSE {loco['model_loco_rmse']:.3g} vs constant-bias baseline "
                       f"{loco['baseline_loco_rmse']:.3g} ({loco['n_folds']} held-out conditions).")
                if beats:
                    app_ui.render_warning_panel(
                        "Correction beats the constant-bias baseline (LOCO)", msg, level="exact")
                else:
                    app_ui.render_warning_panel(
                        "Correction does NOT beat the constant-bias baseline — stay with bias bands",
                        msg + " The leave-one-condition-out error is not lower than simply applying "
                        "the mean bias, so the correction is not recommended for unseen conditions.",
                        level="unsafe")
            else:
                st.caption("LOCO not available (need ≥2 evaluable conditions).")

            show = st.checkbox(
                f"Show 'Corrected (experimental)' overlay — {element}", value=False,
                key=f"corr_overlay_{selected_run}_{element}",
                help="Off by default. Draws raw PHREEQC, the correction, its 95% interval, "
                     "and the measured value together — never the corrected value alone.")
            if show:
                cols = _rm.element_columns(element)
                ann = replicates.annotate(comp, profiles.FLY_ASH_PROFILE)
                ex_mask = residual_stats.exact_mask(ann, statuses).values
                subset = ann[ex_mask].copy()
                subset = subset[pd.to_numeric(subset[cols["phreeqc"]], errors="coerce").notna()]
                if subset.empty:
                    st.caption("No exact-mapped rows with a model prediction to overlay.")
                else:
                    overlay = _rm.corrected_overlay(model, subset)
                    overlay["measured"] = pd.to_numeric(
                        subset[cols["measured"]], errors="coerce").values
                    st.pyplot(compare_plots.corrected_overlay_figure(
                        overlay, element, model.unit))
                    st.caption(
                        "**Corrected = raw PHREEQC + predicted residual.** Display only — these "
                        "corrected values do not change mapping status, validity status, or the "
                        "comparison CSV's residual columns.")
            st.divider()


def _render_results_tab(selected_run: str | None) -> None:
    # What's shown depends on run type, so a literature/synthetic run never displays
    # the lab measured-vs-PHREEQC residual as if it were its own result.
    summary_rt = (
        run_manager.load_run_config(selected_run).get("run_type") if selected_run else None
    )
    if summary_rt == "literature_benchmark":
        _render_literature_summary(selected_run)
        return
    if summary_rt == "synthetic_demo":
        st.warning(
            "This is a synthetic/demo run. Synthetic demo data are for testing the code "
            "only — not scientific output. The lab-experiment comparison is not shown here."
        )
        return

    # lab_experiment / plastic_composite, or no run selected.
    if summary_rt in run_manager.LAB_LIKE_RUN_TYPES and selected_run:
        st.markdown(f"**`{selected_run}` — measured-vs-model comparison.**")
    elif not selected_run:
        st.info("No run selected. Select a lab run in the sidebar to see its comparison.")
        return
    else:
        st.write("This run type has no measured-vs-model comparison.")
    st.caption(
        "Reads this run's own outputs (`experiments/<run>/outputs/`), stamped with "
        "provenance, plus the validation and sustainability tables in `outputs/tables/`."
    )

    # The measured-data-only overview now lives in the **Validate** tab (relocation).
    if summary_rt in run_manager.LAB_LIKE_RUN_TYPES and selected_run:
        _render_stale_results_warning(selected_run)
        data = run_manager.read_data_file(selected_run)
        status = replicates.overall_mapping_status(
            data, run_manager.read_mapping(selected_run), _manifest_if_available())
        if not status["all_exact"]:
            app_ui.render_warning_panel(
                "Preliminary / workflow check only",
                "The graphs below are not final model validation, because one or more "
                "mappings are scenario-level, unsafe, or missing exact PHREEQC metadata. "
                "Plotting is still allowed for inspection.",
                level="preliminary",
            )
    if summary_rt in run_manager.LAB_LIKE_RUN_TYPES:
        _render_condition_results(selected_run)
    _render_results_summary(selected_run)
    # Explicit inclusion panel (counts + exclusions + status-aware scatter + validity),
    # above the residual figures.
    if summary_rt in run_manager.LAB_LIKE_RUN_TYPES and selected_run:
        _render_comparison_inclusion(selected_run)
        _render_systematic_bias(selected_run)
        _render_residual_correction(selected_run)
    _render_comparison_figures(selected_run)
    st.info(
        "📈 **Interpreting the plots:** if measured values vary while PHREEQC "
        "predictions stay constant, this usually means the mapping is too coarse or "
        "more PHREEQC simulations are needed."
    )
    st.caption("Build a shareable report in the **Export** tab; data-quality + "
               "calculation validation is in the **Validate** tab.")


def _render_export_report(selected_run: str | None) -> None:
    """"Export validation report" — build the self-contained bundle + offer a zip.

    Lab-like runs only. Builds ``experiments/<run>/outputs/validation_report_<ts>/``
    (report.html + CSVs + figures + MANIFEST.json), which itself logs an audit event,
    then offers the folder as a zip download.
    """
    if not selected_run:
        return
    try:
        rt = run_manager.load_run_config(selected_run).get("run_type")
    except run_manager.RunManagerError:
        return
    if rt not in run_manager.LAB_LIKE_RUN_TYPES:
        return

    app_ui.section_header("Results report (simulation + validation)",
                          "a self-contained bundle a reviewer can open without the app")
    st.caption(
        "Builds `experiments/<run>/outputs/validation_report_<ts>/` — a self-contained "
        "`report.html` (inline CSS + embedded figures), the supporting CSVs "
        "(measured / predictions / mapping / residuals / excluded / needed simulations), "
        "the audit log, and a `MANIFEST.json` of SHA-256 hashes. The report header always "
        "carries the validity status. (PDF export is future work.)"
    )
    if st.button("Export validation report", key=f"export_report_{selected_run}"):
        with st.spinner("Building validation report…"):
            try:
                out = report.build_report(selected_run)
            except Exception as exc:  # never let a build error crash the tab
                st.error(f"Report build failed: {exc}")
                return
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(out.iterdir()):
                    if p.is_file():
                        zf.write(p, arcname=f"{out.name}/{p.name}")
            st.session_state[f"report_zip_{selected_run}"] = (out.name, buf.getvalue())
            st.success(f"Built `{_rel(out)}`. Download below.")

    blob = st.session_state.get(f"report_zip_{selected_run}")
    if blob:
        name, data_bytes = blob
        st.download_button("⬇️ Download report (.zip)", data=data_bytes,
                           file_name=f"{name}.zip", mime="application/zip",
                           key=f"report_dl_{selected_run}")


def _render_compare_tab(selected_run: str | None) -> None:
    """Run the workflow and read the comparison results (+ interpretation tools)."""
    app_ui.render_page_header(
        "Compare — run the workflow and read the comparison",
        "Run the pipeline for this run, then read the measured-vs-model comparison: "
        f"inclusion counts, residuals, systematic bias, and the validity line "
        f"(current model: {MODEL_NAME}).",
        eyebrow="Validation module · Compare",
    )
    _render_next_step(selected_run)
    if not selected_run:
        st.info("Select or create a **lab_experiment** (or **plastic_composite**) run in the "
                "sidebar, import your experimental data, link it to model predictions (Match "
                "tab), then run the comparison workflow here.")
        return
    app_ui.render_workflow_steps(
        ["Run workflow", "Model comparison", "Residual analysis", "Interpret"],
        current=0,
    )
    app_ui.section_header("Run workflow", "execute the pipeline for this run")
    _render_run_workflow_tab(selected_run)
    st.divider()
    app_ui.section_header("Results", "this run's own outputs, provenance-stamped")
    _render_results_tab(selected_run)
    st.divider()
    _render_assistant(selected_run)
    st.divider()
    app_ui.section_header("Surrogate (experimental)",
                          f"fast {MODEL_NAME} approximation — not a result path")
    _render_surrogate_expander(selected_run)


def _render_processed_viewer() -> None:
    if not config.PROCESSED_DIR.exists():
        st.warning("`data/processed/` does not exist yet — run Phase 1 first.")
        return
    csvs = sorted(p.name for p in config.PROCESSED_DIR.glob("*.csv"))
    if not csvs:
        st.warning("No processed CSVs yet — run Phase 1 first.")
        return
    ordered = [c for c in PREFERRED_PROCESSED if c in csvs] + [
        c for c in csvs if c not in PREFERRED_PROCESSED
    ]
    choice = st.selectbox("Processed CSV", ordered, key="processed_csv_choice")
    path = config.PROCESSED_DIR / choice
    df = _read_csv(str(path), path.stat().st_mtime)
    st.write(f"{df.shape[0]} rows × {df.shape[1]} columns")
    st.dataframe(df, use_container_width=True, height=300)


def _render_legacy_global_form() -> None:
    st.write(
        f"Submitting appends one row to `{_rel(MANUAL_ENTRY_PATH)}` "
        "(existing rows are never overwritten). Leave a field blank if not measured."
    )
    with st.form("experimental_entry", clear_on_submit=True):
        inputs: dict[str, str] = {}
        cols = st.columns(3)
        for i, column in enumerate(config.EXPERIMENTAL_RELEASE_COLUMNS):
            widget_col = cols[i % 3]
            numeric = column in config.EXPERIMENTAL_NUMERIC_COLUMNS
            label = f"{column} (number)" if numeric else column
            if column == "CO2_condition":
                inputs[column] = widget_col.selectbox(column, _CO2_OPTIONS)
            elif column == "precipitate_observed":
                inputs[column] = widget_col.selectbox(column, _YESNO_OPTIONS)
            else:
                inputs[column] = widget_col.text_input(label, value="")
        submitted = st.form_submit_button("Save row")

    if submitted:
        errors: list[str] = []
        for column in config.EXPERIMENTAL_NUMERIC_COLUMNS:
            raw = (inputs.get(column) or "").strip()
            if raw == "":
                continue
            try:
                float(raw)
            except ValueError:
                errors.append(f"'{column}' must be a number (got '{raw}').")
        if not inputs.get("sample_id", "").strip():
            errors.append("'sample_id' is required.")
        if errors:
            for e in errors:
                st.error(e)
        else:
            row = {col: (inputs.get(col) or "").strip() for col in config.EXPERIMENTAL_RELEASE_COLUMNS}
            new_df = pd.DataFrame([row], columns=config.EXPERIMENTAL_RELEASE_COLUMNS)
            MANUAL_ENTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            write_header = not MANUAL_ENTRY_PATH.exists()
            new_df.to_csv(MANUAL_ENTRY_PATH, mode="a", header=write_header, index=False)
            total = len(pd.read_csv(MANUAL_ENTRY_PATH))
            st.success(
                f"Saved sample '{row['sample_id']}'. "
                f"{MANUAL_ENTRY_PATH.name} now has {total} row(s)."
            )
            st.dataframe(new_df, use_container_width=True)
            _read_csv.clear()

    if MANUAL_ENTRY_PATH.exists():
        existing = pd.read_csv(MANUAL_ENTRY_PATH)
        st.markdown("**Current manual-entry file:**")
        st.dataframe(existing, use_container_width=True, height=300)


# Audit status -> emoji for at-a-glance scanning.
_AUDIT_STATUS_EMOJI = {
    calculations.STATUS_PASS: "✅ pass",
    calculations.STATUS_WARNING: "⚠️ warning",
    calculations.STATUS_FAIL: "❌ fail",
    calculations.STATUS_NA: "— not available",
}


def _render_formula_registry(dev_mode: bool) -> None:
    """List every documented formula with equation, I/O, units, provenance."""
    for f in calculations.FORMULAS:
        tag = "🧮 app-calculated" if f.source == "app-calculated" else "📥 parsed from PHREEQC"
        with st.expander(f"{f.name}  ·  {tag}", expanded=False):
            st.latex(f.latex)
            st.markdown(
                f"- **Equation:** `{f.equation}`\n"
                f"- **Inputs:** {', '.join(f'`{c}`' for c in f.inputs)}\n"
                f"- **Output:** `{f.output}`\n"
                f"- **Units:** {f.units}\n"
                f"- **Provenance:** {f.source}\n\n"
                f"{f.explanation}"
            )
            if dev_mode and f.detail:
                st.info(f"🛠️ {f.detail}")


def _render_residual_audit() -> None:
    """Recompute residuals from the stored comparison CSV and report pass/fail."""
    comp_path = config.PROCESSED_DIR / config.COMPARISON_CSV
    if not comp_path.exists():
        st.info(
            "No comparison file yet — run a lab workflow with a sample→PHREEQC mapping to "
            f"generate `{config.COMPARISON_CSV}`, then this audit re-derives every residual."
        )
        return

    comp = _read_csv(str(comp_path), comp_path.stat().st_mtime)
    audit = calculations.audit_comparison(comp)
    if audit.empty:
        st.info("Comparison file has no residual columns to audit yet.")
        return

    counts = audit["status"].value_counts().to_dict()
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("✅ pass", counts.get(calculations.STATUS_PASS, 0))
    s2.metric("⚠️ warning", counts.get(calculations.STATUS_WARNING, 0))
    s3.metric("❌ fail", counts.get(calculations.STATUS_FAIL, 0))
    s4.metric("— not available", counts.get(calculations.STATUS_NA, 0))

    if counts.get(calculations.STATUS_FAIL, 0):
        st.error(
            "At least one stored residual does **not** match a fresh recomputation. "
            "Investigate the mapping / units before trusting the comparison."
        )
    else:
        st.success(
            "Every re-derivable residual matches the stored value within tolerance "
            f"(pass ≤ {calculations.PASS_TOL:g}, warning ≤ {calculations.WARN_TOL:g})."
        )

    display = audit.copy()
    display["status"] = display["status"].map(_AUDIT_STATUS_EMOJI).fillna(display["status"])
    st.dataframe(display, use_container_width=True, height=300)
    st.caption(
        "`input_1 − input_2` is recomputed and compared to the stored residual. "
        "'not available' means a required input (or the stored value) is blank."
    )


def _render_unit_registry() -> None:
    """Molar-mass + conversion registries, rendered straight from units.py (one source)."""
    st.markdown("**Unit registry** — the single conversion authority (`units.py`).")
    cc1, cc2 = st.columns(2)
    with cc1:
        st.caption(f"Molar masses — {units.MOLAR_MASS_SOURCE}")
        st.dataframe(pd.DataFrame(units.molar_mass_rows()), use_container_width=True,
                     hide_index=True, height=240)
    with cc2:
        st.caption("Registered conversions (id · from → to · formula)")
        st.dataframe(pd.DataFrame(units.conversion_registry_rows()), use_container_width=True,
                     hide_index=True, height=240)


def _render_conversion_verification(selected_run: str | None) -> None:
    """Re-derive each converted column from its provenance companions and grade it."""
    st.caption("Recomputes every converted `*_mM` column from its stored original value + "
               "unit through the registry, catching a wrong molar mass or changed formula. "
               "Legacy rows (imported before provenance existed) are flagged, not errored.")
    if not selected_run:
        st.info("Select a run to verify its unit conversions.")
        return
    try:
        data = run_manager.read_data_file(selected_run)
    except run_manager.RunManagerError:
        st.info("This run has no data to verify.")
        return
    report = calculations.verify_conversions(data)
    if report.empty:
        st.info("No converted concentration columns with data in this run.")
        return
    display = report.copy()
    display["status"] = display["status"].map(_AUDIT_STATUS_EMOJI).fillna(display["status"])
    st.dataframe(display, use_container_width=True, hide_index=True, height=240)


def _render_unit_calculator() -> None:
    st.markdown("**ICP unit conversion** — dilution correction then mg/L → mM.")
    c1, c2, c3 = st.columns(3)
    element = c1.selectbox("Element", list(units.MOLAR_MASSES), key="calc_unit_el")
    reported = c2.number_input("Reported ICP (mg/L)", min_value=0.0, value=5.0,
                               step=1.0, key="calc_unit_mgl")
    dil = c3.number_input("Dilution factor", min_value=0.0, value=10.0,
                          step=1.0, key="calc_unit_dil")
    mass = calculations.ATOMIC_MASSES[element]
    corrected = calculations.apply_dilution(reported, dil)
    mM = calculations.mgl_to_mM(corrected, element) if mass else float("nan")
    st.latex(r"\mathrm{corrected} = \mathrm{reported} \times \mathrm{dilution};\quad "
             r"\mathrm{mM} = \dfrac{\mathrm{corrected}}{\mathrm{atomic\ mass}}")
    st.success(
        f"corrected = {reported:g} × {dil:g} = **{corrected:g} mg/L** · "
        f"{element}_mM = {corrected:g} / {mass:g} = **{mM:.4g} mM**"
    )


def _render_ls_calculator() -> None:
    st.markdown("**Liquid/solid ratio** = solution volume (mL) / fly-ash mass (g).")
    c1, c2 = st.columns(2)
    mass_g = c1.number_input("fly_ash_mass_g", min_value=0.0, value=20.0,
                             step=1.0, key="calc_ls_mass")
    vol_mL = c2.number_input("solution_volume_mL", min_value=0.0, value=100.0,
                             step=1.0, key="calc_ls_vol")
    st.latex(r"\mathrm{L/S} = \dfrac{\mathrm{solution\_volume\_mL}}{\mathrm{fly\_ash\_mass\_g}}")
    if mass_g > 0:
        ls = calculations.liquid_solid_ratio(vol_mL, mass_g)
        st.success(f"L/S = {vol_mL:g} / {mass_g:g} = **{ls:.4g} mL/g**")
    else:
        st.warning("Enter a fly-ash mass greater than 0 to compute L/S.")


def _render_calc_verification_tab(dev_mode: bool, selected_run: str | None = None) -> None:
    st.subheader("Calculation verification / formula audit")
    st.info(
        "**PHREEQC is an equilibrium/speciation solver. This app does not rederive PHREEQC "
        "internally.** It parses PHREEQC output values and verifies that downstream "
        "calculations, mappings, unit conversions, and residuals are applied correctly."
    )

    st.markdown("### Formulas used")
    st.caption("Each formula, its inputs/outputs, units, and whether the app computes it or "
               "parses it from PHREEQC.")
    _render_formula_registry(dev_mode)

    st.divider()
    st.markdown("### Unit registry")
    _render_unit_registry()

    st.divider()
    st.markdown("### Unit-conversion re-derivation check")
    _render_conversion_verification(selected_run)

    st.divider()
    st.markdown("### Per-row residual audit")
    st.caption("Recomputes `measured − PHREEQC` from the stored comparison file and checks it "
               "against the stored residual.")
    _render_residual_audit()

    st.divider()
    st.markdown("### Calculators")
    cc1, cc2 = st.columns(2)
    with cc1:
        _render_unit_calculator()
    with cc2:
        _render_ls_calculator()

    if dev_mode:
        st.divider()
        st.markdown("### 🛠️ Developer explanations")
        st.markdown(
            "- **Why pH uses activity:** pH = −log₁₀(a_H⁺) is defined on hydrogen-ion "
            "*activity*. In high-ionic-strength alkali systems activity ≠ concentration, so "
            "an activity model (PHREEQC) is needed; a naive concentration-based pH would be wrong.\n"
            "- **Why the saturation index indicates precipitation/dissolution tendency:** "
            "SI = log₁₀(IAP/Ksp). IAP > Ksp (SI > 0) means the solution holds more dissolved "
            "ions than equilibrium allows, so the phase tends to precipitate; SI < 0 means it "
            "tends to dissolve. It is a *tendency*, not a rate.\n"
            "- **Why residuals alone do not prove model validity:** a small `measured − PHREEQC` "
            "residual can occur for the wrong reasons (compensating errors, a single tuned "
            "sample, or pH-only data). Agreement on one analyte/condition is not validation.\n"
            "- **Why ICP unit conversion must include the dilution factor:** ICP reports the "
            "*diluted* aliquot. Converting mg/L → mM without first multiplying by the dilution "
            "factor understates the true solution concentration by that factor."
        )


def _render_help_tab() -> None:
    st.subheader("Design note — the validation module")
    st.info(
        "This describes the **Validation module** within the broader simulate → validate → "
        "learn platform. The validation module is a generic workflow: **measured data → "
        "model prediction → mapping → residuals → validation status**. PHREEQC and the "
        "fly ash metadata (OA/PF/GS, CO₂ cover) are the current project implementation, "
        "not a hard limit of the system — the same workflow applies to other experiments "
        "and other models."
    )
    st.divider()

    st.subheader("Validation status — what is valid now vs not yet")
    _render_valid_now_section()
    st.caption("ℹ️ " + _PRELIMINARY_CAVEAT)
    st.caption("ℹ️ **Project-specific:** " + _OA_PF_GS_CAVEAT)
    with st.expander("Mapping status definitions"):
        _render_mapping_status_definitions()
    st.divider()

    st.subheader("How this app works")
    st.markdown(
        "1. **Start** — create or open a run in the sidebar (a 'save file' for one "
        "experiment set) and read the three-mode overview + status.\n"
        "2. **Simulate** — describe an experiment in plain language → a structured scenario → "
        "a simulation plan/matrix. _Planning layer — no deterministic model is run yet._\n"
        "3. **Import Data** — add measured rows (lab), upload/enter literature rows, or add "
        "synthetic demo rows, depending on run type, and review unit conversions.\n"
        "4. **Validate** — measured-data overview, data-quality validation, and "
        "calculation verification (the formula/unit/conversion audit).\n"
        "5. **Match** (lab runs) — link each `sample_id` to the model result row "
        "for the same chemistry.\n"
        "6. **Compare Results** — export to the pipeline, run Phase 1 → validate → compare → "
        "sustainability, then read the measured-vs-model comparison, residuals, and "
        "the validity line.\n"
        "7. **Export** — build a shareable results report, download the audit "
        "trail, and read this user guide."
    )

    st.subheader("Run types")
    st.markdown(
        "- **lab_experiment** — our measured release data (pH-only or full ICP). The only "
        "type compared against PHREEQC as real data.\n"
        "- **literature_benchmark** — values reported by other papers, kept separate and "
        "never run through the pipeline as lab data.\n"
        "- **synthetic_demo** — fake data for testing the code only; never scientific output.\n"
        "- **plastic_composite** — lab-like run for plastic-composite experiments."
    )

    st.subheader("Sample → PHREEQC mapping")
    st.markdown(
        "PHREEQC output `.pqo` filenames and measured `sample_id`s differ, so the comparison "
        "needs an explicit link: each measured `sample_id` → one PHREEQC `record_key` "
        "(`<file>|sim<N>|<state>|sol<N>`). Comparisons default to the post-equilibration "
        "(`batch`) state. **Without a mapping, residuals stay NaN** — a deliberate, visible "
        "state rather than a wrong join."
    )

    st.subheader("Residuals")
    st.markdown(
        "`residual_X = measured − PHREEQC` (in mM for Ca/Si/Al/Fe; pH for pH). Positive means "
        "the measured value is higher than the PHREEQC prediction. Fe is often unpredicted by "
        "the CEMDATA18 runs, so `residual_Fe` may be entirely NaN — that means **unavailable**, "
        "not 'PHREEQC predicts zero Fe'."
    )

    st.subheader("Limitations & safety")
    st.warning(
        "- **PHREEQC is equilibrium / speciation modelling.** Its outputs are "
        "thermodynamic predictions, not direct measurements, and assume the modelled "
        "system reached equilibrium.\n"
        "- **pH-only data only validates pH.** Ca/Si/Al/Fe/REE validation requires "
        "ICP-OES / ICP-MS data.\n"
        "- **Literature data must stay separate from lab data** — it is benchmark context, "
        "not our measurements.\n"
        "- **No ML is trained until real measured experimental release data exists.** "
        "The interface and Phase 2 comparison are scaffolding; predictions remain NaN "
        "until measured data and a sample→PHREEQC mapping are provided.\n"
        "- **Entering a value here does not make it scientifically valid.** Check units, "
        "detection limits, dilution factors, and experimental metadata before trusting "
        "any comparison or residual. Garbage in, garbage out."
    )

    st.divider()
    with st.expander("Data safety — what is kept out of version control"):
        st.markdown(
            "- Real measured-release CSVs, uploaded Excel workbooks, generated outputs, "
            "processed CSVs, figures, `run_config.yaml`, condition/replicate mapping CSVs "
            "and per-run experiment files are **git-ignored by default** and are not "
            "committed unless explicitly force-added.\n"
            "- Each run's data lives in its own `experiments/<run>/` folder; only the "
            "folder's `README.md` is tracked.\n"
            "- Synthetic/demo rows are force-tagged `source_type=synthetic_demo` and are "
            "never treated as scientific output."
        )

    with st.expander("Future direction — an ML *correction* layer (not yet started)"):
        st.markdown(
            "The long-term aim is an ML layer that learns **where the model disagrees with "
            "experiment** — a correction on top of the chemistry, **not** a blind "
            "replacement for it. It stays dormant until real measured release data and "
            "exact mappings exist; today the app is a transparent, rule-based validation "
            "workflow with no learned weights."
        )


def _render_surrogate_expander(selected_run: str | None) -> None:
    """Experimental surrogate UI (Audit/Help only). Suggestion/what-if, never a result.

    Loads the selected run's trained surrogate models and shows a prediction ±
    95% interval beside the nearest real PHREEQC run, always labelled as an
    approximation. Surrogate values never enter comparison CSVs, residuals, or mapping.
    """
    with app_ui.advanced_expander("Surrogate (experimental) — fast approximation of PHREEQC"):
        st.caption(
            "**Experimental — not for scientific claims.** Surrogate approximation of PHREEQC "
            "— not a measurement, not a PHREEQC run: a trained statistical model approximating "
            "PHREEQC outputs for fast what-ifs. Surrogate values never enter comparison CSVs, "
            "residuals, mapping status, or validity status."
        )
        if not selected_run:
            st.info("Select a run to load its surrogate. Build a dataset with "
                    "`scripts/10_sample_design.py`, then train + save models "
                    "(`flyash_phreeqc_ml.ml.surrogate`).")
            return
        try:
            from flyash_phreeqc_ml.ml import surrogate as _sur  # lazy: optional sklearn dep
        except Exception as exc:  # pragma: no cover - optional dependency
            st.info(f"scikit-learn not available ({exc}). Install requirements to use the surrogate.")
            return
        try:
            sdir = run_manager.surrogate_dir(selected_run)
        except run_manager.RunManagerError:
            st.info("This run type has no surrogate.")
            return
        models = _sur.load_surrogate(sdir)
        if not models:
            st.info(f"No trained surrogate models in `{_rel(sdir)}`. "
                    "Run scripts/10 to build a dataset, then train + save models there.")
            return

        space = config.SURROGATE_INPUT_SPACE
        (lo_n, hi_n), (lo_l, hi_l), (lo_t, hi_t) = (
            space["NaOH_M"], space["liquid_solid_ratio"], space["temperature_C"])
        c1, c2, c3, c4 = st.columns(4)
        naoh = c1.number_input("NaOH_M", min_value=float(lo_n), max_value=float(hi_n),
                               value=float((lo_n + hi_n) / 2), key=f"sur_naoh_{selected_run}")
        ls = c2.number_input("L/S ratio", min_value=float(lo_l), max_value=float(hi_l),
                             value=float((lo_l + hi_l) / 2), key=f"sur_ls_{selected_run}")
        temp = c3.number_input("temperature_C", min_value=float(lo_t), max_value=float(hi_t),
                               value=float((lo_t + hi_t) / 2), key=f"sur_t_{selected_run}")
        co2 = c4.selectbox("co2_scenario", list(space["co2_scenario"]),
                           key=f"sur_co2_{selected_run}")
        X = pd.DataFrame([{"NaOH_M": naoh, "liquid_solid_ratio": ls,
                           "temperature_C": temp, "co2_scenario": co2}])

        rows = []
        for output, model in models.items():
            p = _sur.predict(model, X).iloc[0]
            rows.append({"output": output, "surrogate_mean": round(float(p["mean"]), 4),
                         "lower95": round(float(p["lower"]), 4),
                         "upper95": round(float(p["upper"]), 4), "domain": p["domain"]})
        pred_df = pd.DataFrame(rows)
        st.markdown("**Surrogate prediction ± 95% interval**")
        st.dataframe(pred_df, use_container_width=True, hide_index=True,
                     height=min(60 + 35 * len(pred_df), 360))
        if (pred_df["domain"] == "extrapolation").any():
            app_ui.render_warning_panel(
                "Extrapolation", "Some inputs fall outside the trained validity domain — "
                "these predictions are extrapolation and must not be trusted.", level="error")

        nearest = _nearest_manifest_row(_manifest_if_available(), naoh, ls)
        if nearest is not None:
            st.markdown("**Nearest real PHREEQC run** (context only — not a comparison):")
            st.dataframe(pd.DataFrame([nearest]), use_container_width=True, hide_index=True,
                         height=80)
        st.caption("Surrogate approximation of PHREEQC — not a measurement, not a PHREEQC run.")


def _render_audit_trail(selected_run: str | None) -> None:
    """"Audit trail" — the run's append-only event log, filterable + downloadable.

    The JSONL file is itself the export; the table just makes it readable (newest
    first, filtered by event_type). The log records actions/ids/counts/hashes only —
    never measured values.
    """
    app_ui.section_header("Audit trail",
                          "append-only log of how this run's comparison was produced")
    if not selected_run:
        st.info("Select a run to see its audit trail.")
        return
    log = audit.read_audit(selected_run)
    if log.empty:
        st.caption("No audit events recorded for this run yet. Import data, accept "
                   "mappings, and run the workflow to populate the trail.")
        return

    present = list(dict.fromkeys(log["event_type"].tolist()))
    chosen = st.multiselect("Filter by event type", present, default=present,
                            key=f"audit_filter_{selected_run}")
    view = log[log["event_type"].isin(chosen)] if chosen else log
    # Newest first, with the payload rendered as compact JSON text.
    view = view.iloc[::-1].reset_index(drop=True)
    display = view.copy()
    display["payload"] = display["payload"].apply(
        lambda p: json.dumps(p, ensure_ascii=False, default=str))
    st.dataframe(display, use_container_width=True, height=320, hide_index=True)
    st.caption(f"{len(log)} event(s) total · showing {len(view)} after filter. "
               "The log is append-only — events are never edited or deleted.")

    raw = audit.audit_log_path(selected_run)
    if raw.exists():
        st.download_button(
            "⬇️ Download audit_log.jsonl",
            data=raw.read_bytes(),
            file_name=f"{selected_run}_audit_log.jsonl",
            mime="application/x-ndjson",
            key=f"audit_dl_{selected_run}",
        )


def _render_assistant_answer_trace(trace: list) -> None:
    """The collapsed 'data used' panel: every tool call + its returned summary."""
    if not trace:
        return
    with st.expander(f"Data used — {len(trace)} tool call(s)", expanded=False):
        st.caption("Every number in the answer above comes from these read-only tool "
                   "results, so the answer is auditable.")
        for i, step in enumerate(trace, start=1):
            st.markdown(f"**{i}. `{step.get('tool', '')}`**"
                        + (f"  ·  input `{step.get('input')}`" if step.get("input") else ""))
            st.caption(str(step.get("summary", "")))


def _render_assistant(selected_run: str | None) -> None:
    """"Ask the assistant" — grounded Q&A about the selected run (interpretation layer).

    Scoped to the selected run, answers ONLY via the read-only tools in
    :mod:`ai.assistant`, shows a collapsed "data used" trace under each answer, and is
    gated by the same per-session data-leaves-machine consent as the import-assist.
    Conversation lives in ``st.session_state`` only — never written to run files.
    """
    app_ui.section_header("Ask the assistant (experimental)",
                          "grounded answers about this run — read-only, cites its sources")
    if not selected_run:
        st.info("Select a run in the sidebar to ask about it.")
        return
    if not ai_assistant.is_enabled():
        st.caption(
            "Disabled: set `ANTHROPIC_API_KEY` and install the `anthropic` SDK to enable the "
            "assistant. It answers only from this run's own tool results — it never invents "
            "numbers and never changes anything.")
        return

    st.caption(ai_assistant.ASSISTANT_DATA_NOTICE)
    consent = st.checkbox(ai_assistant.ASSISTANT_CONSENT_LABEL, key="assistant_consent")

    msgs_key = f"assistant_msgs_{selected_run}"
    msgs = st.session_state.setdefault(msgs_key, [])  # session-only; never persisted

    for m in msgs:
        with st.chat_message(m["role"]):
            st.markdown(m["content"]) if m["content"] else st.caption("(no answer)")
            if m["role"] == "assistant":
                if m.get("error"):
                    st.error(m["error"])
                _render_assistant_answer_trace(m.get("trace") or [])

    with st.form(key=f"assistant_form_{selected_run}", clear_on_submit=True):
        question = st.text_area(
            "Ask about this run",
            placeholder="e.g. Is this comparison validated? What should I test next?",
            height=80, disabled=not consent)
        submitted = st.form_submit_button("Ask", disabled=not consent)

    if submitted and question.strip():
        history = [{"role": m["role"], "content": m["content"]}
                   for m in msgs if m.get("content")]
        with st.spinner("Thinking (calling read-only tools)…"):
            ans = ai_assistant.answer(selected_run, question.strip(), history=history)
        msgs.append({"role": "user", "content": question.strip()})
        msgs.append({"role": "assistant", "content": ans.text,
                     "trace": ans.trace, "error": None if ans.ok else ans.error})
        st.rerun()

    if msgs:
        if st.button("Clear conversation", key=f"assistant_clear_{selected_run}"):
            st.session_state[msgs_key] = []
            st.rerun()


def _render_start_tab(selected_run: str | None) -> None:
    """Start tab: the overview/status + a pointer to the in-app help."""
    _render_overview(selected_run)
    st.divider()
    st.caption("📖 New here? Try the **Simulate** tab to describe an experiment, or see the "
               "**Export** tab → *Help & user guide* for getting-started, input formats, the "
               "mapping guide, how to read results, and data safety.")


def _mass_balance_bar(record: dict):
    """Stacked bar for one element/sample: liquid / solid / unaccounted gap."""
    import matplotlib.pyplot as plt  # lazy: only when a figure is drawn

    fig, ax = plt.subplots(figsize=(3.6, 3.4))
    liquid = max(record["n_liquid"], 0.0)
    solid = max(record["n_solid"], 0.0)
    gap = record["gap"]
    ax.bar(0, liquid, color="#4878CF", label="liquid")
    ax.bar(0, solid, bottom=liquid, color="#6ACC64", label="solid residue")
    # The gap may be negative (over-recovery) — draw it from the top of liquid+solid.
    ax.bar(0, gap, bottom=liquid + solid, color="#C0C0C0", hatch="//",
           label="unaccounted (not yet attributed)")
    ax.axhline(record["n_in"], color="black", lw=1.2, ls="--", label="charged (n_in)")
    ax.set_xticks([])
    ax.set_ylabel("mmol")
    ax.set_title(f"{record['element']} · {record.get('sample_id', '')}", fontsize=9)
    ax.legend(fontsize=7, loc="upper right")
    fig.tight_layout()
    return fig


_ATTR_STATUS_STYLE = {
    attribution.STATUS_CLOSED: "exact",
    attribution.STATUS_MODEL_EXPLAINED: "scenario-level",
    attribution.STATUS_PARTIAL: "preliminary",
    attribution.STATUS_UNEXPLAINED: "unsafe",
}


def _attribution_three_way_figure(result: dict):
    """Three never-merged provenance bands: measured | model attribution | unexplained."""
    import matplotlib.pyplot as plt  # lazy: only when a figure is drawn

    fig, ax = plt.subplots(figsize=(4.6, 3.6))
    m = result["measured"]
    liquid = max(m["n_liquid"] or 0.0, 0.0)
    solid = max(m["n_solid"] or 0.0, 0.0)
    gap = result["gap"] or 0.0

    # Band 1 — MEASURED (liquid + solid + gap). Never mixed with modeled colours.
    ax.bar(0, liquid, color="#4878CF", label="measured liquid")
    ax.bar(0, solid, bottom=liquid, color="#6ACC64", label="measured solid")
    ax.bar(0, gap, bottom=liquid + solid, color="#B0B0B0", hatch="//",
           label="closure gap (measured)")

    # Band 2 — MODEL ATTRIBUTION by phase + the unexplained residual (modeled split of
    # the measured gap). Distinct hatch/colours so it reads as a different provenance.
    by_phase = result.get("by_phase") or {}
    bottom = 0.0
    cmap = plt.get_cmap("tab10")
    for i, (ph, mol) in enumerate(sorted(by_phase.items())):
        seg = min(mol, max(gap, 0.0) - bottom) if gap > 0 else 0.0
        if seg > 0:
            ax.bar(1, seg, bottom=bottom, color=cmap(i % 10), hatch="..",
                   label=f"model: {ph}")
            bottom += seg
    unexplained = result.get("gap_unexplained")
    if unexplained and unexplained > 0:
        ax.bar(1, unexplained, bottom=bottom, color="#D0402B", hatch="xx",
               label="unexplained")

    ax.set_xticks([0, 1])
    ax.set_xticklabels(["measured", "model"], fontsize=8)
    ax.set_ylabel("mmol")
    ax.set_title(f"{result['element']} — gap attribution", fontsize=9)
    ax.legend(fontsize=6, loc="upper right")
    fig.tight_layout()
    return fig


def _render_gap_attribution(selected_run: str, profile, data: pd.DataFrame,
                            element: str) -> None:
    """Explain the closure gap with PHREEQC — preview-before-run, degrade if unavailable.

    Modeled attribution **never** overwrites the measured gap; all text says "model
    attributes" / "predicted to precipitate".
    """
    with st.expander("Explain the gap with PHREEQC (attribution)"):
        st.caption("Modeled attribution: **predicted to precipitate** — never 'the element "
                   "was X'. The measured gap (above) is immutable; the model only *splits* "
                   "it into attributed-to-phase vs still-unexplained.")
        configured = phreeqc_runner.is_configured()
        rows = [r.to_dict() for _, r in data.iterrows()]
        # The first sample whose closure for this element is complete.
        target = next((r for r in rows
                       if mass_balance.closure(r, element, profile=profile)["status"]
                       == mass_balance.STATUS_COMPLETE), None)
        if target is None:
            st.info(f"No complete {element} closure to attribute yet.")
            return

        if not configured:
            res = attribution.attribution_unavailable(target, element, profile=profile)
            st.warning("⚠️ " + res["note"])
            st.pyplot(_attribution_three_way_figure(res))
            st.caption(attribution.attribution_caption(res))
            return

        # Configured: preview the attribution .pqi, then run + attribute on demand.
        inputs = attribution.build_attribution_inputs(target, profile)
        if inputs:
            with st.expander("Preview the attribution .pqi (before running)"):
                st.code(inputs[0].pqi_text, language="text")
        key = f"attr_result_{selected_run}_{element}"
        if st.button(f"Run PHREEQC & attribute {element} gap",
                     key=f"attr_run_{selected_run}_{element}"):
            try:
                sel = _run_attribution_and_parse(selected_run, inputs)
                st.session_state[key] = attribution.attribute_gap(
                    target, element, sel, profile=profile)
            except Exception as exc:  # never crash the tab on a model failure
                st.session_state[key] = attribution.attribution_unavailable(
                    target, element, profile=profile)
                st.error(f"Attribution run failed: {exc}")
        res = st.session_state.get(key)
        if res is None:
            st.info("Build is ready — click **Run** to attribute the gap.")
            return
        status = res["status"]
        app_ui.render_status_badge(f"attribution: {status}",
                                   _ATTR_STATUS_STYLE.get(status, "preliminary"))
        st.pyplot(_attribution_three_way_figure(res))
        st.caption(attribution.attribution_caption(res))


def _run_attribution_and_parse(run_name: str, inputs):
    """Run the first attribution input and parse its SELECTED_OUTPUT (best-effort)."""
    from flyash_phreeqc_ml.parsers.selected_output_parser import parse_selected_output
    workdir = run_manager.generated_simulations_dir(run_name)
    gi = inputs[0]
    pqo = phreeqc_runner.run(gi.pqi_text, workdir, basename=gi.basename)
    # PHREEQC writes USER_PUNCH to a sibling selected-output file.
    for cand in (pqo.with_suffix(".sel"), workdir / "selected.out",
                 pqo.parent / f"{gi.basename}.sel"):
        if cand.exists():
            return parse_selected_output(cand)
    raise phreeqc_runner.PhreeqcRunError("no SELECTED_OUTPUT file produced by the run")


def _literature_experiment_conditions(selected_run: str) -> dict:
    """A compact, representative condition dict for the conditions-match assessment."""
    try:
        data = run_manager.read_data_file(selected_run)
    except Exception:
        return {}
    if data is None or data.empty:
        return {}
    row = data.iloc[0].to_dict()
    out = {}
    for k in ("leachant", "NaOH_M", "acid_M", "temperature_C", "final_pH", "fly_ash_type",
              "material_id"):
        v = row.get(k)
        if v not in (None, "") and not (isinstance(v, float) and v != v):
            out[k] = v
    return out


_LIT_KINDS = {
    "Solubility constant (log Ksp)": "solubility_constant",
    "Candidate precipitate phases (+ solubility)": "candidate_phase",
    "Typical starting element assay (stand-in)": "starting_assay",
    "Partition / distribution (Kd)": "partition",
}


def _lit_source_markdown(rec: dict) -> str:
    """A clickable source label: '[Title (Year)](https://doi.org/…)' or the URL."""
    cite = rec.get("citation") or {}
    link = ai_literature.resolvable_link(cite)
    label = cite.get("title") or "source"
    yr = cite.get("year")
    text = f"{label}{f' ({yr})' if yr else ''}"
    return f"[{text}]({link})" if link else text


def _render_literature_proposer(selected_run: str, profile) -> None:
    """The query form: consent-gated, kind + material/element → propose → quarantine-save."""
    if not ai_literature.is_enabled():
        st.caption(
            "AI literature retrieval is disabled. Set `ANTHROPIC_API_KEY` and "
            "`pip install anthropic` to enable it. Confirming/reviewing any values already "
            "saved below still works without it.")
        return
    st.caption(ai_literature.LITERATURE_DATA_NOTICE)
    if not st.checkbox(ai_literature.LITERATURE_CONSENT_LABEL, key=f"lit_consent_{selected_run}"):
        st.info("Tick the box above to allow a sourced web search for these optional values.")
        return

    c1, c2, c3 = st.columns([2, 2, 1])
    kind_label = c1.selectbox("What to look up", list(_LIT_KINDS), key=f"lit_kind_{selected_run}")
    kind = _LIT_KINDS[kind_label]
    material = c2.text_input("Material", value="Class C fly ash", key=f"lit_mat_{selected_run}")
    extra = c3.text_input("Element / phase", value="", key=f"lit_extra_{selected_run}")
    if st.button("🔎 Search literature (sourced)", key=f"lit_go_{selected_run}"):
        conds = _literature_experiment_conditions(selected_run)
        with st.spinner("Searching the literature for sourced values…"):
            if kind == "solubility_constant":
                cands = ai_literature.propose_solubility_constants(
                    material, extra or None, experiment_conditions=conds)
            elif kind == "candidate_phase":
                cands = ai_literature.propose_candidate_phases(
                    material, experiment_conditions=conds)
            elif kind == "starting_assay":
                cands = ai_literature.propose_starting_assay(
                    material, extra or "Ca", experiment_conditions=conds)
            else:
                cands = ai_literature.propose_partition_behavior(
                    material, extra or "Ca", experiment_conditions=conds)
        if not cands:
            st.warning("No reliably-sourced value found (every result must carry a DOI/URL "
                       "and a supporting quote). Nothing was saved.")
        else:
            added = ai_literature.save_candidates(selected_run, cands)
            st.success(f"Found {len(cands)} sourced candidate(s); {len(added)} new added to "
                       "the quarantine store below. Review and confirm before any use.")
            st.rerun()


def _render_literature_review(selected_run: str, profile) -> None:
    """Review table for quarantined literature values — source-prominent, confirm-gated.

    Every row shows the **clickable DOI/URL**, the supporting quote, and the
    conditions-match warning. Confirmation moves a value to ``literature-confirmed`` (and
    logs an audit event); a conditions-mismatched value needs a **second acknowledgement**.
    """
    with st.expander("📚 Literature values (AI-assisted, sourced) — quarantined until confirmed"):
        st.caption(
            "Proposed values are **source-bound** (DOI preferred, URL fallback) and "
            "**quarantined**: nothing here enters a calculation until you confirm it, and "
            "uncited results are dropped before they are ever shown.")
        _render_literature_proposer(selected_run, profile)

        store = ai_literature.read_store(selected_run)
        if not store:
            st.info("No literature values stored for this run yet.")
            return

        st.markdown("**Stored values** (newest last)")
        for rec in store:
            cid = str(rec.get("candidate_id"))
            confirmed = bool(rec.get("confirmed"))
            mismatch = ai_literature.has_conditions_mismatch(rec.get("conditions_match"))
            tag = "✅ confirmed" if confirmed else "🔒 quarantined"
            st.markdown(
                f"**{rec.get('quantity', '')}** = `{rec.get('value')} {rec.get('unit', '')}` "
                f"· {rec.get('material', '')}  —  {tag}")
            st.markdown(f"Source: {_lit_source_markdown(rec)}")
            cite = rec.get("citation") or {}
            if cite.get("supporting_quote"):
                st.caption(f"“{cite['supporting_quote']}”")
            cm = rec.get("conditions_match") or {}
            if mismatch:
                flags = ", ".join(cm.get("mismatch_flags") or []) or "different conditions"
                st.warning(f"⚠️ Conditions mismatch: {flags}. "
                           f"{cm.get('assessment', '')}".strip())
            elif cm.get("assessment"):
                st.caption(f"Conditions: {cm['assessment']}")

            if not confirmed:
                ack = True
                if mismatch:
                    ack = st.checkbox(ai_literature.MISMATCH_ACK_LABEL,
                                      key=f"lit_ack_{selected_run}_{cid}")
                if st.button("Confirm this value", key=f"lit_confirm_{selected_run}_{cid}",
                             disabled=mismatch and not ack):
                    try:
                        ai_literature.confirm_value(selected_run, cid, acknowledge_mismatch=ack)
                        st.success("Confirmed and logged to the audit trail.")
                        st.rerun()
                    except ai_literature.ConditionsMismatchError:
                        st.error("Tick the conditions-mismatch acknowledgement to confirm.")
            st.divider()


def _render_mass_balance(selected_run: str) -> None:
    """Batch-reaction element closure (deterministic arithmetic; no model/AI/ML).

    Renders only when the active dataset profile opts in (declares
    ``mass_balance_elements``). The gap is element **not yet attributed** to liquid or
    solid — a measured fact with no mechanism attached.
    """
    profile = profiles.FLY_ASH_PROFILE
    with st.expander("Batch-reaction mass balance — element closure (arithmetic)",
                     expanded=False):
        st.caption(
            "Deterministic closure: **gap = moles_in − moles_liquid − moles_solid** "
            "(mmol). No model, AI, or ML — the gap is element *not yet attributed* to "
            "liquid or solid, a measured fact with no mechanism attached."
        )
        if not mass_balance.is_enabled(profile):
            st.info(
                "This run's dataset profile does not declare batch-reaction mass-balance "
                "columns, so no closure is computed. Mass balance is **opt-in per profile** "
                "(set `mass_balance_elements` + the assay units). The schema reserves the "
                "optional columns `material_mass_g`, `liquid_volume_mL`, `solid_mass_g`, and "
                "per element `{el}_starting_content` / `{el}_solid_residue`."
            )
            return

        data = run_manager.read_data_file(selected_run)
        # Quarantine gate: fill ONLY confirmed literature starting-assay stand-ins into
        # blank cells (never overwriting a measured value). Unconfirmed values are ignored.
        lit_records = ai_literature.confirmed_records(selected_run)
        lit_badges: dict = {}
        if lit_records and not data.empty:
            rows = []
            for _, r in data.iterrows():
                nr, b = ai_literature.row_with_confirmed_assays(r.to_dict(), lit_records, profile)
                rows.append(nr)
                lit_badges.update(b)
            data = pd.DataFrame(rows)
        records = mass_balance.closure_records(data, profile)
        if not records:
            st.info("No batch-reaction rows to close yet — enter the material mass, liquid "
                    "volume, starting assay, and solid residue for this run's samples.")
            return

        elements = list(getattr(profile, "mass_balance_elements", ()))
        element = st.selectbox("Element", elements, key=f"mb_el_{selected_run}")
        el_records = [r for r in records if r["element"] == element]

        # Badge any literature stand-in used for THIS element's starting assay (with source).
        overrides = ai_literature.confirmed_assay_overrides(lit_records, profile)
        ov = overrides.get(f"{element}_starting_content")
        if ov:
            _val, rec = ov
            cite = rec.get("citation") or {}
            link = ai_literature.resolvable_link(cite)
            label = cite.get("title") or "source"
            yr = cite.get("year")
            src_md = f"[{label}{f' ({yr})' if yr else ''}]({link})" if link else label
            st.warning(
                f"⚠️ {element} starting assay is a **literature stand-in** "
                f"(`{ai_literature.PROVENANCE_CONFIRMED}`), **not a measurement** — any "
                f"closure/recovery below is computed from it. Source: {src_md}")

        st.markdown("**Closure table** (mmol; provenance per cell below)")
        st.dataframe(mass_balance.closure_table(el_records), use_container_width=True,
                     height=200, hide_index=True)

        # Stacked bars for the complete closures (gap labelled "unaccounted").
        complete = [r for r in el_records if r["status"] == mass_balance.STATUS_COMPLETE]
        if complete:
            cols = st.columns(min(3, len(complete)))
            for i, rec in enumerate(complete[:3]):
                with cols[i]:
                    st.pyplot(_mass_balance_bar(rec))

        # Warnings (validation-surface style) — never silent fixes.
        all_issues = [iss for r in el_records for iss in mass_balance.closure_warnings(r)]
        if all_issues:
            st.markdown("**Sanity warnings**")
            for iss in all_issues:
                msg = f"`{iss['column']}` — {iss['message']}"
                (st.error if iss["severity"] == "error" else
                 st.warning if iss["severity"] == "warning" else st.info)(msg)

        # Provenance per cell — reuse the unit-conversion expander pattern.
        with st.expander("Provenance — formula + molar mass per term"):
            for rec in el_records:
                st.markdown(f"**{rec.get('sample_id', '')} · {rec['element']}** "
                            f"({rec['status']})")
                for term, label in (("n_in", "charged"), ("n_liquid", "liquid"),
                                    ("n_solid", "solid residue")):
                    p = rec["provenance"][term]
                    val = "—" if p["value"] is None else f"{p['value']:.4g} mmol"
                    mm = "" if p["molar_mass"] is None else f" · M = {p['molar_mass']:g} g/mol"
                    cid = p["conversion_id"] or "—"
                    st.caption(f"{label}: {val} · `{cid}`{mm} · {p['formula']}")
                if rec["assumptions"]:
                    for a in rec["assumptions"]:
                        st.caption(f"⚠️ assumption: {a}")

        # Explain the measured gap with PHREEQC (modeled; never overwrites the measured gap).
        _render_gap_attribution(selected_run, profile, data, element)


def _render_validate_tab(selected_run: str | None, dev_mode: bool) -> None:
    """Validate tab: measured-data overview, data validation, and calculation audit."""
    app_ui.render_page_header(
        "Validate — check the data and the calculations",
        "Review the measured-data overview, the data-quality validation, and verify every "
        f"downstream calculation before trusting a model comparison (currently {MODEL_NAME}).",
        eyebrow="Validation module · Validate",
    )
    _render_next_step(selected_run)
    if not selected_run:
        st.info("Select or create a run in the sidebar. Lab runs show a measured-data "
                "overview and data validation here; calculation verification applies to any run.")
        return
    rt = run_manager.load_run_config(selected_run).get("run_type")
    lab_like = rt in run_manager.LAB_LIKE_RUN_TYPES

    if lab_like:
        app_ui.section_header("Measured-data overview",
                              "measured data only — no model comparison")
        _render_measured_overview(selected_run)
        st.divider()
        _render_basic_validation_summary(selected_run)
        st.divider()
        _render_mass_balance(selected_run)
        st.divider()
        _render_literature_review(selected_run, profiles.FLY_ASH_PROFILE)
        st.divider()
    else:
        st.info("This run type has no measured-data overview or lab validation. The "
                "calculation verification below still applies.")

    _render_calc_verification_tab(dev_mode, selected_run)

    st.divider()
    st.subheader("Model raw outputs & model-only plots")
    st.caption(f"These tables and figures are **{MODEL_NAME} model predictions**, not "
               "measured experimental data.")
    with st.expander(f"Processed {MODEL_NAME} tables", expanded=False):
        _render_processed_viewer()
    with st.expander(f"{MODEL_NAME} model-output figures", expanded=False):
        _render_phreeqc_only_figures()

    st.divider()
    app_ui.section_header("Validation & sustainability tables", "from the QA/QC scripts")
    any_table = False
    for label, name in [
        ("Validation report", config.EXPERIMENTAL_VALIDATION_REPORT_CSV),
        ("Sustainability score", config.SUSTAINABILITY_SCORE_CSV),
    ]:
        path = config.TABLES_DIR / name
        if path.exists():
            any_table = True
            with st.expander(f"{label} — {name}"):
                st.dataframe(_read_csv(str(path), path.stat().st_mtime),
                             use_container_width=True, height=300)
    if not any_table:
        st.caption("No validation/sustainability tables yet — run the workflow in the "
                   "**Compare Results** tab to generate them.")


def _render_user_guide() -> None:
    """Render the markdown user guide (docs/user_guide/) so docs live in one place."""
    guide_dir = _PROJECT_ROOT / "docs" / "user_guide"
    order = ["getting_started.md", "input_formats.md", "mapping_guide.md",
             "interpreting_results.md", "data_safety.md", "faq.md"]
    files = [f for f in order if (guide_dir / f).exists()]
    files += sorted(p.name for p in guide_dir.glob("*.md") if p.name not in order) \
        if guide_dir.exists() else []
    if not files:
        st.info("User guide not found (expected markdown in `docs/user_guide/`).")
        return
    titles = {f: f.replace("_", " ").replace(".md", "").title() for f in files}
    choice = st.selectbox("Guide page", files, format_func=lambda f: titles[f],
                          key="user_guide_choice")
    st.markdown((guide_dir / choice).read_text(encoding="utf-8"))


def _render_export_tab(selected_run: str | None) -> None:
    """Export tab: validation report + downloads + audit trail + Help / user guide."""
    app_ui.render_page_header(
        "Export — share results and read the docs",
        "Build a self-contained results report (the model predictions used, replicate "
        "uncertainty, and validation against measured data), download the audit trail, and "
        "read the user guide — everything a reviewer needs without the app.",
        eyebrow="Export",
    )
    _render_next_step(selected_run)

    _render_export_report(selected_run)
    st.divider()
    _render_audit_trail(selected_run)

    st.divider()
    app_ui.section_header("Help & user guide", "for someone using the app for the first time")
    with st.expander("📖 User guide", expanded=False):
        _render_user_guide()
    with st.expander("Reference — design note, validation status & limitations", expanded=False):
        _render_help_tab()


# --------------------------------------------------------------------------- #
# Page — wide layout, run-management sidebar, and a tabbed dashboard
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="Geochemical Simulation & Validation Platform",
                   layout="wide", page_icon="🧪")
app_ui.inject_global_css()
app_ui.render_hero(
    "AI-Assisted Geochemical Simulation & Validation Platform",
    "Describe an experiment and the variables you care about; the platform extracts a "
    "structured scenario, clarifies assumptions, and plans a geochemical simulation — "
    "then, where you have measured data, validates and corrects the predictions against "
    "it. Three modes: Simulate, Validate, Learn & Improve.",
    eyebrow="Describe experiment → AI scenario → simulation plan → (future) predicted variables + uncertainty → validate against measured data",
    chips=[
        ("Simulate · Validate · Learn", "info"),
        ("Transparent, auditable methods", "neutral"),
        (f"Reference module: Class C fly ash + {MODEL_NAME}", "neutral"),
    ],
)

# Sidebar "save files" — selecting a run here drives every tab below.
SELECTED_RUN = _render_run_sidebar()

st.sidebar.divider()
DEV_MODE = st.sidebar.checkbox(
    "🛠️ Developer explanation mode", value=False, key="dev_mode",
    help="Show deeper chemistry/statistics explanations, mainly in the "
         "Validate tab.",
)

_render_ai_settings_panel()

tab_start, tab_simulate, tab_import, tab_validate, tab_match, tab_compare, tab_export = st.tabs([
    "Start", "Simulate", "Import Data", "Validate", "Match", "Compare Results", "Export",
])

with tab_start:
    _render_start_tab(SELECTED_RUN)
with tab_simulate:
    _render_simulate_tab(SELECTED_RUN, DEV_MODE)
with tab_import:
    _render_import_tab(SELECTED_RUN)
with tab_validate:
    _render_validate_tab(SELECTED_RUN, DEV_MODE)
with tab_match:
    _render_match_tab(SELECTED_RUN)
with tab_compare:
    _render_compare_tab(SELECTED_RUN)
with tab_export:
    _render_export_tab(SELECTED_RUN)
