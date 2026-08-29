"""Phase 1C-R3 non-finite ML-output regressions using synthetic fixtures only.

The models and values in this module exist only to exercise fail-closed software contracts. They
are not project research, measured fly-ash data, experimental validation, or scientific evidence.
"""
from __future__ import annotations

import json
import math
import pickle

import pytest

from flyash_phreeqc_ml.instruments import virtual_lab_machine_runner as runner
from flyash_phreeqc_ml.instruments import virtual_lab_machines as machines
from flyash_phreeqc_ml.ml_models import (model_registry, model_schema, predict as predict_backend,
                                         train, training_data, uncertainty)


class _SyntheticConstantPipeline:
    """Minimal test-only fitted-pipeline stand-in returning one configured scalar."""

    def __init__(self, value):
        self.value = value

    def predict(self, _frame):
        return [self.value]


def _synthetic_output_model(value=42.0, *, residual_sigma=1.5):
    """Approved-status test container; never presented as research or validation evidence."""
    return model_schema.TrainedModel(
        name="synthetic_r3_model_output_fixture",
        target=model_schema.TARGET_COMPRESSIVE,
        model_type=model_schema.MODEL_RIDGE,
        model_family=model_schema.MODEL_FAMILY_COMPOSITE,
        pipeline=_SyntheticConstantPipeline(value),
        numeric_features=["plastic_dosage_percent"],
        feature_ranges={"plastic_dosage_percent": [0.0, 25.0]},
        residual_sigma=residual_sigma,
        source_type=training_data.SOURCE_LAB,
        validation_status=model_schema.VALIDATION_EXPERIMENTAL,
        training_data_status=model_schema.TRAINING_DATA_APPROVED,
        n_train=12,
        version="synthetic-r3-test-only",
    )


def _predict_direct(model):
    return predict_backend.predict(model, {"plastic_dosage_percent": 10.0})


def _run_generic(model):
    return runner.run_virtual_lab_machine(
        machines.ML_SURROGATE,
        {"model": model, "features": {"plastic_dosage_percent": 10.0}},
    )


def _assert_strict_json_finite(value):
    """Strict JSON encoding rejects every numeric NaN and Infinity recursively."""
    json.dumps(value, allow_nan=False)


def _assert_invalid_prediction(prediction):
    assert prediction.refused is True
    assert prediction.refusal_code == predict_backend.REFUSE_INVALID_MODEL_OUTPUT
    assert prediction.refusal_reason == predict_backend.INVALID_MODEL_OUTPUT_MESSAGE
    assert prediction.value is None
    assert prediction.lower is None
    assert prediction.upper is None
    assert prediction.sigma is None
    serialized = prediction.to_dict()
    assert serialized["refused"] is True
    assert serialized["refusal_code"] == "invalid_model_output"
    _assert_strict_json_finite(serialized)


def _assert_invalid_machine_result(result):
    assert result.status == runner.STATUS_TRAINED_MODEL_REQUIRED
    assert result.output_data_type == machines.OUT_ADVISORY_INTERPRETATION
    assert result.output_data_type != machines.OUT_ML_PREDICTION
    assert result.can_be_used_for_validation_claim is False
    assert result.provenance["model_artifact_usable"] is False
    assert not {"value", "lower", "upper", "sigma"}.intersection(result.results)
    assert "traceback" not in str(result.to_dict()).lower()
    _assert_strict_json_finite(result.to_dict())


@pytest.mark.parametrize(
    "invalid_mean",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive_infinity", "negative_infinity"],
)
def test_specialized_backend_refuses_nonfinite_prediction_means(invalid_mean):
    _assert_invalid_prediction(_predict_direct(_synthetic_output_model(invalid_mean)))


@pytest.mark.parametrize(
    "invalid_mean",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive_infinity", "negative_infinity"],
)
def test_generic_runner_refuses_nonfinite_prediction_means(invalid_mean):
    _assert_invalid_machine_result(_run_generic(_synthetic_output_model(invalid_mean)))


def test_specialized_backend_refuses_infinite_residual_sigma_and_interval():
    prediction = _predict_direct(_synthetic_output_model(42.0, residual_sigma=float("inf")))
    _assert_invalid_prediction(prediction)


def test_generic_runner_refuses_infinite_residual_sigma_and_interval():
    result = _run_generic(_synthetic_output_model(42.0, residual_sigma=float("inf")))
    _assert_invalid_machine_result(result)


