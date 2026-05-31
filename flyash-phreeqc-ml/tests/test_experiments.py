"""Tests for the experiment-planning + QA/QC tools (Feature 6).

Covers sample-id generation, de-duplication (replicates preserved), the validator's
error detection (missing required columns, impossible pH), and that the
sustainability score survives missing REE data.
"""
from __future__ import annotations

import pandas as pd

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.experiments import (
    build_experiment_plan,
    compute_sustainability_scores,
    make_sample_id,
    validate_experimental_df,
)


# --------------------------------------------------------------------------- #
# Plan generator
# --------------------------------------------------------------------------- #
def test_sample_id_format():
    sid = make_sample_id(
        naoh_m=0.5, liquid_solid_ratio=5, time_min=10, co2_condition="open", replicate=1
    )
    assert sid == "CFA-NaOH0.5M-LS5-10min-open-R1"


def test_sample_id_compact_numbers():
    # 1.0 -> "1", 0 -> "0" so ids stay short and stable.
    assert make_sample_id(
        naoh_m=1.0, liquid_solid_ratio=5, time_min=60, co2_condition="open", replicate=2
    ) == "CFA-NaOH1M-LS5-60min-open-R2"
    assert make_sample_id(
        naoh_m=0, liquid_solid_ratio=5, time_min=60, co2_condition="open", replicate=1
    ) == "CFA-NaOH0M-LS5-60min-open-R1"


def test_plan_has_no_duplicate_sample_ids():
    df = build_experiment_plan()
    assert df["sample_id"].is_unique


def test_plan_keeps_distinct_replicates():
    df = build_experiment_plan()
    base = "CFA-NaOH0.5M-LS5-60min-open"
    # The replicate-check set must contribute R1, R2, R3 as separate rows.
    for r in (1, 2, 3):
        assert f"{base}-R{r}" in set(df["sample_id"])


def test_plan_columns_match_spec():
    df = build_experiment_plan()
    assert list(df.columns)[:3] == ["sample_id", "experiment_set", "replicate"]
    # Plan reuses the canonical release column name (not "flash_type").
    assert "fly_ash_type" in df.columns
    assert "flash_type" not in df.columns


# --------------------------------------------------------------------------- #
# Validator
# --------------------------------------------------------------------------- #
def _severities(report: list[dict]) -> set[str]:
    return {r["severity"] for r in report}


def _checks(report: list[dict]) -> set[str]:
    return {r["check"] for r in report}


def test_validator_catches_missing_required_columns():
    df = pd.DataFrame({"sample_id": ["S1"], "NaOH_M": [0.5]})  # almost everything missing
    report = validate_experimental_df(df)
    assert "required_columns" in _checks(report)
    assert "error" in _severities(report)


def test_validator_catches_impossible_pH():
    # Full schema present so the only error is the pH range.
    row = {c: "" for c in config.EXPERIMENTAL_RELEASE_COLUMNS}
    row.update({"sample_id": "S1", "NaOH_M": "0.5", "time_min": "60",
                "liquid_solid_ratio": "5", "CO2_condition": "open",
                "final_pH": "17", "notes": "dilution 10x"})
    report = validate_experimental_df(pd.DataFrame([row]))
    ph_issues = [r for r in report if r["check"] == "pH_range"]
    assert ph_issues and ph_issues[0]["severity"] == "error"
    assert "required_columns" not in _checks(report)  # schema was complete


def test_validator_flags_unknown_co2_and_negative_concentration():
    row = {c: "" for c in config.EXPERIMENTAL_RELEASE_COLUMNS}
    row.update({"sample_id": "S1", "NaOH_M": "0.5", "time_min": "60",
                "liquid_solid_ratio": "5", "CO2_condition": "vacuum", "Ca_mM": "-2"})
    report = validate_experimental_df(pd.DataFrame([row]))
    checks = _checks(report)
    assert "CO2_condition_vocab" in checks
    assert "concentration_nonnegative" in checks


def test_validator_clean_row_passes():
    row = {c: "" for c in config.EXPERIMENTAL_RELEASE_COLUMNS}
    row.update({"sample_id": "S1", "NaOH_M": "0.5", "time_min": "60",
                "temperature_C": "25", "liquid_solid_ratio": "5",
                "CO2_condition": "open", "final_pH": "12.5", "Ca_mM": "1.2",
                "notes": "dilution factor 10"})
    report = validate_experimental_df(pd.DataFrame([row]))
    assert "error" not in _severities(report)


# --------------------------------------------------------------------------- #
# Sustainability score
# --------------------------------------------------------------------------- #
def test_sustainability_handles_missing_REE_without_crashing():
    # No Sc/REE columns at all, and a zero-bulk row -> no exception, NaN proxies.
    df = pd.DataFrame(
        [
            {"sample_id": "S1", "NaOH_M": 0.5, "time_min": 60,
             "Ca_mM": 1.0, "Si_mM": 2.0, "Al_mM": 0.5, "Fe_mM": 0.1},
            {"sample_id": "S2", "NaOH_M": 0.0, "time_min": 60},  # nothing dissolved/measured
        ]
    )
    scores = compute_sustainability_scores(df)
    assert list(scores.columns)
    assert scores.loc[0, "total_bulk_dissolved_mM"] == 3.6
    assert scores.loc[0, "NaOH_time_intensity"] == 0.5 * 60
    # REE column absent -> proxy is NaN, not an error.
    assert pd.isna(scores.loc[0, "REE_selectivity_proxy"])
    # Row with no bulk measured -> NaN (no divide-by-zero blow-up).
    assert pd.isna(scores.loc[1, "total_bulk_dissolved_mM"])
    # Missing-data penalty counts absent/blank required fields.
    assert scores.loc[1, "penalty_missing_data"] >= scores.loc[0, "penalty_missing_data"]


def test_sustainability_selectivity_proxy():
    df = pd.DataFrame(
        [{"sample_id": "S1", "NaOH_M": 0.5, "time_min": 60,
          "Ca_mM": 1.0, "Si_mM": 1.0, "Al_mM": 0.0, "Fe_mM": 0.0,
          "Sc_ppb": 50.0, "total_REE_ppb": 200.0}]
    )
    scores = compute_sustainability_scores(df)
    assert scores.loc[0, "total_bulk_dissolved_mM"] == 2.0
    assert scores.loc[0, "REE_selectivity_proxy"] == 100.0   # 200 / 2
    assert scores.loc[0, "Sc_selectivity_proxy"] == 25.0     # 50 / 2
