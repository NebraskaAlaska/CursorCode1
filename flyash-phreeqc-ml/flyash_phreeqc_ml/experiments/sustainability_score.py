"""Feature 3 — sustainability / cost *proxy* indicators (a scaffold).

These are deliberately simple, dimensionless-ish indicators computed per measured
row. They are **not** real dollar costs or life-cycle numbers — they are proxies to
help rank conditions (e.g. "which condition gets the most REE per unit of bulk
dissolution and reagent intensity"). Real costing comes later, once measured data
and process assumptions exist.

Every indicator degrades gracefully: a missing input yields ``NaN`` for that
indicator rather than raising, so a partially-filled sheet still scores.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Bulk-matrix elements summed into the "how much did we dissolve" indicator.
_BULK_COLUMNS = ["Ca_mM", "Si_mM", "Al_mM", "Fe_mM"]

# Fields counted as "required measured fields" for the missing-data penalty.
_REQUIRED_MEASURED_FIELDS = [
    "final_pH",
    "conductivity_mS_cm",
    "Ca_mM",
    "Si_mM",
    "Al_mM",
    "Fe_mM",
    "Na_mM",
    "K_mM",
    "Sc_ppb",
    "total_REE_ppb",
]

SUSTAINABILITY_COLUMNS = [
    "sample_id",
    "NaOH_mol_per_L",
    "treatment_time_min",
    "total_bulk_dissolved_mM",
    "REE_selectivity_proxy",
    "Sc_selectivity_proxy",
    "NaOH_time_intensity",
    "penalty_bulk_dissolution",
    "penalty_missing_data",
]


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """Numeric view of *name*, or an all-NaN series if the column is absent."""
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype="float64")


def compute_sustainability_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Compute proxy indicators for each row of an experimental-release frame.

    Returns a new DataFrame with one row per input row and the columns listed in
    :data:`SUSTAINABILITY_COLUMNS`. Division-by-zero (no bulk dissolution measured)
    yields ``NaN`` for the selectivity proxies rather than ``inf``.
    """
    out = pd.DataFrame(index=df.index)
    out["sample_id"] = df["sample_id"] if "sample_id" in df.columns else ""

    naoh = _col(df, "NaOH_M")
    time_min = _col(df, "time_min")

    out["NaOH_mol_per_L"] = naoh
    out["treatment_time_min"] = time_min

    # Bulk dissolution = sum of matrix elements; all-NaN row -> NaN (min_count=1).
    bulk = pd.concat([_col(df, c) for c in _BULK_COLUMNS], axis=1).sum(axis=1, min_count=1)
    out["total_bulk_dissolved_mM"] = bulk

    # Selectivity proxies: trace (ppb) per unit bulk (mM). Guard zero denominators.
    safe_bulk = bulk.where(bulk != 0, other=np.nan)
    out["REE_selectivity_proxy"] = _col(df, "total_REE_ppb") / safe_bulk
    out["Sc_selectivity_proxy"] = _col(df, "Sc_ppb") / safe_bulk

    out["NaOH_time_intensity"] = naoh * time_min
    out["penalty_bulk_dissolution"] = bulk

    # Count of missing required measured fields per row (absent column counts as
    # missing for every row).
    present = pd.concat([_col(df, c) for c in _REQUIRED_MEASURED_FIELDS], axis=1)
    out["penalty_missing_data"] = present.isna().sum(axis=1).astype(int)

    return out[SUSTAINABILITY_COLUMNS].reset_index(drop=True)
