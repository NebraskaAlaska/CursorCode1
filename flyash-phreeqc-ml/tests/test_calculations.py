"""Tests for the calculation registry + residual audit helpers."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from flyash_phreeqc_ml import calculations as calc
from flyash_phreeqc_ml import units


# --------------------------------------------------------------------------- #
# Unit conversions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "element, mass",
    [("Ca", 40.078), ("Si", 28.085), ("Al", 26.982), ("Fe", 55.845)],
)
def test_mgl_to_mM_matches_atomic_mass(element, mass):
    # 100 mg/L of element X -> 100 / atomic_mass mM
    assert calc.mgl_to_mM(100.0, element) == pytest.approx(100.0 / mass)


def test_mgl_to_mM_worked_example_ca():
    # 50 mg/L Ca / 40.078 ≈ 1.2475 mM
    assert calc.mgl_to_mM(50.0, "Ca") == pytest.approx(1.24757, rel=1e-4)


def test_mgl_to_mM_unknown_element_raises():
    # Now a typed error from the single conversion authority (no silent guess).
    with pytest.raises(units.UnknownElementError):
        calc.mgl_to_mM(10.0, "Zz")


def test_apply_dilution():
    assert calc.apply_dilution(5.0, 10.0) == pytest.approx(50.0)


def test_liquid_solid_ratio():
    assert calc.liquid_solid_ratio(100.0, 20.0) == pytest.approx(5.0)


def test_mass_released_mg():
    assert calc.mass_released_mg(50.0, 0.1) == pytest.approx(5.0)


def test_recovery_percent():
    assert calc.recovery_percent(2.0, 8.0) == pytest.approx(25.0)


def test_residual():
    assert calc.residual(12.8, 9.63) == pytest.approx(3.17)


# --------------------------------------------------------------------------- #
# classify / audit_residual
# --------------------------------------------------------------------------- #
def test_classify_pass_within_tol():
    assert calc.classify(3.17, 3.17) == calc.STATUS_PASS


def test_classify_warning_rounding_band():
    # diff = 1e-5, inside warn band (1e-4) but outside pass band (1e-6)
    assert calc.classify(3.170010, 3.170000) == calc.STATUS_WARNING


def test_classify_fail_beyond_tol():
    assert calc.classify(3.17, 2.00) == calc.STATUS_FAIL


def test_classify_not_available_when_missing():
    assert calc.classify(None, 3.17) == calc.STATUS_NA
    assert calc.classify(3.17, None) == calc.STATUS_NA


def test_audit_residual_pass():
    res = calc.audit_residual(measured=12.80, predicted=9.63, stored=3.17)
    assert res["status"] == calc.STATUS_PASS
    assert res["calculated_value"] == pytest.approx(3.17)
    assert res["difference"] == pytest.approx(0.0, abs=1e-9)


def test_audit_residual_fail():
    res = calc.audit_residual(measured=12.80, predicted=9.63, stored=1.00)
    assert res["status"] == calc.STATUS_FAIL


def test_audit_residual_not_available_missing_input():
    res = calc.audit_residual(measured=None, predicted=9.63, stored=3.17)
    assert res["status"] == calc.STATUS_NA
    assert res["calculated_value"] is None


# --------------------------------------------------------------------------- #
# audit_comparison over a frame
# --------------------------------------------------------------------------- #
def test_audit_comparison_pass_and_fail_rows():
    df = pd.DataFrame([
        # row 0: stored matches recomputation -> pass
        {"sample_id": "S1", "final_pH": 12.80, "phreeqc_pH": 9.63, "residual_pH": 3.17},
        # row 1: stored is wrong -> fail
        {"sample_id": "S2", "final_pH": 11.00, "phreeqc_pH": 9.00, "residual_pH": 9.99},
    ])
    audit = calc.audit_comparison(df)
    by_sample = audit.set_index("sample_id")["status"].to_dict()
    assert by_sample["S1"] == calc.STATUS_PASS
    assert by_sample["S2"] == calc.STATUS_FAIL


def test_audit_comparison_not_available_when_input_missing():
    # residual_pH column exists, but phreeqc_pH is blank -> not available
    df = pd.DataFrame([
        {"sample_id": "S1", "final_pH": 12.80, "phreeqc_pH": math.nan, "residual_pH": 3.17},
    ])
    audit = calc.audit_comparison(df)
    assert audit.iloc[0]["status"] == calc.STATUS_NA


def test_audit_comparison_skips_absent_residual_columns():
    # only pH residual present; element residual columns absent -> not audited
    df = pd.DataFrame([
        {"sample_id": "S1", "final_pH": 12.80, "phreeqc_pH": 9.63, "residual_pH": 3.17},
    ])
    audit = calc.audit_comparison(df)
    assert set(audit["formula"]) == {"residual_pH = final_pH - phreeqc_pH"}


def test_audit_comparison_empty_frame_returns_empty_with_columns():
    audit = calc.audit_comparison(pd.DataFrame())
    assert list(audit.columns) == calc.AUDIT_COLUMNS
    assert audit.empty


def test_formula_registry_is_populated_and_well_formed():
    assert len(calc.FORMULAS) >= 8
    for f in calc.FORMULAS:
        assert f.name and f.equation and f.latex and f.output and f.units
        assert f.source in {"app-calculated", "parsed from PHREEQC"}
