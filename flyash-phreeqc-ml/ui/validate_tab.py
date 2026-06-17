"""Validate tab — measured overview, basic validation, calc verification,
model-output viewer, validation & sustainability tables.

Extracted from app.py by the UI modularization refactor — see
docs/refactor_plan.md. Behavior is unchanged (verbatim move)."""
from __future__ import annotations

from pathlib import Path
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
import app_ui  # noqa: E402  (presentation-only UI helper layer)
from flyash_phreeqc_ml import audit  # noqa: E402  (append-only audit log)
from flyash_phreeqc_ml import calculations  # noqa: E402
from flyash_phreeqc_ml import config  # noqa: E402
from flyash_phreeqc_ml import phreeqc_runner  # noqa: E402  (on-demand PHREEQC, Prompt 11)
from flyash_phreeqc_ml import profiles  # noqa: E402
from flyash_phreeqc_ml import attribution  # noqa: E402  (PHREEQC gap attribution)
from flyash_phreeqc_ml import mass_balance  # noqa: E402  (deterministic element closure)
from flyash_phreeqc_ml import run_manager  # noqa: E402
from flyash_phreeqc_ml import units  # noqa: E402  (single conversion authority)
from flyash_phreeqc_ml.ai import literature as ai_literature  # noqa: E402  (sourced lit values)
from flyash_phreeqc_ml.experiments import validate_experimental_df  # noqa: E402
from flyash_phreeqc_ml.viz import measured_overview  # noqa: E402

from ui.common import _audit_once, _png_provenance_caption, _render_next_step
from ui.state import MODEL_NAME, _COMPARISON_FIGURES, _PROJECT_ROOT, _read_csv

# Processed CSVs surfaced first in the data viewer.
PREFERRED_PROCESSED = [
    config.MASTER_DATASET_CSV,
    config.PHREEQC_RESULTS_CSV,
    config.PHREEQC_SI_CSV,
    config.PHREEQC_ASSEMBLAGE_CSV,
]

def _figure_dirs() -> list[Path]:
    """Where plots may live. Pipeline writes to reports/figures; outputs/figures
    is checked too since the task referred to it."""
    return [config.FIGURES_DIR, _PROJECT_ROOT / "outputs" / "figures"]

_LIVE_MEASURED_NOTE = ("Live measured-data-only figure — drawn fresh from this run's measured "
                       "data each render; **not affected by the Simulate tab** (plan-only).")

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


# Tab entry point (app.py calls ui.validate_tab.render).
render = _render_validate_tab
