"""Tests for PHREEQC gap attribution (attribution.py) — PHREEQC run is mocked.

Pins: attribution arithmetic on a synthetic selected-output, the
``precipitate_in_measured_solid`` flag both ways, status assignment across all four
states, the unavailable/degrade path, and that no modeled value lands in a
measured-labeled field (the Prompt-22 closure is immutable input). Synthetic only.
"""
from __future__ import annotations

import pytest

from flyash_phreeqc_ml import attribution as attr, mass_balance as mb, phreeqc_runner as pr
from flyash_phreeqc_ml import profiles, units
from flyash_phreeqc_ml.compare import inclusion as I

M_CA = units.MOLAR_MASSES["Ca"]
CANDIDATE = {"Calcite": "Ca", "Portlandite": "Ca", "Gibbsite": "Al", "SiO2(am)": "Si"}

# Profiles: fly-ash convention (False) and the alternate (True).
PROF_FALSE = profiles.DatasetProfile(
    name="batch (precip in filtrate)", grouping="fly_ash",
    mass_balance_elements=("Ca", "Si", "Al"), starting_content_unit="wt%",
    solid_residue_unit="wt%", mass_balance_candidate_phases=CANDIDATE,
    precipitate_in_measured_solid=False)
PROF_TRUE = profiles.DatasetProfile(
    name="batch (precip in solid)", grouping="fly_ash",
    mass_balance_elements=("Ca", "Si", "Al"), starting_content_unit="wt%",
    solid_residue_unit="wt%", mass_balance_candidate_phases=CANDIDATE,
    precipitate_in_measured_solid=True)


def _row(**over):
    row = {"sample_id": "B1", "material_mass_g": 5.0, "liquid_volume_mL": 50.0,
           "solid_mass_g": 4.0, "Ca_starting_content": 2.0, "Ca_solid_residue": 0.4,
           "Ca_mM": 20.0, "Si_starting_content": 1.0, "Si_solid_residue": 0.1,
           "Si_mM": 5.0, "Al_starting_content": 1.0, "Al_solid_residue": 0.1, "Al_mM": 5.0}
    row.update(over)
    return row


# Measured Ca gap (mmol) from Prompt 22: 100/M − 1.0 − 16/M (n_liquid=1.0 at 20 mM).
GAP_CA = (100.0 / M_CA) - 1.0 - (16.0 / M_CA)   # ≈ 1.09591 (significant)


def _selected(calcite_mol=0.0, ca_sol_mol=0.0015):
    """A synthetic PHREEQC selected-output final row (values in MOL, as PHREEQC emits)."""
    return {pr.phase_moles_column("Calcite"): calcite_mol,
            pr.sol_moles_column("Ca"): ca_sol_mol}


# --------------------------------------------------------------------------- #
# Arithmetic on a synthetic selected output (False = precipitate explains the gap)
# --------------------------------------------------------------------------- #
def test_attribution_arithmetic_partial():
    # Calcite precipitated 0.5 mmol (= 5e-4 mol) of a ~1.096 mmol gap → ~46% explained.
    res = attr.attribute_gap(_row(), "Ca", _selected(calcite_mol=5e-4), profile=PROF_FALSE)
    assert res["provenance"] == "phreeqc"
    assert res["modeled_precipitated_moles"] == pytest.approx(0.5)   # 5e-4 mol → mmol
    assert res["by_phase"] == {"Calcite": pytest.approx(0.5)}
    assert res["gap"] == pytest.approx(GAP_CA)
    assert res["gap_explained"] == pytest.approx(0.5)
    assert res["gap_unexplained"] == pytest.approx(GAP_CA - 0.5)
    assert res["fraction_explained"] == pytest.approx(0.5 / GAP_CA)
    assert res["status"] == attr.STATUS_PARTIAL


def test_attribution_caps_explained_at_gap():
    # Model precipitates MORE than the gap → explained capped at the gap, not negative.
    res = attr.attribute_gap(_row(), "Ca", _selected(calcite_mol=2e-3), profile=PROF_FALSE)
    assert res["gap_explained"] == pytest.approx(GAP_CA)
    assert res["gap_unexplained"] == pytest.approx(0.0, abs=1e-12)
    assert res["status"] == attr.STATUS_MODEL_EXPLAINED


# --------------------------------------------------------------------------- #
# precipitate_in_measured_solid flag both ways
# --------------------------------------------------------------------------- #
def test_flag_false_precipitate_explains_gap():
    res = attr.attribute_gap(_row(), "Ca", _selected(calcite_mol=5e-4), profile=PROF_FALSE)
    assert res["gap_explained"] == pytest.approx(0.5)


def test_flag_true_precipitate_does_not_reduce_gap():
    # Same model output, but precipitate is in the measured solid → explains 0 of the gap.
    res = attr.attribute_gap(_row(), "Ca", _selected(calcite_mol=5e-4), profile=PROF_TRUE)
    assert res["modeled_precipitated_moles"] == pytest.approx(0.5)   # still reported
    assert res["gap_explained"] == pytest.approx(0.0)
    assert res["gap_unexplained"] == pytest.approx(GAP_CA)
    assert res["status"] == attr.STATUS_UNEXPLAINED


