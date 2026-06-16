"""Tests for the refined-sweep loop (``strategy.refined_sweep_plan`` + provenance).

Pins: deterministic conversion of each suggestion kind into concrete sweep values; physical
clamping (no ≤0 concentration/time); the small-sweep cap; ``add_selected_output`` produces no
false chemistry values; the generated matrix is **plan-only** (never auto-run); refined
provenance (parent run / objective / edited values) is recorded in a saved run; and ranking /
refinement stay off the validation result path.
"""
from __future__ import annotations

import json

import pandas as pd

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.simulation import batch_executor as BE
from flyash_phreeqc_ml.simulation import matrix as MX
from flyash_phreeqc_ml.simulation import phreeqc_executor as E
from flyash_phreeqc_ml.simulation import run_registry as RR
from flyash_phreeqc_ml.simulation import strategy as S
from flyash_phreeqc_ml.simulation.scenario_schema import SimulationScenario


def _table(points):
    """points: list of (conc, pH, Ca, Fe) all status=success."""
    return pd.DataFrame([{"scenario_id": f"SIM-{i:03d}", "status": "success",
                          "leachant_concentration_M": c, "pH": ph, "Ca_mM": ca, "Fe_mM": fe}
                         for i, (c, ph, ca, fe) in enumerate(points, 1)])


def _plan(points, objective, *, max_scenarios=S.DEFAULT_MAX_SCENARIOS):
    t = _table(points)
    r = S.rank_results(t, objective, axis_col="leachant_concentration_M")
    sug = S.suggest_refined_sweep(r, t, "leachant_concentration_M")
    return sug, r, t, S.refined_sweep_plan(sug, r, t, max_scenarios=max_scenarios)


_POINTS = [(0.1, 9.0, 1.0, 0.5), (0.5, 11.0, 2.0, 0.8), (1.0, 13.0, 3.0, 1.2)]


# --------------------------------------------------------------------------- #
# Conversion to matrix values
# --------------------------------------------------------------------------- #
def test_extend_upper_to_values():
    sug, _, _, p = _plan(_POINTS, S.maximize("Ca"))     # best Ca at the highest conc
    assert sug.kind == "extend_upper" and p.kind == "extend_upper"
    assert p.can_generate
    assert max(p.values) > 1.0                           # added higher values
    assert all(v > 0 for v in p.values)


def test_extend_lower_to_values_is_physical():
    sug, _, _, p = _plan(_POINTS, S.minimize("Ca"))      # best Ca at the lowest conc
    assert sug.kind == "extend_lower"
    assert min(p.values) < 0.1                            # added lower values
    assert all(v > 0 for v in p.values)                  # never ≤ 0 for a concentration
    assert any("nonphysical" in w for w in p.warnings)   # clamping was flagged


def test_refine_internal_to_values():
    sug, _, _, p = _plan(_POINTS, S.target_ph(10, 12))   # best pH (11) is the middle scenario
    assert sug.kind == "refine_internal"
    assert len(p.values) >= 3
    assert all(0.1 <= v <= 1.0 for v in p.values)        # finer values within the swept span


def test_prevents_nonphysical_time_values():
    t = pd.DataFrame([{"scenario_id": "A", "status": "success", "time_min": 5.0, "Ca_mM": 2.0},
                      {"scenario_id": "B", "status": "success", "time_min": 10.0, "Ca_mM": 1.0}])
    r = S.rank_results(t, S.minimize("Ca"), axis_col="time_min")   # best at the low edge
    sug = S.suggest_refined_sweep(r, t, "time_min")
    p = S.refined_sweep_plan(sug, r, t)
    assert all(v > 0 for v in p.values)                  # no ≤ 0 reaction times


def test_cap_refined_matrix_size():
    sug, r, t, p = _plan(_POINTS, S.target_ph(10, 12), max_scenarios=3)  # refine → 5 raw values
    assert len(p.values) <= 3
    assert p.truncated and any("capped" in w for w in p.warnings)


def test_add_selected_output_makes_no_false_values():
    t = _table(_POINTS)
    r = S.rank_results(t, S.maximize("Sc"))              # Sc absent → no_rankable_metrics
    sug = S.suggest_refined_sweep(r, t, "leachant_concentration_M")
    p = S.refined_sweep_plan(sug, r, t)
    assert sug.kind == "add_selected_output"
    assert p.blocked and p.values == []                  # no fabricated chemistry values
    assert not p.can_generate


