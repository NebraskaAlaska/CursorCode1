"""Tests for the single conversion authority (units.py) + conversion provenance.

Pins (per the spec): round-trip conversions against hand-computed values, identity
tagging, unknown-unit / unknown-element refusal (typed, no silent fallback),
verify_conversions catching a deliberately corrupted column, and the provenance
companions surviving save/read through run_manager. Synthetic data only.
"""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml import calculations, config, import_mapping as im, run_manager, units


# --------------------------------------------------------------------------- #
# Round-trip conversions vs hand-computed values
# --------------------------------------------------------------------------- #
def test_mgL_to_mM_hand_computed():
    r = units.convert(100.0, "mg/L", "mM", "Ca")
    assert r.value == pytest.approx(100.0 / 40.078)
    assert r.conversion_id == "mgL_to_mM"
    assert r.molar_mass == pytest.approx(40.078)
    assert "M_Ca" in r.formula


def test_ppm_equals_mgL():
    a = units.convert(100.0, "ppm", "mM", "Si").value
    b = units.convert(100.0, "mg/L", "mM", "Si").value
    assert a == pytest.approx(b) == pytest.approx(100.0 / 28.085)


def test_ppb_is_microgram_per_litre():
    # 1000 ppb (= 1 mg/L) of Ca -> 1/40.078 mM.
    r = units.convert(1000.0, "ppb", "mM", "Ca")
    assert r.value == pytest.approx(1.0 / 40.078)
    assert r.conversion_id == "ppb_to_mM"


def test_molality_to_mM_is_factor_1000_no_element():
    r = units.convert(0.005, "mol/kgw", "mM")
    assert r.value == pytest.approx(5.0)
    assert r.conversion_id == "molality_to_mM"
    assert r.molar_mass is None
    assert units.molality_to_mM(0.005) == pytest.approx(5.0)


def test_config_factor_is_the_one_from_units():
    assert config.PHREEQC_MOLALITY_TO_MM == units.MOLALITY_TO_MM_FACTOR == 1000.0


# --------------------------------------------------------------------------- #
# Identity tagging
# --------------------------------------------------------------------------- #
def test_identity_tagging():
    r = units.convert(5.0, "mM", "mM", "Ca")
    assert r.value == 5.0
    assert r.conversion_id == units.IDENTITY_ID
    assert r.molar_mass is None


# --------------------------------------------------------------------------- #
# Refusals — typed, no silent fallback
# --------------------------------------------------------------------------- #
def test_unknown_unit_refused_with_supported_list():
    with pytest.raises(units.UnknownUnitError) as exc:
        units.convert(1.0, "g/L", "mM", "Ca")
    msg = str(exc.value)
    assert "not recognized for Ca" in msg
    assert "mg/L, ppm, ppb, mM" in msg


def test_unknown_element_refused():
    with pytest.raises(units.UnknownElementError):
        units.convert(1.0, "mg/L", "mM", "Zz")


def test_no_silent_fallback_on_missing_element_for_mass_conversion():
    # mg/L → mM needs a molar mass; element=None must refuse, not pass through.
    with pytest.raises(units.UnknownElementError):
        units.convert(1.0, "mg/L", "mM", None)


# --------------------------------------------------------------------------- #
# Series + companion helpers
# --------------------------------------------------------------------------- #
def test_convert_series_and_identity():
    s = pd.Series([40.078, 80.156, None])
    out, meta = units.convert_series(s, "mg/L", "mM", "Ca")
    assert out.iloc[0] == pytest.approx(1.0)
    assert out.iloc[1] == pytest.approx(2.0)
    assert pd.isna(out.iloc[2])
    assert meta.conversion_id == "mgL_to_mM"


def test_provenance_column_helpers():
    cols = units.provenance_columns_for("Ca_mM")
    assert cols == ["Ca_mM_orig_value", "Ca_mM_orig_unit", "Ca_mM_conversion_id"]
    assert units.is_conversion_provenance_column("Ca_mM_orig_unit")
    assert not units.is_conversion_provenance_column("Ca_mM")


