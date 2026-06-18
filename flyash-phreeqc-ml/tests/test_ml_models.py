"""ML surrogate prediction engine — schema, training, prediction, registry, demo, assistant.

Small mocked / synthetic data only; no real literature APIs and no real lab data. scikit-learn is
required for the train/predict tests (skipped if unavailable). The honesty contract is the point:
only approved+provenant rows train a real model, models are never "validated", demo models are
loudly synthetic, predictions carry uncertainty + applicability warnings, and the assistant never
emits a strength number.
"""
from __future__ import annotations

import pathlib

import pytest

import flyash_phreeqc_ml
from flyash_phreeqc_ml.ml_models import (feature_schema, model_card, model_registry, model_schema,
                                         predict as predict_mod, train, training_data)

HAS_SKLEARN = train.sklearn_available()
sklearn_only = pytest.mark.skipif(not HAS_SKLEARN, reason="scikit-learn not installed")


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _approved_lab_rows(n=14, seed=3):
    rows = training_data.demo_rows(n=n, seed=seed)
    for r in rows:                                      # relabel as approved lab data (not demo)
        r.source_type = training_data.SOURCE_LAB
        r.user_review_status = training_data.REVIEW_APPROVED
    return rows


def _composite_evidence(strength=32.5, conf=0.6, doi="10.1/x"):
    return {"provenance": {"source": "openalex", "doi": doi, "title": "T", "citation": "Smith 2020"},
            "compressive_strength_MPa": strength, "plastic_type": "PET", "plastic_dosage": "10%",
            "water_binder_ratio": 0.4, "curing_time": "28 days", "density_kg_m3": 1800,
            "extraction_confidence": conf, "citation": "Smith 2020"}


# --------------------------------------------------------------------------- #
# Schema + provenance
# --------------------------------------------------------------------------- #
def test_training_row_from_evidence_carries_provenance_and_is_pending():
    r = training_data.from_composite_evidence(_composite_evidence())
    assert r.source_type == training_data.SOURCE_LITERATURE
    assert r.user_review_status == training_data.REVIEW_PENDING       # not eligible until reviewed
    assert r.doi == "10.1/x" and r.citation == "Smith 2020"
    assert r.has_provenance is True
    assert r.compressive_strength_MPa == 32.5
    assert r.density_g_cm3 == pytest.approx(1.8)                      # kg/m3 -> g/cm3
    assert r.plastic_dosage_percent == 10.0 and r.curing_time_days == 28.0


def test_literature_row_without_source_has_no_provenance():
    r = training_data.TrainingRow(source_type=training_data.SOURCE_LITERATURE,
                                  compressive_strength_MPa=20.0)
    assert r.has_provenance is False
    kept, excluded = training_data.eligible_rows([r], target=model_schema.TARGET_COMPRESSIVE,
                                                 allow_unapproved=True)
    assert kept == []
    assert any("provenance" in e["reason"] for e in excluded)


def test_unapproved_rows_excluded_by_default():
    rows = [training_data.from_composite_evidence(_composite_evidence()) for _ in range(3)]
    kept, excluded = training_data.eligible_rows(rows, target=model_schema.TARGET_COMPRESSIVE)
    assert kept == []
    assert all("not approved" in e["reason"] for e in excluded)
    for r in rows:
        r.user_review_status = training_data.REVIEW_APPROVED
    kept, _ = training_data.eligible_rows(rows, target=model_schema.TARGET_COMPRESSIVE)
    assert len(kept) == 3


def test_low_confidence_literature_excluded_by_default():
    rows = [training_data.from_composite_evidence(_composite_evidence(conf=0.2)) for _ in range(3)]
    for r in rows:
        r.user_review_status = training_data.REVIEW_APPROVED
    kept, excluded = training_data.eligible_rows(rows, target=model_schema.TARGET_COMPRESSIVE)
    assert kept == []
    assert all("confidence" in e["reason"] for e in excluded)


def test_demo_rows_clearly_labelled_and_quarantined():
    rows = training_data.demo_rows(n=6)
    assert all(r.source_type == training_data.SOURCE_DEMO for r in rows)
    assert all(r.user_review_status == training_data.REVIEW_APPROVED for r in rows)
    # demo rows are excluded from a *real* dataset unless explicitly included
    kept, excluded = training_data.eligible_rows(rows, target=model_schema.TARGET_COMPRESSIVE)
    assert kept == [] and all("demo" in e["reason"] for e in excluded)
    kept2, _ = training_data.eligible_rows(rows, target=model_schema.TARGET_COMPRESSIVE,
                                           include_demo=True)
    assert len(kept2) == 6


def test_supported_targets_and_displays():
    assert model_schema.TARGET_COMPRESSIVE in model_schema.SUPPORTED_TARGETS
    assert model_schema.is_supported_target(model_schema.TARGET_COMPRESSIVE)
    assert not model_schema.is_supported_target("toughness_J")
    assert "MPa" in model_schema.target_display(model_schema.TARGET_COMPRESSIVE)


