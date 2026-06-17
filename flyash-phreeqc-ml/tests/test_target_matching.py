"""Tests for the target-matching / inverse simulation-search layer.

PHREEQC is **not** executed here — the module is pure (target parsing + grid building +
scoring of a result table the executor already produced). Coverage: deterministic target
parsing (pH range / target value / constraint / maximise-minimise / combined), the capped
search grid, scoring (range / value / constraint feasibility / missing-metric / best
candidate / feasible-first), no-auto-execution + off-result-path boundaries, and the target
spec landing in run provenance. One AppTest renders the Simulate Step-10 section end-to-end.
"""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml.simulation import target_matching as TM
from flyash_phreeqc_ml.simulation.scenario_schema import SimulationScenario


def _scenario(**over):
    flat = dict(material_name="Class C fly ash", solid_mass_g=2.0, liquid_volume_mL=10.0,
                leachant_type="NaOH", leachant_concentration_M=0.5, time_min=60.0,
                temperature_C=25.0, target_elements=["Ca", "Si", "Al", "Fe"])
    flat.update(over)
    return SimulationScenario.from_flat_dict(flat)


def _executed(rows):
    """A result-table-like frame (status=success unless given)."""
    out = []
    for r in rows:
        d = {"status": "success"}
        d.update(r)
        out.append(d)
    return pd.DataFrame(out)


# --------------------------------------------------------------------------- #
# Target parsing (deterministic, no AI)
# --------------------------------------------------------------------------- #
def test_parse_ph_range():
    spec = TM.parse_target_spec("target pH 10 to 12")
    assert len(spec.metrics) == 1
    m = spec.metrics[0]
    assert m.kind == TM.TARGET_RANGE and m.column == "pH"
    assert (m.low, m.high) == (10.0, 12.0)


def test_parse_target_value():
    spec = TM.parse_target_spec("Ca around 5 mM")
    m = spec.metrics[0]
    assert m.kind == TM.TARGET_VALUE and m.column == "Ca_mM" and m.value == 5.0
    assert m.tolerance and m.tolerance > 0          # a default ± is set, never None


def test_parse_constraint_less_than():
    for text in ("Fe below 0.1 mM", "Fe < 0.1 mM", "keep Fe below 0.1"):
        spec = TM.parse_target_spec(text)
        m = spec.metrics[0]
        assert m.kind == TM.TARGET_CONSTRAINT and m.column == "Fe_mM"
        assert m.op == TM.OP_LT and m.threshold == 0.1, text


def test_parse_constraint_at_least_is_ge():
    spec = TM.parse_target_spec("Al at least 1 mM")
    m = spec.metrics[0]
    assert m.kind == TM.TARGET_CONSTRAINT and m.op == TM.OP_GE and m.threshold == 1.0


def test_parse_maximize_minimize_pair():
    spec = TM.parse_target_spec("highest Ca with lowest Fe")
    kinds = {(m.column, m.kind) for m in spec.metrics}
    assert ("Ca_mM", TM.TARGET_MAXIMIZE) in kinds
    assert ("Fe_mM", TM.TARGET_MINIMIZE) in kinds


def test_parse_maximize_with_constraint():
    spec = TM.parse_target_spec("maximize Si while keeping Fe below 0.1 mM")
    obj = spec.objective_metrics
    con = spec.constraint_metrics
    assert [(m.column, m.kind) for m in obj] == [("Si_mM", TM.TARGET_MAXIMIZE)]
    assert [(m.column, m.op, m.threshold) for m in con] == [("Fe_mM", TM.OP_LT, 0.1)]


def test_parse_value_and_min_combo():
    # The release-fraction example: "pH near 12 and low Al"
    spec = TM.parse_target_spec(
        "find a NaOH concentration and release fraction combination that gives pH near 12 and low Al")
    cols = {(m.column, m.kind) for m in spec.metrics}
    assert ("pH", TM.TARGET_VALUE) in cols
    assert ("Al_mM", TM.TARGET_MINIMIZE) in cols


