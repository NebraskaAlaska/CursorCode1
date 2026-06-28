"""XRD honesty: refuse an exact pattern without phase/structure; never claim measured/identified."""
import routes_materials
import routes_xrd


def test_no_phases_refuses_exact_pattern():
    result = routes_xrd.expected({"phases": []})
    assert result["result_type"] == routes_xrd.STATUS_REFERENCE_NEEDED
    assert result["measured"] is False
    assert result["exact_pattern"] is False
    assert result["peaks"] == []
    assert "reference" in result["message"].lower()


def test_formula_only_card_cannot_yield_a_pattern():
    # A card minted from a bare formula has no phases → XRD must refuse.
    card = routes_materials.synthesize(formula="NaCl")
    result = routes_xrd.expected(card)
    assert result["result_type"] == routes_xrd.STATUS_REFERENCE_NEEDED
    assert result["peaks"] == []
    assert result["measured"] is False


def test_known_phase_returns_expected_reference_peaks():
    result = routes_xrd.expected({"phases": [{"name": "Quartz"}]})
    assert result["result_type"] == routes_xrd.STATUS_REFERENCE_AVAILABLE
    assert result["measured"] is False          # expected, never measured
    assert result["exact_pattern"] is False
    assert result["peaks"], "expected approximate reference peaks for a known phase"
    assert all(p["approx_2theta_deg"] for p in result["peaks"])
    # Honesty language must be present.
    assert any("never a measured identification" in w for w in result["warnings"])


def test_unknown_phase_is_marked_reference_needed():
    result = routes_xrd.expected({"phases": [{"name": "Quartz"}, {"name": "Blargite"}]})
    statuses = {e["phase"]: e["status"] for e in result["entries"]}
    assert statuses["Quartz"] == routes_xrd.STATUS_REFERENCE_AVAILABLE
    assert statuses["Blargite"] == routes_xrd.STATUS_REFERENCE_NEEDED
    assert "Blargite" in result["unknown_phases"]
