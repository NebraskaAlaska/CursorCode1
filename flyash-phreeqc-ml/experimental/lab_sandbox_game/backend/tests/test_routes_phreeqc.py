"""PHREEQC gate: preview-only until inputs are complete; never executes; confirm gate is enforced."""
import routes_phreeqc


def _full_setup():
    return {
        "composition": {"basis": "oxide_wt_percent", "values": {"SiO2": 34, "CaO": 24}},
        "source_term": "1% release of Ca, Si, Al, Fe, Na, K",
        "leachant": "0.5 M NaOH",
        "database": "phreeqc.dat",
        "temperature_c": 25,
    }


def setup_function(_):
    routes_phreeqc.reset_store()


def test_missing_inputs_returns_no_preview():
    result = routes_phreeqc.preview({"composition": {"values": {"SiO2": 34}}})
    assert result["status"] == "missing_inputs"
    assert result["preview_id"] is None
    assert result["preview_text"] is None
    assert result["executed"] is False
    assert set(result["missing"]) == {"source_term", "leachant", "database"}


def test_full_inputs_build_a_preview_but_execute_nothing():
    result = routes_phreeqc.preview(_full_setup())
    assert result["status"] == "ready_for_review"
    assert result["preview_id"] and result["preview_id"].startswith("pqprev_")
    assert "PREVIEW" in result["preview_text"]
    assert result["executed"] is False
    assert result["auto_run"] is False


def test_run_without_a_known_preview_id_errors():
    result = routes_phreeqc.run(preview_id="does-not-exist", confirm=True)
    assert result["status"] == "error"
    assert result["executed"] is False


def test_run_without_confirmation_is_held():
    pid = routes_phreeqc.preview(_full_setup())["preview_id"]
    result = routes_phreeqc.run(preview_id=pid, confirm=False)
    assert result["status"] == "awaiting_confirmation"
    assert result["executed"] is False
    assert result["auto_run"] is False


def test_run_with_confirmation_satisfies_gate_but_still_does_not_execute():
    pid = routes_phreeqc.preview(_full_setup())["preview_id"]
    result = routes_phreeqc.run(preview_id=pid, confirm=True)
    assert result["status"] == "confirmed_not_executed"
    assert result["executed"] is False           # the scaffold NEVER runs PHREEQC
    assert result["auto_run"] is False
    assert "does NOT execute" in result["message"]


def test_preview_id_is_deterministic():
    a = routes_phreeqc.preview(_full_setup())["preview_id"]
    b = routes_phreeqc.preview(_full_setup())["preview_id"]
    assert a == b
