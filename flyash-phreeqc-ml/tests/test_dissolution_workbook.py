"""Tests for the Class C fly ash dissolution-workbook parser.

The synthetic workbook mirrors the **real** file's layout: the ICP OES sheet lays
unit groups out *horizontally* — a single top row holds ``mg/L`` and ``mmol/l``,
each anchoring a group of condition columns (``NaOH-OA/PF/GS``) — and each element
block (Calcium / Silicon / Aluminum) has one shared header row spanning both unit
groups, with reaction-time rows. The pH sheet has a ``Sample | Time (min) | pH``
label list (incl. HCl rows) plus a NaOH pH matrix. The tests pin: horizontal block
parsing, mmol/l preference, mg/L fallback conversion, ``-`` treated as missing,
pH read from the pH column (not Time), join by (condition, time), and HCl kept
separate.
"""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml import dissolution_workbook as dw
from flyash_phreeqc_ml.calculations import ATOMIC_MASSES

TS = "2026-06-08T00:00:00"
N = None  # blank cell


@pytest.fixture()
def workbook(tmp_path):
    """Write a synthetic dissolution workbook (real horizontal layout)."""
    hdr = [N, "Time", N, "NaOH-OA", "NaOH-PF", "NaOH-GS", N, "NaOH-OA", "NaOH-PF", "NaOH-GS"]
    icp = [
        [N, N, N, "mg/L", N, N, N, "mmol/l", N, N],            # global unit row
        ["Calcium", N, N, N, N, N, N, N, N, N],
        hdr,
        [N, 10, N, 93.43, 5.74, 6.87, N, 2.5, 2.1, 1.8],       # mmol preferred over mg
        [N, 60, N, 7.82, 11.66, 4.185, N, 3.0, 2.6, 2.2],
        [N, 720, N, 20.25, "-", "-", N, 0.5, "-", "-"],        # PF/GS missing at 720
        [N] * 10,
        ["Silicon", N, N, N, N, N, N, N, N, N],
        hdr,
        [N, 10, N, 8.64, 20.31, 18.7, N, 1.2, 1.0, 0.8],
        [N, 60, N, 51.76, 43.36, 32.81, N, 1.5, 1.3, 1.1],
        [N] * 10,
        ["Aluminum", N, N, N, N, N, N, N, N, N],
        hdr,
        [N, 10, N, 54.0, 79.5, 71.0, N, "-", 2.9, 2.6],        # OA mmol missing -> mg fallback
        [N, 60, N, 60.0, 91.9, 69.9, N, 4.8, 3.4, 2.5],
    ]
    ph = [
        ["Sample", "Time (min)", "pH", N, N, N, N, "pH", N, N],
        ["0.5M NaOH-OA-10", 10, 13.1, N, N, "Time", N, "NaOH-OA", "NaOH-PF", "NaOH-GS"],
        ["0.5M NaOH-OA-60", 60, 13.05, N, N, 10, N, 13.1, 14, 14],
        ["0.5M NaOH-OA-720", 720, 12.7, N, N, 20, N, 13.15, 14, 13.99],   # 20-min only in matrix
        ["0.5M NaOH-PF-10", 10, 14, N, N, 60, N, 13.05, 13.88, 13.89],
        ["0.5M NaOH-PF-60", 60, 13.88, N, N, N, N, N, N, N],
        ["0.5M NaOH-PF-720", 720, "-", N, N, N, N, N, N, N],             # pH "-" -> blank
        ["0.5M NaOH-GS-10", 10, 14, N, N, N, N, N, N, N],
        ["0.5M NaOH-GS-60", 60, 13.89, N, N, N, N, N, N, N],
        ["0.5M NaOH-GS-720", 720, "-", N, N, N, N, N, N, N],
        ["0.5M HCL-OA-10", 10, 3.21, N, N, N, N, N, N, N],
    ]
    path = tmp_path / "dissolution.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame(icp).to_excel(writer, sheet_name="ICP OES", index=False, header=False)
        pd.DataFrame(ph).to_excel(writer, sheet_name="pH", index=False, header=False)
    return path


def _row(df, needle):
    return df[df["sample_id"].str.contains(needle)].iloc[0]


# --------------------------------------------------------------------------- #
# Sheet location
# --------------------------------------------------------------------------- #
def test_find_workbook_sheets(workbook):
    assert dw.find_workbook_sheets(pd.ExcelFile(workbook)) == ("ICP OES", "pH")


def test_find_workbook_sheets_missing_raises(tmp_path):
    path = tmp_path / "bad.xlsx"
    pd.DataFrame({"a": [1]}).to_excel(path, sheet_name="Sheet1", index=False)
    with pytest.raises(dw.DissolutionWorkbookError):
        dw.find_workbook_sheets(pd.ExcelFile(path))


# --------------------------------------------------------------------------- #
# Horizontal ICP parsing
# --------------------------------------------------------------------------- #
def test_unit_column_map_splits_groups(workbook):
    grid = pd.ExcelFile(workbook).parse("ICP OES", header=None)
    cmap = dw._unit_column_map(grid.values, *grid.shape)
    assert cmap[3] == "mg" and cmap[4] == "mg" and cmap[5] == "mg"
    assert cmap[7] == "mmol" and cmap[8] == "mmol" and cmap[9] == "mmol"


