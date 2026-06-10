"""Tests for the flexible experimental-file import helper.

Covers the contract the Data-tab import flow relies on:
* CSV and simple-workbook Excel both read into a DataFrame,
* sheet listing works for an Excel file,
* fuzzy column-mapping suggestions resolve the documented aliases,
* mg/L (and ppb) → mM conversion uses the calculations atomic masses,
* unknown extra columns are preserved (never dropped silently),
* acid/HCl leachant rows are not forced into NaOH_M, and
* missing required fields are reported before saving.
"""
from __future__ import annotations

import io

import pandas as pd
import pytest

from flyash_phreeqc_ml import import_mapping as im
from flyash_phreeqc_ml.calculations import ATOMIC_MASSES


# --------------------------------------------------------------------------- #
# File reading + sheets
# --------------------------------------------------------------------------- #
def test_file_kind_resolves_extensions():
    assert im.file_kind("data.csv") == "csv"
    assert im.file_kind("Book1.xlsx") == "excel"
    assert im.file_kind("legacy.xls") == "excel"
    with pytest.raises(im.ImportMappingError):
        im.file_kind("notes.txt")


def test_csv_import_still_works(tmp_path):
    path = tmp_path / "lab.csv"
    pd.DataFrame({"sample_id": ["S1", "S2"], "pH": [12.9, 13.1]}).to_csv(path, index=False)
    df = im.read_tabular(path, kind="csv")
    assert list(df.columns) == ["sample_id", "pH"]
    assert len(df) == 2


def test_excel_import_from_simple_workbook(tmp_path):
    path = tmp_path / "book.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({"sample_id": ["S1"], "pH": [13.0]}).to_excel(
            writer, sheet_name="NaOH_run", index=False
        )
        pd.DataFrame({"sample_id": ["A1"], "pH": [2.0]}).to_excel(
            writer, sheet_name="HCl_run", index=False
        )

    sheets = im.list_excel_sheets(path)
    assert sheets == ["NaOH_run", "HCl_run"]

    df = im.read_tabular(path, kind="excel", sheet="HCl_run")
    assert df.loc[0, "sample_id"] == "A1"
    assert df.loc[0, "pH"] == 2.0


def test_read_tabular_from_bytes_buffer():
    raw = b"sample_id,pH\nS1,13.0\n"
    df = im.read_tabular(io.BytesIO(raw), kind="csv")
    assert df.loc[0, "sample_id"] == "S1"


# --------------------------------------------------------------------------- #
# Fuzzy mapping suggestions
# --------------------------------------------------------------------------- #
def test_fuzzy_mapping_suggestions_resolve_documented_aliases():
    uploaded = ["pH", "initial pH", "Ca", "Si", "Al", "Fe",
                "NaOH", "reaction time", "L/S"]
    mapping = im.suggest_column_mapping(uploaded)
    assert mapping["final_pH"] == "pH"
    assert mapping["initial_pH"] == "initial pH"
    assert mapping["Ca_mM"] == "Ca"
    assert mapping["Si_mM"] == "Si"
    assert mapping["Al_mM"] == "Al"
    assert mapping["Fe_mM"] == "Fe"
    assert mapping["NaOH_M"] == "NaOH"
    assert mapping["time_min"] == "reaction time"
    assert mapping["liquid_solid_ratio"] == "L/S"


def test_fuzzy_mapping_prefers_exact_name_and_is_one_to_one():
    # An exact canonical name wins; each source column used at most once.
    mapping = im.suggest_column_mapping(["final_pH", "pH", "aluminium"])
    assert mapping["final_pH"] == "final_pH"          # exact match wins
    assert mapping["Al_mM"] == "aluminium"
    used = [v for v in mapping.values() if v is not None]
    assert len(used) == len(set(used))                # no source reused


def test_unmapped_columns_are_reported():
    raw = pd.DataFrame(columns=["sample_id", "pH", "weird_lab_field"])
    mapping = im.suggest_column_mapping(raw.columns)
    assert im.unmapped_columns(raw, mapping) == ["weird_lab_field"]


