"""Results section — a clean read-out of the assistant's latest simulation prediction.

Separated from the setup controls (which live in Workspace). Shows the headline numbers
(estimated pH, key element totals, target-match status), the standing **"model prediction only
— not validated"** label, sweep plots when a sweep was run, and the raw table under an expander.
It reads the assistant's :class:`AgentState` from the session (the same state the Assistant and
Workspace build) — it computes nothing scientific itself and writes nothing.

Validation lives in **Data & Validation** (measured-vs-model), kept deliberately separate:
*simulation predicts; validation compares with reality.*
"""
from __future__ import annotations

import streamlit as st

import app_ui
from flyash_phreeqc_ml.agent import agent_state as _astate
from flyash_phreeqc_ml.simulation import batch_executor

NOT_VALIDATED = ("Model prediction only — these are estimates under your reviewed assumptions, "
                 "NOT validated against measured data. Validate in Data & Validation.")


def _get_state(run: str | None):
    s = st.session_state.get(f"asst_state__{run or '_none_'}")
    return s if isinstance(s, _astate.AgentState) else None


def _f(value, nd=2):
    try:
        return round(float(value), nd)
    except (TypeError, ValueError):
        return None


def _render_results(selected_run: str | None) -> None:
    app_ui.render_page_header(
        "Results",
        "The latest simulation prediction from the assistant — headline estimates, sweep plots, "
        "and the raw output. Predictions, not validation.",
        eyebrow="Model prediction · not validated")

    state = _get_state(selected_run)
    if state is None or not state.has_results:
        with st.container(border=True):
            st.markdown("**No simulation results yet.**")
            st.caption("Describe and run an experiment in the **Assistant** (or build + run one in "
                       "**Workspace**). Results appear here once a simulation has been executed.")
        return

    app_ui.render_warning_panel("Not validation", NOT_VALIDATED, level="warning")

    # ---- headline cards ------------------------------------------------- #
    parsed = state.parsed_result
    pH = _f(getattr(parsed, "pH", None)) if parsed is not None else None
    totals = {k: _f(v, 3) for k, v in getattr(parsed, "element_totals_mM", {}).items()} \
        if parsed is not None else {}
    tm = state.target_match_result
    tm_status = "—"
    if tm is not None and getattr(tm, "best", None):
        tm_status = f"{tm.best.get('scenario_id')} (score {tm.best.get('objective_score')})"

    cards = [
        {"label": "Estimated pH", "value": pH if pH is not None else "—", "status": "accent"},
        {"label": "Elements", "value": len(totals) or "—",
         "caption": ", ".join(list(totals)[:5]) or None},
        {"label": "Target match", "value": tm_status},
        {"label": "Validation status", "value": "Not validated", "status": "warning",
         "caption": "needs measured data"},
    ]
    app_ui.render_metric_cards(cards)

    if totals:
        app_ui.section_header("Key element totals (mM)")
        app_ui.render_metric_cards(
            [{"label": el, "value": v} for el, v in list(totals.items())[:6]])

    # ---- sweep plots (only when a sweep was run) ------------------------ #
    table = state.result_table
    if table is not None and hasattr(table, "empty") and not table.empty:
        axis_col, axis_label = batch_executor.detect_sweep_axis(state.sweep_matrix)
        if axis_col is not None:
            app_ui.section_header(f"pH vs {axis_label}")
            frame = batch_executor.sweep_plot_frame(table, axis_col, "pH")
            if not frame.empty:
                st.line_chart(frame.set_index("x")["y"], height=240)
            el_cols = [c for c in table.columns if c.endswith("_mM")]
            if el_cols:
                app_ui.section_header(f"Element totals vs {axis_label}")
                el = el_cols[0]
                ef = batch_executor.sweep_plot_frame(table, axis_col, el)
                if not ef.empty:
                    st.caption(f"{el}")
                    st.line_chart(ef.set_index("x")["y"], height=240)

    # ---- ranking (if computed) ----------------------------------------- #
    ranking = state.ranking
    if ranking is not None and getattr(ranking, "ok", False):
        app_ui.section_header("Ranking")
        st.caption(f"Top scenario: **{ranking.top_scenario_id}** (driver: {ranking.driving_metric}). "
                   "Ranking is over model predictions, not validation.")

    # ---- raw table (hidden by default) --------------------------------- #
    if table is not None and hasattr(table, "empty") and not table.empty:
        with app_ui.advanced_expander("Show raw result table"):
            st.dataframe(table, use_container_width=True, height=280)
            st.download_button("⬇️ Download results (CSV)", data=table.to_csv(index=False),
                               file_name="simulation_results.csv", mime="text/csv",
                               key="results_dl")
    if state.last_explanation:
        with app_ui.advanced_expander("Show grounded explanation"):
            st.json(state.last_explanation)

    # ---- next actions (navigational) ----------------------------------- #
    app_ui.section_header("Next")
    st.caption("• **Save run** with provenance: ask the assistant to *save the run*, or use "
               "**Projects**.  • **Export report** / browse runs: **Projects**.  • **Upload "
               "validation data** to compare with measured results: **Data & Validation**.  • "
               "**Refine the sweep**: ask the assistant or use **Workspace**.")


# The app dispatches to ``render``.
render = _render_results