def test_narrow_failures_produces_smaller_range():
    t = pd.DataFrame([{"scenario_id": "A", "status": "failed"},
                      {"scenario_id": "B", "status": "failed"},
                      {"scenario_id": "C", "status": "success", "pH": 11.0, "Ca_mM": 2.0,
                       "leachant_concentration_M": 0.5}])
    r = S.rank_results(t, S.maximize("Ca"), axis_col="leachant_concentration_M")
    sug = S.suggest_refined_sweep(r, t, "leachant_concentration_M")
    p = S.refined_sweep_plan(sug, r, t)
    assert sug.kind == "narrow_failures"
    assert p.values and all(v > 0 for v in p.values)


# --------------------------------------------------------------------------- #
# The generated matrix is plan-only (never auto-runs)
# --------------------------------------------------------------------------- #
def test_refined_matrix_is_plan_only():
    sc = SimulationScenario.from_flat_dict(dict(
        material_name="fly ash", solid_mass_g=2, liquid_volume_mL=10, leachant_type="NaOH",
        leachant_concentration_M=0.5, time_min=60, temperature_C=25))
    _, _, _, p = _plan(_POINTS, S.maximize("Ca"))
    mtx = MX.build_simulation_matrix(sc, ranges={p.axis: p.values})
    assert (mtx["status"] == MX.STATUS_PLAN_ONLY).all()   # nothing is executed
    assert list(mtx["leachant_concentration_M"]) == sorted(p.values)


# --------------------------------------------------------------------------- #
# Provenance: a saved run records the refinement (incl. user edits)
# --------------------------------------------------------------------------- #
def _batch(tmp_path):
    results = []
    for i in (1, 2):
        sid = f"SIM-{i:03d}"
        (tmp_path / f"{sid}.pqi").write_text("SOLUTION 1\nEND\n")
        ex = E.ExecutionResult(sid, E.STATUS_SUCCESS, input_path=str(tmp_path / f"{sid}.pqi"),
                               output_path=str(tmp_path / f"{sid}.pqo"), runtime_seconds=0.1)
        results.append(BE.BatchScenarioResult(sid, ex, E.ParsedSimulation(
            sid, E.PARSE_PARSED, pH=11.0, element_totals_mM={"Ca": 2.0})))
    return BE.BatchResult(results=results, requested=2, max_scenarios=20)


def test_saved_run_records_refinement_provenance(tmp_path):
    refinement = {
        "parent_run_id": "sim-20260616-000000-parent",
        "parent_top_scenario_id": "SIM-002",
        "objective": "maximise Ca", "objective_kind": "maximize",
        "ranking_top_score": 1.0, "reason": "extended upward",
        "suggestion_kind": "extend_upper", "axis": "leachant_concentration_M",
        "suggested_values": [1.0, 1.5, 2.0],
        "applied_values": [1.0, 1.5, 2.0, 2.5],          # user added a value → edited
        "user_edited": True, "created_at": "2026-06-16T01:00:00",
    }
    record = RR.build_run_record(
        run_id="sim-child", created_at="2026-06-16T01:00:00", batch=_batch(tmp_path),
        refinement=refinement, label="refined run")
    reg = RR.SimulationRunRegistry(base_dir=tmp_path / "runs")
    d = reg.save_run(record)
    meta = json.loads((d / RR.RUN_METADATA_FILE).read_text())
    assert meta["refinement"]["parent_run_id"] == "sim-20260616-000000-parent"
    assert meta["refinement"]["parent_top_scenario_id"] == "SIM-002"
    assert meta["refinement"]["user_edited"] is True
    assert meta["refinement"]["applied_values"] == [1.0, 1.5, 2.0, 2.5]   # edits preserved


def test_no_refinement_when_none(tmp_path):
    record = RR.build_run_record(run_id="sim-plain", created_at="t", batch=_batch(tmp_path))
    assert record.refinement is None


def test_refinement_save_does_not_touch_result_path(tmp_path):
    results_csv = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    before = results_csv.stat().st_mtime if results_csv.exists() else None
    rec = RR.build_run_record(run_id="sim-x", created_at="t", batch=_batch(tmp_path),
                              refinement={"axis": "leachant_concentration_M"})
    RR.SimulationRunRegistry(base_dir=tmp_path / "runs").save_run(rec)
    after = results_csv.stat().st_mtime if results_csv.exists() else None
    assert before == after
