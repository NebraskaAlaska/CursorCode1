"""Plotly figure builders for the fly ash lab data pipeline.

Each function returns a ``plotly.graph_objects.Figure`` and tolerates missing
columns / empty data by returning an empty annotated figure rather than raising.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from . import config, scoring


def _empty_figure(message: str) -> go.Figure:
    """Return a placeholder figure with a centered message."""
    fig = go.Figure()
    fig.add_annotation(text=message, x=0.5, y=0.5, xref="paper", yref="paper",
                       showarrow=False, font=dict(size=14, color="gray"))
    fig.update_layout(xaxis=dict(visible=False), yaxis=dict(visible=False))
    return fig


def _has(df: pd.DataFrame, *cols: str) -> bool:
    """True if all columns exist and at least one row has all of them non-null."""
    if not all(c in df.columns for c in cols):
        return False
    return df.dropna(subset=list(cols)).shape[0] > 0


def strength_vs_age(df: pd.DataFrame) -> go.Figure:
    """Compressive strength vs curing age, coloured by mix."""
    cols = ("curing_age_days", "compressive_strength_MPa")
    if not _has(df, *cols):
        return _empty_figure("No strength-vs-age data")
    fig = px.scatter(
        df.dropna(subset=list(cols)), x="curing_age_days", y="compressive_strength_MPa",
        color="mix_id", hover_data=[c for c in ("specimen_id", "mix_type") if c in df.columns],
        title="Compressive strength vs curing age",
    )
    fig.update_layout(xaxis_title="Curing age (days)", yaxis_title="Strength (MPa)")
    return fig


def strength_vs_flyash(df: pd.DataFrame) -> go.Figure:
    """Compressive strength vs fly ash replacement percent."""
    cols = ("fly_ash_replacement_percent", "compressive_strength_MPa")
    if not _has(df, *cols):
        return _empty_figure("No strength-vs-fly-ash data")
    fig = px.scatter(
        df.dropna(subset=list(cols)), x="fly_ash_replacement_percent",
        y="compressive_strength_MPa", color="mix_id",
        title="Compressive strength vs fly ash replacement %",
    )
    fig.update_layout(xaxis_title="Fly ash replacement (%)", yaxis_title="Strength (MPa)")
    return fig


def ph_vs_age(df: pd.DataFrame) -> go.Figure:
    """Leachate pH vs curing age."""
    cols = ("curing_age_days", "leachate_pH")
    if not _has(df, *cols):
        return _empty_figure("No pH-vs-age data")
    fig = px.scatter(
        df.dropna(subset=list(cols)), x="curing_age_days", y="leachate_pH",
        color="mix_id", title="Leachate pH vs curing age",
    )
    fig.update_layout(xaxis_title="Curing age (days)", yaxis_title="Leachate pH")
    return fig


def conductivity_vs_flyash(df: pd.DataFrame) -> go.Figure:
    """Leachate conductivity vs fly ash percent."""
    cols = ("fly_ash_replacement_percent", "leachate_conductivity_uS_cm")
    if not _has(df, *cols):
        return _empty_figure("No conductivity-vs-fly-ash data")
    fig = px.scatter(
        df.dropna(subset=list(cols)), x="fly_ash_replacement_percent",
        y="leachate_conductivity_uS_cm", color="mix_id",
        title="Leachate conductivity vs fly ash %",
    )
    fig.update_layout(xaxis_title="Fly ash replacement (%)",
                      yaxis_title="Conductivity (uS/cm)")
    return fig


def co2_vs_strength(df: pd.DataFrame) -> go.Figure:
    """Estimated CO2 saving vs compressive strength."""
    cols = ("estimated_co2_saving_kg", "compressive_strength_MPa")
    if not _has(df, *cols):
        return _empty_figure("No CO2-vs-strength data")
    fig = px.scatter(
        df.dropna(subset=list(cols)), x="compressive_strength_MPa",
        y="estimated_co2_saving_kg", color="mix_id",
        title="CO2 saving vs compressive strength",
    )
    fig.update_layout(xaxis_title="Strength (MPa)",
                      yaxis_title="Estimated CO2 saving (kg)")
    return fig


def flow_vs_wb(df: pd.DataFrame) -> go.Figure:
    """Flow vs water/binder ratio."""
    cols = ("water_binder_ratio", "flow_mm")
    if not _has(df, *cols):
        return _empty_figure("No flow-vs-w/b data")
    fig = px.scatter(
        df.dropna(subset=list(cols)), x="water_binder_ratio", y="flow_mm",
        color="mix_id", title="Flow vs water/binder ratio",
    )
    fig.update_layout(xaxis_title="Water/binder ratio", yaxis_title="Flow (mm)")
    return fig


def reuse_score_by_mix(scored: pd.DataFrame) -> go.Figure:
    """Bar chart of each mix's best reuse score, coloured by best application."""
    if scored is None or scored.empty or "best_score" not in scored.columns:
        return _empty_figure("No reuse scores")
    data = scored.dropna(subset=["best_score"]).sort_values("best_score", ascending=False)
    if data.empty:
        return _empty_figure("No reuse scores")
    labels = data.get("best_application", pd.Series(index=data.index)).map(
        lambda a: config.APPLICATION_LABELS.get(a, a) if isinstance(a, str) else a
    )
    fig = px.bar(
        data, x="mix_id", y="best_score", color=labels,
        title="Best reuse score by mix",
        labels={"color": "Best application"},
    )
    fig.update_layout(xaxis_title="Mix", yaxis_title="Best reuse score (0-100)")
    return fig


def build_all_figures(df: pd.DataFrame, scored: pd.DataFrame | None = None) -> dict[str, go.Figure]:
    """Build every standard figure and return them keyed by a short name."""
    if scored is None:
        try:
            scored = scoring.reuse_scores(df)
        except Exception:  # pragma: no cover - defensive for malformed data
            scored = pd.DataFrame()
    return {
        "strength_vs_age": strength_vs_age(df),
        "strength_vs_flyash": strength_vs_flyash(df),
        "ph_vs_age": ph_vs_age(df),
        "conductivity_vs_flyash": conductivity_vs_flyash(df),
        "co2_vs_strength": co2_vs_strength(df),
        "flow_vs_wb": flow_vs_wb(df),
        "reuse_score_by_mix": reuse_score_by_mix(scored),
    }


FIGURE_TITLES = {
    "strength_vs_age": "Compressive strength vs curing age",
    "strength_vs_flyash": "Compressive strength vs fly ash replacement %",
    "ph_vs_age": "Leachate pH vs curing age",
    "conductivity_vs_flyash": "Leachate conductivity vs fly ash %",
    "co2_vs_strength": "CO2 saving vs compressive strength",
    "flow_vs_wb": "Flow vs water/binder ratio",
    "reuse_score_by_mix": "Best reuse score by mix",
}
