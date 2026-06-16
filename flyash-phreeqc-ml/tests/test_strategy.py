"""Tests for the simulation strategy / ranking layer (``simulation.strategy``).

Pins: deterministic objective parsing; ranking with complete data, a missing-metric warning,
and a maximize/minimize weighted objective; refined-sweep suggestions at the lower edge, the
upper edge, and an internal optimum; and the hard boundaries — the module executes nothing and
imports no AI / no result-path code.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd

import flyash_phreeqc_ml as pkg
from flyash_phreeqc_ml.simulation import strategy as S

PKG_DIR = Path(pkg.__file__).resolve().parent


def _table(rows):
    return pd.DataFrame(rows)


def _sweep_table(points):
    """points: list of (conc, pH, Ca, Fe). All status=success."""
    return _table([{"scenario_id": f"SIM-{i:03d}", "status": "success",
                    "leachant_concentration_M": c, "pH": ph, "Ca_mM": ca, "Fe_mM": fe}
                   for i, (c, ph, ca, fe) in enumerate(points, 1)])


# --------------------------------------------------------------------------- #
# Objective parsing
# --------------------------------------------------------------------------- #
def test_parse_maximize_element():
    o = S.parse_objective("maximize Ca")
    assert o.kind == S.OBJ_MAXIMIZE
    assert o.metrics[0].column == "Ca_mM" and o.metrics[0].direction == S.DIR_MAX


def test_parse_target_ph_range():
    o = S.parse_objective("I want a target pH 10 to 12")
    assert o.kind == S.OBJ_TARGET_PH
    m = o.metrics[0]
    assert m.column == "pH" and (m.target_low, m.target_high) == (10.0, 12.0)


def test_parse_minimize_impurity():
    o = S.parse_objective("reduce impurity dissolution")
    assert o.kind in (S.OBJ_MINIMIZE, S.OBJ_WEIGHTED)
    cols = {m.column for m in o.metrics}
    assert "Fe_mM" in cols                                  # impurity → Fe (+ Al) by default
    assert all(m.direction == S.DIR_MIN for m in o.metrics)


def test_parse_maximize_and_minimize_is_weighted():
    o = S.parse_objective("maximize Ca while minimizing Fe")
    assert o.kind == S.OBJ_WEIGHTED
    dirs = {(m.column, m.direction) for m in o.metrics}
    assert ("Ca_mM", S.DIR_MAX) in dirs and ("Fe_mM", S.DIR_MIN) in dirs


def test_parse_selectivity_and_reagent_and_avoid():
    assert S.parse_objective("maximize Sc/Fe selectivity").kind == S.OBJ_SELECTIVITY
    assert S.parse_objective("minimize reagent concentration").kind == S.OBJ_MINIMIZE_REAGENT
    assert S.parse_objective("avoid unsafe pH 12 to 14").kind == S.OBJ_AVOID_PH


def test_parse_unknown_when_vague():
    o = S.parse_objective("best concentration")
    assert o.kind == S.OBJ_UNKNOWN and not o.is_defined and o.notes


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def test_rank_target_ph_complete_data():
    t = _sweep_table([(0.1, 9.0, 1.0, 0.5), (0.5, 11.0, 2.0, 0.8), (1.0, 13.0, 3.0, 1.2)])
    r = S.rank_results(t, S.target_ph(10, 12), axis_col="leachant_concentration_M")
    assert r.ok and r.top_scenario_id == "SIM-002"         # only SIM-002 (pH 11) is in range
    assert r.ranked.iloc[0]["score"] == 1.0
    assert r.driving_metric == "pH"


def test_rank_missing_metric_warns_not_false_rank():
    t = _sweep_table([(0.1, 9.0, 1.0, 0.5), (0.5, 11.0, 2.0, 0.8)])
    r = S.rank_results(t, S.maximize("Sc"))                 # no Sc_mM column
    assert r.status == "no_rankable_metrics"
    assert "Sc_mM" in r.missing_metrics
    assert any("not available" in w for w in r.warnings)


def test_rank_maximize_ca_minimize_fe():
    t = _sweep_table([(0.1, 9.0, 1.0, 0.5), (0.5, 11.0, 2.0, 0.8), (1.0, 13.0, 3.0, 1.2)])
    obj = S.weighted([S.ObjectiveMetric("Ca_mM", S.DIR_MAX, label="Ca"),
                      S.ObjectiveMetric("Fe_mM", S.DIR_MIN, label="Fe")])
    r = S.rank_results(t, obj, axis_col="leachant_concentration_M")
    assert r.ok and len(r.ranked) == 3
    assert set(r.used_metrics) == {"Ca_mM", "Fe_mM"}
    # SIM-001 best-Fe, SIM-003 best-Ca → the balanced middle wins, and tradeoffs are noted
    assert r.tradeoffs


def test_rank_no_successful_rows():
    t = _table([{"scenario_id": "SIM-001", "status": "failed", "Ca_mM": None}])
    r = S.rank_results(t, S.maximize("Ca"))
    assert r.status == "no_successful_rows"


def test_rank_selectivity_ratio():
    t = _sweep_table([(0.1, 9, 1.0, 0.5), (0.5, 11, 2.0, 0.2)])     # SIM-002 has higher Ca/Fe
    r = S.rank_results(t, S.selectivity("Ca", "Fe"), axis_col="leachant_concentration_M")
    assert r.ok and r.top_scenario_id == "SIM-002"


# --------------------------------------------------------------------------- #
# Refined sweep
# --------------------------------------------------------------------------- #
def _rank_for(points, objective):
    t = _sweep_table(points)
    r = S.rank_results(t, objective, axis_col="leachant_concentration_M")
    return r, t


def test_refined_sweep_lower_edge():
    # minimize Ca → best at the lowest concentration (0.1) = lower edge
    r, t = _rank_for([(0.1, 9, 1.0, 0.5), (0.5, 11, 2.0, 0.8), (1.0, 13, 3.0, 1.2)],
                     S.minimize("Ca"))
    sug = S.suggest_refined_sweep(r, t, "leachant_concentration_M")
    assert sug.kind == "extend_lower"
    assert sug.suggested_values and sug.suggested_values[0] < 0.1
    assert sug.suggested_values[0] > 0                      # never below 0 for a concentration


def test_refined_sweep_upper_edge():
    # maximize Ca → best at the highest concentration (1.0) = upper edge
    r, t = _rank_for([(0.1, 9, 1.0, 0.5), (0.5, 11, 2.0, 0.8), (1.0, 13, 3.0, 1.2)],
                     S.maximize("Ca"))
    sug = S.suggest_refined_sweep(r, t, "leachant_concentration_M")
    assert sug.kind == "extend_upper"
    assert sug.suggested_values and sug.suggested_values[0] > 1.0


def test_refined_sweep_internal_optimum():
    # target pH 10-12 → best is the middle scenario (pH 11) = internal
    r, t = _rank_for([(0.1, 9, 1.0, 0.5), (0.5, 11, 2.0, 0.8), (1.0, 13, 3.0, 1.2)],
                     S.target_ph(10, 12))
    sug = S.suggest_refined_sweep(r, t, "leachant_concentration_M")
    assert sug.kind == "refine_internal"
    assert len(sug.suggested_values) >= 1
    assert all(0.1 < v < 1.0 for v in sug.suggested_values)


def test_refined_sweep_many_failures():
    t = _table([{"scenario_id": "SIM-001", "status": "failed"},
                {"scenario_id": "SIM-002", "status": "failed"},
                {"scenario_id": "SIM-003", "status": "success", "pH": 11.0, "Ca_mM": 2.0,
                 "leachant_concentration_M": 0.5}])
    r = S.rank_results(t, S.maximize("Ca"), axis_col="leachant_concentration_M")
    sug = S.suggest_refined_sweep(r, t, "leachant_concentration_M")
    assert sug.kind == "narrow_failures"


def test_refined_sweep_missing_metric_suggests_selected_output():
    t = _sweep_table([(0.1, 9, 1.0, 0.5), (0.5, 11, 2.0, 0.8)])
    r = S.rank_results(t, S.maximize("Sc"))
    sug = S.suggest_refined_sweep(r, t, "leachant_concentration_M")
    assert sug.kind == "add_selected_output"


# --------------------------------------------------------------------------- #
# Boundaries — no execution, no AI, no result path
# --------------------------------------------------------------------------- #
def _imports(path):
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append(node.module or "")
    return out


def test_strategy_imports_nothing_that_executes_or_is_ai_or_result_path():
    imports = " ".join(_imports(PKG_DIR / "simulation" / "strategy.py"))
    for bad in ("subprocess", "phreeqc_executor", "batch_executor", "phreeqc_runner",
                "import_assist", "scenario_parser", "residuals", "inclusion", "mapping_table",
                "scenarios", "replicates", "attribution", "mass_balance", "run_manager"):
        assert bad not in imports, f"strategy must not import {bad!r}"


def test_objective_never_executes():
    # parsing + ranking + suggesting are pure: they return data and run nothing.
    obj = S.parse_objective("maximize Ca")
    t = _sweep_table([(0.1, 9, 1.0, 0.5), (0.5, 11, 2.0, 0.8)])
    r = S.rank_results(t, obj, axis_col="leachant_concentration_M")
    S.suggest_refined_sweep(r, t, "leachant_concentration_M")
    # nothing to assert beyond "no exception + no exec import" (above) — these are inert.
