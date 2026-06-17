"""Compare Results tab — run workflow + comparison/residuals/bias/correction +
assistant + surrogate.

Extracted from app.py by the UI modularization refactor — see
docs/refactor_plan.md. Behavior is unchanged (verbatim move)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
import app_ui  # noqa: E402  (presentation-only UI helper layer)
from ui.formatters import has_numeric as _has_numeric, nearest_manifest_row as _nearest_manifest_row  # noqa: E402
from flyash_phreeqc_ml import audit  # noqa: E402  (append-only audit log)
from flyash_phreeqc_ml import config  # noqa: E402
from flyash_phreeqc_ml import profiles  # noqa: E402
from flyash_phreeqc_ml import replicates  # noqa: E402
from flyash_phreeqc_ml import run_manager  # noqa: E402
from flyash_phreeqc_ml import scenarios  # noqa: E402
from flyash_phreeqc_ml.ai import assistant as ai_assistant  # noqa: E402  (grounded Q&A)
from flyash_phreeqc_ml.compare import comparison_inclusion  # noqa: E402
from flyash_phreeqc_ml.compare import inclusion as compare_inclusion  # noqa: E402
from flyash_phreeqc_ml.ml import residual_stats  # noqa: E402  (descriptive bias stats)
from flyash_phreeqc_ml.viz import compare_plots  # noqa: E402

from ui.common import _png_provenance_caption, _render_next_step
from ui.state import MODEL_NAME, _COMPARISON_FIGURES, _FIGURE_CAPTIONS, _ICP_MEASURED_COLS, _PROJECT_ROOT, _manifest_if_available, _read_csv, _rel, _run_comparison_path, _scenario_manifest

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

def _looks_like_test(comp: pd.DataFrame) -> bool:
    if "sample_id" not in comp.columns:
        return False
    sids = comp["sample_id"].astype(str)
    return bool(sids.str.upper().str.contains("TEST").any())

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


# Tab entry point (app.py calls ui.compare_tab.render).
render = _render_compare_tab