# --------------------------------------------------------------------------- #
# Training
# --------------------------------------------------------------------------- #
@sklearn_only
def test_insufficient_rows_refuses_training():
    rows = _approved_lab_rows(n=6)
    with pytest.raises(train.InsufficientTrainingDataError) as exc:
        train.train_model(rows, demo=False)
    assert exc.value.n == 6 and exc.value.minimum == train.MIN_REAL_TRAINING_ROWS
    assert "Not enough approved data" in exc.value.message()


@sklearn_only
def test_approved_rows_train_a_model_with_metrics_and_card():
    rows = _approved_lab_rows(n=14)
    model = train.train_model(rows, target=model_schema.TARGET_COMPRESSIVE, date="2026-01-01")
    assert model.validation_status == model_schema.VALIDATION_EXPERIMENTAL
    assert model.is_validated is False                  # never "validated" in v1
    assert model.n_train == 14
    for k in ("MAE", "RMSE", "method", "n_validation"):
        assert k in model.metrics
    # model card has the required honesty sections
    card = model.card
    for k in ("intended_use", "not_intended_use", "limitations", "known_failure_cases",
              "applicability_domain", "validation_status", "date_trained",
              "extraction_uncertainty_warning"):
        assert card.get(k)
    assert card["date_trained"] == "2026-01-01"
    md = model_card.render_markdown(card)
    assert "Intended use" in md and "NOT intended use" in md


@sklearn_only
def test_demo_model_is_flagged_demo_and_bypasses_gate():
    model = train.train_demo_model(n=12)
    assert model.is_demo and model.validation_status == model_schema.VALIDATION_DEMO
    assert model.source_type == training_data.SOURCE_DEMO
    assert any("DEMO" in lim for lim in model.card["limitations"])


@sklearn_only
def test_all_targets_train():
    for t in model_schema.SUPPORTED_TARGETS:
        model = train.train_demo_model(target=t)
        assert model.target == t and model.n_train > 0


def test_training_without_sklearn_raises_clear_error(monkeypatch):
    monkeypatch.setattr(train, "sklearn_available", lambda: False)
    with pytest.raises(train.SklearnNotAvailableError) as exc:
        train.train_model(_approved_lab_rows(n=14))
    assert "scikit-learn" in str(exc.value)


@sklearn_only
def test_no_target_values_refused():
    rows = _approved_lab_rows(n=12)
    for r in rows:
        r.compressive_strength_MPa = None
    with pytest.raises(train.NoTargetValuesError):
        train.train_model(rows, target=model_schema.TARGET_COMPRESSIVE)


# --------------------------------------------------------------------------- #
# Registry (safe paths, save/load, overwrite guard, no source-tree writes)
# --------------------------------------------------------------------------- #
def test_registry_refuses_protected_locations(tmp_path):
    src = pathlib.Path(flyash_phreeqc_ml.__file__).parent           # .../flyash_phreeqc_ml
    for bad in (src / "models", tmp_path / "data" / "raw" / "models",
                tmp_path / "data" / "processed" / "models"):
        with pytest.raises(model_registry.ModelRegistryError):
            model_registry.ModelRegistry(bad)


@sklearn_only
def test_registry_save_load_list_overwrite(tmp_path):
    model = train.train_demo_model()
    reg = model_registry.ModelRegistry(tmp_path / "reg")
    reg.save(model)
    records = reg.list_models()
    assert records and records[0]["target"] == model.target
    assert records[0]["validation_status"] == model_schema.VALIDATION_DEMO
    # the artifact + card + meta were written under the registry dir, not the source tree
    mdir = reg.model_dir(model.name)
    assert (mdir / "model.joblib").exists() and (mdir / "model_card.json").exists()
    src = str(pathlib.Path(flyash_phreeqc_ml.__file__).parent)
    assert src not in str(mdir)
    # overwrite guard
    with pytest.raises(model_registry.ModelExistsError):
        reg.save(model, overwrite=False)
    reg.save(model, overwrite=True)                     # explicit confirm → ok
    loaded = reg.load(model.name)
    assert loaded.target == model.target


@sklearn_only
def test_has_strength_model_query(tmp_path):
    base = tmp_path / "reg"
    assert model_registry.has_strength_model(base) is False
    reg = model_registry.ModelRegistry(base)
    reg.save(train.train_demo_model())
    assert model_registry.has_strength_model(base) is True
    assert model_registry.has_strength_model(base, include_demo=False) is False
    assert model_schema.TARGET_COMPRESSIVE in model_registry.available_targets(base)


def test_default_registry_dir_under_outputs():
    d = str(model_registry.default_registry_dir())
    assert d.endswith("outputs/model_registry") or "outputs" in d


# --------------------------------------------------------------------------- #
# Prediction
# --------------------------------------------------------------------------- #
def test_predict_requires_a_model():
    p = predict_mod.predict(None, {"plastic_dosage_percent": 10})
    assert p.refused and p.refusal_reason and "No trained model" in p.refusal_reason