# --------------------------------------------------------------------------- #
# Unit conversion
# --------------------------------------------------------------------------- #
def test_mgl_to_mM_conversion_for_each_element():
    for el in ("Ca", "Si", "Al", "Fe"):
        col = f"{el}_mM"
        got = im.convert_concentration(100.0, el, "mg/L")
        assert got == pytest.approx(100.0 / ATOMIC_MASSES[el])
        # ppm is treated the same as mg/L.
        assert im.convert_concentration(100.0, el, "ppm") == pytest.approx(got)


def test_ppb_conversion_is_microgram_per_litre():
    # 1000 ppb (= 1 mg/L) of Ca -> 1/40.078 mM.
    assert im.convert_concentration(1000.0, "Ca", "ppb") == pytest.approx(1.0 / ATOMIC_MASSES["Ca"])


def test_mM_passthrough_is_identity():
    assert im.convert_concentration(5.0, "Ca", "mM") == 5.0


def test_build_frame_converts_mgL_columns_to_mM():
    raw = pd.DataFrame({"sample_id": ["S1"], "Ca": [100.0], "Si": [56.17]})
    mapping = im.suggest_column_mapping(raw.columns)
    out = im.build_schema_frame(
        raw, mapping, {"Ca_mM": "mg/L", "Si_mM": "mg/L"},
        filename="lab.csv", import_timestamp="2026-06-08T00:00:00",
    )
    assert out.loc[0, "Ca_mM"] == pytest.approx(100.0 / ATOMIC_MASSES["Ca"])
    assert out.loc[0, "Si_mM"] == pytest.approx(56.17 / ATOMIC_MASSES["Si"])
    assert out.loc[0, "units_assumed"] == "Ca_mM=mg/L; Si_mM=mg/L"


# --------------------------------------------------------------------------- #
# Provenance + extra columns
# --------------------------------------------------------------------------- #
def test_build_frame_preserves_extra_columns_and_provenance():
    raw = pd.DataFrame({"sample_id": ["S1"], "pH": [13.0], "operator": ["JD"]})
    mapping = im.suggest_column_mapping(raw.columns)
    out = im.build_schema_frame(
        raw, mapping, {}, filename="lab.csv", sheet_name="Sheet1",
        import_timestamp="2026-06-08T00:00:00",
    )
    assert "extra__operator" in out.columns
    assert out.loc[0, "extra__operator"] == "JD"
    assert out.loc[0, "original_file_name"] == "lab.csv"
    assert out.loc[0, "original_sheet_name"] == "Sheet1"
    assert out.loc[0, "original_row_number"] == 2
    assert out.loc[0, "final_pH"] == 13.0


# --------------------------------------------------------------------------- #
# Acid / leachant handling
# --------------------------------------------------------------------------- #
def test_is_acid_leachant():
    assert im.is_acid_leachant("HCl")
    assert im.is_acid_leachant("1M HCl acid")
    assert not im.is_acid_leachant("NaOH")
    assert not im.is_acid_leachant("")


def test_hcl_rows_not_forced_into_naoh_m():
    raw = pd.DataFrame({
        "sample_id": ["B1", "A1"],
        "leachant": ["NaOH", "HCl"],
        "NaOH_M": [4.0, 4.0],          # acid row wrongly carries a NaOH value
        "pH": [13.0, 2.0],
    })
    mapping = im.suggest_column_mapping(raw.columns)
    out = im.build_schema_frame(raw, mapping, {}, import_timestamp="2026-06-08T00:00:00")
    base = out[out["leachant"] == "NaOH"].iloc[0]
    acid = out[out["leachant"] == "HCl"].iloc[0]
    assert str(base["NaOH_M"]) == "4.0"
    assert im._is_blank(acid["NaOH_M"])               # acid row's NaOH_M blanked
    assert acid["import_warning"] == im.ACID_IMPORT_NOTE


def test_default_leachant_applies_when_unmapped():
    raw = pd.DataFrame({"sample_id": ["S1"], "pH": [13.0]})
    mapping = im.suggest_column_mapping(raw.columns)
    out = im.build_schema_frame(
        raw, mapping, {}, default_leachant="NaOH", import_timestamp="2026-06-08T00:00:00",
    )
    assert out.loc[0, "leachant"] == "NaOH"


