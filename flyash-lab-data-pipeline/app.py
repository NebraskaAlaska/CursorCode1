"""Streamlit app: Fly Ash Lab Data Pipeline & Auto-Report Generator.

Run with:  streamlit run app.py

The app is organised into eight tabs that walk through the workflow: upload data,
check quality, review mix designs, strength and leachate results, CO2/cost
estimates, reuse ranking, and an auto-generated HTML report. The sidebar exposes
all CO2/cost factors and scoring weights as *editable assumptions*.
"""

from __future__ import annotations

import datetime as _dt
import os

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from src import (
    calculations,
    config,
    data_loader,
    plotting,
    report_generator,
    scoring,
    validation,
)

st.set_page_config(page_title="Fly Ash Lab Data Pipeline", layout="wide")

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
PROCESSED_DIR = os.path.join(os.path.dirname(__file__), "data", "processed")


# ---------------------------------------------------------------------------
# Sidebar: editable assumptions
# ---------------------------------------------------------------------------
def sidebar_assumptions() -> tuple[dict, dict]:
    """Render sidebar controls and return (factors, scoring_presets) overrides."""
    st.sidebar.header("Editable assumptions")
    st.sidebar.caption("These are tunable assumptions, not fixed scientific facts.")

    with st.sidebar.expander("CO₂ / cost factors", expanded=False):
        factors = {
            "cement_co2_per_kg": st.number_input(
                "Cement CO₂ (kg/kg)", value=float(config.DEFAULT_FACTORS["cement_co2_per_kg"]),
                min_value=0.0, step=0.05, format="%.3f"),
            "fly_ash_co2_per_kg": st.number_input(
                "Fly ash CO₂ (kg/kg)", value=float(config.DEFAULT_FACTORS["fly_ash_co2_per_kg"]),
                min_value=0.0, step=0.01, format="%.3f"),
            "cement_cost_per_kg": st.number_input(
                "Cement cost (/kg)", value=float(config.DEFAULT_FACTORS["cement_cost_per_kg"]),
                min_value=0.0, step=0.01, format="%.3f"),
            "fly_ash_cost_per_kg": st.number_input(
                "Fly ash cost (/kg)", value=float(config.DEFAULT_FACTORS["fly_ash_cost_per_kg"]),
                min_value=0.0, step=0.01, format="%.3f"),
            "currency": config.DEFAULT_FACTORS["currency"],
        }

    presets = {app: dict(weights) for app, weights in config.SCORING_PRESETS.items()}
    with st.sidebar.expander("Scoring weights", expanded=False):
        app_key = st.selectbox(
            "Application", options=list(presets.keys()),
            format_func=lambda a: config.APPLICATION_LABELS.get(a, a))
        st.caption("Adjust the weight of each sub-score for the selected application.")
        for sub in config.SCORING_SUBSCORES:
            presets[app_key][sub] = st.slider(
                sub.replace("_", " "), min_value=0.0, max_value=1.0,
                value=float(presets[app_key][sub]), step=0.05, key=f"w_{app_key}_{sub}")
    return factors, presets


