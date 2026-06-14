"""Tests for the PHREEQC surrogate layer (Prompt 12) — no PHREEQC needed.

Coverage: seeded sampling reproducibility, model-card contents, extrapolation
flagging, a train/predict round-trip on a synthetic function, and rough 95%-interval
coverage from honest (held-out) validation.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from flyash_phreeqc_ml.ml import sampling, surrogate


@pytest.fixture()
def fast_gp(monkeypatch):
    """Skip GP hyperparameter optimization (fast + deterministic).

    For tests that exercise fit/predict/domain logic but NOT fit *quality*. The
    coverage/RMSE test deliberately does **not** use this — it keeps the optimizer.
    """
    monkeypatch.setenv("FLYASH_GP_FAST", "1")


# --------------------------------------------------------------------------- #
# Sampling
# --------------------------------------------------------------------------- #
def test_sampling_reproducible_and_in_range():
    a = sampling.latin_hypercube_design(60, seed=1)
    b = sampling.latin_hypercube_design(60, seed=1)
    pd.testing.assert_frame_equal(a, b)                       # same seed → identical
    c = sampling.latin_hypercube_design(60, seed=2)
    assert not a.drop(columns="sample_id").equals(c.drop(columns="sample_id"))
    assert a["NaOH_M"].between(0.1, 5.0).all()
    assert a["liquid_solid_ratio"].between(2.0, 20.0).all()
    assert set(a["co2_scenario"]).issubset({"atm", "low", "none"})
    assert list(a["sample_id"]) == [f"S{i:04d}" for i in range(60)]


# --------------------------------------------------------------------------- #
# Synthetic dataset + helpers
# --------------------------------------------------------------------------- #
def _synthetic(n=140, seed=0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    a = rng.uniform(0.0, 10.0, n)
    b = rng.uniform(0.0, 5.0, n)
    y = 2.0 * a - 3.0 * b + np.sin(a) + rng.normal(0.0, 0.25, n)
    return pd.DataFrame({"a": a, "b": b, "y": y})


def test_train_round_trip_and_model_card(tmp_path, fast_gp):
    df = _synthetic(n=40)
    models = surrogate.train_surrogate(
        df, input_cols=["a", "b"], output_cols=["y"], categorical_cols=[],
        n_folds=4, seed=0, date="2026-01-01")
    assert "y" in models
    card = models["y"].card
    for key in ("output", "method", "training_set_hash", "n_samples",
                "input_ranges", "cv_metric", "library_versions", "date"):
        assert key in card
    assert card["date"] == "2026-01-01"
    assert card["n_samples"] == len(df)
    assert "a" in card["input_ranges"] and "b" in card["input_ranges"]
    assert set(card["cv_metric"]) >= {"rmse", "mae", "folds"}
    assert "scikit-learn" in card["library_versions"]

    # Predict round-trip — interior rows, sane interval ordering, decent fit.
    pred = surrogate.predict(models["y"], df[["a", "b"]].head(8))
    assert {"mean", "lower", "upper", "domain"} <= set(pred.columns)
    assert (pred["domain"] == surrogate.DOMAIN_INTERIOR).all()
    assert (pred["lower"] <= pred["upper"]).all()

    # Persistence writes a joblib + a JSON model card per output.
    out = surrogate.save_surrogate(models, tmp_path)
    assert (out / "y.joblib").exists()
    saved_card = json.loads((out / "y.model_card.json").read_text())
    assert saved_card["training_set_hash"] == card["training_set_hash"]
    reloaded = surrogate.load_surrogate(tmp_path)
    assert "y" in reloaded


def test_extrapolation_flagged_outside_training_box(fast_gp):
    df = _synthetic(n=40)
    models = surrogate.train_surrogate(
        df, input_cols=["a", "b"], output_cols=["y"], categorical_cols=[])
    far = pd.DataFrame({"a": [1000.0], "b": [1.0]})       # a far beyond [0,10]
    inside = pd.DataFrame({"a": [5.0], "b": [2.5]})
    assert surrogate.predict(models["y"], far)["domain"].iloc[0] == surrogate.DOMAIN_EXTRAPOLATION
    assert surrogate.predict(models["y"], inside)["domain"].iloc[0] == surrogate.DOMAIN_INTERIOR


def test_validation_table_and_rough_coverage():
    df = _synthetic(n=180)
    table = surrogate.validate_surrogate(
        df, input_cols=["a", "b"], output_cols=["y"], categorical_cols=[],
        test_size=0.25, n_folds=5, seed=0)
    assert not table.empty
    row = table[table["output"] == "y"].iloc[0]
    assert set(table.columns) >= {"output", "method", "n_train", "n_test",
                                  "rmse", "mae", "coverage95", "cv_rmse", "cv_mae"}
    # A smooth function with small noise → the 95% interval should roughly cover.
    assert 0.6 <= row["coverage95"] <= 1.0
    assert row["rmse"] < 5.0  # the GP fits this well


def test_train_skips_outputs_with_too_few_rows(fast_gp):
    df = pd.DataFrame({"a": [1.0, 2.0], "b": [0.1, 0.2], "y": [1.0, 2.0]})
    models = surrogate.train_surrogate(
        df, input_cols=["a", "b"], output_cols=["y"], categorical_cols=[])
    assert models == {}  # < 4 usable rows → skipped, not a crash
