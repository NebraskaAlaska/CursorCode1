"""Focused Streamlit regressions for legacy Match environment capture."""
from __future__ import annotations

from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest


@pytest.fixture(autouse=True)
def _restore_app_module_functions():
    """AppTest executes in-process, so restore patches made by its test script."""
    from ui import match_tab
    originals = {
        "check_availability": match_tab.phreeqc_executor.check_availability,
        "is_cemdata_compatible": match_tab.phreeqc_runner.is_cemdata_compatible,
        "generated_simulations_dir": match_tab.run_manager.generated_simulations_dir,
        "confirm_reviewed_input": match_tab.phreeqc_runner.confirm_reviewed_input,
        "run": match_tab.phreeqc_runner.run,
        "ingest": match_tab.phreeqc_runner.ingest,
    }
    yield
    match_tab.phreeqc_executor.check_availability = originals["check_availability"]
    match_tab.phreeqc_runner.is_cemdata_compatible = originals["is_cemdata_compatible"]
    match_tab.run_manager.generated_simulations_dir = originals["generated_simulations_dir"]
    match_tab.phreeqc_runner.confirm_reviewed_input = originals["confirm_reviewed_input"]
    match_tab.phreeqc_runner.run = originals["run"]
    match_tab.phreeqc_runner.ingest = originals["ingest"]


def _button(at, label: str):
    return next(button for button in at.button if button.label == label)


def _text(at) -> str:
    parts = []
    for name in ("markdown", "caption", "warning", "error", "info", "success", "code"):
        for element in getattr(at, name, []) or []:
            parts.append(str(getattr(element, "value", "")))
    return " ".join(parts)


def _match_app(*, state: str, workdir: Path) -> str:
    incompatible = state == "incompatible"
    ready = state in {"incompatible", "ready", "confirmation_failure"}
    fail_confirmation = state == "confirmation_failure"
    return f'''
from pathlib import Path
import pandas as pd
import streamlit as st
from ui import match_tab
from flyash_phreeqc_ml.simulation import phreeqc_executor, phreeqc_run_contract

file_identity = phreeqc_run_contract.FileIdentity("/captured/file", "a" * 64, 1, 1, 0o755)
environment = phreeqc_run_contract.ExecutionEnvironmentIdentity(file_identity, file_identity)
availability = phreeqc_executor.PhreeqcAvailability(
    {ready!r}, {ready!r}, {ready!r}, {ready!r},
    "/captured/phreeqc" if {ready!r} else None,
    "/captured/cemdata.dat" if {ready!r} else None,
    "PHREEQC unavailable for regression" if not {ready!r} else "ready",
    environment_identity=environment if {ready!r} else None)
match_tab.phreeqc_executor.check_availability = lambda **kwargs: availability
match_tab.phreeqc_runner.is_cemdata_compatible = lambda database: {not incompatible!r}
match_tab.run_manager.generated_simulations_dir = lambda run: Path({str(workdir)!r})

if "confirm_hashes" not in st.session_state:
    st.session_state["confirm_hashes"] = []
if "runner_calls" not in st.session_state:
    st.session_state["runner_calls"] = []

def fake_confirm(reviewed, **kwargs):
    expected = kwargs.get("expected_environment")
    st.session_state["confirm_hashes"].append(expected.identity_hash if expected else None)
    if {fail_confirmation!r} and len(st.session_state["confirm_hashes"]) == 2:
        raise match_tab.phreeqc_runner.PhreeqcNotConfiguredError("environment changed")
    return phreeqc_run_contract.confirm_reviewed(reviewed, expected)

def fake_run(generated_input, run_workdir, **kwargs):
    st.session_state["runner_calls"].append({{
        "scenario_id": generated_input.scenario_id,
        "exe": kwargs.get("exe"),
        "database": kwargs.get("database"),
        "environment_hash": kwargs["confirmation"].execution_environment.identity_hash,
    }})
    return Path(run_workdir) / (generated_input.basename + ".pqo")

match_tab.phreeqc_runner.confirm_reviewed_input = fake_confirm
match_tab.phreeqc_runner.run = fake_run
match_tab.phreeqc_runner.ingest = lambda *args, **kwargs: ["mock-record"]

data = pd.DataFrame([{{
    "sample_id": "CFA-NaOH0.5M-LS5-10min-PF-R1", "leachant": "NaOH",
    "NaOH_M": 0.5, "liquid_solid_ratio": 5.0, "temperature_C": 25.0,
    "time_min": 10.0, "CO2_condition": "PF", "final_pH": 13.1,
}}])
needs_new = pd.DataFrame([{{
    "condition_key": "NaOH0.5M_PF_10min_LS5_T25C", "n_replicates": 1,
    "confidence": "low", "reason": "regression",
}}])
match_tab._render_generate_simulation("r2-ui", data, pd.DataFrame(), needs_new)
'''


@pytest.mark.parametrize("state", ["unavailable", "incompatible"])
def test_match_preview_survives_unavailable_or_incompatible_environment_without_runner(
        tmp_path, state):
    at = AppTest.from_string(_match_app(state=state, workdir=tmp_path / "workspace"),
                             default_timeout=60).run()
    _button(at, "Generate simulation input").click().run()

    assert not at.exception
    text = _text(at)
    assert "Generated PHREEQC input" in text
    assert "preview" in text.lower()
    assert "Run PHREEQC for 2 variant(s) & ingest" not in [b.label for b in at.button]
    assert at.session_state["confirm_hashes"] == []
    assert at.session_state["runner_calls"] == []
    assert not (tmp_path / "workspace").exists()


def _generate_and_confirm(at):
    _button(at, "Generate simulation input").click().run()
    checkbox = next(item for item in at.checkbox if "reviewed the exact 2" in item.label)
    checkbox.set_value(True).run()
    _button(at, "▶️ Run PHREEQC for 2 variant(s) & ingest").click().run()
    return at


def test_match_multi_variant_run_uses_one_captured_environment_and_exact_paths(tmp_path):
    at = AppTest.from_string(
        _match_app(state="ready", workdir=tmp_path / "workspace"),
        default_timeout=60).run()
    _generate_and_confirm(at)

    assert not at.exception
    assert len(at.session_state["confirm_hashes"]) == 2
    assert len(set(at.session_state["confirm_hashes"])) == 1
    assert len(at.session_state["runner_calls"]) == 2
    assert {call["environment_hash"] for call in at.session_state["runner_calls"]} == {
        at.session_state["confirm_hashes"][0]}
    assert {call["exe"] for call in at.session_state["runner_calls"]} == {"/captured/file"}
    assert {call["database"] for call in at.session_state["runner_calls"]} == {
        "/captured/file"}


def test_match_builds_all_confirmations_before_any_variant_or_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    at = AppTest.from_string(
        _match_app(state="confirmation_failure", workdir=workspace),
        default_timeout=60).run()
    _generate_and_confirm(at)

    assert not at.exception
    assert len(at.session_state["confirm_hashes"]) == 2
    assert len(set(at.session_state["confirm_hashes"])) == 1
    assert at.session_state["runner_calls"] == []
    assert not workspace.exists()
    assert "before any variant ran" in _text(at)