# --------------------------------------------------------------------------- #
# Importer emits provenance companions + identity tagging
# --------------------------------------------------------------------------- #
def test_build_frame_emits_conversion_provenance():
    raw = pd.DataFrame({"sample_id": ["S1"], "Ca": [100.0]})
    mapping = im.suggest_column_mapping(raw.columns)
    out = im.build_schema_frame(raw, mapping, {"Ca_mM": "mg/L"},
                                import_timestamp="2026-06-08T00:00:00")
    assert out.loc[0, "Ca_mM"] == pytest.approx(100.0 / 40.078)
    assert out.loc[0, "Ca_mM_orig_value"] == pytest.approx(100.0)
    assert out.loc[0, "Ca_mM_orig_unit"] == "mg/L"
    assert out.loc[0, "Ca_mM_conversion_id"] == "mgL_to_mM"


def test_build_frame_identity_when_already_mM():
    raw = pd.DataFrame({"sample_id": ["S1"], "Ca": [3.0]})
    mapping = im.suggest_column_mapping(raw.columns)
    out = im.build_schema_frame(raw, mapping, {"Ca_mM": "mM"},
                                import_timestamp="2026-06-08T00:00:00")
    assert out.loc[0, "Ca_mM"] == pytest.approx(3.0)
    assert out.loc[0, "Ca_mM_conversion_id"] == units.IDENTITY_ID


def test_build_frame_refuses_undeclared_unit():
    raw = pd.DataFrame({"sample_id": ["S1"], "Ca": [1.0]})
    mapping = im.suggest_column_mapping(raw.columns)
    with pytest.raises(units.UnknownUnitError):
        im.build_schema_frame(raw, mapping, {"Ca_mM": "g/L"})


# --------------------------------------------------------------------------- #
# verify_conversions — the after-the-fact catch
# --------------------------------------------------------------------------- #
def _converted_frame():
    raw = pd.DataFrame({"sample_id": ["S1", "S2"], "Ca": [100.0, 200.0]})
    mapping = im.suggest_column_mapping(raw.columns)
    return im.build_schema_frame(raw, mapping, {"Ca_mM": "mg/L"},
                                 import_timestamp="2026-06-08T00:00:00")


def test_verify_conversions_passes_on_consistent_data():
    rep = calculations.verify_conversions(_converted_frame())
    ca = rep[rep["column"] == "Ca_mM"].iloc[0]
    assert ca["status"] == calculations.STATUS_PASS
    assert ca["n_pass"] == 2
    assert ca["conversion_id"] == "mgL_to_mM"
    assert ca["molar_mass_used"] == pytest.approx(40.078)


def test_verify_conversions_catches_corrupted_column():
    df = _converted_frame()
    df.loc[0, "Ca_mM"] = 999.0  # stored value no longer matches orig/unit
    rep = calculations.verify_conversions(df)
    ca = rep[rep["column"] == "Ca_mM"].iloc[0]
    assert ca["status"] == calculations.STATUS_FAIL
    assert ca["n_fail"] == 1
    assert ca["n_pass"] == 1


def test_verify_conversions_flags_legacy_without_provenance():
    # A pre-provenance run: Ca_mM has data but no companion columns.
    legacy = pd.DataFrame({"sample_id": ["S1"], "Ca_mM": [2.5]})
    rep = calculations.verify_conversions(legacy)
    ca = rep[rep["column"] == "Ca_mM"].iloc[0]
    assert ca["conversion_id"] == units.LEGACY_ID
    assert ca["status"] == calculations.STATUS_LEGACY


# --------------------------------------------------------------------------- #
# Provenance survives save/read through run_manager
# --------------------------------------------------------------------------- #
def test_provenance_columns_survive_save_and_read(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path)
    run_manager.create_run("units_run", "lab_experiment")
    df = _converted_frame()
    run_manager.save_lab_dataframe("units_run", df)
    back = run_manager.read_data_file("units_run")
    for col in ("Ca_mM_orig_value", "Ca_mM_orig_unit", "Ca_mM_conversion_id"):
        assert col in back.columns
    assert str(back.loc[0, "Ca_mM_conversion_id"]) == "mgL_to_mM"
    # And the re-derivation check still passes on the reloaded run.
    rep = calculations.verify_conversions(back)
    assert rep[rep["column"] == "Ca_mM"].iloc[0]["status"] == calculations.STATUS_PASS
