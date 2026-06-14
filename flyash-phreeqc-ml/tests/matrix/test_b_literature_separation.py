"""MATRIX (b) — CLAIM: a literature-style fly-ash dataset is supported as a *separate*
run type whose data can **never** be mixed into lab measured data. The run-type
guardrails (Prompt: Experiment Run Manager) keep literature values out of a lab run's
experimental_release.csv and out of the lab comparison path.
"""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml import run_manager


def test_literature_data_round_trips_in_its_own_file(literature_runs):
    lit = literature_runs["lit"]
    df = run_manager.read_data_file(lit)
    assert df.iloc[0]["source_id"] == "paper2020"
    # It lives in the literature file, not a release file.
    assert run_manager.data_file_path(lit).name == "literature_benchmark.csv"


def test_literature_run_refuses_lab_release_paths(literature_runs):
    lit = literature_runs["lit"]
    # The lab-only guardrails reject a literature run — literature can't become lab data.
    with pytest.raises(run_manager.RunTypeError):
        run_manager.lab_release_path(lit)
    with pytest.raises(run_manager.RunTypeError):
        run_manager.save_lab_dataframe(lit, pd.DataFrame({"sample_id": ["x"]}))
    with pytest.raises(run_manager.RunTypeError):
        run_manager.comparison_path(lit)         # no lab comparison for a literature run


def test_lab_run_release_file_is_independent(literature_runs):
    lab = literature_runs["lab"]
    # A lab run writes only its own release file; the literature row never appears.
    run_manager.save_lab_dataframe(lab, pd.DataFrame([{"sample_id": "LAB1", "final_pH": 13.0}]))
    lab_df = run_manager.read_data_file(lab)
    assert "source_id" not in lab_df.columns
    assert list(lab_df["sample_id"]) == ["LAB1"]
