"""MATRIX (c) — CLAIM: residuals are exactly ``measured − model predicted`` for every
variable. Computed against a model with hand-known predictions and asserted to the
exact arithmetic value (the sign convention and the join are correct, not approximate).
"""
from __future__ import annotations

import pytest

from flyash_phreeqc_ml.compare import compare_measured_to_manifest


def test_residuals_equal_hand_computed_values(hand_residual_dataset):
    d = hand_residual_dataset
    comp = compare_measured_to_manifest(d["measured"], d["manifest"], d["mapping"])
    row = comp.iloc[0]
    # residual = measured − model predicted, exactly.
    assert row["residual_pH"] == pytest.approx(d["expected_residual_pH"])    # 13.0 − 12.5
    assert row["residual_Ca"] == pytest.approx(d["expected_residual_Ca"])    # 2.6 − 2.0
    # Positive = measured higher than the model (sign convention).
    assert row["residual_pH"] > 0 and row["residual_Ca"] > 0
