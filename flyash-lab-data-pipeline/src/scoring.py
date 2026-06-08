"""Leaching-risk and reuse-ranking scoring.

The reuse score combines several normalised sub-scores (all scaled so higher =
more desirable for reuse) using per-application weight profiles. Weights and the
risk reference points are *editable assumptions* (see :mod:`src.config`).
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from . import config


# ---------------------------------------------------------------------------
# Leaching risk
# ---------------------------------------------------------------------------
def ph_risk(ph: float) -> float:
    """Sub-risk (0..1) from pH: 0 at neutral, rising to 1 far from neutral.

    Returns ``nan`` if pH is missing.
    """
    if ph is None or pd.isna(ph):
        return float("nan")
    deviation = abs(float(ph) - config.PH_NEUTRAL)
    return float(min(1.0, deviation / config.PH_RISK_SPAN))


def conductivity_risk(conductivity: float) -> float:
    """Sub-risk (0..1) from conductivity (uS/cm), linear between low/high refs.

    Returns ``nan`` if conductivity is missing.
    """
    if conductivity is None or pd.isna(conductivity):
        return float("nan")
    lo, hi = config.CONDUCTIVITY_LOW, config.CONDUCTIVITY_HIGH
    if hi <= lo:
        return float("nan")
    scaled = (float(conductivity) - lo) / (hi - lo)
    return float(min(1.0, max(0.0, scaled)))


def leaching_risk_score(row: pd.Series) -> float:
    """Combined leaching risk (0..1) as the mean of available sub-risks.

    Uses pH and conductivity sub-risks; ignores whichever is missing. Returns
    ``nan`` only when both are missing.
    """
    parts = [ph_risk(row.get("leachate_pH")),
             conductivity_risk(row.get("leachate_conductivity_uS_cm"))]
    parts = [p for p in parts if not pd.isna(p)]
    if not parts:
        return float("nan")
    return float(np.mean(parts))


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
def _normalise_higher_better(series: pd.Series) -> pd.Series:
    """Min-max normalise to 0..1 (higher input -> higher score).

    A flat or all-NaN series maps to a neutral 0.5 where defined, NaN elsewhere.
    """
    s = series.astype("float64")
    valid = s.dropna()
    if valid.empty:
        return pd.Series(np.nan, index=s.index)
    lo, hi = valid.min(), valid.max()
    if hi == lo:
        return s.notna().map({True: 0.5, False: np.nan}).astype("float64")
    return (s - lo) / (hi - lo)


# ---------------------------------------------------------------------------
# Per-mix aggregation and sub-scores
# ---------------------------------------------------------------------------
def mix_level_table(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate specimen rows to one row per ``mix_id`` for scoring.

    Numeric fields are averaged across the mix's specimens. Requires that
    derived columns (from :func:`calculations.add_derived_columns`) are present.
    """
    agg_cols = {
        "compressive_strength_MPa": "mean",
        "fly_ash_replacement_percent": "mean",
        "red_mud_percent": "mean",
        "estimated_co2_saving_kg": "mean",
        "flow_mm": "mean",
        "leachate_pH": "mean",
        "leachate_conductivity_uS_cm": "mean",
    }
    present = {k: v for k, v in agg_cols.items() if k in df.columns}
    grouped = df.groupby("mix_id", dropna=False).agg(present)
    grouped = grouped.reset_index()
    return grouped


