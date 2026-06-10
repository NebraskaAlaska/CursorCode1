"""Tests for the measured-data overview (first plot family — data only, no model).

Pin the pure data-prep contract the Run + Results overview relies on: variable
selection (only present, numeric columns), exclusion of blank/non-numeric values
with reasons (counts must add up), per-condition mean ± std replicate aggregation,
and the no-time-column fallback. Synthetic data only; no Streamlit/matplotlib here.
"""
from __future__ import annotations

import math

import pandas as pd

from flyash_phreeqc_ml.viz import measured_overview as mo


def _ph_only(n_rep: int = 3) -> pd.DataFrame:
    """pH-only run: one condition with replicates; ICP columns blank/absent."""
    return pd.DataFrame([
        {"sample_id": f"S-R{i}", "leachant": "NaOH", "time_min": 10,
         "liquid_solid_ratio": 5, "CO2_condition": "open",
         "final_pH": 13.0 + i * 0.1, "Ca_mM": ""}
        for i in range(1, n_rep + 1)
    ])


# --------------------------------------------------------------------------- #
# Variable selection
# --------------------------------------------------------------------------- #
def test_available_variables_lists_only_present_numeric():
    df = _ph_only()
    # final_pH has numbers; Ca_mM is all-blank; the rest are absent columns.
    assert mo.available_variables(df) == ["final_pH"]


def test_available_variables_orders_ph_first_then_icp():
    df = pd.DataFrame([{"sample_id": "S1", "final_pH": 13.0, "Si_mM": 1.2, "Ca_mM": 2.5}])
    assert mo.available_variables(df) == ["final_pH", "Ca_mM", "Si_mM"]


def test_available_variables_empty_when_no_data():
    assert mo.available_variables(pd.DataFrame()) == []
    # Column present but entirely non-numeric / blank -> not offered.
    df = pd.DataFrame([{"sample_id": "S1", "final_pH": ""}, {"sample_id": "S2", "final_pH": "x"}])
    assert mo.available_variables(df) == []


# --------------------------------------------------------------------------- #
# Exclusion reasons + counts add up
# --------------------------------------------------------------------------- #
def test_blank_and_nonnumeric_values_excluded_with_reasons():
    df = pd.DataFrame([
        {"sample_id": "S1", "leachant": "NaOH", "time_min": 10, "final_pH": 13.0},
        {"sample_id": "S2", "leachant": "NaOH", "time_min": 10, "final_pH": ""},     # blank
        {"sample_id": "S3", "leachant": "NaOH", "time_min": 10, "final_pH": "n/a"},  # non-numeric
    ])
    ov = mo.prepare_overview(df, "final_pH")
    assert ov["n_shown"] == 1
    assert ov["n_excluded"] == 2
    reasons = dict(zip(ov["excluded"]["sample_id"], ov["excluded"]["reason"]))
    assert reasons["S2"] == "missing value (blank)"
    assert "non-numeric value" in reasons["S3"] and "n/a" in reasons["S3"]
    # Counts add up: every row is either shown or excluded.
    assert ov["n_shown"] + ov["n_excluded"] == len(df)


def test_nan_float_counts_as_missing():
    df = pd.DataFrame([
        {"sample_id": "S1", "final_pH": 13.0},
        {"sample_id": "S2", "final_pH": float("nan")},
    ])
    ov = mo.prepare_overview(df, "final_pH")
    assert ov["n_shown"] == 1 and ov["n_excluded"] == 1
    assert ov["excluded"].iloc[0]["reason"] == "missing value (blank)"


def test_missing_variable_column_gives_empty_overview():
    df = _ph_only()
    ov = mo.prepare_overview(df, "Fe_mM")  # column not present
    assert ov["n_shown"] == 0 and ov["plot"].empty
    assert list(ov["plot"].columns) == mo.PLOT_COLUMNS


# --------------------------------------------------------------------------- #
# Replicate aggregation (mean ± std per condition)
# --------------------------------------------------------------------------- #
def test_group_stats_mean_std_and_single_replicate_nan():
    df = pd.DataFrame([
        {"sample_id": "A-R1", "leachant": "NaOH", "time_min": 10, "liquid_solid_ratio": 5,
         "CO2_condition": "open", "final_pH": 13.0},
        {"sample_id": "A-R2", "leachant": "NaOH", "time_min": 10, "liquid_solid_ratio": 5,
         "CO2_condition": "open", "final_pH": 13.2},
        {"sample_id": "B-R1", "leachant": "NaOH", "time_min": 60, "liquid_solid_ratio": 5,
         "CO2_condition": "open", "final_pH": 12.5},  # single replicate
    ])
    ov = mo.prepare_overview(df, "final_pH")
    gs = {r["condition_key"]: r for _, r in ov["group_stats"].iterrows()}
    a = next(v for k, v in gs.items() if "10min" in k)
    b = next(v for k, v in gs.items() if "60min" in k)
    assert int(a["n"]) == 2
    assert a["mean"] == 13.1
    assert abs(a["std"] - pd.Series([13.0, 13.2]).std(ddof=1)) < 1e-9
    assert int(b["n"]) == 1
    assert math.isnan(b["std"])              # ddof=1 -> NaN for one replicate
    assert ov["replicate_counts"] == {a["condition_key"]: 2, b["condition_key"]: 1}


def test_replicate_ids_parsed_into_plot_frame():
    ov = mo.prepare_overview(_ph_only(3), "final_pH")
    assert set(ov["plot"]["replicate_id"]) == {"R1", "R2", "R3"}


# --------------------------------------------------------------------------- #
# Time-column presence / fallback
# --------------------------------------------------------------------------- #
def test_has_time_true_includes_time_column():
    ov = mo.prepare_overview(_ph_only(), "final_pH")
    assert ov["has_time"] is True
    assert mo.TIME_COLUMN in ov["plot"].columns


def test_no_time_column_fallback():
    df = pd.DataFrame([
        {"sample_id": "S1", "leachant": "NaOH", "liquid_solid_ratio": 5,
         "CO2_condition": "open", "final_pH": 13.0},
        {"sample_id": "S2", "leachant": "NaOH", "liquid_solid_ratio": 10,
         "CO2_condition": "open", "final_pH": 12.0},
    ])
    ov = mo.prepare_overview(df, "final_pH")
    assert ov["has_time"] is False
    assert mo.TIME_COLUMN not in ov["plot"].columns
    assert ov["n_shown"] == 2 and ov["n_conditions"] == 2


def test_time_column_present_but_blank_is_not_used():
    df = pd.DataFrame([
        {"sample_id": "S1", "leachant": "NaOH", "time_min": "", "final_pH": 13.0},
        {"sample_id": "S2", "leachant": "NaOH", "time_min": "", "final_pH": 12.0},
    ])
    ov = mo.prepare_overview(df, "final_pH")
    assert ov["has_time"] is False           # column exists but no numeric value
    assert mo.TIME_COLUMN not in ov["plot"].columns
