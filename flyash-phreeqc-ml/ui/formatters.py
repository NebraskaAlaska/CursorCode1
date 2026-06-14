"""Pure formatters / small data-prep extracted from app.py (refactor preparation).

These have **no** Streamlit, no ``session_state``, and no app-level globals — only
pandas / stdlib — so they are trivially safe to live outside the monolithic app
script. app.py re-imports them under their historical underscore names, leaving every
call site unchanged. See ``docs/refactor_plan.md``.
"""
from __future__ import annotations

import pandas as pd


def is_present(value) -> bool:
    """True if a cell value is a real, non-blank entry."""
    return value not in (None, "") and str(value).strip().lower() != "nan"


def has_numeric(df: pd.DataFrame, col: str) -> bool:
    """True if the column exists and has at least one numeric (non-NaN) value."""
    return col in df.columns and bool(pd.to_numeric(df[col], errors="coerce").notna().any())


def nearest_manifest_row(manifest: pd.DataFrame, naoh: float, ls: float) -> dict | None:
    """The batch model scenario closest to (NaOH_M, L/S) — display context only."""
    if manifest is None or manifest.empty:
        return None
    df = manifest.copy()
    if "state" in df.columns:
        batch = df[df["state"].astype(str).str.lower() == "batch"]
        df = batch if not batch.empty else df
    dist = (pd.to_numeric(df.get("NaOH_M"), errors="coerce") - naoh).abs().fillna(9e9) \
        + (pd.to_numeric(df.get("liquid_solid_ratio"), errors="coerce") - ls).abs().fillna(9e9)
    idx = df.index[0] if dist.isna().all() else dist.idxmin()
    r = df.loc[idx]
    cols = ["scenario_label", "NaOH_M", "liquid_solid_ratio", "CO2_condition",
            "predicted_pH", "predicted_Ca_mM", "predicted_Si_mM", "predicted_Al_mM", "generated"]
    return {c: r.get(c) for c in cols if c in df.columns}