def compute_subscores(mix_df: pd.DataFrame) -> pd.DataFrame:
    """Compute the seven normalised reuse sub-scores per mix (0..1, higher better).

    Args:
        mix_df: One row per mix (see :func:`mix_level_table`).
    """
    out = mix_df.copy()

    out["sub_strength"] = _normalise_higher_better(
        out.get("compressive_strength_MPa", pd.Series(index=out.index, dtype="float64")))
    out["sub_fly_ash_usage"] = _normalise_higher_better(
        out.get("fly_ash_replacement_percent", pd.Series(index=out.index, dtype="float64")))
    out["sub_co2_saving"] = _normalise_higher_better(
        out.get("estimated_co2_saving_kg", pd.Series(index=out.index, dtype="float64")))
    out["sub_flow"] = _normalise_higher_better(
        out.get("flow_mm", pd.Series(index=out.index, dtype="float64")))

    # Safety sub-scores: 1 - risk (higher = safer).
    ph_r = out.get("leachate_pH", pd.Series(index=out.index, dtype="float64")).apply(ph_risk)
    cond_r = out.get("leachate_conductivity_uS_cm",
                     pd.Series(index=out.index, dtype="float64")).apply(conductivity_risk)
    out["sub_ph_safety"] = 1.0 - ph_r
    out["sub_conductivity_safety"] = 1.0 - cond_r

    # Lower red-mud demand is better: invert the normalised red-mud percent.
    red = _normalise_higher_better(
        out.get("red_mud_percent", pd.Series(index=out.index, dtype="float64")))
    out["sub_low_red_mud"] = 1.0 - red

    return out


_SUBSCORE_COLUMN = {
    "strength": "sub_strength",
    "fly_ash_usage": "sub_fly_ash_usage",
    "co2_saving": "sub_co2_saving",
    "ph_safety": "sub_ph_safety",
    "conductivity_safety": "sub_conductivity_safety",
    "flow": "sub_flow",
    "low_red_mud": "sub_low_red_mud",
}


def _weighted_score(row: pd.Series, weights: dict) -> float:
    """Weighted average of available sub-scores; weights renormalised over present ones."""
    num = 0.0
    wsum = 0.0
    for key, weight in weights.items():
        col = _SUBSCORE_COLUMN.get(key)
        if col is None or weight == 0:
            continue
        val = row.get(col)
        if pd.isna(val):
            continue
        num += weight * float(val)
        wsum += weight
    if wsum == 0:
        return float("nan")
    return num / wsum


def reuse_scores(
    df: pd.DataFrame,
    presets: Optional[dict] = None,
    applications: Optional[list[str]] = None,
) -> pd.DataFrame:
    """Score and rank each mix for each reuse application.

    Args:
        df: Specimen-level data with derived columns present.
        presets: Mapping of application -> weight dict; defaults to
            ``config.SCORING_PRESETS``. Pass overrides here for in-app sliders.
        applications: Subset of application keys to score; defaults to all.

    Returns:
        A DataFrame with one row per mix, columns for each sub-score, one
        ``score_<application>`` column per application (0..100), a
        ``best_application`` label, and ``best_score``.
    """
    presets = presets or config.SCORING_PRESETS
    applications = applications or list(presets.keys())

    mix_df = mix_level_table(df)
    scored = compute_subscores(mix_df)

    for app in applications:
        weights = presets[app]
        scored[f"score_{app}"] = scored.apply(
            lambda r: _weighted_score(r, weights) * 100.0, axis=1
        )

    score_cols = [f"score_{app}" for app in applications]
    if score_cols:
        best_idx = scored[score_cols].idxmax(axis=1)
        scored["best_application"] = best_idx.apply(
            lambda c: c.replace("score_", "") if isinstance(c, str) else None
        )
        scored["best_score"] = scored[score_cols].max(axis=1)
    return scored


def ranking_table(scored: pd.DataFrame, applications: Optional[list[str]] = None) -> pd.DataFrame:
    """Return a compact ranking table (mix_id, per-app scores, best app/score).

    Sorted by ``best_score`` descending.
    """
    applications = applications or [
        c.replace("score_", "") for c in scored.columns if c.startswith("score_")
    ]
    cols = ["mix_id"] + [f"score_{a}" for a in applications] + ["best_application", "best_score"]
    cols = [c for c in cols if c in scored.columns]
    table = scored[cols].copy()
    if "best_score" in table.columns:
        table = table.sort_values("best_score", ascending=False, na_position="last")
    return table.reset_index(drop=True)
