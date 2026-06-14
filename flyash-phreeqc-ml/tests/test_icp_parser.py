"""Tests for the CFA+MK mix-design ICP parser (parsers/icp_parser.py).

Runs against a **synthetic** mix-design workbook that mirrors the real file's sheet /
block structure (so no confidential research data is needed), plus one optional test
that runs against the real workbook only if it happens to exist locally.
"""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.parsers import icp_parser
from tests.fixtures.synthetic.mix_design_workbook import (
    MIX_DESIGN_VALUES, write_mix_design_workbook)

REAL_WORKBOOK = config.ICP_DIR / "CFA + MK design mix_UMass.xlsx"


@pytest.fixture()
def mix_design(tmp_path):
    return write_mix_design_workbook(tmp_path / "mix_design.xlsx")


def test_extract_oxide_tables_from_synthetic(mix_design):
    df = icp_parser.extract_oxide_tables(mix_design)
    assert not df.empty
    assert set(df["material"]) == {"CFA", "MK"}
    # Values are matched to oxides by column index — check a few against the synthetic data.
    by = {(r["material"], r["oxide"]): r["weight_pct"] for _, r in df.iterrows()}
    assert by[("CFA", "SiO2")] == pytest.approx(MIX_DESIGN_VALUES["CFA"]["SiO2"])
    assert by[("CFA", "CaO")] == pytest.approx(MIX_DESIGN_VALUES["CFA"]["CaO"])
    assert by[("MK", "Al2O3")] == pytest.approx(MIX_DESIGN_VALUES["MK"]["Al2O3"])


def test_parse_icp_workbook_dumps_and_extracts(mix_design, tmp_path):
    out_dir = tmp_path / "processed"
    result = icp_parser.parse_icp_workbook(mix_design, out_dir)
    assert "oxide_compositions" in result
    assert not result["oxide_compositions"].empty
    # The raw, header-less dump is the authoritative record.
    dumps = list(out_dir.glob("icp_raw_*.csv"))
    assert dumps, "parse_icp_workbook must dump each sheet to a raw CSV"


@pytest.mark.skipif(not REAL_WORKBOOK.exists(),
                    reason="real mix-design workbook not present locally (optional)")
def test_real_workbook_if_present():
    # Only runs when the approved real workbook is on disk; skipped in CI / clean clones.
    df = icp_parser.extract_oxide_tables(REAL_WORKBOOK)
    assert isinstance(df, pd.DataFrame)
    # The real file has the known oxide-composition blocks; the parser should find some.
    assert not df.empty
    assert {"material", "oxide", "weight_pct"}.issubset(df.columns)