def test_parse_no_target_returns_empty_with_note():
    spec = TM.parse_target_spec("just simulate it")
    assert not spec.is_defined
    assert spec.notes and any("manually" in n for n in spec.notes)


def test_parse_does_not_double_count_a_column():
    # Fe appears once as a constraint — it must not also become a maximise/value metric.
    spec = TM.parse_target_spec("keep Fe below 0.1 and minimize Fe")
    fe = [m for m in spec.metrics if m.column == "Fe_mM"]
    assert len(fe) == 1 and fe[0].kind == TM.TARGET_CONSTRAINT


# --------------------------------------------------------------------------- #
# Building the search grid
# --------------------------------------------------------------------------- #
def test_build_grid_cartesian_product():
    params = [TM.scenario_parameter("leachant_concentration_M", [0.1, 0.5, 1.0]),
              TM.release_fraction_parameter([0.001, 0.01])]
    cands, truncated = TM.build_search_grid(_scenario(), params)
    assert not truncated and len(cands) == 6
    # each candidate carries its varied conc + release fraction + plan-only status
    concs = {c.leachant_concentration_M for c in cands}
    rfs = {c.release_fraction for c in cands}
    assert concs == {0.1, 0.5, 1.0} and rfs == {0.001, 0.01}
    assert all(c.status == TM.STATUS_PLAN_ONLY for c in cands)
    # the scenario field override actually lands in the candidate's flat scenario
    c = next(c for c in cands if c.leachant_concentration_M == 1.0)
    assert c.scenario_flat["leachant_concentration_M"] == 1.0


def test_build_grid_is_capped():
    params = [TM.scenario_parameter("leachant_concentration_M", [0.1 * i for i in range(1, 9)]),
              TM.release_fraction_parameter([0.001, 0.005, 0.01, 0.02])]   # 8 × 4 = 32 > 20
    cands, truncated = TM.build_search_grid(_scenario(), params, max_scenarios=20)
    assert truncated and len(cands) == 20


def test_build_grid_empty_without_parameters():
    cands, truncated = TM.build_search_grid(_scenario(), [])
    assert cands == [] and truncated is False


def test_grid_preview_frame_columns():
    params = [TM.release_fraction_parameter([0.01])]
    cands, _ = TM.build_search_grid(_scenario(), params)
    df = TM.grid_preview_frame(cands)
    assert {"scenario_id", "leachant_concentration_M", "release_fraction", "status"} <= set(df.columns)
    assert (df["status"] == TM.STATUS_PLAN_ONLY).all()


# --------------------------------------------------------------------------- #
# Scoring a result table
# --------------------------------------------------------------------------- #
def test_score_target_range():
    table = _executed([{"scenario_id": "A", "pH": 11.0}, {"scenario_id": "B", "pH": 13.5},
                       {"scenario_id": "C", "pH": 9.0}])
    res = TM.score_results(TM.parse_target_spec("target pH 10 to 12"), table)
    assert res.ok
    # inside the band scores 1.0 and ranks first; the farthest out ranks last
    assert res.best["scenario_id"] == "A" and res.best["objective_score"] == 1.0
    order = list(res.ranked["scenario_id"])
    assert order[0] == "A" and order[-1] == "B"        # 13.5 is 1.5 out vs 9.0 only 1.0 out


def test_score_target_value():
    table = _executed([{"scenario_id": "A", "Ca_mM": 5.0}, {"scenario_id": "B", "Ca_mM": 6.0},
                       {"scenario_id": "C", "Ca_mM": 9.0}])
    res = TM.score_results(TM.parse_target_spec("Ca around 5 mM"), table)
    assert res.best["scenario_id"] == "A" and res.best["objective_score"] == 1.0