# --------------------------------------------------------------------------- #
# Pre-save validation
# --------------------------------------------------------------------------- #
def test_summarize_import_reports_missing_required_fields():
    # Only sample_id + pH mapped: required metadata columns are blank.
    raw = pd.DataFrame({"sample_id": ["S1", "S2"], "pH": [13.0, 12.8]})
    mapping = im.suggest_column_mapping(raw.columns)
    out = im.build_schema_frame(raw, mapping, {}, import_timestamp="2026-06-08T00:00:00")
    report = im.summarize_import(out)
    assert report["n_rows"] == 2
    # All schema columns exist (build_schema_frame creates them), but rows are
    # missing required values like experiment_date / fly_ash_type.
    assert report["missing_required_columns"] == []
    assert report["rows_missing_required"] == 2
    assert report["classifications"].get("pH-only") == 2


def test_summarize_import_flags_ph_range_blank_and_duplicate_ids():
    out = im.build_schema_frame(
        pd.DataFrame({
            "sample_id": ["S1", "S1", ""],
            "final_pH": [13.0, 99.0, 7.0],
        }),
        {"sample_id": "sample_id", "final_pH": "final_pH"},
        {}, import_timestamp="2026-06-08T00:00:00",
    )
    report = im.summarize_import(out)
    assert any(p["value"] == 99.0 for p in report["ph_out_of_range"])
    assert report["blank_sample_ids"] == 1
    assert "S1" in report["duplicate_sample_ids"]


def test_summarize_import_counts_rows_with_no_measured_values():
    out = im.build_schema_frame(
        pd.DataFrame({"sample_id": ["S1"], "fly_ash_type": ["Class C"]}),
        {"sample_id": "sample_id", "fly_ash_type": "fly_ash_type"},
        {}, import_timestamp="2026-06-08T00:00:00",
    )
    report = im.summarize_import(out)
    assert report["rows_no_measured_values"] == 1
    assert report["classifications"].get("incomplete") == 1


# --------------------------------------------------------------------------- #
# Legacy CO2 vocabulary migration (cup covers)
# --------------------------------------------------------------------------- #
def test_legacy_co2_open_maps_to_oa():
    raw = pd.DataFrame({"sample_id": ["S1"], "CO2": ["open"]})
    out = im.build_schema_frame(
        raw, {"sample_id": "sample_id", "CO2_condition": "CO2"}, {},
        import_timestamp="2026-06-08T00:00:00",
    )
    assert out.loc[0, "CO2_condition"] == "OA"
    assert im.CO2_OPEN_TO_OA_NOTE in out.loc[0, "import_warning"]


def test_legacy_co2_sealed_is_flagged_not_mapped():
    # 'sealed' is ambiguous (PF vs GS unknown): never auto-mapped — left as-is + flagged.
    raw = pd.DataFrame({"sample_id": ["S1"], "CO2": ["sealed"]})
    out = im.build_schema_frame(
        raw, {"sample_id": "sample_id", "CO2_condition": "CO2"}, {},
        import_timestamp="2026-06-08T00:00:00",
    )
    assert out.loc[0, "CO2_condition"] == "sealed"        # not silently mapped to PF/GS
    assert im.CO2_SEALED_AMBIGUOUS_NOTE in out.loc[0, "import_warning"]
    report = im.summarize_import(out)
    assert report["co2_unresolved"] == 1                  # surfaced for the user to resolve


def test_cup_cover_co2_values_pass_through():
    raw = pd.DataFrame({"sample_id": ["S1", "S2"], "CO2": ["PF", "GS"]})
    out = im.build_schema_frame(
        raw, {"sample_id": "sample_id", "CO2_condition": "CO2"}, {},
        import_timestamp="2026-06-08T00:00:00",
    )
    assert list(out["CO2_condition"]) == ["PF", "GS"]
    assert im.summarize_import(out)["co2_unresolved"] == 0
