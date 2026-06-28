"""ICP honesty: process only supplied rows; never fabricate measured data from a composition."""
import routes_icp


def test_no_rows_generates_nothing():
    result = routes_icp.process([])
    assert result["corrected"] == []
    assert result["residuals"] == []
    assert result["fabricated"] is False
    assert any("does not generate" in w for w in result["warnings"])


def test_processes_only_supplied_rows_no_invention():
    rows = [
        {"sample_id": "S1", "element": "Ca", "concentration": 40.078, "unit": "mg/L", "role": "measured"},
        {"sample_id": "S1", "element": "Ca", "concentration": 80.156, "unit": "mg/L", "role": "predicted"},
    ]
    result = routes_icp.process(rows)
    assert len(result["corrected"]) == len(rows)           # exactly as many rows as provided
    # 40.078 mg/L Ca ≈ 1.0 mM; 80.156 ≈ 2.0 mM.
    by_role = {r["role"]: r["value_mM"] for r in result["corrected"]}
    assert abs(by_role["measured"] - 1.0) < 1e-3
    assert abs(by_role["predicted"] - 2.0) < 1e-3
    # Residual exists for the measured+predicted pair (validation, not simulation).
    assert len(result["residuals"]) == 1
    assert abs(result["residuals"][0]["residual_mM"] - (-1.0)) < 1e-3


def test_below_detection_limit_is_flagged_not_zeroed():
    rows = [{"sample_id": "S1", "element": "Si", "concentration": 0.01, "unit": "mg/L",
             "detection_limit": 0.05, "role": "measured"}]
    result = routes_icp.process(rows)
    assert result["corrected"][0]["below_detection_limit"] is True


def test_refuses_to_fabricate_measured_from_composition():
    result = routes_icp.refuse_measured_from_composition({"SiO2": 34, "CaO": 24})
    assert result["accepted"] is False
    assert result["fabricated"] is False
    assert "fabricating measured data" in result["reason"]
    assert routes_icp.can_synthesize_measured_from_composition() is False