def test_score_constraint_feasibility_and_feasible_first():
    table = _executed([
        {"scenario_id": "A", "Si_mM": 20.0, "Fe_mM": 0.5},    # high Si but INFEASIBLE
        {"scenario_id": "B", "Si_mM": 18.0, "Fe_mM": 0.05},   # feasible, lower Si
        {"scenario_id": "C", "Si_mM": 3.0, "Fe_mM": 0.02},    # feasible, low Si
        {"scenario_id": "D", "Si_mM": 25.0, "Fe_mM": 0.9},    # highest Si but INFEASIBLE
    ])
    res = TM.score_results(TM.parse_target_spec("maximize Si while keeping Fe below 0.1 mM"), table)
    assert res.n_feasible == 2
    # the best is the feasible row with the highest Si, NOT the higher-Si infeasible ones
    assert res.best["scenario_id"] == "B" and res.best["feasible"] is True
    order = list(res.ranked["scenario_id"])
    assert order[:2] == ["B", "C"]                    # feasible first


def test_score_excludes_failed_rows():
    table = _executed([{"scenario_id": "ok", "Si_mM": 5.0},
                       {"scenario_id": "bad", "status": "failed", "Si_mM": 999.0}])
    res = TM.score_results(TM.parse_target_spec("maximize Si"), table)
    assert list(res.ranked["scenario_id"]) == ["ok"]      # the failed scenario is not ranked


def test_score_missing_metric_warns_no_fabrication():
    table = _executed([{"scenario_id": "A", "Ca_mM": 5.0}])
    res = TM.score_results(TM.parse_target_spec("maximize Sc"), table)
    assert res.status == TM.MATCH_NO_METRICS
    assert "Sc_mM" in res.missing_metrics
    assert any("not in the executed results" in w for w in res.warnings)
    assert res.best is None                                # nothing fabricated


def test_score_partial_missing_metric_still_ranks_present_one():
    table = _executed([{"scenario_id": "A", "Si_mM": 2.0}, {"scenario_id": "B", "Si_mM": 8.0}])
    # Si present, Sc absent — Si is ranked, Sc is reported missing (not scored)
    res = TM.score_results(
        TM.target_from_metrics([TM.metric_maximize("Si_mM"), TM.metric_maximize("Sc_mM")]), table)
    assert res.ok and "Sc_mM" in res.missing_metrics
    assert res.best["scenario_id"] == "B"


def test_score_pure_constraint_picks_first_feasible():
    table = _executed([{"scenario_id": "A", "Fe_mM": 0.5}, {"scenario_id": "B", "Fe_mM": 0.02}])
    res = TM.score_results(TM.parse_target_spec("Fe below 0.1 mM"), table)
    assert res.best["scenario_id"] == "B" and res.best["feasible"] is True
    assert res.n_feasible == 1


def test_score_no_feasible_warns():
    table = _executed([{"scenario_id": "A", "Fe_mM": 0.5}, {"scenario_id": "B", "Fe_mM": 0.9}])
    res = TM.score_results(TM.parse_target_spec("Fe below 0.1 mM"), table)
    assert res.n_feasible == 0
    assert any("No candidate satisfies all constraints" in w for w in res.warnings)
    assert res.best["feasible"] is False


def test_score_breakdown_has_metric_and_constraint_detail():
    table = _executed([{"scenario_id": "A", "Si_mM": 10.0, "Fe_mM": 0.05}])
    res = TM.score_results(TM.parse_target_spec("maximize Si while keeping Fe below 0.1 mM"), table)
    bd = res.breakdowns[0]
    assert bd.scenario_id == "A"
    assert bd.metric_scores and bd.metric_scores[0]["column"] == "Si_mM"
    assert bd.constraint_results and bd.constraint_results[0]["satisfied"] is True


def test_score_no_rows():
    res = TM.score_results(TM.parse_target_spec("maximize Si"),
                           _executed([{"scenario_id": "x", "status": "failed", "Si_mM": 1}]))
    assert res.status == TM.MATCH_NO_ROWS and res.best is None


def test_score_no_metrics_defined():
    res = TM.score_results(TM.parse_target_spec("just run it"),
                           _executed([{"scenario_id": "A", "Si_mM": 1.0}]))
    assert res.status == TM.MATCH_NO_METRICS


