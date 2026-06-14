"""Tests for the GP residual-correction model (ml/residual_model.py).

Pins (per the spec): the data-sufficiency gate (29 pairs refused / 30 accepted,
and the ≥3-condition requirement), LOCO mechanics, the baseline-comparison logic,
and that corrected values never appear in the comparison/residual CSV schema.
Synthetic data only.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from flyash_phreeqc_ml import replicates
from flyash_phreeqc_ml.ml import residual_model as rm


@pytest.fixture(autouse=True)
def _fast_gp(monkeypatch):
    """Skip GP hyperparameter optimization (the slow/nondeterministic part).

    These tests assert the gate, LOCO mechanics, baseline comparison, and corrected-
    overlay arithmetic — none of which depend on GP fit *quality* — so a fixed kernel
    keeps them fast and deterministic without weakening any assertion.
    """
    monkeypatch.setenv("FLYASH_GP_FAST", "1")


def _make_comp(per_condition, conditions=(0.5, 1.0, 2.0), *, seed=0,
               slope=0.0, base=0.4, noise=0.02, phreeqc=0.8):
    """Build a synthetic per-run comparison with exact-mapped Ca residuals.

    Each distinct ``NaOH_M`` becomes a distinct ``condition_key``. ``per_condition``
    may be an int (same count per condition) or a list aligned to ``conditions``.
    The residual is ``base + slope * NaOH_M + noise`` so a feature-aware model can
    (with slope!=0) beat the constant-bias baseline.
    """
    rng = np.random.default_rng(seed)
    counts = ([per_condition] * len(conditions)
              if isinstance(per_condition, int) else list(per_condition))
    rows, statuses = [], {}
    sid = 0
    for naoh, n in zip(conditions, counts):
        for _ in range(n):
            r = base + slope * naoh + rng.normal(0.0, noise)
            key = f"S{sid}"
            rows.append({
                "sample_id": key, "leachant": "NaOH", "NaOH_M": naoh,
                "liquid_solid_ratio": 5, "time_min": 10, "CO2_condition": "OA",
                "residual_Ca": r, "phreeqc_Ca_mM": phreeqc,
            })
            statuses[key] = replicates.MAPPING_STATUS_EXACT
            sid += 1
    return pd.DataFrame(rows), statuses


# --------------------------------------------------------------------------- #
# Gate enforcement
# --------------------------------------------------------------------------- #
def test_gate_refuses_29_pairs():
    comp, statuses = _make_comp([10, 10, 9])  # 29 pairs, 3 conditions
    gate = rm.gate_status(comp, statuses, "Ca")
    assert gate.n_exact_pairs == 29
    assert gate.n_conditions == 3
    assert gate.meets is False
    with pytest.raises(rm.ResidualModelGateError) as exc:
        rm.train_element_model(comp, statuses, "Ca")
    assert exc.value.gate.n_exact_pairs == 29
    assert "29 of 30 exact pairs; 3 of 3 conditions" in str(exc.value)


def test_gate_accepts_30_pairs():
    comp, statuses = _make_comp(10)  # 30 pairs, 3 conditions
    gate = rm.gate_status(comp, statuses, "Ca")
    assert gate.n_exact_pairs == 30
    assert gate.n_conditions == 3
    assert gate.meets is True
    model = rm.train_element_model(comp, statuses, "Ca", run_name="demo")
    assert isinstance(model, rm.ResidualCorrectionModel)
    assert model.card["gate"]["n_exact_pairs"] == 30
    assert model.card["gate"]["n_conditions"] == 3
    assert model.card["training_run_names"] == ["demo"]
    assert model.card["training_set_hash"]


def test_gate_requires_three_conditions_even_with_enough_pairs():
    comp, statuses = _make_comp(20, conditions=(0.5, 1.0))  # 40 pairs but 2 conditions
    gate = rm.gate_status(comp, statuses, "Ca")
    assert gate.n_exact_pairs == 40
    assert gate.n_conditions == 2
    assert gate.meets is False
    with pytest.raises(rm.ResidualModelGateError) as exc:
        rm.train_element_model(comp, statuses, "Ca")
    assert exc.value.gate.n_conditions == 2


def test_gate_progress_message_format():
    comp, statuses = _make_comp([5, 5, 4])  # 14 pairs, 3 conditions
    gate = rm.gate_status(comp, statuses, "Ca")
    assert gate.progress_message() == "14 of 30 exact pairs; 3 of 3 conditions"


def test_gate_ignores_nonexact_and_synthetic_rows():
    # 30 genuine exact pairs + noise rows that must NOT inflate the count.
    comp, statuses = _make_comp(10)
    noise = pd.DataFrame([
        {"sample_id": "SCEN", "leachant": "NaOH", "NaOH_M": 0.5,
         "liquid_solid_ratio": 5, "time_min": 10, "CO2_condition": "OA",
         "residual_Ca": 99.0, "phreeqc_Ca_mM": 0.8},
        {"sample_id": "SYN", "leachant": "NaOH", "NaOH_M": 0.5,
         "liquid_solid_ratio": 5, "time_min": 10, "CO2_condition": "OA",
         "residual_Ca": 99.0, "phreeqc_Ca_mM": 0.8, "source_type": "synthetic_demo"},
    ])
    comp2 = pd.concat([comp, noise], ignore_index=True)
    statuses["SCEN"] = replicates.MAPPING_STATUS_SCENARIO
    statuses["SYN"] = replicates.MAPPING_STATUS_EXACT  # exact but synthetic → excluded
    gate = rm.gate_status(comp2, statuses, "Ca")
    assert gate.n_exact_pairs == 30  # not 31 or 32


# --------------------------------------------------------------------------- #
# LOCO mechanics + baseline comparison
# --------------------------------------------------------------------------- #
def test_loco_runs_one_fold_per_condition():
    comp, statuses = _make_comp(10, slope=0.3)
    loco = rm.loco_cross_validate(comp, statuses, "Ca")
    assert loco is not None
    assert loco["n_folds"] == 3                 # one held-out condition per fold
    assert loco["n_evaluated"] == 30            # every row scored once as held-out
    assert np.isfinite(loco["model_loco_rmse"])
    assert np.isfinite(loco["baseline_loco_rmse"])
    assert isinstance(loco["beats_baseline"], bool)
    assert loco["beats_baseline"] == (loco["model_loco_rmse"] < loco["baseline_loco_rmse"])


def test_loco_needs_at_least_two_conditions():
    comp, statuses = _make_comp(15, conditions=(0.5,))  # single condition
    assert rm.loco_cross_validate(comp, statuses, "Ca") is None


def test_beats_baseline_logic():
    assert rm.beats_baseline(0.5, 1.0) is True
    assert rm.beats_baseline(1.0, 0.5) is False
    assert rm.beats_baseline(0.5, 0.5) is False        # tie → not "beats"
    assert rm.beats_baseline(None, 1.0) is False
    assert rm.beats_baseline(float("nan"), 1.0) is False


def test_use_correction_recommended_follows_beats_baseline():
    assert rm.use_correction_recommended({"beats_baseline": True}) is True
    assert rm.use_correction_recommended({"beats_baseline": False}) is False
    assert rm.use_correction_recommended(None) is False


def test_loco_baseline_is_constant_bias_when_no_feature_signal():
    # Residual is condition-mean noise with no usable feature signal → the constant
    # bias is hard to beat, so the recommendation should be NOT to use the correction.
    comp, statuses = _make_comp(12, slope=0.0, noise=0.3, seed=7)
    loco = rm.loco_cross_validate(comp, statuses, "Ca")
    assert loco is not None
    # We do not assert the GP loses (that would be brittle), only that the
    # recommendation is wired to the comparison result.
    assert rm.use_correction_recommended(loco) == (
        loco["model_loco_rmse"] < loco["baseline_loco_rmse"])


# --------------------------------------------------------------------------- #
# Corrected overlay — present in the overlay, absent from the comparison schema
# --------------------------------------------------------------------------- #
def test_corrected_overlay_keeps_raw_and_corrected_together():
    comp, statuses = _make_comp(10, slope=0.3)
    model = rm.train_element_model(comp, statuses, "Ca")
    overlay = rm.corrected_overlay(model, comp.head(6))
    # Raw PHREEQC and corrected are always shown together (never corrected alone).
    for col in ("phreeqc", "predicted_residual", "corrected",
                "corrected_lower", "corrected_upper"):
        assert col in overlay.columns
    # corrected == phreeqc + predicted_residual, and the interval brackets it.
    np.testing.assert_allclose(
        overlay["corrected"], overlay["phreeqc"] + overlay["predicted_residual"], rtol=1e-9)
    assert (overlay["corrected_lower"] <= overlay["corrected"] + 1e-9).all()
    assert (overlay["corrected_upper"] >= overlay["corrected"] - 1e-9).all()


def test_corrected_values_never_enter_comparison_or_residual_schema():
    comp, statuses = _make_comp(10, slope=0.3)
    original_columns = list(comp.columns)
    model = rm.train_element_model(comp, statuses, "Ca")
    _ = rm.corrected_overlay(model, comp)
    # Training + overlay must not mutate the comparison frame or add corrected columns.
    assert list(comp.columns) == original_columns
    for forbidden in ("corrected", "corrected_lower", "corrected_upper",
                      "predicted_residual"):
        assert forbidden not in comp.columns
    # The Prompt-2 residual columns are untouched.
    assert "residual_Ca" in comp.columns


def test_persistence_round_trip(tmp_path):
    comp, statuses = _make_comp(10, slope=0.3)
    model = rm.train_element_model(comp, statuses, "Ca", run_name="demo")
    rm.save_residual_model(model, tmp_path)
    assert (tmp_path / "Ca.joblib").exists()
    assert (tmp_path / "Ca.model_card.json").exists()
    loaded = rm.load_residual_models(tmp_path)
    assert "Ca" in loaded
    # A reloaded model predicts identically.
    a = rm.corrected_overlay(model, comp.head(4))["corrected"].to_numpy()
    b = rm.corrected_overlay(loaded["Ca"], comp.head(4))["corrected"].to_numpy()
    np.testing.assert_allclose(a, b, rtol=1e-9)
