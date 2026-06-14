"""Tests for the replicate/uncertainty closeout (SEM, batches, comparison error bars).

Pins: SEM math (std/√n), batch grouping parsed from synthetic sample names, measured
std/SEM flowing into the comparison frame (so plots can draw measured error bars), and
n=1 conditions degrading gracefully — NaN spread, never a fake zero. Synthetic only.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from flyash_phreeqc_ml import profiles, replicates as rep
from flyash_phreeqc_ml.viz import measured_overview


# --------------------------------------------------------------------------- #
# SEM math
# --------------------------------------------------------------------------- #
def _three():
    base = {"leachant": "NaOH", "NaOH_M": 0.5, "time_min": 10, "liquid_solid_ratio": 5,
            "CO2_condition": "OA"}
    return pd.DataFrame([
        {**base, "sample_id": "NaOH-OA-10min-R1", "final_pH": 13.0, "Ca_mM": 2.0,
         "Si_mM": 1.0, "Al_mM": 0.5},
        {**base, "sample_id": "NaOH-OA-10min-R2", "final_pH": 13.2, "Ca_mM": 2.2,
         "Si_mM": 1.1, "Al_mM": 0.6},
        {**base, "sample_id": "NaOH-OA-10min-R3", "final_pH": 13.1, "Ca_mM": 2.1,
         "Si_mM": 1.2, "Al_mM": 0.7},
    ])


def test_replicate_summary_sem_is_std_over_sqrt_n():
    row = rep.replicate_summary(_three()).iloc[0]
    vals = pd.Series([13.0, 13.2, 13.1])
    expected_std = vals.std(ddof=1)
    assert row["std_final_pH"] == pytest.approx(expected_std)
    assert row["sem_final_pH"] == pytest.approx(expected_std / math.sqrt(3))
    # SEM is strictly smaller than std for n > 1.
    assert row["sem_final_pH"] < row["std_final_pH"]
    assert "sem_final_pH" in rep.REPLICATE_SUMMARY_COLUMNS


def test_single_replicate_std_and_sem_are_nan_not_zero():
    row = rep.replicate_summary(_three().iloc[[0]]).iloc[0]
    assert row["number_of_replicates"] == 1
    assert math.isnan(row["std_final_pH"])
    assert math.isnan(row["sem_final_pH"])      # NaN, never a fake 0


def test_overview_group_stats_carry_sem():
    ov = measured_overview.prepare_overview(_three(), "final_pH")
    assert "sem" in measured_overview.GROUP_STAT_COLUMNS
    g = ov["group_stats"].iloc[0]
    assert g["n"] == 3
    assert g["sem"] == pytest.approx(g["std"] / math.sqrt(3))


def test_overview_single_replicate_sem_nan():
    ov = measured_overview.prepare_overview(_three().iloc[[0]], "final_pH")
    g = ov["group_stats"].iloc[0]
    assert g["n"] == 1
    assert math.isnan(g["std"]) and math.isnan(g["sem"])


# --------------------------------------------------------------------------- #
# Batch grouping (parsed from synthetic names / a column)
# --------------------------------------------------------------------------- #
BATCH_PROFILE = profiles.DatasetProfile(
    name="batch demo", grouping="fly_ash",
    batch_pattern=r"(?:^|[-_])B(\d+)\b", group_by_batch=True,
)
BATCH_COL_PROFILE = profiles.DatasetProfile(
    name="batch col demo", grouping="fly_ash",
    batch_column="batch_id", group_by_batch=True,
)


def _batched_names():
    base = {"leachant": "NaOH", "NaOH_M": 0.5, "time_min": 10, "liquid_solid_ratio": 5,
            "CO2_condition": "OA"}
    return pd.DataFrame([
        {**base, "sample_id": "NaOH-OA-10min-B1-R1", "final_pH": 13.0},
        {**base, "sample_id": "NaOH-OA-10min-B1-R2", "final_pH": 13.1},
        {**base, "sample_id": "NaOH-OA-10min-B2-R1", "final_pH": 12.4},
        {**base, "sample_id": "NaOH-OA-10min-B2-R2", "final_pH": 12.5},
    ])


def test_batch_parsed_from_sample_name_splits_conditions():
    df = _batched_names()
    # Same condition metadata, but two batches → two condition_keys when group_by_batch.
    keys = {rep.condition_key(r, BATCH_PROFILE) for r in df.to_dict("records")}
    assert len(keys) == 2
    assert all(k.endswith("_batch1") or k.endswith("_batch2") for k in keys)
    # batch_id parsed from the name.
    assert rep.batch_id(df.iloc[0].to_dict(), BATCH_PROFILE) == "1"
    assert rep.batch_id(df.iloc[2].to_dict(), BATCH_PROFILE) == "2"


def test_without_group_by_batch_batches_fold_into_one_condition():
    df = _batched_names()
    # Default fly-ash profile ignores batch → one condition_key, batches averaged together.
    keys = {rep.condition_key(r) for r in df.to_dict("records")}
    assert len(keys) == 1


def test_batch_from_explicit_column():
    df = pd.DataFrame([
        {"sample_id": "S1", "leachant": "NaOH", "NaOH_M": 0.5, "time_min": 10,
         "liquid_solid_ratio": 5, "CO2_condition": "OA", "batch_id": "A", "final_pH": 13.0},
        {"sample_id": "S2", "leachant": "NaOH", "NaOH_M": 0.5, "time_min": 10,
         "liquid_solid_ratio": 5, "CO2_condition": "OA", "batch_id": "B", "final_pH": 13.0},
    ])
    keys = {rep.condition_key(r, BATCH_COL_PROFILE) for r in df.to_dict("records")}
    assert len(keys) == 2
    # annotate adds the batch_id column when the profile defines batches.
    ann = rep.annotate(df, BATCH_COL_PROFILE)
    assert rep.BATCH_ID_COLUMN in ann.columns
    assert list(ann[rep.BATCH_ID_COLUMN]) == ["A", "B"]


def test_annotate_no_batch_column_for_default_profile():
    # Fly-ash default defines no batch → annotate keeps its existing columns.
    ann = rep.annotate(_three())
    assert rep.BATCH_ID_COLUMN not in ann.columns


# --------------------------------------------------------------------------- #
# Uncertainty into the comparison frame (error-bar data)
# --------------------------------------------------------------------------- #
def _manifest():
    return pd.DataFrame([{"phreeqc_record_key": "k1", "state": "batch",
                          "predicted_pH": 13.0, "predicted_Ca_mM": 2.0,
                          "predicted_Si_mM": 1.0, "predicted_Al_mM": 0.5}])


def test_condition_mean_comparison_carries_std_and_sem():
    df = _three()
    ck = rep.condition_key(df.iloc[0].to_dict())
    cmap = pd.DataFrame([{"condition_key": ck, "phreeqc_record_key": "k1"}])
    comp = rep.condition_mean_comparison(df, cmap, _manifest())
    row = comp.iloc[0]
    # Error-bar data flows into the comparison frame.
    assert "std_final_pH" in comp.columns and "sem_final_pH" in comp.columns
    assert row["sem_final_pH"] == pytest.approx(
        pd.Series([13.0, 13.2, 13.1]).std(ddof=1) / math.sqrt(3))
    # residual = mean − model = 13.1 − 13.0 = 0.1; std ≈ 0.1 → within the replicate spread.
    assert row["residual_pH"] == pytest.approx(0.1)
    assert bool(row["within_meas_std_pH"]) is True


def test_condition_mean_comparison_n1_degrades_gracefully():
    df = _three().iloc[[0]]                       # one replicate
    ck = rep.condition_key(df.iloc[0].to_dict())
    cmap = pd.DataFrame([{"condition_key": ck, "phreeqc_record_key": "k1"}])
    comp = rep.condition_mean_comparison(df, cmap, _manifest())
    row = comp.iloc[0]
    assert row["n_replicates"] == 1
    assert math.isnan(row["std_final_pH"]) and math.isnan(row["sem_final_pH"])
    # No spread → the within-uncertainty flag is undefined (None), not a fake True/0.
    assert row["within_meas_std_pH"] is None
    assert "n_replicates<2" in row["warning"]


def test_replicate_role_definitions_present():
    # The three replicate roles are documented as a profile-level notion.
    assert set(rep.REPLICATE_ROLE_DEFINITIONS) == {"time_point", "batch", "true_replicate"}
