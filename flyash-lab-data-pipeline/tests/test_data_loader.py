"""Unit tests for data loading and header normalisation."""

import io

import pandas as pd

from src import calculations, data_loader, scoring, validation


def _csv_bytes(header: str) -> bytes:
    row = "S1,M1,M1-1,T1,28,300,700,400,0,2000,180,31.2,11.4,1200"
    return f"{header}\n{row}\n".encode("utf-8")


def test_mixed_case_headers_map_to_canonical():
    # Regression: mixed-case unit suffixes must survive normalisation so that
    # strength/pH/conductivity are populated, not lost to lower-cased columns.
    header = ("sample_id,mix_id,specimen_id,test_id,curing_age_days,fly_ash_mass_g,"
              "cement_mass_g,water_mass_g,red_mud_mass_g,sand_mass_g,flow_mm,"
              "compressive_strength_MPa,leachate_pH,leachate_conductivity_uS_cm")
    df = data_loader.load_data(io.BytesIO(_csv_bytes(header)), filename="x.csv")
    assert df["compressive_strength_MPa"].iloc[0] == 31.2
    assert df["leachate_pH"].iloc[0] == 11.4
    assert df["leachate_conductivity_uS_cm"].iloc[0] == 1200


def test_messy_headers_still_match():
    # Spacing, capitalisation, and punctuation variations should all resolve.
    header = ("Sample ID,Mix ID,Specimen ID,Test ID,Curing Age Days,Fly Ash Mass g,"
              "Cement Mass g,Water Mass g,Red Mud Mass g,Sand Mass g,Flow mm,"
              "Compressive Strength MPa,Leachate pH,Leachate Conductivity uS cm")
    df = data_loader.load_data(io.BytesIO(_csv_bytes(header)), filename="x.csv")
    assert df["compressive_strength_MPa"].iloc[0] == 31.2
    assert df["leachate_pH"].iloc[0] == 11.4


def test_loaded_data_has_no_spurious_missing_strength():
    header = ("sample_id,mix_id,specimen_id,test_id,curing_age_days,fly_ash_mass_g,"
              "cement_mass_g,water_mass_g,red_mud_mass_g,sand_mass_g,flow_mm,"
              "compressive_strength_MPa,leachate_pH,leachate_conductivity_uS_cm")
    df = data_loader.load_data(io.BytesIO(_csv_bytes(header)), filename="x.csv")
    df = calculations.add_derived_columns(df)
    df = calculations.infer_data_status(df)
    issues = validation.validate(df)
    assert not [it for it in issues if it["code"] == "missing_strength"]
    # leaching risk should be a real number, not NaN, when pH/conductivity present.
    risk = scoring.leaching_risk_score(df.iloc[0])
    assert pd.notna(risk)
