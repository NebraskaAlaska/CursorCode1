"""Tests for the descriptive systematic-bias layer (ml/residual_stats.py).

Pins (per the spec): only ``exact`` rows are counted (scenario-level / unsafe /
synthetic never are), the ``min_n`` sufficiency gate, the residual-sign → wording
mapping, and the arithmetic (mean / std / sem) against hand-computed values.
Synthetic data only; pandas/numpy only (no sklearn).
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from flyash_phreeqc_ml import replicates
from flyash_phreeqc_ml.ml import residual_stats as rs

approx = pytest.approx


def _row(sid, residual_ca, *, residual_ph=None, source_type=None, **extra):
    """A comparison row with the metadata needed for the fly-ash condition_key."""
    row = {
        "sample_id": sid,
        "leachant": "NaOH",
        "NaOH_M": 0.5,
        "CO2_condition": "OA",
        "time_min": 10,
        "liquid_solid_ratio": 5,
        "residual_Ca": residual_ca,
    }
    if residual_ph is not None:
        row["residual_pH"] = residual_ph
    if source_type is not None:
        row["source_type"] = source_type
    row.update(extra)
    return row


# Five exact Ca residuals chosen so the statistics are hand-checkable.
_FIVE = [0.1, 0.2, 0.3, 0.4, 0.5]          # mean 0.3
_FIVE_MEAN = 0.3
_FIVE_STD = math.sqrt(0.10 / 4)            # sample std, ddof=1 → 0.158113883…
_FIVE_SEM = _FIVE_STD / math.sqrt(5)


def _exact_five_plus_noise():
    """Five exact rows + one each of scenario-level / unsafe / synthetic (residual 99)."""
    rows = [_row(f"S{i}", v) for i, v in enumerate(_FIVE)]
    rows.append(_row("SCEN", 99.0))
    rows.append(_row("UNSAFE", 99.0))
    rows.append(_row("SYN", 99.0, source_type="synthetic_demo"))
    comp = pd.DataFrame(rows)
    statuses = {f"S{i}": replicates.MAPPING_STATUS_EXACT for i in range(len(_FIVE))}
    statuses["SCEN"] = replicates.MAPPING_STATUS_SCENARIO
    statuses["UNSAFE"] = replicates.MAPPING_STATUS_UNSAFE
    statuses["SYN"] = replicates.MAPPING_STATUS_EXACT  # exact, but synthetic → still excluded
    return comp, statuses


# --------------------------------------------------------------------------- #
# Filtering: only exact, non-synthetic rows are counted
# --------------------------------------------------------------------------- #
def test_only_exact_nonsynthetic_rows_counted():
    comp, statuses = _exact_five_plus_noise()
    table = rs.bias_table(comp, statuses, min_n=5)

    pooled = table[(table["element"] == "Ca")
                   & (table["condition_key"] == rs.ALL_CONDITIONS)].iloc[0]
    # The 99-valued scenario/unsafe/synthetic rows must not pollute the mean.
    assert int(pooled["n_exact_pairs"]) == 5
    assert pooled["mean_residual"] == approx(_FIVE_MEAN)


def test_synthetic_excluded_even_when_marked_exact():
    comp, statuses = _exact_five_plus_noise()
    # Sanity: the synthetic row IS marked exact, yet must not be counted.
    assert statuses["SYN"] == replicates.MAPPING_STATUS_EXACT
    mask = rs.exact_mask(comp, statuses)
    syn_idx = comp.index[comp["sample_id"] == "SYN"][0]
    assert not bool(mask.loc[syn_idx])
    assert int(mask.sum()) == 5


def test_no_exact_rows_returns_empty():
    comp = pd.DataFrame([_row("A", 1.0), _row("B", 2.0)])
    statuses = {"A": replicates.MAPPING_STATUS_SCENARIO,
                "B": replicates.MAPPING_STATUS_UNSAFE}
    table = rs.bias_table(comp, statuses)
    assert table.empty
    assert list(table.columns) == rs.BIAS_TABLE_COLUMNS


# --------------------------------------------------------------------------- #
# Arithmetic against hand-computed values
# --------------------------------------------------------------------------- #
def test_mean_std_sem_match_hand_computation():
    comp, statuses = _exact_five_plus_noise()
    table = rs.bias_table(comp, statuses, min_n=5)
    pooled = table[(table["element"] == "Ca")
                   & (table["condition_key"] == rs.ALL_CONDITIONS)].iloc[0]

    assert int(pooled["n_exact_pairs"]) == 5
    assert pooled["mean_residual"] == approx(_FIVE_MEAN)
    assert pooled["std"] == approx(_FIVE_STD)
    assert pooled["sem"] == approx(_FIVE_SEM)
    assert bool(pooled["sufficient"]) is True
    assert pooled["unit"] == "mM"


def test_single_pair_has_nan_std_and_sem():
    comp = pd.DataFrame([_row("S0", 0.4)])
    table = rs.bias_table(comp, {"S0": replicates.MAPPING_STATUS_EXACT}, min_n=5)
    pooled = table[table["condition_key"] == rs.ALL_CONDITIONS].iloc[0]
    assert int(pooled["n_exact_pairs"]) == 1
    assert pooled["mean_residual"] == approx(0.4)
    assert pd.isna(pooled["std"])
    assert pd.isna(pooled["sem"])
    assert bool(pooled["sufficient"]) is False


# --------------------------------------------------------------------------- #
# min_n gating
# --------------------------------------------------------------------------- #
def test_min_n_gate_marks_insufficient():
    rows = [_row(f"S{i}", v) for i, v in enumerate([0.1, 0.2, 0.3])]  # n = 3
    comp = pd.DataFrame(rows)
    statuses = {f"S{i}": replicates.MAPPING_STATUS_EXACT for i in range(3)}

    table = rs.bias_table(comp, statuses, min_n=5)
    pooled = table[table["condition_key"] == rs.ALL_CONDITIONS].iloc[0]
    assert int(pooled["n_exact_pairs"]) == 3
    assert bool(pooled["sufficient"]) is False
    # The mean is still stored (the UI hides it), but describe_* says "insufficient".
    msg = rs.describe_bias_row(pooled.to_dict(), min_n=5)
    assert "insufficient exact pairs (3 of 5 needed)" in msg


def test_min_n_threshold_is_inclusive():
    rows = [_row(f"S{i}", v) for i, v in enumerate(_FIVE)]  # n = 5
    comp = pd.DataFrame(rows)
    statuses = {f"S{i}": replicates.MAPPING_STATUS_EXACT for i in range(5)}
    table = rs.bias_table(comp, statuses, min_n=5)
    pooled = table[table["condition_key"] == rs.ALL_CONDITIONS].iloc[0]
    assert bool(pooled["sufficient"]) is True


# --------------------------------------------------------------------------- #
# Sign → wording
# --------------------------------------------------------------------------- #
def test_bias_direction_sign_mapping():
    assert rs.bias_direction(0.3) == "under"     # measured > model → underpredict
    assert rs.bias_direction(-0.3) == "over"     # measured < model → overpredict
    assert rs.bias_direction(0.0) is None
    assert rs.bias_direction(float("nan")) is None
    assert rs.bias_direction(None) is None


def test_describe_row_underpredict_wording():
    row = {"element": "Ca", "condition_key": rs.ALL_CONDITIONS, "n_exact_pairs": 5,
           "mean_residual": _FIVE_MEAN, "std": _FIVE_STD, "sem": _FIVE_SEM,
           "sufficient": True, "unit": "mM"}
    msg = rs.describe_bias_row(row, min_n=5)
    assert "underpredicts Ca" in msg
    assert "all conditions" in msg
    assert "mM" in msg
    assert "5 exact-mapped pairs" in msg


def test_describe_row_overpredict_wording():
    row = {"element": "Si", "condition_key": "NaOH0.5M_OA_10min_LS5", "n_exact_pairs": 6,
           "mean_residual": -1.2, "std": 0.4, "sem": 0.16, "sufficient": True, "unit": "mM"}
    msg = rs.describe_bias_row(row, min_n=5)
    assert "overpredicts Si" in msg
    # The verb carries the sign; the magnitude is reported as a positive number.
    assert "1.2" in msg
    assert "-1.2" not in msg


def test_negative_pooled_mean_reads_as_overpredict():
    rows = [_row(f"P{i}", 0.0, residual_ph=v) for i, v in enumerate([-0.5, -0.4, -0.6, -0.5, -0.5])]
    comp = pd.DataFrame(rows)
    statuses = {f"P{i}": replicates.MAPPING_STATUS_EXACT for i in range(5)}
    table = rs.bias_table(comp, statuses, min_n=5)
    pooled = table[(table["element"] == "pH")
                   & (table["condition_key"] == rs.ALL_CONDITIONS)].iloc[0]
    assert pooled["mean_residual"] < 0
    assert pooled["unit"] == "pH units"
    assert "overpredicts pH" in rs.describe_bias_row(pooled.to_dict(), min_n=5)


# --------------------------------------------------------------------------- #
# Bands + plot frame + status collection
# --------------------------------------------------------------------------- #
def test_sufficient_bands_only_for_pooled_sufficient_elements():
    comp, statuses = _exact_five_plus_noise()
    table = rs.bias_table(comp, statuses, min_n=5)
    bands = rs.sufficient_bias_bands(table)
    assert "Ca" in bands
    assert bands["Ca"]["mean"] == approx(_FIVE_MEAN)
    assert bands["Ca"]["n"] == 5
    # min_n=6 would push the n=5 pooled row below threshold → no band.
    table6 = rs.bias_table(comp, statuses, min_n=6)
    assert rs.sufficient_bias_bands(table6) == {}


def test_exact_residuals_frame_excludes_noise():
    comp, statuses = _exact_five_plus_noise()
    pts = rs.exact_residuals(comp, statuses, "Ca")
    assert len(pts) == 5
    assert set(pts.columns) == {"sample_id", "condition_key", "residual"}
    assert 99.0 not in set(pts["residual"])


def test_collect_sample_statuses_uses_inclusion_join():
    # Exact-eligible manifest mirroring the inclusion test's known-good setup.
    manifest = pd.DataFrame([
        {"phreeqc_record_key": "k1", "state": "batch", "liquid_solid_ratio": 5.0,
         "CO2_condition": "OA", "temperature_C": float("nan"), "scenario_label": "k1"},
    ])
    data = pd.DataFrame([
        {"sample_id": "X1", "leachant": "NaOH", "liquid_solid_ratio": 5,
         "CO2_condition": "OA", "final_pH": 13.0},
    ])
    mapping = pd.DataFrame([{"sample_id": "X1", "phreeqc_record_key": "k1"}])
    comp = data.copy()
    comp["phreeqc_record_key"] = "k1"
    comp["phreeqc_pH"] = 13.1
    statuses = rs.collect_sample_statuses(data, mapping, comp, manifest=manifest)
    assert statuses.get("X1") == replicates.MAPPING_STATUS_EXACT