# ---------------------------------------------------------------------------
# Processing pipeline
# ---------------------------------------------------------------------------
def process(df_raw: pd.DataFrame, factors: dict) -> dict:
    """Run the full pipeline and return a dict of artefacts."""
    df = calculations.add_derived_columns(df_raw, factors)
    df = calculations.infer_data_status(df)
    stats = calculations.strength_statistics(df)
    issues = validation.validate(df, stats)
    return {"df": df, "stats": stats, "issues": issues}


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
def tab_upload(factors: dict):
    st.subheader("Upload Data")
    st.write("Upload a CSV or Excel file of lab results, or download a blank template.")

    c1, c2 = st.columns(2)
    c1.download_button("⬇ Download CSV template", data=data_loader.template_csv_bytes(),
                       file_name="flyash_template.csv", mime="text/csv")
    c2.download_button("⬇ Download Excel template", data=data_loader.template_excel_bytes(),
                       file_name="flyash_template.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    uploaded = st.file_uploader("Choose a CSV or Excel file", type=["csv", "xlsx", "xls"])
    if uploaded is not None:
        try:
            df_raw = data_loader.load_data(uploaded, filename=uploaded.name)
        except Exception as exc:  # surface load errors to the user
            st.error(f"Could not read the file: {exc}")
            return
        st.session_state["raw_df"] = df_raw
        st.session_state["source_file"] = uploaded.name
        artefacts = process(df_raw, factors)
        st.session_state["artefacts"] = artefacts
        st.success(f"Loaded {len(artefacts['df'])} specimen rows from {uploaded.name}.")
        st.dataframe(artefacts["df"].head(20), use_container_width=True)


def _require_data() -> dict | None:
    art = st.session_state.get("artefacts")
    if art is None:
        st.info("Upload a data file on the **Upload Data** tab to begin.")
        return None
    return art


def tab_quality():
    st.subheader("Data Quality Check")
    art = _require_data()
    if not art:
        return
    summary = validation.validation_summary(art["issues"])
    c1, c2, c3 = st.columns(3)
    c1.metric("Total issues", summary["total"])
    c2.metric("Errors", summary["errors"])
    c3.metric("Warnings", summary["warnings"])
    if summary["errors"]:
        st.error("Errors should be fixed before relying on rankings.")
    issues_df = validation.issues_dataframe(art["issues"])
    if issues_df.empty:
        st.success("No data-quality issues found.")
    else:
        st.dataframe(issues_df, use_container_width=True)


def tab_mix_summary():
    st.subheader("Mix Design Summary")
    art = _require_data()
    if not art:
        return
    df = art["df"]
    cols = ["mix_id", "total_binder_mass_g", "water_binder_ratio",
            "fly_ash_replacement_percent", "red_mud_percent",
            "estimated_co2_saving_kg", "estimated_cost_saving"]
    cols = [c for c in cols if c in df.columns]
    summary = df.groupby("mix_id", dropna=False)[cols[1:]].mean().reset_index()
    st.dataframe(summary.round(3), use_container_width=True)
    st.caption("Values averaged across each mix's specimens.")


def tab_strength():
    st.subheader("Strength Results")
    art = _require_data()
    if not art:
        return
    df = art["df"]
    st.markdown("**Strength statistics (per mix & age)**")
    st.dataframe(art["stats"].round(2), use_container_width=True)
    st.plotly_chart(plotting.strength_vs_age(df), use_container_width=True)
    st.plotly_chart(plotting.strength_vs_flyash(df), use_container_width=True)
    if "strength_source" in df.columns:
        calc = (df["strength_source"] == "calculated").sum()
        if calc:
            st.caption(f"{calc} strength value(s) were back-calculated from peak load and area.")


def tab_leachate():
    st.subheader("Leachate Results")
    art = _require_data()
    if not art:
        return
    df = art["df"]
    st.plotly_chart(plotting.ph_vs_age(df), use_container_width=True)
    st.plotly_chart(plotting.conductivity_vs_flyash(df), use_container_width=True)
    risk = df.apply(scoring.leaching_risk_score, axis=1)
    show = df[["mix_id", "specimen_id", "leachate_pH", "leachate_conductivity_uS_cm"]].copy()
    show["leaching_risk"] = risk.round(3)
    st.dataframe(show, use_container_width=True)


def tab_co2_cost(factors: dict):
    st.subheader("CO₂ / Cost Estimate")
    art = _require_data()
    if not art:
        return
    df = art["df"]
    total_co2 = df["estimated_co2_saving_kg"].sum()
    total_cost = df["estimated_cost_saving"].sum()
    c1, c2 = st.columns(2)
    c1.metric("Total estimated CO₂ saving (kg)", f"{total_co2:,.1f}")
    c2.metric(f"Total estimated cost saving ({factors['currency']})", f"{total_cost:,.2f}")
    st.plotly_chart(plotting.co2_vs_strength(df), use_container_width=True)
    st.plotly_chart(plotting.flow_vs_wb(df), use_container_width=True)
    st.caption("Estimates use the editable CO₂/cost assumptions in the sidebar.")


def tab_ranking(presets: dict):
    st.subheader("Reuse Ranking")
    art = _require_data()
    if not art:
        return
    df = art["df"]
    scored = scoring.reuse_scores(df, presets=presets)
    st.session_state["scored"] = scored
    table = scoring.ranking_table(scored)
    pretty = report_generator._format_ranking(table)
    st.dataframe(pretty, use_container_width=True)
    st.plotly_chart(plotting.reuse_score_by_mix(scored), use_container_width=True)
    st.caption("Scores (0–100) reflect the editable weighting assumptions in the sidebar.")


def tab_report(factors: dict, presets: dict):
    st.subheader("Auto Report")
    art = _require_data()
    if not art:
        return
    df = art["df"]

    st.markdown("**Export processed data**")
    c1, c2 = st.columns(2)
    cleaned_cols = [c for c in config.EXPECTED_COLUMNS if c in df.columns]
    c1.download_button("⬇ Cleaned CSV", data=data_loader.dataframe_to_csv_bytes(df[cleaned_cols]),
                       file_name="cleaned_data.csv", mime="text/csv")
    c2.download_button("⬇ Processed CSV (with derived metrics)",
                       data=data_loader.dataframe_to_csv_bytes(df),
                       file_name="processed_data.csv", mime="text/csv")

    st.markdown("**Generate HTML report**")
    if st.button("Build report"):
        scored = scoring.reuse_scores(df, presets=presets)
        figures = plotting.build_all_figures(df, scored)
        meta = {"source_file": st.session_state.get("source_file", "uploaded data"),
                "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
        html = report_generator.build_report(
            df=df, issues=art["issues"], stats=art["stats"], scored=scored,
            figures=figures, factors=factors, meta=meta)
        st.session_state["report_html"] = html
        # Also persist a copy to the reports/ directory.
        os.makedirs(REPORTS_DIR, exist_ok=True)
        out_path = os.path.join(
            REPORTS_DIR, f"report_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
        report_generator.save_report(html, out_path)
        st.success(f"Report generated and saved to {out_path}")

    if st.session_state.get("report_html"):
        st.download_button("⬇ Download HTML report", data=st.session_state["report_html"],
                           file_name="flyash_report.html", mime="text/html")
        with st.expander("Preview report", expanded=False):
            components.html(st.session_state["report_html"], height=600, scrolling=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    st.title("Fly Ash Lab Data Pipeline & Auto-Report Generator")
    st.caption("Class C fly ash reuse research — upload, validate, analyse, rank, and report.")
    factors, presets = sidebar_assumptions()

    # Re-run the pipeline with the current sidebar factors whenever data is loaded,
    # so CO2/cost edits propagate live without re-uploading.
    if st.session_state.get("raw_df") is not None:
        st.session_state["artefacts"] = process(st.session_state["raw_df"], factors)

    tabs = st.tabs([
        "Upload Data", "Data Quality Check", "Mix Design Summary", "Strength Results",
        "Leachate Results", "CO₂/Cost Estimate", "Reuse Ranking", "Auto Report",
    ])
    with tabs[0]:
        tab_upload(factors)
    with tabs[1]:
        tab_quality()
    with tabs[2]:
        tab_mix_summary()
    with tabs[3]:
        tab_strength()
    with tabs[4]:
        tab_leachate()
    with tabs[5]:
        tab_co2_cost(factors)
    with tabs[6]:
        tab_ranking(presets)
    with tabs[7]:
        tab_report(factors, presets)


if __name__ == "__main__":
    main()