# --------------------------------------------------------------------------- #
# Status assignment across all four
# --------------------------------------------------------------------------- #
def test_status_model_explained():
    res = attr.attribute_gap(_row(), "Ca", _selected(calcite_mol=GAP_CA / 1000.0),
                             profile=PROF_FALSE)
    assert res["status"] == attr.STATUS_MODEL_EXPLAINED


def test_status_unexplained_when_model_precipitates_nothing():
    res = attr.attribute_gap(_row(), "Ca", _selected(calcite_mol=0.0), profile=PROF_FALSE)
    assert res["status"] == attr.STATUS_UNEXPLAINED


def test_status_closed_when_gap_within_uncertainty():
    # Liquid + solid ≈ n_in → a tiny gap (< 5% of n_in) → closed regardless of the model.
    row = _row(Ca_mM=42.0)        # n_liquid = 2.1 → gap ≈ -0.004 mmol (tiny)
    res = attr.attribute_gap(row, "Ca", _selected(calcite_mol=1e-4), profile=PROF_FALSE)
    assert abs(res["gap_fraction"]) <= attr.CLOSED_FRACTION_TOL
    assert res["status"] == attr.STATUS_CLOSED


def test_status_partial():
    res = attr.attribute_gap(_row(), "Ca", _selected(calcite_mol=5e-4), profile=PROF_FALSE)
    assert res["status"] == attr.STATUS_PARTIAL


# --------------------------------------------------------------------------- #
# Immutability — no modeled value in a measured-labeled field
# --------------------------------------------------------------------------- #
def test_measured_block_is_immutable_closure_output():
    row = _row()
    closure = mb.closure(row, "Ca", profile=PROF_FALSE)        # the Prompt-22 truth
    res = attr.attribute_gap(row, "Ca", _selected(calcite_mol=5e-4), profile=PROF_FALSE)
    m = res["measured"]
    # The measured block is exactly the closure's measured terms — untouched by the model.
    assert m["n_liquid"] == pytest.approx(closure["n_liquid"])
    assert m["n_solid"] == pytest.approx(closure["n_solid"])
    assert m["gap"] == pytest.approx(closure["gap"])
    assert res["gap"] == pytest.approx(closure["gap"])
    # No modeled key leaked into the measured block.
    for modeled in ("modeled_precipitated_moles", "by_phase", "gap_explained",
                    "modeled_solution_moles"):
        assert modeled not in m


# --------------------------------------------------------------------------- #
# Unavailable / degrade + honesty
# --------------------------------------------------------------------------- #
def test_attribution_unavailable_degrades_to_measured_gap():
    res = attr.attribution_unavailable(_row(), "Ca", profile=PROF_FALSE)
    assert res["provenance"] == "measured"
    assert res["modeled_precipitated_moles"] is None and res["by_phase"] == {}
    assert res["gap_unexplained"] == pytest.approx(GAP_CA)   # whole gap, unattributed
    assert "configure PHREEQC" in res["note"]


def test_caption_is_honest_model_language():
    cap = attr.attribution_caption(
        attr.attribute_gap(_row(), "Ca", _selected(calcite_mol=5e-4), profile=PROF_FALSE))
    assert "attributes" in cap and "Calcite" in cap
    assert "the element was" not in cap.lower()


# --------------------------------------------------------------------------- #
# Status aggregation → validity (one source of truth)
# --------------------------------------------------------------------------- #
def test_overall_attribution_status_is_worst():
    results = [{"status": attr.STATUS_CLOSED}, {"status": attr.STATUS_PARTIAL},
               {"status": attr.STATUS_MODEL_EXPLAINED}]
    assert attr.overall_attribution_status(results) == attr.STATUS_PARTIAL
    assert attr.overall_attribution_status([]) is None


def test_unexplained_attribution_caps_validity_at_preliminary():
    from flyash_phreeqc_ml import report
    valid_inclusions = {"final_pH": {"validity": I.VALIDITY_VALID}}
    # Without attribution → valid; with a non-closed attribution → capped at preliminary.
    assert report._overall_validity(valid_inclusions) == I.VALIDITY_VALID
    assert report._overall_validity(valid_inclusions, attribution_status=attr.STATUS_CLOSED) \
        == I.VALIDITY_VALID
    assert report._overall_validity(valid_inclusions, attribution_status=attr.STATUS_UNEXPLAINED) \
        == I.VALIDITY_PRELIMINARY


# --------------------------------------------------------------------------- #
# Run-input builder threads the extras + keeps OA→1 behaviour
# --------------------------------------------------------------------------- #
def test_build_attribution_inputs_has_extras_and_one_input_for_oa():
    row = _row(leachant="NaOH", NaOH_M=0.5, CO2_condition="OA")
    inputs = attr.build_attribution_inputs(row, PROF_FALSE)
    assert len(inputs) == 1            # OA → a single atm_CO2 input (build_input behaviour)
    text = inputs[0].pqi_text
    assert "SELECTED_OUTPUT" in text and "USER_PUNCH" in text
    assert "Calcite" in text and "dissolved batch material" in text
