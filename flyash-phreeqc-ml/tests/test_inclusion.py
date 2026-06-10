"""Tests for the single comparison-inclusion function (compare/inclusion.py).

Pins: counts ↔ exclusion-table arithmetic consistency, one correct reason per
excluded row, status-aware filtering (unsafe excluded by default / flagged when
toggled), the scenario-level collapse trigger (6→2 fires, 1:1 does not), and the
overall validity-status rules. Synthetic data only.
"""
from __future__ import annotations

import pandas as pd

from flyash_phreeqc_ml import replicates
from flyash_phreeqc_ml.compare import comparison_inclusion
from flyash_phreeqc_ml.compare import inclusion as I

# Three exact-eligible batch scenarios (open CO2), so we can map 1:1 or collapse.
MANIFEST = pd.DataFrame([
    {"phreeqc_record_key": k, "state": "batch", "liquid_solid_ratio": 5.0,
     "CO2_condition": "open", "temperature_C": float("nan"), "scenario_label": k}
    for k in ("k1", "k2", "k3")
])


def _build(data_rows, mapping_pairs, *, pred_by_key=None, pred=13.1):
    """Make (data, mapping, comparison_df) for variable final_pH."""
    data = pd.DataFrame(data_rows)
    mapping = pd.DataFrame(mapping_pairs, columns=["sample_id", "phreeqc_record_key"])
    keymap = dict(mapping_pairs)
    comp = data.copy()
    comp["phreeqc_record_key"] = comp["sample_id"].map(keymap)
    pbk = pred_by_key or {k: pred for k in set(keymap.values())}
    comp["phreeqc_pH"] = comp["phreeqc_record_key"].map(pbk)
    return data, mapping, comp


def _naoh(sid, ph=13.0, **extra):
    row = {"sample_id": sid, "leachant": "NaOH", "liquid_solid_ratio": 5,
           "CO2_condition": "open", "final_pH": ph}
    row.update(extra)
    return row


# --------------------------------------------------------------------------- #
# Counts ↔ exclusion arithmetic
# --------------------------------------------------------------------------- #
def test_counts_and_exclusion_are_consistent():
    data = [_naoh("S1"), _naoh("S2"), _naoh("S3"),
            {"sample_id": "A1", "leachant": "HCl", "acid_M": 0.5, "liquid_solid_ratio": 5,
             "CO2_condition": "open", "final_pH": 3.2},     # unsafe
            _naoh("U1")]                                     # unmapped
    mapping = [("S1", "k1"), ("S2", "k1"), ("S3", "k1"), ("A1", "k1")]
    d, m, comp = _build(data, mapping)
    inc = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST)

    assert inc["n_total"] == 5
    assert inc["rows_plotted"] + len(inc["excluded"]) == inc["n_total"]
    assert sum(inc["reason_counts"].values()) == len(inc["excluded"])
    # 3 NaOH plotted; HCl excluded (unsafe), U1 excluded (no mapping).
    assert inc["rows_plotted"] == 3
    assert inc["reason_counts"][I.REASON_NO_MAPPING] == 1
    assert inc["reason_counts"][I.REASON_UNSAFE] == 1
    assert inc["unmapped_rows"] == 1
    assert inc["rows_with_mapping"] == 4


# --------------------------------------------------------------------------- #
# One correct reason per excluded row
# --------------------------------------------------------------------------- #
def test_reason_no_mapping():
    d, m, comp = _build([_naoh("S1")], [])  # no mapping rows
    inc = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST)
    assert inc["excluded"].iloc[0]["reason"] == I.REASON_NO_MAPPING


def test_reason_unsafe_default_excluded():
    data = [{"sample_id": "A1", "leachant": "HCl", "acid_M": 0.5, "liquid_solid_ratio": 5,
             "CO2_condition": "open", "final_pH": 3.2}]
    d, m, comp = _build(data, [("A1", "k1")])
    inc = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST)
    assert inc["rows_plotted"] == 0
    assert inc["excluded"].iloc[0]["reason"] == I.REASON_UNSAFE
    assert inc["excluded"].iloc[0]["mapping_status"] == replicates.MAPPING_STATUS_UNSAFE


def test_reason_model_prediction_missing():
    # Mapped NaOH row but the model prediction for this variable is NaN.
    d, m, comp = _build([_naoh("S1")], [("S1", "k1")], pred_by_key={"k1": float("nan")})
    inc = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST)
    assert inc["excluded"].iloc[0]["reason"] == I.REASON_NO_PREDICTION


def test_reason_measured_value_missing():
    d, m, comp = _build([_naoh("S1", ph="")], [("S1", "k1")])  # blank measured pH
    inc = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST)
    assert inc["excluded"].iloc[0]["reason"] == I.REASON_NO_MEASURED


