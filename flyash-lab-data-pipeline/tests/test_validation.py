"""Unit tests for data-quality validation rules."""

import numpy as np
import pandas as pd
import pytest

from src import calculations, data_loader, validation


def _base_row(**overrides):
    row = {
        "sample_id": "S1", "mix_id": "M1", "specimen_id": "M1-1", "test_id": "T1",
        "fly_ash_mass_g": 300, "cement_mass_g": 700, "water_mass_g": 400,
        "red_mud_mass_g": 0, "sand_mass_g": 2000, "curing_age_days": 28,
        "compressive_strength_MPa": 30.0, "leachate_pH": 11.0,
        "leachate_conductivity_uS_cm": 1000, "data_status": "tested",
    }
    row.update(overrides)
    return row


def _process(rows):
    df = pd.DataFrame(rows)
    df = data_loader.ensure_expected_columns(data_loader.coerce_types(df))
    df = calculations.add_derived_columns(df)
    df = calculations.infer_data_status(df)
    return df


def _codes(issues):
    return {it["code"] for it in issues}


def test_clean_data_has_no_errors():
    df = _process([_base_row()])
    issues = validation.validate(df)
    assert all(it["severity"] != "error" for it in issues), issues


def test_missing_mix_id_is_error():
    df = _process([_base_row(mix_id=None)])
    issues = validation.validate(df)
    assert "missing_mix_id" in _codes(issues)
    assert any(it["code"] == "missing_mix_id" and it["severity"] == "error" for it in issues)


def test_negative_mass_is_error():
    df = _process([_base_row(cement_mass_g=-100)])
    issues = validation.validate(df)
    assert "negative_mass" in _codes(issues)


def test_zero_binder_is_error():
    df = _process([_base_row(fly_ash_mass_g=0, cement_mass_g=0, red_mud_mass_g=0)])
    issues = validation.validate(df)
    assert "zero_binder" in _codes(issues)


def test_ph_out_of_range_is_error():
    df = _process([_base_row(leachate_pH=15)])
    issues = validation.validate(df)
    assert "ph_out_of_range" in _codes(issues)


def test_wb_ratio_out_of_range_warns():
    df = _process([_base_row(water_mass_g=50)])  # w/b = 0.05, too dry
    issues = validation.validate(df)
    assert "wb_ratio_out_of_range" in _codes(issues)


def test_missing_strength_is_warning_not_error():
    df = _process([_base_row(compressive_strength_MPa=np.nan, peak_load_kN=np.nan,
                             data_status="pending")])
    issues = validation.validate(df)
    strength_issues = [it for it in issues if it["code"] == "missing_strength"]
    assert strength_issues
    assert all(it["severity"] == "warning" for it in strength_issues)


def test_missing_curing_age_warns():
    df = _process([_base_row(curing_age_days=np.nan)])
    issues = validation.validate(df)
    assert "missing_curing_age" in _codes(issues)


def test_duplicate_sample_id_only_warns_when_specimen_distinct():
    rows = [_base_row(sample_id="DUP", specimen_id="A", test_id="TA"),
            _base_row(sample_id="DUP", specimen_id="B", test_id="TB")]
    df = _process(rows)
    issues = validation.validate(df)
    dup = [it for it in issues if it["code"] == "duplicate_sample_id"]
    assert dup
    assert all(it["severity"] == "warning" for it in dup)


def test_duplicate_sample_id_errors_when_specimen_also_dup():
    rows = [_base_row(sample_id="DUP", specimen_id="X", test_id="TX"),
            _base_row(sample_id="DUP", specimen_id="X", test_id="TY")]
    df = _process(rows)
    issues = validation.validate(df)
    assert "duplicate_specimen_id" in _codes(issues)
    dup_sample = [it for it in issues if it["code"] == "duplicate_sample_id"]
    assert any(it["severity"] == "error" for it in dup_sample)


def test_high_cv_warns():
    rows = [_base_row(specimen_id="M1-1", compressive_strength_MPa=10),
            _base_row(specimen_id="M1-2", compressive_strength_MPa=40)]
    df = _process(rows)
    stats = calculations.strength_statistics(df)
    issues = validation.validate(df, stats)
    assert "high_cv" in _codes(issues)


def test_unknown_data_status_warns():
    df = _process([_base_row(data_status="weird")])
    issues = validation.validate(df)
    assert "unknown_data_status" in _codes(issues)


def test_validation_summary_counts():
    df = _process([_base_row(mix_id=None, cement_mass_g=-1)])
    issues = validation.validate(df)
    summary = validation.validation_summary(issues)
    assert summary["errors"] >= 2
    assert summary["total"] == len(issues)