@sklearn_only
def test_prediction_returns_value_interval_and_status():
    model = train.train_demo_model()
    p = predict_mod.predict(model, {"plastic_dosage_percent": 10, "water_binder_ratio": 0.4,
                                    "curing_time_days": 28, "CaO_wt": 24, "plastic_type": "PET"})
    assert not p.refused and isinstance(p.value, float)
    assert p.lower is not None and p.upper is not None and p.lower <= p.value <= p.upper
    assert p.status == model_schema.VALIDATION_DEMO
    assert any("not a validated" in w or "not validated" in w.lower() for w in p.warnings)


@sklearn_only
def test_prediction_incomplete_inputs_refused():
    model = train.train_demo_model()
    p = predict_mod.predict(model, {"fly_ash_source": "x"})          # no core feature
    assert p.refused and "too incomplete" in (p.refusal_reason or "")


@sklearn_only
def test_prediction_out_of_domain_warning():
    model = train.train_demo_model()
    p = predict_mod.predict(model, {"plastic_dosage_percent": 9999})
    assert p.out_of_domain and any("Out-of-domain" in w for w in p.warnings)


@sklearn_only
def test_prediction_missing_features_warning():
    model = train.train_demo_model()
    p = predict_mod.predict(model, {"plastic_dosage_percent": 10})   # most inputs blank
    assert p.missing_features and any("left blank" in w for w in p.warnings)


@sklearn_only
def test_demo_prediction_flagged_demo():
    model = train.train_demo_model()
    p = predict_mod.predict(model, {"plastic_dosage_percent": 10, "curing_time_days": 28})
    assert p.is_demo and any("DEMO MODEL" in w for w in p.warnings)


# --------------------------------------------------------------------------- #
# Persistence round-trip for the curated dataset
# --------------------------------------------------------------------------- #
def test_dataset_save_load_roundtrip(tmp_path):
    rows = _approved_lab_rows(n=4)
    path = tmp_path / "ds" / "composite.jsonl"
    training_data.save_dataset(path, rows)
    back = training_data.load_dataset(path)
    assert len(back) == 4
    assert back[0].user_review_status == training_data.REVIEW_APPROVED
    assert back[0].compressive_strength_MPa is not None


def test_feature_schema_core_features_present():
    assert set(feature_schema.CORE_FEATURES).issubset(set(feature_schema.ALL_FEATURES))


# --------------------------------------------------------------------------- #
# Assistant integration (routing; no LLM number; PHREEQC not the strength engine)
# --------------------------------------------------------------------------- #
from flyash_phreeqc_ml.agent import agent_orchestrator as orch       # noqa: E402
from flyash_phreeqc_ml.agent import agent_state, domains             # noqa: E402

_STRENGTH_PROMPT = ("I am mixing fly ash with PET plastic and want to predict compressive "
                    "strength after 28 days")


def _has_mpa_number(text: str) -> bool:
    return any(tok.lower().endswith("mpa") and any(c.isdigit() for c in tok)
              for tok in text.replace("(", " ").replace(")", " ").split())


def test_strength_prompt_routes_to_polymer_composite_not_phreeqc():
    d = domains.classify(_STRENGTH_PROMPT)
    assert d == domains.POLYMER_COMPOSITE
    assert not domains.is_executable(d)                 # PHREEQC is never the strength engine
    assert domains.supports_ml_surrogate(d)


def test_no_model_routes_to_literature_not_a_prediction():
    s = agent_state.AgentState()
    r = orch.respond(s, _STRENGTH_PROMPT, use_ai=False, ml_model_available=False)
    msg = r.assistant_message
    assert "literature" in msg.lower() or "Evidence Library" in msg
    assert domains.ML_SURROGATE_MARKER not in msg       # no false "model available" claim
    assert not _has_mpa_number(msg)                     # no fabricated number


def test_trained_model_offers_ml_surrogate():
    s = agent_state.AgentState()
    r = orch.respond(s, _STRENGTH_PROMPT, use_ai=False, ml_model_available=True)
    msg = r.assistant_message
    assert domains.ML_SURROGATE_MARKER in msg
    assert "Prediction Models" in msg
    assert "experimental" in msg.lower() and "not validated" in msg.lower()
    assert not _has_mpa_number(msg)                     # still no LLM-generated number


def test_leaching_prompt_never_gets_ml_offer_even_if_model_exists():
    s = agent_state.AgentState()
    r = orch.respond(s, "Leach Class C fly ash in 0.5 M NaOH and predict pH and calcium release",
                     use_ai=False, ml_model_available=True)
    assert s.domain == domains.LEACHING_GEOCHEMISTRY
    assert domains.ML_SURROGATE_MARKER not in r.assistant_message


def test_planning_message_default_unchanged_without_model():
    # The ml_model_available=False message must equal the historical planning-only message.
    base = domains.planning_only_message(domains.POLYMER_COMPOSITE)
    explicit = domains.planning_only_message(domains.POLYMER_COMPOSITE, ml_model_available=False)
    assert base == explicit
    assert domains.ML_SURROGATE_MARKER not in base