# --------------------------------------------------------------------------- #
# Status-aware filtering (unsafe excluded by default; flagged when toggled)
# --------------------------------------------------------------------------- #
def test_unsafe_excluded_by_default_and_flagged_when_toggled():
    data = [{"sample_id": "A1", "leachant": "HCl", "acid_M": 0.5, "liquid_solid_ratio": 5,
             "CO2_condition": "open", "final_pH": 3.2}]
    d, m, comp = _build(data, [("A1", "k1")])

    off = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST, include_unsafe=False)
    assert off["rows_plotted"] == 0  # never in the default plot

    on = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST, include_unsafe=True)
    assert on["rows_plotted"] == 1
    assert bool(on["plotted"].iloc[0]["flagged"]) is True
    assert on["validity"] == I.VALIDITY_UNSAFE


# --------------------------------------------------------------------------- #
# Collapse trigger
# --------------------------------------------------------------------------- #
def test_collapse_triggers_on_six_to_two():
    data = [_naoh(f"S{i}", ph=13.0 + i * 0.05) for i in range(6)]
    mapping = [(f"S{i}", "k1" if i < 3 else "k2") for i in range(6)]  # 6 rows -> 2 preds
    d, m, comp = _build(data, mapping, pred_by_key={"k1": 13.1, "k2": 12.9})
    inc = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST)
    assert inc["rows_plotted"] == 6
    assert inc["unique_predictions_used"] == 2
    assert inc["collapse_warning"] is True


def test_collapse_does_not_trigger_on_one_to_one():
    data = [_naoh(f"S{i}", ph=13.0 + i * 0.1) for i in range(3)]
    mapping = [(f"S{i}", f"k{i + 1}") for i in range(3)]  # 3 rows -> 3 distinct preds
    d, m, comp = _build(data, mapping, pred_by_key={"k1": 13.1, "k2": 12.9, "k3": 12.7})
    inc = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST)
    assert inc["rows_plotted"] == 3
    assert inc["unique_predictions_used"] == 3
    assert inc["collapse_warning"] is False


# --------------------------------------------------------------------------- #
# Overall validity rules
# --------------------------------------------------------------------------- #
def test_validity_valid_all_exact_enough_rows():
    data = [_naoh(f"S{i}", ph=13.0 + i * 0.1) for i in range(3)]
    mapping = [(f"S{i}", f"k{i + 1}") for i in range(3)]
    d, m, comp = _build(data, mapping, pred_by_key={"k1": 13.1, "k2": 12.9, "k3": 12.7})
    inc = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST, min_valid_rows=3)
    assert inc["validity"] == I.VALIDITY_VALID
    assert inc["validity_severity"] == "success"


def test_validity_single_sample():
    d, m, comp = _build([_naoh("S1")], [("S1", "k1")])
    inc = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST)
    assert inc["validity"] == I.VALIDITY_SINGLE_SAMPLE


def test_validity_preliminary_when_scenario_level_included():
    # time_min makes the mapping scenario-level (model can't confirm time).
    data = [_naoh(f"S{i}", ph=13.0 + i * 0.1, time_min=10) for i in range(3)]
    mapping = [(f"S{i}", f"k{i + 1}") for i in range(3)]
    d, m, comp = _build(data, mapping, pred_by_key={"k1": 13.1, "k2": 12.9, "k3": 12.7})
    inc = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST)
    assert replicates.MAPPING_STATUS_SCENARIO in set(inc["plotted"]["mapping_status"])
    assert inc["validity"] == I.VALIDITY_PRELIMINARY


def test_validity_needs_new_when_nothing_plotted_but_measured_exists():
    d, m, comp = _build([_naoh("S1"), _naoh("S2")], [])  # measured present, all unmapped
    inc = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST)
    assert inc["rows_plotted"] == 0
    assert inc["validity"] == I.VALIDITY_NEEDS_NEW


def test_validity_nothing_to_compare_when_no_measured_values():
    d, m, comp = _build([_naoh("S1", ph="")], [("S1", "k1")])  # mapped but no measured value
    inc = comparison_inclusion(d, m, comp, "final_pH", manifest=MANIFEST)
    assert inc["measured_rows_available"] == 0
    assert inc["validity"] == I.VALIDITY_NONE


def test_only_valid_status_implies_validation():
    # No non-valid validity message should claim the model is validated.
    for v in (I.VALIDITY_PRELIMINARY, I.VALIDITY_SINGLE_SAMPLE, I.VALIDITY_UNSAFE,
              I.VALIDITY_NEEDS_NEW, I.VALIDITY_NONE):
        msg = I._validity_message(v, rows_plotted=2, min_valid_rows=3, n_unsafe_plotted=1)
        assert "validated" not in msg.lower()
    valid_msg = I._validity_message(I.VALIDITY_VALID, rows_plotted=3, min_valid_rows=3,
                                    n_unsafe_plotted=0)
    assert "validated" in valid_msg.lower()
