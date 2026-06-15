"""Smoke test for the seven-tab UI (Start / Import / Validate / Match / Simulate / Compare / Export).

Level achieved: **Streamlit AppTest harness** — the full app script runs end-to-end, so
every tab's render function executes in one pass (st.tabs renders all tab bodies). We
assert no exception and the seven tab labels, in two states:

* no run selected (every tab must show its empty state, not crash);
* a populated synthetic lab run selected (data + mapping + comparison present, so the
  populated render paths — measured overview, inclusion, report export, audit trail,
  user guide — all execute).

This is the highest level workable here: app.py is a Streamlit *script* (it calls
st.set_page_config etc. at import), so it can't be imported as a plain module — the render
functions are exercised through AppTest, not called directly.
"""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml import config, mapping_table, run_manager, scenarios
from flyash_phreeqc_ml.compare import compare_measured_vs_phreeqc

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

EXPECTED_TABS = ["Start", "Import", "Validate", "Match", "Simulate", "Compare", "Export"]
APP = "app.py"


@pytest.fixture()
def synthetic_run(tmp_path, monkeypatch):
    """A populated lab run (data + exact mapping + comparison) under temp config dirs."""
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    proc = tmp_path / "processed"
    proc.mkdir()
    monkeypatch.setattr(config, "PROCESSED_DIR", proc)
    pheq = pd.DataFrame([{"record_key": "f|sim1|batch|sol1", "source_file": "L-S_5_atmCO2.pqo",
                         "simulation": 1, "state": "batch", "solution_number": 1, "pH": 12.9,
                          "mol_Ca": 0.002, "temperature_c": 25}])
    pheq.to_csv(proc / config.PHREEQC_RESULTS_CSV, index=False)
    manifest = scenarios.build_scenario_manifest(pheq)

    run = "smoke_run"
    run_manager.create_run(run, "lab_experiment")
    measured = pd.DataFrame([
        {"sample_id": f"S{i}", "fly_ash_type": "Class C fly ash", "leachant": "NaOH",
         "NaOH_M": "", "acid_M": "", "CO2_condition": "OA", "liquid_solid_ratio": 5,
         "final_pH": 13.4} for i in range(3)])
    run_manager.save_lab_dataframe(run, measured)
    data = run_manager.read_data_file(run)
    table = mapping_table.build_suggestion_table(data, manifest, None)
    for _, r in mapping_table.exact_suggestions(table).iterrows():
        run_manager.add_condition_mapping(run, r["condition_key"], r["phreeqc_record_key"])
    run_manager.apply_condition_mapping(run)
    comp = compare_measured_vs_phreeqc(data, pheq, mapping=run_manager.read_mapping(run))
    cp = run_manager.comparison_path(run)
    cp.parent.mkdir(parents=True, exist_ok=True)
    comp.to_csv(cp, index=False)
    run_manager.write_comparison_meta(run)
    return run


def test_app_boots_no_run_selected():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert at.exception is None or len(at.exception) == 0
    assert [t.label for t in at.tabs] == EXPECTED_TABS


def _select_run(at, run):
    """Drive the sidebar 'Open a run' selectbox to the given run, then re-run."""
    at.sidebar.selectbox[0].set_value(run).run()
    return at


def test_app_boots_with_populated_run(synthetic_run):
    at = AppTest.from_file(APP, default_timeout=90).run()
    _select_run(at, synthetic_run)
    assert at.exception is None or len(at.exception) == 0
    assert [t.label for t in at.tabs] == EXPECTED_TABS
    # The run is actually selected, so the populated render paths executed.
    text = " ".join(str(m.value) for m in at.markdown)
    assert synthetic_run in text


def test_all_tabs_render_each_run_type(tmp_path, monkeypatch):
    # Each run type must render all tabs without crashing (empty/typed states).
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    for rt in ("lab_experiment", "literature_benchmark", "synthetic_demo"):
        run = f"r_{rt}"
        run_manager.create_run(run, rt)
        at = AppTest.from_file(APP, default_timeout=90).run()
        _select_run(at, run)
        assert at.exception is None or len(at.exception) == 0, rt
        assert [t.label for t in at.tabs] == EXPECTED_TABS
