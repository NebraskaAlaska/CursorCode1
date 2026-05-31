"""Tests for Phase-2 experimental-release template ingestion.

These cover the contract the Monday lab data must satisfy:
* the shipped template header matches the canonical schema,
* a correctly-filled file parses with the right columns, dtypes, and values,
* numeric junk and missing columns are handled predictably,
* the "is there real measured data yet?" detector behaves, and
* directory loading skips the blank template.
"""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.parsers import (
    ExperimentalSchemaError,
    has_measured_data,
    load_experimental_release,
    parse_experimental_release,
)

# A minimal, valid two-row sample matching the template schema.
_ROW_1 = {
    "sample_id": "S001",
    "experiment_date": "2026-06-01",
    "fly_ash_type": "CFA",
    "NaOH_M": "4.0",
    "time_min": "60",
    "temperature_C": "25",
    "liquid_solid_ratio": "10",
    "CO2_condition": "atm_CO2",
    "initial_pH": "13.1",
    "final_pH": "9.6",
    "conductivity_mS_cm": "45.2",
    "Ca_mM": "0.12",
    "Si_mM": "1.8",
    "Al_mM": "0.9",
    "Fe_mM": "0.02",
    "Na_mM": "500",
    "K_mM": "3.1",
    "Sc_ppb": "12.5",
    "total_REE_ppb": "88.0",
    "filtration_notes": "0.45um",
    "precipitate_observed": "yes",
    "notes": "baseline run",
}
_ROW_2 = {**_ROW_1, "sample_id": "S002", "Ca_mM": "0.20", "final_pH": "10.1"}


def _write_csv(path, rows) -> None:
    pd.DataFrame(rows, columns=config.EXPERIMENTAL_RELEASE_COLUMNS).to_csv(path, index=False)


# --------------------------------------------------------------------------- #
# Shipped template
# --------------------------------------------------------------------------- #
def test_template_exists_and_header_matches_schema():
    path = config.EXPERIMENTAL_ICP_DIR / config.EXPERIMENTAL_TEMPLATE_CSV
    assert path.exists(), "the experimental template CSV should be shipped in the repo"
    header = pd.read_csv(path, nrows=0).columns.tolist()
    assert header == config.EXPERIMENTAL_RELEASE_COLUMNS


def test_template_has_no_data_rows():
    path = config.EXPERIMENTAL_ICP_DIR / config.EXPERIMENTAL_TEMPLATE_CSV
    assert len(pd.read_csv(path)) == 0


# --------------------------------------------------------------------------- #
# Parsing a filled file
# --------------------------------------------------------------------------- #
def test_parse_valid_file(tmp_path):
    csv = tmp_path / "run.csv"
    _write_csv(csv, [_ROW_1, _ROW_2])

    df = parse_experimental_release(csv)

    # provenance + all canonical columns present, in order.
    assert df.columns[0] == "source_file"
    assert list(df.columns[1:]) == config.EXPERIMENTAL_RELEASE_COLUMNS
    assert df["source_file"].iloc[0] == "run.csv"
    assert len(df) == 2

    # numeric columns are numeric and correct.
    assert pd.api.types.is_numeric_dtype(df["Ca_mM"])
    assert df["Ca_mM"].tolist() == [0.12, 0.20]
    assert df["final_pH"].tolist() == [9.6, 10.1]

    # date parsed to datetime.
    assert pd.api.types.is_datetime64_any_dtype(df["experiment_date"])
    assert df["experiment_date"].iloc[0] == pd.Timestamp("2026-06-01")

    # text columns stay text.
    assert df["fly_ash_type"].iloc[0] == "CFA"


def test_numeric_junk_becomes_nan(tmp_path):
    bad = {**_ROW_1, "Ca_mM": "not_a_number", "Si_mM": ""}
    csv = tmp_path / "junk.csv"
    _write_csv(csv, [bad])

    df = parse_experimental_release(csv)
    assert pd.isna(df["Ca_mM"].iloc[0])
    assert pd.isna(df["Si_mM"].iloc[0])


def test_missing_column_strict_raises(tmp_path):
    df = pd.DataFrame([_ROW_1], columns=config.EXPERIMENTAL_RELEASE_COLUMNS)
    df = df.drop(columns=["Ca_mM"])
    csv = tmp_path / "missing.csv"
    df.to_csv(csv, index=False)

    with pytest.raises(ExperimentalSchemaError):
        parse_experimental_release(csv, strict=True)


def test_missing_column_non_strict_fills_nan(tmp_path):
    df = pd.DataFrame([_ROW_1], columns=config.EXPERIMENTAL_RELEASE_COLUMNS)
    df = df.drop(columns=["Ca_mM"])
    csv = tmp_path / "missing.csv"
    df.to_csv(csv, index=False)

    out = parse_experimental_release(csv, strict=False)
    assert "Ca_mM" in out.columns
    assert pd.isna(out["Ca_mM"].iloc[0])


def test_extra_columns_are_kept(tmp_path):
    df = pd.DataFrame([_ROW_1], columns=config.EXPERIMENTAL_RELEASE_COLUMNS)
    df["operator"] = "lab-tech-1"
    csv = tmp_path / "extra.csv"
    df.to_csv(csv, index=False)

    out = parse_experimental_release(csv)
    assert "operator" in out.columns
    assert out["operator"].iloc[0] == "lab-tech-1"


# --------------------------------------------------------------------------- #
# Measured-data detection + directory loading
# --------------------------------------------------------------------------- #
def test_has_measured_data_false_for_template():
    # The real shipped template (header only) has no measured data.
    template = load_experimental_release(include_template=True)  # default dir
    # Whether or not other filled files exist, the empty template alone is not data.
    empty = pd.DataFrame(columns=["source_file"] + config.EXPERIMENTAL_RELEASE_COLUMNS)
    assert has_measured_data(empty) is False


def test_has_measured_data_true_for_filled(tmp_path):
    csv = tmp_path / "run.csv"
    _write_csv(csv, [_ROW_1])
    df = parse_experimental_release(csv)
    assert has_measured_data(df) is True


def test_load_directory_skips_template(tmp_path):
    # template (blank) + one filled file in the same directory.
    template = tmp_path / config.EXPERIMENTAL_TEMPLATE_CSV
    _write_csv(template, [])
    filled = tmp_path / "monday.csv"
    _write_csv(filled, [_ROW_1, _ROW_2])

    df = load_experimental_release(tmp_path)
    assert len(df) == 2  # only the filled file's rows
    assert set(df["source_file"]) == {"monday.csv"}
    assert has_measured_data(df) is True
