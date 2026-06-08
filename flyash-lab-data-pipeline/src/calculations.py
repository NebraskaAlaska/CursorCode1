"""Derived metrics and statistics for fly ash lab data.

All functions take and return pandas DataFrames and avoid any Streamlit/UI
dependencies so they can be unit-tested directly.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from . import config


# ---------------------------------------------------------------------------
# Specimen loaded area
# ---------------------------------------------------------------------------
def loaded_area_mm2(row: pd.Series) -> float:
    """Return the loaded cross-sectional area (mm^2) for a specimen row.

    Preference order:
        1. Explicit ``loaded_area_mm2`` if provided.
        2. Cylinder: pi/4 * diameter^2 when ``diameter_mm`` present.
        3. Cube/prism: ``length_mm * width_mm`` when both present.

    Returns ``nan`` if no area can be determined.
    """
    area = row.get("loaded_area_mm2")
    if pd.notna(area) and area > 0:
        return float(area)

    diameter = row.get("diameter_mm")
    if pd.notna(diameter) and diameter > 0:
        return math.pi / 4.0 * float(diameter) ** 2

    length = row.get("length_mm")
    width = row.get("width_mm")
    if pd.notna(length) and pd.notna(width) and length > 0 and width > 0:
        return float(length) * float(width)

    return float("nan")


def back_calculate_strength(df: pd.DataFrame) -> pd.DataFrame:
    """Fill missing ``compressive_strength_MPa`` from peak load and area.

    strength (MPa) = peak_load_kN * 1000 (N) / area (mm^2)   [since 1 MPa = 1 N/mm^2]

    Adds a ``strength_source`` column: "reported" where strength was already
    present, "calculated" where it was derived, "missing" where neither was
    available. Also materialises ``loaded_area_mm2`` where it could be derived.
    """
    df = df.copy()
    sources = []
    for idx, row in df.iterrows():
        reported = row.get("compressive_strength_MPa")
        if pd.notna(reported):
            sources.append("reported")
            continue

        area = loaded_area_mm2(row)
        load_kn = row.get("peak_load_kN")
        if pd.notna(load_kn) and load_kn > 0 and not math.isnan(area) and area > 0:
            df.at[idx, "compressive_strength_MPa"] = float(load_kn) * 1000.0 / area
            if pd.isna(row.get("loaded_area_mm2")):
                df.at[idx, "loaded_area_mm2"] = area
            sources.append("calculated")
        else:
            sources.append("missing")
    df["strength_source"] = sources
    return df


# ---------------------------------------------------------------------------
# Binder / ratio metrics
# ---------------------------------------------------------------------------
def add_derived_columns(
    df: pd.DataFrame, factors: Optional[dict] = None
) -> pd.DataFrame:
    """Add all derived mix/CO2/cost metrics to the DataFrame.

    Args:
        df: Loaded lab data (numeric columns already coerced).
        factors: Editable CO2/cost assumptions; defaults to ``config.DEFAULT_FACTORS``.

    Adds columns:
        total_binder_mass_g, water_binder_ratio, fly_ash_replacement_percent,
        red_mud_percent, estimated_cement_saved_g, estimated_co2_saving_kg,
        estimated_cost_saving, plus ``compressive_strength_MPa`` back-calc and
        ``strength_source`` (via :func:`back_calculate_strength`).
    """
    factors = {**config.DEFAULT_FACTORS, **(factors or {})}
    df = back_calculate_strength(df)

    fly_ash = df["fly_ash_mass_g"].fillna(0)
    cement = df["cement_mass_g"].fillna(0)
    red_mud = df["red_mud_mass_g"].fillna(0)
    water = df["water_mass_g"]

    total_binder = fly_ash + cement + red_mud
    df["total_binder_mass_g"] = total_binder

    # Avoid divide-by-zero: ratios/percentages are NaN where binder is zero.
    safe_binder = total_binder.replace(0, np.nan)
    df["water_binder_ratio"] = water / safe_binder
    df["fly_ash_replacement_percent"] = fly_ash / safe_binder * 100.0
    df["red_mud_percent"] = red_mud / safe_binder * 100.0

    # CO2 / cost: assume fly ash (and red mud) displace cement 1:1 by mass.
    # "Cement saved" = the non-cement binder mass that stands in for cement.
    cement_saved = fly_ash + red_mud
    df["estimated_cement_saved_g"] = cement_saved

    cement_saved_kg = cement_saved / 1000.0
    fly_ash_kg = fly_ash / 1000.0
    df["estimated_co2_saving_kg"] = (
        cement_saved_kg * factors["cement_co2_per_kg"]
        - fly_ash_kg * factors["fly_ash_co2_per_kg"]
    )
    df["estimated_cost_saving"] = (
        cement_saved_kg * factors["cement_cost_per_kg"]
        - fly_ash_kg * factors["fly_ash_cost_per_kg"]
    )
    return df


# ---------------------------------------------------------------------------
# Data status
# ---------------------------------------------------------------------------
def infer_data_status(df: pd.DataFrame) -> pd.DataFrame:
    """Fill a blank ``data_status`` using simple rules; keep explicit values.

    Rules (only applied where ``data_status`` is blank):
        * "tested"  if a compressive strength is present.
        * "pending" otherwise.
    Explicit values ("failed", "needs_retest", etc.) are always preserved.
    Unknown explicit values are kept as-is (validation will flag them).
    """
    df = df.copy()

    def _status(row: pd.Series) -> str:
        existing = row.get("data_status")
        if isinstance(existing, str) and existing.strip():
            return existing.strip().lower()
        if pd.notna(row.get("compressive_strength_MPa")):
            return "tested"
        return "pending"

    df["data_status"] = df.apply(_status, axis=1)
    return df


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def strength_statistics(df: pd.DataFrame) -> pd.DataFrame:
    """Compute compressive-strength statistics per (mix_id, curing_age_days).

    Returns a DataFrame with columns: mix_id, curing_age_days, n, mean_MPa,
    std_MPa, cv_percent. Specimens without a strength value are ignored.
    Standard deviation uses the sample definition (ddof=1); CV is NaN when the
    mean is zero or only one specimen is present.
    """
    subset = df.dropna(subset=["compressive_strength_MPa"]).copy()
    if subset.empty:
        return pd.DataFrame(
            columns=["mix_id", "curing_age_days", "n", "mean_MPa", "std_MPa", "cv_percent"]
        )

    grouped = subset.groupby(["mix_id", "curing_age_days"], dropna=False)[
        "compressive_strength_MPa"
    ]
    stats = grouped.agg(n="count", mean_MPa="mean", std_MPa=lambda s: s.std(ddof=1))
    stats = stats.reset_index()
    stats["cv_percent"] = np.where(
        (stats["mean_MPa"].abs() > 0) & (stats["n"] > 1),
        stats["std_MPa"] / stats["mean_MPa"] * 100.0,
        np.nan,
    )
    return stats


def coefficient_of_variation(values) -> float:
    """Return the coefficient of variation (%) of a sequence of numbers.

    Returns ``nan`` for fewer than two values or a zero mean.
    """
    arr = pd.Series(values, dtype="float64").dropna()
    if len(arr) < 2:
        return float("nan")
    mean = arr.mean()
    if mean == 0:
        return float("nan")
    return float(arr.std(ddof=1) / mean * 100.0)
