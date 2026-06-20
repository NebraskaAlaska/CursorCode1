"""Pins for the cross-cutting **lab modes** (validation / uncertainty / evidence).

The headline invariant: **validation mode never labels a simulation 'validated' without measured
data.** Plus: uncertainty mode suggests real variables to vary (no fabricated statistics), and the
mode flags live on the agent state defaulting to off.
"""
from __future__ import annotations

from flyash_phreeqc_ml.agent import agent_state, domains
from flyash_phreeqc_ml.instruments import lab_modes


# --------------------------------------------------------------------------- #
# Validation mode
# --------------------------------------------------------------------------- #
def test_simulation_alone_is_never_validated():
    v = lab_modes.assess_validation(has_measured=False, has_simulation=True)
    assert v.is_validated is False
    assert v.status == lab_modes.VAL_SIMULATION_ONLY
    assert "not validated" in v.label.lower()
    assert "measured" in v.note.lower()


def test_nothing_at_all_is_not_validated():
    v = lab_modes.assess_validation(has_measured=False, has_simulation=False)
    assert v.is_validated is False
    assert v.status == lab_modes.VAL_NO_DATA


def test_measured_only_is_not_validated_yet():
    v = lab_modes.assess_validation(has_measured=True, has_simulation=False)
    assert v.is_validated is False
    assert v.status == lab_modes.VAL_MEASURED_ONLY


def test_measured_plus_prediction_is_comparable():
    v = lab_modes.assess_validation(has_measured=True, has_simulation=True)
    assert v.is_validated is True
    assert v.status == lab_modes.VAL_COMPARED


# --------------------------------------------------------------------------- #
# Uncertainty / sensitivity
# --------------------------------------------------------------------------- #
def test_leaching_sensitivity_variables_include_the_real_levers():
    variables = lab_modes.sensitivity_variables(domains.LEACHING_GEOCHEMISTRY)
    blob = " ".join(variables).lower()
    assert "release fraction" in blob
    assert "liquid/solid" in blob or "l/s" in blob
    assert "naoh" in blob or "concentration" in blob


def test_generic_sensitivity_for_other_domains():
    variables = lab_modes.sensitivity_variables(domains.MECHANICAL_TESTING)
    assert variables and "release fraction (the dominant assumption)" not in variables


def test_uncertainty_disclaimer_disclaims_fabricated_certainty():
    assert "does not invent" in lab_modes.UNCERTAINTY_DISCLAIMER.lower()


# --------------------------------------------------------------------------- #
# State flags
# --------------------------------------------------------------------------- #
def test_mode_flags_default_off_on_agent_state():
    state = agent_state.AgentState()
    assert state.validation_mode is False
    assert state.uncertainty_mode is False
    assert state.evidence_mode is False
    modes = lab_modes.modes_from_state(state)
    assert modes == {"validation": False, "uncertainty": False, "evidence": False}


def test_modes_from_state_reads_set_flags():
    state = agent_state.AgentState()
    state.validation_mode = True
    state.evidence_mode = True
    modes = lab_modes.modes_from_state(state)
    assert modes["validation"] is True and modes["evidence"] is True
    assert modes["uncertainty"] is False


def test_evidence_note_promises_no_invented_sources():
    assert "invented" in lab_modes.evidence_note().lower()
