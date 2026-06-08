"""Unit tests for derived calculations."""

import math

import numpy as np
import pandas as pd
import pytest

from src import calculations, data_loader


def _base_row(**overrides):
    """A minimal valid specimen row dict with sensible defaults."""
    row = {
        "sample_id": "S1", "mix_id": "M1", "specimen_id": "M1-1", "test_id": "T1",
        "fly_ash_mass_g": 300, "cement_mass_g": 700, "water_mass_g": 400,
        "red_mud_mass_g": 0, "sand_mass_g": 2000, "curing_age_days": 28,
        "compressive_strength_MPa": 30.0,
    }
    row.update(overrides)
    return row


def _frame(rows):
    df = pd.DataFrame(rows)
    return data_loader.ensure_expected_columns(data_loader.coerce_types(df))


def test_binder_and_ratios():
    df = _frame([_base_row()])
    out = calculations.add_derived_columns(df)
    assert out["total_binder_mass_g"].iloc[0] == 1000
    assert out["water_binder_ratio"].iloc[0] == pytest.approx(0.4)
    assert out["fly_ash_replacement_percent"].iloc[0] == pytest.approx(30.0)
    assert out["red_mud_percent"].iloc[0] == pytest.approx(0.0)


def test_zero_binder_gives_nan_ratio():
    df = _frame([_base_row(fly_ash_mass_g=0, cement_mass_g=0, red_mud_mass_g=0)])
    out = calculations.add_derived_columns(df)
    assert out["total_binder_mass_g"].iloc[0] == 0
    assert math.isnan(out["water_binder_ratio"].iloc[0])
    assert math.isnan(out["fly_ash_replacement_percent"].iloc[0])


def test_co2_and_cost_saving_use_factors():
    df = _frame([_base_row()])
    factors = {"cement_co2_per_kg": 1.0, "fly_ash_co2_per_kg": 0.0,
               "cement_cost_per_kg": 0.10, "fly_ash_cost_per_kg": 0.0}
    out = calculations.add_derived_columns(df, factors)
    # cement saved = fly_ash + red_mud = 300 g = 0.3 kg
    assert out["estimated_cement_saved_g"].iloc[0] == 300
    assert out["estimated_co2_saving_kg"].iloc[0] == pytest.approx(0.3)  # 0.3 kg * 1.0
    assert out["estimated_cost_saving"].iloc[0] == pytest.approx(0.03)   # 0.3 kg * 0.10


def test_back_calculate_strength_from_load_and_area():
    # 75 kN over 2500 mm^2 = 75000 N / 2500 = 30 MPa
    row = _base_row(compressive_strength_MPa=np.nan,
                    peak_load_kN=75, loaded_area_mm2=2500)
    df = _frame([row])
    out = calculations.add_derived_columns(df)
    assert out["compressive_strength_MPa"].iloc[0] == pytest.approx(30.0)
    assert out["strength_source"].iloc[0] == "calculated"


def test_area_from_cylinder_diameter():
    row = _base_row(compressive_strength_MPa=np.nan, peak_load_kN=100,
                    loaded_area_mm2=np.nan, diameter_mm=100, specimen_shape="cylinder")
    df = _frame([row])
    out = calculations.add_derived_columns(df)
    area = math.pi / 4 * 100 ** 2
    assert out["loaded_area_mm2"].iloc[0] == pytest.approx(area)
    assert out["compressive_strength_MPa"].iloc[0] == pytest.approx(100_000 / area)


def test_reported_strength_is_kept():
    df = _frame([_base_row(compressive_strength_MPa=42.0, peak_load_kN=999)])
    out = calculations.add_derived_columns(df)
    assert out["compressive_strength_MPa"].iloc[0] == 42.0
    assert out["strength_source"].iloc[0] == "reported"


def test_strength_statistics_mean_std_cv():
    rows = [_base_row(specimen_id="M1-1", compressive_strength_MPa=30),
            _base_row(specimen_id="M1-2", compressive_strength_MPa=34)]
    df = calculations.add_derived_columns(_frame(rows))
    stats = calculations.strength_statistics(df)
    assert len(stats) == 1
    assert stats["mean_MPa"].iloc[0] == pytest.approx(32.0)
    assert stats["std_MPa"].iloc[0] == pytest.approx(np.std([30, 34], ddof=1))
    expected_cv = np.std([30, 34], ddof=1) / 32.0 * 100
    assert stats["cv_percent"].iloc[0] == pytest.approx(expected_cv)


def test_cv_nan_for_single_value():
    df = calculations.add_derived_columns(_frame([_base_row()]))
    stats = calculations.strength_statistics(df)
    assert math.isnan(stats["cv_percent"].iloc[0])


def test_infer_data_status():
    rows = [
        _base_row(specimen_id="A", compressive_strength_MPa=30, data_status=None),
        _base_row(specimen_id="B", compressive_strength_MPa=np.nan, data_status=None),
        _base_row(specimen_id="C", compressive_strength_MPa=np.nan, data_status="needs_retest"),
    ]
    df = calculations.add_derived_columns(_frame(rows))
    df = calculations.infer_data_status(df)
    statuses = dict(zip(df["specimen_id"], df["data_status"]))
    assert statuses["A"] == "tested"
    assert statuses["B"] == "pending"
    assert statuses["C"] == "needs_retest"  # explicit value preserved
