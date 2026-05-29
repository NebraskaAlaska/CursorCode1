"""Tests for the measured-vs-PHREEQC residual computation (Phase 2).

Confirms the residual definitions match the spec exactly and that PHREEQC
molalities are converted to mM before differencing.
"""
from __future__ import annotations

import pandas as pd

from flyash_phreeqc_ml.compare import compare_measured_vs_phreeqc, phreeqc_predictions_mM


def _phreeqc_results():
    # One batch state: Ca 0.001 mol/kgw -> 1.0 mM, pH 9.6.
    return pd.DataFrame(
        [
            {
                "record_key": "f|sim1|batch|sol1",
                "source_file": "f.pqo",
                "simulation": 1,
                "state": "batch",
                "solution_number": 1,
                "solution_label": "",
                "pH": 9.6,
                "mol_Ca": 0.001,
                "mol_Si": 0.002,
                "mol_Al": 0.0005,
                # no mol_Fe column -> phreeqc_Fe_mM should be NaN
            }
        ]
    )


def test_molality_converted_to_mM():
    pred = phreeqc_predictions_mM(_phreeqc_results())
    assert pred["phreeqc_Ca_mM"].iloc[0] == 1.0   # 0.001 * 1000
    assert pred["phreeqc_Si_mM"].iloc[0] == 2.0
    assert pd.isna(pred["phreeqc_Fe_mM"].iloc[0])  # element absent from PHREEQC run


def test_residuals_match_spec():
    measured = pd.DataFrame(
        [
            {
                "sample_id": "S1",
                "Ca_mM": 1.5,
                "Si_mM": 2.5,
                "Al_mM": 0.7,
                "Fe_mM": 0.1,
                "final_pH": 10.0,
            }
        ]
    )
    mapping = {"S1": "f|sim1|batch|sol1"}

    comp = compare_measured_vs_phreeqc(measured, _phreeqc_results(), mapping=mapping)
    row = comp.iloc[0]

    assert row["residual_Ca"] == 1.5 - 1.0   # measured - phreeqc(mM)
    assert row["residual_Si"] == 2.5 - 2.0
    assert round(row["residual_Al"], 6) == round(0.7 - 0.5, 6)
    assert pd.isna(row["residual_Fe"])       # phreeqc Fe is NaN -> residual NaN
    assert round(row["residual_pH"], 6) == round(10.0 - 9.6, 6)


def test_no_mapping_leaves_predictions_nan():
    measured = pd.DataFrame([{"sample_id": "S1", "Ca_mM": 1.5, "final_pH": 10.0}])
    comp = compare_measured_vs_phreeqc(measured, _phreeqc_results(), mapping=None)
    assert pd.isna(comp["phreeqc_Ca_mM"].iloc[0])
    assert pd.isna(comp["residual_Ca"].iloc[0])
