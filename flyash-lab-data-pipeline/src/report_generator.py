"""Assemble an HTML research report from processed data and analysis outputs."""

from __future__ import annotations

import os
from typing import Optional

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config, scoring, validation
from .plotting import FIGURE_TITLES

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )


def _figure_html(figures: dict) -> list[dict]:
    """Render Plotly figures to embeddable HTML fragments.

    The first figure embeds the Plotly JS via CDN; the rest reuse it.
    """
    out = []
    first = True
    for key, fig in figures.items():
        html = fig.to_html(
            full_html=False,
            include_plotlyjs="cdn" if first else False,
        )
        out.append({"title": FIGURE_TITLES.get(key, key), "html": html})
        first = False
    return out


def best_and_risky_mixes(scored: pd.DataFrame, df: pd.DataFrame,
                         top_n: int = 5) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (best mixes by reuse score, risky/failed mixes) for the report.

    Risky/failed = mixes whose specimens include a 'failed'/'needs_retest' status
    or whose mean leaching risk is high (> 0.5).
    """
    best = pd.DataFrame()
    if scored is not None and not scored.empty and "best_score" in scored.columns:
        best = (scored.dropna(subset=["best_score"])
                .sort_values("best_score", ascending=False)
                .head(top_n)[["mix_id", "best_application", "best_score"]]
                .copy())
        best["best_application"] = best["best_application"].map(
            lambda a: config.APPLICATION_LABELS.get(a, a) if isinstance(a, str) else a)

    risky_rows = []
    if "mix_id" in df.columns:
        risk = df.apply(scoring.leaching_risk_score, axis=1)
        work = df.assign(_risk=risk)
        for mix_id, grp in work.groupby("mix_id", dropna=False):
            statuses = set(str(s).lower() for s in grp.get("data_status", pd.Series(dtype=object))
                           if isinstance(s, str))
            mean_risk = grp["_risk"].mean()
            flagged = bool(statuses & {"failed", "needs_retest"}) or (
                pd.notna(mean_risk) and mean_risk > 0.5)
            if flagged:
                reasons = []
                if "failed" in statuses:
                    reasons.append("has failed specimen(s)")
                if "needs_retest" in statuses:
                    reasons.append("needs retest")
                if pd.notna(mean_risk) and mean_risk > 0.5:
                    reasons.append(f"high leaching risk ({mean_risk:.2f})")
                risky_rows.append({"mix_id": mix_id, "reasons": "; ".join(reasons),
                                   "mean_leaching_risk": round(float(mean_risk), 3)
                                   if pd.notna(mean_risk) else None})
    risky = pd.DataFrame(risky_rows)
    return best, risky


def recommend_next_experiments(df: pd.DataFrame, scored: pd.DataFrame,
                               issues: list[dict]) -> list[str]:
    """Heuristic, data-driven suggestions for follow-up experiments."""
    recs: list[str] = []

    # Pending specimens -> finish testing.
    if "data_status" in df.columns:
        pending = (df["data_status"].astype(str).str.lower() == "pending").sum()
        if pending:
            recs.append(f"Test the {pending} specimen(s) still marked 'pending' to "
                        "complete their curing-age strength curves.")

    # High-CV groups -> replicate.
    high_cv = [it for it in issues if it.get("code") == "high_cv"]
    if high_cv:
        recs.append(f"Add replicate specimens for the {len(high_cv)} mix/age group(s) "
                    "with high strength variability to tighten the coefficient of variation.")

    # Sparse curing ages -> add ages.
    if "curing_age_days" in df.columns:
        ages = df["curing_age_days"].dropna().nunique()
        if ages and ages < 3:
            recs.append("Broaden the curing-age range (e.g. 7/28/56/90 days) to capture "
                        "strength development trends.")

    # Top mix -> scale up its best application.
    if scored is not None and not scored.empty and "best_score" in scored.columns:
        top = scored.dropna(subset=["best_score"]).sort_values(
            "best_score", ascending=False).head(1)
        if not top.empty:
            mix = top.iloc[0]["mix_id"]
            app = top.iloc[0].get("best_application")
            app_label = config.APPLICATION_LABELS.get(app, app)
            recs.append(f"Scale up and confirm mix '{mix}' for {app_label}, including "
                        "durability and leaching follow-up.")

    # Red mud exploration (kept small).
    if "red_mud_percent" in df.columns and (df["red_mud_percent"].fillna(0) > 0).any():
        recs.append("Run a small bounded red-mud dosage series (supply is limited) to map "
                    "its effect on strength and leaching at low replacement levels.")
    else:
        recs.append("Consider a single small optional red-mud comparison batch to benchmark "
                    "against the fly-ash-only mixes.")

    if not recs:
        recs.append("Collect more specimens across additional mixes and ages to enable "
                    "robust ranking.")
    return recs


def build_report(
    df: pd.DataFrame,
    issues: list[dict],
    stats: pd.DataFrame,
    scored: pd.DataFrame,
    figures: dict,
    factors: Optional[dict] = None,
    meta: Optional[dict] = None,
) -> str:
    """Render the full HTML report and return it as a string.

    Args:
        df: Processed specimen-level data (with derived columns).
        issues: Validation issues list.
        stats: Strength statistics table.
        scored: Reuse-score table (per mix).
        figures: Mapping of figure key -> Plotly Figure.
        factors: CO2/cost assumptions used (shown in the report).
        meta: Optional extra metadata (e.g. {"generated_at": ..., "source_file": ...}).
    """
    factors = {**config.DEFAULT_FACTORS, **(factors or {})}
    meta = meta or {}

    summary = validation.validation_summary(issues)
    best, risky = best_and_risky_mixes(scored, df)
    recs = recommend_next_experiments(df, scored, issues)
    ranking = scoring.ranking_table(scored) if scored is not None and not scored.empty \
        else pd.DataFrame()

    n_samples = len(df)
    n_mixes = df["mix_id"].dropna().nunique() if "mix_id" in df.columns else 0
    status_counts = (df["data_status"].astype(str).str.lower().value_counts().to_dict()
                     if "data_status" in df.columns else {})

    context = {
        "title": config.PROJECT_TITLE,
        "meta": meta,
        "n_samples": n_samples,
        "n_mixes": n_mixes,
        "status_counts": status_counts,
        "factors": factors,
        "summary": summary,
        "issues": issues,
        "best_html": _df_to_html(best),
        "risky_html": _df_to_html(risky),
        "ranking_html": _df_to_html(_format_ranking(ranking)),
        "stats_html": _df_to_html(stats.round(2) if stats is not None else pd.DataFrame()),
        "figures": _figure_html(figures),
        "recommendations": recs,
        "has_errors": summary["errors"] > 0,
    }
    template = _env().get_template("report_template.html")
    return template.render(**context)


def _format_ranking(ranking: pd.DataFrame) -> pd.DataFrame:
    """Round score columns and prettify application labels for display."""
    if ranking is None or ranking.empty:
        return ranking
    out = ranking.copy()
    for col in out.columns:
        if col.startswith("score_") or col == "best_score":
            out[col] = out[col].round(1)
    if "best_application" in out.columns:
        out["best_application"] = out["best_application"].map(
            lambda a: config.APPLICATION_LABELS.get(a, a) if isinstance(a, str) else a)
    out.columns = [config.APPLICATION_LABELS.get(c.replace("score_", ""), c)
                   if c.startswith("score_") else c for c in out.columns]
    return out


def _df_to_html(df: pd.DataFrame) -> str:
    """Render a DataFrame as an HTML table, or a friendly note if empty."""
    if df is None or df.empty:
        return "<p class='muted'>None.</p>"
    return df.to_html(index=False, classes="data-table", border=0, na_rep="—")


def save_report(html: str, path: str) -> str:
    """Write the report HTML to ``path`` (creating parent dirs) and return it."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path