def test_specialized_backend_refuses_finite_mean_with_nan_interval(monkeypatch):
    monkeypatch.setattr(
        uncertainty,
        "predict_with_uncertainty",
        lambda _model, _frame: (42.0, 1.0, float("nan"), 43.0, uncertainty.METHOD_CV_RESIDUAL),
    )
    _assert_invalid_prediction(_predict_direct(_synthetic_output_model()))


def test_generic_runner_refuses_finite_mean_with_nan_interval(monkeypatch):
    monkeypatch.setattr(
        uncertainty,
        "predict_with_uncertainty",
        lambda _model, _frame: (42.0, float("nan"), 41.0, 43.0,
                                uncertainty.METHOD_CV_RESIDUAL),
    )
    _assert_invalid_machine_result(_run_generic(_synthetic_output_model()))


def test_prediction_to_dict_fails_closed_if_a_prediction_is_later_corrupted():
    corrupted = predict_backend.Prediction(
        target=model_schema.TARGET_COMPRESSIVE,
        value=float("nan"),
        lower=float("-inf"),
        upper=float("inf"),
        sigma=float("nan"),
    )
    serialized = corrupted.to_dict()
    assert serialized["refused"] is True
    assert serialized["refusal_code"] == "invalid_model_output"
    assert all(serialized[field] is None for field in ("value", "lower", "upper", "sigma"))
    _assert_strict_json_finite(serialized)


def test_generic_runner_defense_rejects_corrupt_success_envelope(monkeypatch):
    corrupt_success = predict_backend.Prediction(
        target=model_schema.TARGET_COMPRESSIVE,
        value=float("inf"),
        interval_method=uncertainty.METHOD_NONE,
    )
    monkeypatch.setattr(predict_backend, "predict", lambda _model, _features: corrupt_success)
    _assert_invalid_machine_result(_run_generic(_synthetic_output_model()))


def test_invalid_output_does_not_mutate_or_rewrite_the_artifact(tmp_path):
    model = _synthetic_output_model(float("nan"))
    registry = model_registry.ModelRegistry(tmp_path / "synthetic_r3_registry")
    registry.save(model)
    artifact_path = registry.model_dir(model.name) / model_registry.MODEL_ARTIFACT
    persisted_before = artifact_path.read_bytes()

    loaded = registry.load(model.name)
    object_before = pickle.dumps(loaded)
    _assert_invalid_prediction(_predict_direct(loaded))
    _assert_invalid_machine_result(_run_generic(loaded))

    assert pickle.dumps(loaded) == object_before
    assert artifact_path.read_bytes() == persisted_before
    assert math.isnan(loaded.pipeline.value)


@pytest.fixture(scope="module")
def synthetic_approved_fitted_model():
    if not train.sklearn_available():
        pytest.skip("scikit-learn not installed")
    rows = training_data.demo_rows(n=14, seed=53)
    for index, row in enumerate(rows):
        row.source_type = training_data.SOURCE_LAB
        row.source_id = f"synthetic-r3-approved-fixture-{index}"
        row.user_review_status = training_data.REVIEW_APPROVED
        row.notes = "synthetic Phase 1C-R3 fixture; not project research or validation evidence"
    return train.train_model(
        rows, date="2026-08-29", name="synthetic_r3_approved_fitted_fixture")


def test_legitimate_approved_model_preserves_finite_prediction_interval_and_provenance(
        synthetic_approved_fitted_model):
    model = synthetic_approved_fitted_model
    prediction = _predict_direct(model)
    assert prediction.refused is False
    assert all(math.isfinite(value) for value in
               (prediction.value, prediction.lower, prediction.upper, prediction.sigma))
    assert prediction.lower <= prediction.value <= prediction.upper
    assert prediction.warnings
    _assert_strict_json_finite(prediction.to_dict())

    result = _run_generic(model)
    assert result.status == runner.STATUS_PROCESSED
    assert result.output_data_type == machines.OUT_ML_PREDICTION
    assert all(math.isfinite(result.results[field])
               for field in ("value", "lower", "upper", "sigma"))
    assert result.warnings == prediction.warnings
    assert result.provenance["model_artifact_usable"] is True
    assert result.provenance["model_name"] == model.name
    assert result.provenance["model_version"] == model.version
    assert result.provenance["training_data_status"] == model_schema.TRAINING_DATA_APPROVED
    assert result.provenance["n_training_rows"] == model.n_train
    assert result.can_be_used_for_validation_claim is False
    _assert_strict_json_finite(result.to_dict())
