"""MATRIX (f) — CLAIM: a **non-PHREEQC** model's predictions, supplied via the generic
CSV contract, are mapped and compared **end-to-end** through the same manifest →
suggestion → mapping-status → comparison → inclusion chain as PHREEQC — and nothing in
that chain touches the PHREEQC parser.
"""
from __future__ import annotations

from flyash_phreeqc_ml import mapping_table, replicates, scenarios
from flyash_phreeqc_ml.compare import compare_measured_to_manifest, comparison_inclusion
from flyash_phreeqc_ml.compare import inclusion as I
from flyash_phreeqc_ml.parsers import generic_prediction_parser as gpp


def test_generic_parser_normalizes_to_manifest_inputs(generic_prediction_dataset):
    parsed = generic_prediction_dataset["parsed"]
    assert list(parsed["record_key"]) == ["M1"]
    assert parsed.iloc[0]["model_name"] == "ToyThermo"
    assert parsed.iloc[0]["predicted_pH"] == 12.8
    # Identity-tagged provenance when already in the target unit.
    assert parsed.iloc[0]["predicted_Ca_mM__conversion_id"] == "identity"


def test_generic_prediction_end_to_end(generic_prediction_dataset):
    d = generic_prediction_dataset
    data, manifest, mapping = d["measured"], d["manifest"], d["mapping"]

    # The manifest is model-agnostic: a generic prediction is scored like a batch result.
    assert manifest.iloc[0]["state"] == "batch"
    assert manifest.iloc[0]["phreeqc_record_key"] == "M1"   # historical key name; generic id

    # Suggestion + mapping status work without knowing the model.
    table = mapping_table.build_suggestion_table(data, manifest, None)
    assert table.iloc[0]["mapping_status"] == replicates.MAPPING_STATUS_EXACT

    # Comparison + inclusion run through the same residual columns as PHREEQC.
    comp = compare_measured_to_manifest(data, manifest, mapping)
    assert "residual_pH" in comp.columns
    inc = comparison_inclusion(data, mapping, comp, "final_pH", manifest=manifest)
    assert inc["rows_plotted"] == 3
    assert inc["validity"] == I.VALIDITY_VALID
    # residual = 13.4 − 12.8 = 0.6 for every row.
    assert all(abs(r - 0.6) < 1e-9 for r in comp["residual_pH"])


def test_specific_contract_errors():
    import pandas as pd
    with __import__("pytest").raises(gpp.MissingRequiredColumn):
        gpp.parse_predictions(pd.DataFrame([{"record_key": "x", "pred_pH": 12.0}]))
    with __import__("pytest").raises(gpp.NoPredictionColumns):
        gpp.parse_predictions(pd.DataFrame([{"record_key": "x", "model_name": "m"}]))
    with __import__("pytest").raises(gpp.DuplicateRecordKey):
        gpp.parse_predictions(pd.DataFrame([
            {"record_key": "x", "model_name": "m", "pred_pH": 12.0},
            {"record_key": "x", "model_name": "m", "pred_pH": 12.1}]))
    with __import__("pytest").raises(gpp.InvalidPredictionValue):
        gpp.parse_predictions(pd.DataFrame([
            {"record_key": "x", "model_name": "m", "pred_pH": "not-a-number"}]))
