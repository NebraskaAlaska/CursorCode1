"""Tests for the simulation-plan matrix builder.

The matrix is a *plan only* — every row is ``plan_only`` and no PHREEQC is executed. Covers
single-scenario generation (after confirmation), the documented column set, and the
range-expansion design (Cartesian product over rangeable fields).
"""
from __future__ import annotations

from flyash_phreeqc_ml.simulation import matrix as M
from flyash_phreeqc_ml.simulation import rule_parser
from flyash_phreeqc_ml.simulation import scenario_schema as S


def _scenario():
    return rule_parser.parse(
        "2 g of Class C fly ash in 10 mL of 0.5 M HCl for 60 min; measure Ca, Si, Al, Fe; "
        "simulate the liquid and what may have precipitated.").scenario


def test_matrix_single_row_after_confirmation():
    df = M.build_simulation_matrix(_scenario())
    assert list(df.columns) == M.MATRIX_COLUMNS
    assert len(df) == 1
    row = df.iloc[0]
    assert row["scenario_id"] == "SIM-001"
    assert row["material"] == "Class C fly ash"
    assert row["solid_mass_g"] == 2.0
    assert row["liquid_volume_mL"] == 10.0
    assert row["liquid_solid_ratio"] == 5.0
    assert row["leachant_type"] == "HCl"
    assert row["leachant_concentration_M"] == 0.5
    assert row["time_min"] == 60.0
    assert row["target_elements"] == "Ca, Si, Al, Fe"
    assert "precipitated_phases" in row["desired_outputs"]
    assert row["status"] == M.STATUS_PLAN_ONLY


def test_matrix_status_is_plan_only_label():
    # The status column flags the plan, and the schema exposes the human label.
    df = M.build_simulation_matrix(_scenario())
    assert (df["status"] == "plan_only").all()
    assert "no PHREEQC result" in S.PLAN_ONLY_LABEL


def test_matrix_range_expansion_cartesian():
    sc = _scenario()
    df = M.build_simulation_matrix(
        sc, ranges={"leachant_concentration_M": [0.1, 0.5], "time_min": [30, 60]})
    assert len(df) == 4                                  # 2 × 2
    assert list(df["scenario_id"]) == ["SIM-001", "SIM-002", "SIM-003", "SIM-004"]
    assert set(df["leachant_concentration_M"]) == {0.1, 0.5}
    assert set(df["time_min"]) == {30, 60}
    # Non-swept fields are constant across the matrix.
    assert set(df["solid_mass_g"]) == {2.0}
    assert (df["status"] == M.STATUS_PLAN_ONLY).all()


def test_matrix_ignores_unknown_range_fields():
    df = M.build_simulation_matrix(_scenario(), ranges={"not_a_field": [1, 2, 3]})
    assert len(df) == 1                                  # unknown range key is ignored


def test_matrix_handles_empty_scenario():
    df = M.build_simulation_matrix(S.SimulationScenario())
    assert len(df) == 1
    assert df.iloc[0]["material"] == "unspecified"
    assert df.iloc[0]["status"] == M.STATUS_PLAN_ONLY
