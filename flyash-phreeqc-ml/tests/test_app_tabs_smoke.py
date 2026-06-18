"""Smoke test for the assistant-first UI (four sections + the Data & Validation sub-tabs).

The app is navigated by a sidebar **section** radio:
**Research Assistant · Projects / Runs · Data & Validation · Engine Settings**. The Research
Assistant is the default (first) workspace; the technical workflows (Advanced Simulate behind
an expander; Import / Validate / Match / Compare as Data & Validation sub-tabs; report/audit in
Projects / Runs) remain reachable. This uses the **Streamlit AppTest harness** — app.py is a
script (it calls st.set_page_config at import), so the render functions are exercised through
AppTest, not called directly. We assert no exception, the broad identity, and that every
section renders, in the no-run and populated-run states.
"""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml import config, mapping_table, run_manager, scenarios
from flyash_phreeqc_ml.compare import compare_measured_vs_phreeqc

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

APP = "app.py"
SECTIONS = ["Assistant", "Workspace", "Results", "Data & Validation", "Projects",
            "Evidence Library", "Engine Library", "Settings"]
DATA_SUBTABS = ["Import", "Validate", "Match", "Compare"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _nav_radio(at):
    matches = [r for r in at.radio if getattr(r, "key", None) == "nav_section"]
    assert matches, "the sidebar section nav (key=nav_section) is missing"
    return matches[0]


def _goto(at, section):
    _nav_radio(at).set_value(section).run()
    return at


def _markdown(at) -> str:
    return " ".join(str(m.value) for m in at.markdown)


def _no_exception(at) -> bool:
    return at.exception is None or len(at.exception) == 0


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


# --------------------------------------------------------------------------- #
# Identity + default workspace
# --------------------------------------------------------------------------- #
def test_app_boots_default_is_assistant():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert _no_exception(at)
    # The default (first) section is the Assistant.
    assert _nav_radio(at).value == "Assistant"
    md = _markdown(at)
    assert "Materials Research Assistant" in md
    assert "What can I help with?" in md          # the assistant's example chips


def test_identity_is_broad_not_fly_ash_only():
    """The primary identity is a broad materials assistant; fly ash / PHREEQC are an example +
    the first engine, not the whole product (all asserted strings live in the hero markdown)."""
    at = AppTest.from_file(APP, default_timeout=60).run()
    md = _markdown(at)
    assert "Materials Research Assistant" in md
    assert "Broad materials research software" in md
    assert "Planning support" in md                          # non-leaching domains supported
    assert "Class C fly ash is the first mature demo" in md  # explicitly: not the whole product
    assert "engine" in md.lower()                            # PHREEQC framed as the engine


def test_sidebar_nav_has_seven_sections():
    at = AppTest.from_file(APP, default_timeout=60).run()
    options = list(_nav_radio(at).options)
    assert options == SECTIONS
    # The left nav contains the product sections (not a row of equal technical tabs).
    for expected in ("Assistant", "Workspace", "Results", "Data & Validation", "Projects",
                     "Engine Library", "Settings"):
        assert expected in options


# --------------------------------------------------------------------------- #
# Every section renders (no-run state)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("section", SECTIONS)
def test_each_section_renders_no_run(section):
    at = AppTest.from_file(APP, default_timeout=90).run()
    _goto(at, section)
    assert _no_exception(at), f"{section} crashed in the no-run state"


def test_assistant_advanced_details_hidden_by_default():
    """The default Research Assistant view is conversational; technical content (scenario JSON,
    generated PHREEQC input, provenance, …) is gated behind collapsed expanders."""
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert _no_exception(at)
    labels = [getattr(e, "label", "") for e in at.expander]
    adv = [e for e in at.expander if "Advanced details" in getattr(e, "label", "")]
    assert adv, "the assistant's technical 'Advanced details' expander is missing"
    # It is collapsed by default (hidden until the user opens it), when AppTest exposes the flag.
    if hasattr(adv[0], "expanded"):
        assert adv[0].expanded is False
    assert any("How the assistant works" in lbl for lbl in labels)
    # The default surface is conversational: a chat input + example chips are present.
    assert len(at.chat_input) >= 1
    assert "What can I help with?" in _markdown(at)


def test_advanced_workflows_reachable():
    """The technical workflows are not gone — Workspace is the full manual builder (Simulate),
    and Data & Validation exposes the four measured-vs-model sub-tabs."""
    at = AppTest.from_file(APP, default_timeout=90).run()
    _goto(at, "Data & Validation")
    assert _no_exception(at)
    assert [t.label for t in at.tabs] == DATA_SUBTABS
    # Workspace = the structured experiment builder (the manual Simulate workflow).
    at2 = AppTest.from_file(APP, default_timeout=90).run()
    _goto(at2, "Workspace")
    assert _no_exception(at2)


# --------------------------------------------------------------------------- #
# Populated run — every section renders the populated paths
# --------------------------------------------------------------------------- #
def _select_run(at, run):
    at.sidebar.selectbox[0].set_value(run).run()
    return at


@pytest.mark.parametrize("section", SECTIONS)
def test_each_section_renders_with_populated_run(synthetic_run, section):
    at = AppTest.from_file(APP, default_timeout=120).run()
    _select_run(at, synthetic_run)
    _goto(at, section)
    assert _no_exception(at), f"{section} crashed with a populated run"


def test_all_sections_render_each_run_type(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    for rt in ("lab_experiment", "literature_benchmark", "synthetic_demo"):
        run = f"r_{rt}"
        run_manager.create_run(run, rt)
        for section in SECTIONS:
            at = AppTest.from_file(APP, default_timeout=120).run()
            _select_run(at, run)
            _goto(at, section)
            assert _no_exception(at), f"{rt} / {section}"