# --------------------------------------------------------------------------- #
# Provenance (the target spec lands in the saved run record)
# --------------------------------------------------------------------------- #
def test_target_match_provenance_payload():
    sc = _scenario()
    params = [TM.scenario_parameter("leachant_concentration_M", [0.1, 0.5]),
              TM.release_fraction_parameter([0.01])]
    cands, trunc = TM.build_search_grid(sc, params)
    table = _executed([{"scenario_id": c.scenario_id, "leachant_concentration_M":
                        c.leachant_concentration_M, "release_fraction": c.release_fraction,
                        "Si_mM": 5.0, "Fe_mM": 0.05} for c in cands])
    spec = TM.parse_target_spec("maximize Si while keeping Fe below 0.1 mM")
    res = TM.score_results(spec, table)
    prov = TM.target_match_provenance(spec, params, cands, res, created_at="2026-01-01T00:00:00")

    assert prov["target_spec"]["metrics"]                       # the target spec is recorded
    assert prov["search_parameters"][0]["name"] == "leachant_concentration_M"
    assert len(prov["candidate_grid"]) == len(cands)            # the full grid is recorded
    assert prov["best_candidate"]["scenario_id"] == res.best["scenario_id"]
    assert "not" in prov["not_validation"].lower() and "valid" in prov["not_validation"].lower()
    assert prov["scoring_method"]                               # the scoring method is documented


def test_build_run_record_stores_target_match(monkeypatch, tmp_path):
    from flyash_phreeqc_ml import config
    from flyash_phreeqc_ml.simulation import batch_executor, phreeqc_executor, run_registry
    monkeypatch.setattr(config, "SIMULATION_RUNS_DIR", tmp_path / "sim_runs")

    # A one-scenario batch with a NOT_RUN execution (no PHREEQC needed for the provenance test).
    ex = phreeqc_executor.ExecutionResult("TGT-001", phreeqc_executor.STATUS_NOT_RUN)
    batch = batch_executor.BatchResult(
        results=[batch_executor.BatchScenarioResult("TGT-001", ex, None)], requested=1)
    prov = {"target_spec": {"metrics": [{"kind": "maximize", "column": "Si_mM"}]},
            "best_candidate": {"scenario_id": "TGT-001"}, "not_validation": "not validation"}
    rec = run_registry.build_run_record(run_id="sim-x", created_at="t", batch=batch,
                                        target_match=prov)
    assert rec.target_match == prov
    assert rec.to_metadata_dict()["target_match"] == prov      # survives serialization


# --------------------------------------------------------------------------- #
# AppTest — the Simulate Step-10 target-matching section renders end-to-end
# --------------------------------------------------------------------------- #
def _by_key(elements, key):
    for el in elements:
        if getattr(el, "key", None) == key:
            return el
    raise KeyError(key)


def test_simulate_tab_renders_target_matching_section(monkeypatch, tmp_path):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    from flyash_phreeqc_ml import config
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")

    at = AppTest.from_file("app.py", default_timeout=120).run()
    assert not at.exception
    # The Simulate workflow lives in the Workspace section (the assistant is the default).
    at.session_state["nav_section"] = "Workspace"
    at.run()
    _by_key(at.text_area, "sim_desc").set_value(
        "2 g of Class C fly ash in 10 mL of 0.5 M NaOH for 60 minutes at room temperature")
    _by_key(at.text_area, "sim_outputs").set_value("maximize Si while keeping Fe below 0.1 mM")
    at.run()
    _by_key(at.button, "sim_parse_btn").click()
    at.run()
    _by_key(at.checkbox, "sim_confirm_chk").set_value(True)
    at.run()
    _by_key(at.button, "sim_gen_btn").click()
    at.run()

    assert not at.exception
    text = " ".join(str(m.value) for m in at.markdown)
    assert "Target matching (inverse search)" in text          # Step 10 rendered
    assert "Search grid" in text                               # the capped grid preview rendered