def test_parse_icp_blocks_prefers_mmol_and_falls_back_to_mg(workbook):
    long = dw.parse_icp_sheet(pd.ExcelFile(workbook).parse("ICP OES", header=None))
    assert set(long["element_col"]) == {"Ca_mM", "Si_mM", "Al_mM"}

    ca = long[(long.element_col == "Ca_mM") & (long.condition_code == "OA") & (long.time_min == 10)].iloc[0]
    assert ca["value_mM"] == 2.5 and ca["unit_source"] == "mmol"   # mmol wins over 93.43 mg/L

    al = long[(long.element_col == "Al_mM") & (long.condition_code == "OA") & (long.time_min == 10)].iloc[0]
    assert al["unit_source"] == "mg"                               # mmol was "-"
    assert al["value_mM"] == pytest.approx(54.0 / ATOMIC_MASSES["Al"])

    # "-" cells are missing: PF/GS have no 720-min Calcium point.
    ca720 = long[(long.element_col == "Ca_mM") & (long.time_min == 720)]
    assert set(ca720["condition_code"]) == {"OA"}


def test_pH_read_from_pH_column_not_time(workbook):
    rows = dw.parse_ph_sheet(pd.ExcelFile(workbook).parse("pH", header=None))
    oa10 = [r for r in rows if r["leachant"] == "NaOH" and r["condition_code"] == "OA"
            and r["time_min"] == 10][0]
    assert oa10["final_pH"] == 13.1                                # NOT 10 (the Time value)


# --------------------------------------------------------------------------- #
# Full normalisation + join
# --------------------------------------------------------------------------- #
def test_normalize_joins_chemistry_to_naoh_rows(workbook):
    df, report = dw.normalize_dissolution_workbook(
        workbook, defaults={}, include_hcl=True, filename="dissolution.xlsx", import_timestamp=TS,
    )
    assert report["n_hcl"] == 1
    assert report["n_with_chem"] >= 7              # chem now populates (regression: was 0)

    oa10 = _row(df, "NaOH-OA-10min")
    assert oa10["Ca_mM"] == 2.5                     # mmol preferred
    assert oa10["Si_mM"] == 1.2
    assert oa10["Al_mM"] == pytest.approx(54.0 / ATOMIC_MASSES["Al"])  # mg/L fallback
    assert oa10["final_pH"] == 13.1                 # correct pH, not Time
    assert str(oa10["NaOH_M"]) == "0.5"

    oa720 = _row(df, "NaOH-OA-720min")
    assert oa720["Ca_mM"] == 0.5
    assert dw._is_blank(oa720["Si_mM"])             # no 720-min Silicon point
    assert oa720["final_pH"] == 12.7


def test_twenty_minute_row_comes_from_matrix(workbook):
    df, _ = dw.normalize_dissolution_workbook(workbook, import_timestamp=TS)
    oa20 = _row(df, "NaOH-OA-20min")                # only present in the pH matrix
    assert oa20["final_pH"] == 13.15
    assert dw._is_blank(oa20["Ca_mM"])              # no ICP point at 20 min


def test_missing_ph_dash_is_blank_not_value(workbook):
    df, _ = dw.normalize_dissolution_workbook(workbook, import_timestamp=TS)
    pf720 = _row(df, "NaOH-PF-720min")
    assert dw._is_blank(pf720["final_pH"])          # "-" -> blank, never a number


def test_hcl_kept_separate(workbook):
    df, _ = dw.normalize_dissolution_workbook(workbook, include_hcl=True, import_timestamp=TS)
    acid = df[df["leachant"] == "HCl"].iloc[0]
    assert acid["final_pH"] == 3.21
    assert dw._is_blank(acid["NaOH_M"])             # never forced into NaOH_M
    assert str(acid["acid_M"]) == "0.5"
    assert dw._is_blank(acid["Ca_mM"])              # HCl has no NaOH ICP chemistry
    assert dw.im.ACID_IMPORT_NOTE in acid["import_warning"]


def test_can_exclude_hcl(workbook):
    df, report = dw.normalize_dissolution_workbook(workbook, include_hcl=False, import_timestamp=TS)
    assert report["n_hcl"] == 0
    assert (df["leachant"] == "HCl").sum() == 0


def test_condition_code_preserved(workbook):
    df, _ = dw.normalize_dissolution_workbook(workbook, import_timestamp=TS)
    for _, row in df.iterrows():
        code = row["extra__condition_code"]
        assert code in ("OA", "PF", "GS")
        assert code in row["sample_id"]
        assert f"condition_code={code}" in row["notes"]


def test_debug_pivots_present(workbook):
    _, report = dw.normalize_dissolution_workbook(workbook, import_timestamp=TS)
    assert set(report["icp_debug"]) == {"Ca_mM", "Si_mM", "Al_mM"}
    ca = report["icp_debug"]["Ca_mM"]
    assert "time_min" in ca.columns and "OA" in ca.columns


def test_defaults_fill_metadata(workbook):
    defaults = {
        "experiment_date": "2026-06-08", "temperature_C": "25", "liquid_solid_ratio": "20",
        "CO2_condition": "sealed", "initial_pH": "13.5", "fly_ash_type": "Class C fly ash",
    }
    df, _ = dw.normalize_dissolution_workbook(workbook, defaults=defaults, import_timestamp=TS)
    assert (df["temperature_C"] == "25").all()
    assert (df["CO2_condition"] == "sealed").all()
    assert (df["fly_ash_type"] == "Class C fly ash").all()
