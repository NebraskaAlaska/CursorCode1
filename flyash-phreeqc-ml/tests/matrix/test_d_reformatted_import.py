"""MATRIX (d) — CLAIM: a differently-formatted upload (reordered / renamed columns and
a non-target unit) is supported: the importer's fuzzy mapping resolves the headers onto
the canonical schema and the **Prompt-16 unit contract** converts mg/L → mM with
recorded conversion provenance.
"""
from __future__ import annotations

import pytest

from flyash_phreeqc_ml import import_mapping as im, units


def test_reformatted_upload_resolves_and_converts(reformatted_upload):
    raw, unit_choice = reformatted_upload["raw"], reformatted_upload["units"]

    # Fuzzy mapping resolves "Calcium"->Ca_mM, "pH"->final_pH, "Sample"->sample_id.
    mapping = im.suggest_column_mapping(raw.columns)
    assert mapping["Ca_mM"] == "Calcium"
    assert mapping["final_pH"] == "pH"
    assert mapping["sample_id"] == "Sample"

    out = im.build_schema_frame(raw, mapping, unit_choice,
                                import_timestamp="2026-06-08T00:00:00")
    # mg/L -> mM via the single conversion authority.
    assert out.loc[0, "Ca_mM"] == pytest.approx(100.0 / units.MOLAR_MASSES["Ca"])
    assert out.loc[1, "Ca_mM"] == pytest.approx(200.0 / units.MOLAR_MASSES["Ca"])
    # Conversion provenance is recorded (auditable later).
    assert out.loc[0, "Ca_mM_orig_value"] == pytest.approx(100.0)
    assert out.loc[0, "Ca_mM_orig_unit"] == "mg/L"
    assert out.loc[0, "Ca_mM_conversion_id"] == "mgL_to_mM"
    # pH passes through unchanged.
    assert out.loc[0, "final_pH"] == 13.0


def test_undeclared_unit_is_refused_not_guessed(reformatted_upload):
    raw = reformatted_upload["raw"]
    mapping = im.suggest_column_mapping(raw.columns)
    with pytest.raises(units.UnknownUnitError):
        im.build_schema_frame(raw, mapping, {"Ca_mM": "g/L"})
