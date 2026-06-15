"""Tests for the GP model-incompleteness estimator (ml/incompleteness_model.py).

Pins (per the spec): the data-sufficiency gate (29 well-determined rows refused / 30
accepted, ≥3 conditions), LOCO mechanics + the constant-bias baseline comparison, the
noise-domination guard (don't fit noise), that literature-provenance rows never become
training targets, and that predicted values never enter measured/derived closure columns.
optimizer=None fast-mode keeps the suite quick. Synthetic data only — no PHREEQC, no
network.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from flyash_phreeqc_ml import (attribution, config, mass_balance, profiles, replicates,
                               run_manager, units)
from flyash_phreeqc_ml.ml import incompleteness_model as im


@pytest.fixture(autouse=True)
def _fast_gp(monkeypatch):
    """Fixed-kernel GP (no L-BFGS-B) — fast + deterministic; no assertion needs fit quality."""
    monkeypatch.setenv("FLYASH_GP_FAST", "1")


CK = replicates.CONDITION_KEY_COLUMN


def _make_recovery(per_condition, conditions=(0.5, 1.0, 2.0), *, slope=0.5, base=1.0,
                   noise=0.01, sigma=0.05, seed=0, status=attribution.STATUS_UNEXPLAINED,
                   provenance="measured"):
    """A synthetic per-row recovery frame with a well-determined Ca shortfall target.

    target (unexplained residual) = base + slope*NaOH + noise; gap = target (attribution
    unavailable offline → unexplained == gap); gap_sigma = ``sigma`` (≤ 0.5·|gap| when
    base is well above zero, so rows are 'well-determined'). Each NaOH_M is its own
    condition. ``per_condition`` is an int or a list aligned to ``conditions``.
    """
    rng = np.random.default_rng(seed)
    counts = ([per_condition] * len(conditions) if isinstance(per_condition, int)
              else list(per_condition))
    rows, sid = [], 0
    for naoh, n in zip(conditions, counts):
        for _ in range(n):
            target = base + slope * naoh + rng.normal(0.0, noise)
            rows.append({
                "sample_id": f"S{sid}", "leachant": "NaOH", "NaOH_M": naoh,
                "liquid_solid_ratio": 5, "time_min": 10, "CO2_condition": "OA",
                CK: f"NaOH{naoh}",
                im.target_column("Ca"): target, im.gap_column("Ca"): target,
                im.gap_sigma_column("Ca"): sigma,
                im.status_column("Ca"): status, im.provenance_column("Ca"): provenance,
            })
            sid += 1
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Gate enforcement (29 refused / 30 accepted), mirroring residual_model
# --------------------------------------------------------------------------- #
def test_gate_refuses_29_rows():
    rec = _make_recovery([10, 10, 9])                  # 29 well-determined rows, 3 conditions
    gate = im.gate_status(rec, "Ca")
    assert gate.n_rows == 29 and gate.n_conditions == 3 and gate.meets is False
    with pytest.raises(im.IncompletenessGateError) as exc:
        im.train_element_model(rec, "Ca")
    assert exc.value.gate.n_rows == 29
    assert "29 of 30 well-determined rows; 3 of 3 conditions" in str(exc.value)


def test_gate_accepts_30_rows():
    rec = _make_recovery(10)                            # 30 rows, 3 conditions
    gate = im.gate_status(rec, "Ca")
    assert gate.n_rows == 30 and gate.n_conditions == 3 and gate.meets is True
    model = im.train_element_model(rec, "Ca", run_name="demo")
    assert isinstance(model, im.IncompletenessModel)
    assert model.card["gate"]["n_rows"] == 30
    assert model.card["kind"] == "model_incompleteness_gp"
    assert "shortfall" in model.card["non_claim"].lower()


def test_gate_needs_three_conditions():
    rec = _make_recovery(20, conditions=(0.5, 1.0))     # 40 rows but only 2 conditions
    gate = im.gate_status(rec, "Ca")
    assert gate.n_rows == 40 and gate.n_conditions == 2 and gate.meets is False
    with pytest.raises(im.IncompletenessGateError):
        im.train_element_model(rec, "Ca")


# --------------------------------------------------------------------------- #
# Well-determined filter: bad σ, bad status, and literature provenance excluded
# --------------------------------------------------------------------------- #
def test_large_sigma_rows_are_not_well_determined():
    rec = _make_recovery(10, sigma=5.0)                 # σ ≫ 0.5·|gap| → none well-determined
    assert im.gate_status(rec, "Ca").n_rows == 0


def test_literature_provenance_rows_never_train():
    measured = _make_recovery(10)                       # 30 measured rows
    literature = _make_recovery(10, provenance="literature-confirmed", seed=7)
    mixed = pd.concat([measured, literature], ignore_index=True)
    # Only the 30 measured rows are eligible; the literature stand-ins are excluded.
    ex = im._extract_well_determined(mixed, "Ca", profiles.FLY_ASH_PROFILE)
    assert len(ex) == 30
    assert im.gate_status(mixed, "Ca").n_rows == 30


def test_untrustworthy_status_rows_excluded():
    rec = _make_recovery(10, status="incomplete")       # not a complete-closure status
    assert im.gate_status(rec, "Ca").n_rows == 0


# --------------------------------------------------------------------------- #
# Noise guard: don't fit noise
# --------------------------------------------------------------------------- #
def test_noise_dominated_refuses_to_fit():
    # No systematic slope + a large σ relative to the tiny scatter → consistent with noise.
    rec = _make_recovery(10, slope=0.0, noise=0.01, sigma=0.4)
    assert im.gate_status(rec, "Ca").meets is True       # the gate is met...
    with pytest.raises(im.NoLearnablePatternError) as exc:
        im.train_element_model(rec, "Ca")
    assert exc.value.assessment["noise_dominated"] is True
    assert "measurement noise" in str(exc.value)


def test_signal_bearing_is_not_noise_dominated():
    rec = _make_recovery(10)                             # strong slope, small σ
    ex = im._extract_well_determined(rec, "Ca", profiles.FLY_ASH_PROFILE)
    assess = im.assess_signal(ex)
    assert assess["noise_dominated"] is False
    assert assess["chi2_reduced"] > im.NOISE_CHI2_MAX


# --------------------------------------------------------------------------- #
# LOCO mechanics + baseline comparison
# --------------------------------------------------------------------------- #
def test_loco_runs_per_condition_against_baseline():
    rec = _make_recovery(10)
    loco = im.loco_cross_validate(rec, "Ca")
    assert loco is not None
    for key in ("model_loco_rmse", "baseline_loco_rmse", "beats_baseline", "n_folds"):
        assert key in loco
    assert loco["n_folds"] == 3                          # one held-out fold per condition
    assert isinstance(loco["beats_baseline"], bool)


def test_beats_baseline_logic_and_recommendation():
    assert im.beats_baseline(0.10, 0.20) is True
    assert im.beats_baseline(0.30, 0.20) is False
    assert im.beats_baseline(None, 0.20) is False
    assert im.use_model_recommended({"beats_baseline": True}) is True
    assert im.use_model_recommended({"beats_baseline": False}) is False
    assert im.use_model_recommended(None) is False


# --------------------------------------------------------------------------- #
# Prediction framing + active-learning hook; ML never enters closure arithmetic
# --------------------------------------------------------------------------- #
def test_predict_shortfall_columns_are_prediction_only():
    rec = _make_recovery(10)
    model = im.train_element_model(rec, "Ca", run_name="demo")
    probe = rec.head(3).copy()
    before_cols = set(probe.columns)
    pred = im.predict_shortfall(model, probe)
    assert set(pred.columns) <= {"sample_id", CK, "predicted_shortfall",
                                 "shortfall_lower", "shortfall_upper"}
    # No measured/derived closure column appears in the prediction frame...
    assert not any(c.startswith(("gap_", "unexplained_")) for c in pred.columns)
    # ...and predicting never mutates the input (no ml column leaks back into the data).
    assert set(probe.columns) == before_cols


def test_flag_underattributed_conditions_sorted_and_flagged():
    rec = _make_recovery(10)
    model = im.train_element_model(rec, "Ca", run_name="demo")
    flagged = im.flag_underattributed_conditions(model, rec, threshold=0.0)
    assert "underattributed" in flagged.columns
    vals = flagged["predicted_shortfall"].tolist()
    assert vals == sorted(vals, reverse=True)            # weakest (largest shortfall) first
    # A huge threshold flags nothing; a tiny one flags the strongly-shortfall rows.
    none_flagged = im.flag_underattributed_conditions(model, rec, threshold=1e9)
    assert not none_flagged["underattributed"].any()


def test_overlay_is_labeled_ml_predicted():
    rec = _make_recovery(10)
    model = im.train_element_model(rec, "Ca", run_name="demo")
    ov = im.incompleteness_overlay(model, rec.head(2))
    assert (ov["estimate_kind"] == "ml-predicted shortfall (experimental)").all()


# --------------------------------------------------------------------------- #
# build_recovery_dataset: the target is the *measured* gap minus *modeled* attribution
# --------------------------------------------------------------------------- #
# precipitate_in_measured_solid=False here so attribution CREDITS the precipitate to the
# gap — this test exercises the "passes" branch (unexplained = gap − attributed). The
# retained (default True → 0 gap-closure) branch is covered by matrix test_g (red mud).
BATCH_PROFILE = profiles.DatasetProfile(
    name="batch", grouping="fly_ash", mass_balance_elements=("Ca",),
    starting_content_unit="wt%", solid_residue_unit="wt%",
    mass_balance_candidate_phases={"Calcite": "Ca"},
    precipitate_in_measured_solid=False,
    feature_numeric_fields=("NaOH_M", "liquid_solid_ratio", "time_min"),
    feature_categorical_fields=("leachant_family", "condition_code"))


def _batch_row(naoh, ca_mM):
    return {"sample_id": f"B-{naoh}", "fly_ash_type": "Class C fly ash", "leachant": "NaOH",
            "NaOH_M": naoh, "acid_M": "", "CO2_condition": "OA", "liquid_solid_ratio": 5,
            "temperature_C": 25, "time_min": 10, "final_pH": 13.0,
            "material_mass_g": 5.0, "liquid_volume_mL": 50.0, "solid_mass_g": 4.0,
            "Ca_starting_content": 2.0, "Ca_solid_residue": 0.4, "Ca_mM": ca_mM}


def test_build_recovery_dataset_target_is_measured_gap_not_ml():
    data = pd.DataFrame([_batch_row("1.0", 20.0)])
    rec = im.build_recovery_dataset(data, BATCH_PROFILE)
    row = rec.iloc[0]
    # The gap column is exactly the measured closure gap (no model, no ML).
    closure = mass_balance.closure(_batch_row("1.0", 20.0), "Ca", profile=BATCH_PROFILE)
    assert row[im.gap_column("Ca")] == pytest.approx(closure["gap"])
    # Offline (no selected output) attribution is unavailable → unexplained == gap.
    assert row[im.target_column("Ca")] == pytest.approx(closure["gap"])
    assert row[im.provenance_column("Ca")] == "measured"
    # No ML/prediction columns leak into the recovery dataset.
    assert not any("predicted_shortfall" in c for c in rec.columns)


def test_build_recovery_dataset_uses_attribution_when_selected_output_given():
    data = pd.DataFrame([_batch_row("1.0", 20.0)])
    # Mock a PHREEQC selected output that precipitates ~40% of the gap as calcite.
    from flyash_phreeqc_ml import phreeqc_runner as pr
    closure = mass_balance.closure(_batch_row("1.0", 20.0), "Ca", profile=BATCH_PROFILE)
    ck = replicates.condition_key(_batch_row("1.0", 20.0), BATCH_PROFILE)
    sel = {pr.phase_moles_column("Calcite"): (closure["gap"] * 0.4) / 1000.0}
    rec = im.build_recovery_dataset(data, BATCH_PROFILE, selected_outputs={ck: sel})
    row = rec.iloc[0]
    # Unexplained = gap − attributed (≈ 60% of the gap), strictly less than the gap.
    assert row[im.target_column("Ca")] < row[im.gap_column("Ca")]
    assert row[im.target_column("Ca")] == pytest.approx(closure["gap"] * 0.6, rel=1e-6)
    assert row[im.status_column("Ca")] == attribution.STATUS_PARTIAL


# --------------------------------------------------------------------------- #
# Persistence (mirror residual_model: joblib + model_card.json)
# --------------------------------------------------------------------------- #
def test_save_and_load_roundtrip(tmp_path):
    rec = _make_recovery(10)
    model = im.train_element_model(rec, "Ca", run_name="demo")
    im.save_incompleteness_model(model, tmp_path)
    assert (tmp_path / "Ca.joblib").exists()
    assert (tmp_path / "Ca.model_card.json").exists()
    loaded = im.load_incompleteness_models(tmp_path)
    assert "Ca" in loaded and loaded["Ca"].element == "Ca"
