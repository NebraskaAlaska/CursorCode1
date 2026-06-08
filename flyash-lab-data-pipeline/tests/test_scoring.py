"""Unit tests for leaching-risk and reuse-ranking scoring."""

import math

import numpy as np
import pandas as pd
import pytest

from src import calculations, config, data_loader, scoring


def _base_row(**overrides):
    row = {
        "sample_id": "S1", "mix_id": "M1", "specimen_id": "M1-1", "test_id": "T1",
        "fly_ash_mass_g": 300, "cement_mass_g": 700, "water_mass_g": 400,
        "red_mud_mass_g": 0, "sand_mass_g": 2000, "curing_age_days": 28,
        "compressive_strength_MPa": 30.0, "flow_mm": 180,
        "leachate_pH": 7.0, "leachate_conductivity_uS_cm": 500,
    }
    row.update(overrides)
    return row


def _process(rows):
    df = pd.DataFrame(rows)
    df = data_loader.ensure_expected_columns(data_loader.coerce_types(df))
    return calculations.add_derived_columns(df)


# --- leaching risk ---------------------------------------------------------
def test_ph_risk_zero_at_neutral():
    assert scoring.ph_risk(7.0) == pytest.approx(0.0)


def test_ph_risk_increases_with_deviation():
    assert scoring.ph_risk(9.0) < scoring.ph_risk(12.0)


def test_ph_risk_saturates_at_one():
    assert scoring.ph_risk(0.0) == pytest.approx(1.0)
    assert scoring.ph_risk(14.0) == pytest.approx(1.0)


def test_conductivity_risk_monotonic_and_bounded():
    assert scoring.conductivity_risk(config.CONDUCTIVITY_LOW) == pytest.approx(0.0)
    assert scoring.conductivity_risk(config.CONDUCTIVITY_HIGH) == pytest.approx(1.0)
    assert scoring.conductivity_risk(1e9) == pytest.approx(1.0)
    lo_mid = scoring.conductivity_risk(1000)
    hi_mid = scoring.conductivity_risk(3000)
    assert 0 < lo_mid < hi_mid < 1


def test_leaching_risk_handles_missing():
    assert math.isnan(scoring.leaching_risk_score(
        pd.Series({"leachate_pH": np.nan, "leachate_conductivity_uS_cm": np.nan})))
    # one present -> uses it
    val = scoring.leaching_risk_score(
        pd.Series({"leachate_pH": 7.0, "leachate_conductivity_uS_cm": np.nan}))
    assert val == pytest.approx(0.0)


# --- reuse scoring ---------------------------------------------------------
def test_reuse_scores_one_row_per_mix():
    df = _process([_base_row(mix_id="A"), _base_row(mix_id="B", compressive_strength_MPa=10)])
    scored = scoring.reuse_scores(df)
    assert set(scored["mix_id"]) == {"A", "B"}
    assert len(scored) == 2


def test_higher_strength_ranks_higher_for_blocks():
    rows = [
        _base_row(mix_id="STRONG", compressive_strength_MPa=60),
        _base_row(mix_id="WEAK", compressive_strength_MPa=5),
    ]
    df = _process(rows)
    scored = scoring.reuse_scores(df)
    s = dict(zip(scored["mix_id"], scored["score_blocks_pavers"]))
    assert s["STRONG"] > s["WEAK"]


def test_lower_leaching_risk_better_for_disposal():
    rows = [
        _base_row(mix_id="SAFE", leachate_pH=7.0, leachate_conductivity_uS_cm=400),
        _base_row(mix_id="RISKY", leachate_pH=13.5, leachate_conductivity_uS_cm=8000),
    ]
    df = _process(rows)
    scored = scoring.reuse_scores(df)
    s = dict(zip(scored["mix_id"], scored["score_stabilized_disposal_monolith"]))
    assert s["SAFE"] > s["RISKY"]


def test_weighted_score_renormalises_over_present_subscores():
    # Single mix: normalisation maps lone values to 0.5; score should be ~50.
    df = _process([_base_row()])
    scored = scoring.reuse_scores(df)
    assert 0 <= scored["score_cement_replacement"].iloc[0] <= 100


def test_ranking_table_sorted_by_best_score():
    rows = [
        _base_row(mix_id="A", compressive_strength_MPa=60, fly_ash_mass_g=500, cement_mass_g=500),
        _base_row(mix_id="B", compressive_strength_MPa=10, fly_ash_mass_g=100, cement_mass_g=900),
    ]
    df = _process(rows)
    scored = scoring.reuse_scores(df)
    table = scoring.ranking_table(scored)
    scores = table["best_score"].tolist()
    assert scores == sorted(scores, reverse=True)


def test_low_red_mud_preferred():
    rows = [
        _base_row(mix_id="NO_RM", red_mud_mass_g=0, cement_mass_g=700),
        _base_row(mix_id="HI_RM", red_mud_mass_g=300, cement_mass_g=400),
    ]
    df = _process(rows)
    scored = scoring.reuse_scores(df)
    s = dict(zip(scored["mix_id"], scored["sub_low_red_mud"]))
    assert s["NO_RM"] > s["HI_RM"]
