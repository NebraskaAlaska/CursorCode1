"""Tests for the deterministic batch-reaction element closure (mass_balance.py).

Arithmetic only — no model / AI / ML. Pins: closure vs hand-computed moles, a
conversion_id on every derived term, incomplete handling, the negative-gap warning,
uncertainty propagation vs a hand-checked case, and that FLY_ASH_PROFILE (which does
not opt in) is unchanged. Synthetic data only.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from flyash_phreeqc_ml import config, mass_balance as mb, profiles, units


# A profile that OPTS IN to the batch mass balance (wt% assays, default columns).
BATCH_PROFILE = profiles.DatasetProfile(
    name="batch demo", grouping="fly_ash",
    mass_balance_elements=("Ca", "Si", "Al", "Fe"),
    starting_content_unit="wt%", solid_residue_unit="wt%",
)

M_CA = units.MOLAR_MASSES["Ca"]  # 40.078


def _realistic_row(**over):
    """5 g material, 2 wt% Ca; 50 mL liquid measured at 40 mM Ca; 4 g residue at 0.4 wt%."""
    row = {
        "sample_id": "B1",
        "material_mass_g": 5.0, "material_id": "CFA", "reagent": "HCl",
        "reagent_conc_M": 1.0, "reagent_volume_mL": 50.0, "liquid_volume_mL": 50.0,
        "solid_mass_g": 4.0,
        "Ca_starting_content": 2.0, "Ca_solid_residue": 0.4, "Ca_mM": 40.0,
    }
    row.update(over)
    return row


# Hand-computed expectations (mmol):
N_IN = 5.0 * 2.0 * 10.0 / M_CA          # 100 mg / 40.078 = 2.495133...
N_LIQ = 40.0 * (50.0 / 1000.0)          # 40 mM × 0.05 L = 2.0
N_SOL = 4.0 * 0.4 * 10.0 / M_CA         # 16 mg / 40.078 = 0.399222...
GAP = N_IN - N_LIQ - N_SOL              # 0.095911...


# --------------------------------------------------------------------------- #
# Closure arithmetic
# --------------------------------------------------------------------------- #
def test_closure_matches_hand_computed_moles():
    res = mb.closure(_realistic_row(), "Ca", profile=BATCH_PROFILE)
    assert res["status"] == mb.STATUS_COMPLETE
    assert res["unit"] == "mmol"
    assert res["n_in"] == pytest.approx(N_IN)
    assert res["n_liquid"] == pytest.approx(N_LIQ)
    assert res["n_solid"] == pytest.approx(N_SOL)
    assert res["gap"] == pytest.approx(GAP)
    assert res["gap_fraction"] == pytest.approx(GAP / N_IN)
    assert res["missing_fields"] == []


def test_term_functions_match_closure():
    row = _realistic_row()
    assert mb.moles_in(row, "Ca", BATCH_PROFILE)["value"] == pytest.approx(N_IN)
    assert mb.moles_liquid(row, "Ca", BATCH_PROFILE)["value"] == pytest.approx(N_LIQ)
    assert mb.moles_solid(row, "Ca", BATCH_PROFILE)["value"] == pytest.approx(N_SOL)


def test_conversion_id_on_every_derived_term():
    res = mb.closure(_realistic_row(), "Ca", profile=BATCH_PROFILE)
    prov = res["provenance"]
    # Every derived molar term carries a conversion_id (no silent molar conversion).
    assert prov["n_in"]["conversion_id"] == "mg_to_mmol"
    assert prov["n_solid"]["conversion_id"] == "mg_to_mmol"
    assert prov["n_liquid"]["conversion_id"]              # non-None (molar input id)
    # ...and the molar mass actually used is recorded for the mass→mmol terms.
    assert prov["n_in"]["molar_mass"] == pytest.approx(M_CA)
    assert prov["n_solid"]["molar_mass"] == pytest.approx(M_CA)


def test_liquid_carries_import_conversion_id_when_present():
    row = _realistic_row()
    row["Ca_mM_conversion_id"] = "mgL_to_mM"     # Prompt-16 import provenance companion
    res = mb.closure(row, "Ca", profile=BATCH_PROFILE)
    assert res["provenance"]["n_liquid"]["conversion_id"] == "mgL_to_mM"


def test_mg_per_kg_assay_unit():
    prof = profiles.DatasetProfile(name="mgkg", grouping="fly_ash",
                                   mass_balance_elements=("Ca",),
                                   starting_content_unit="mg/kg", solid_residue_unit="mg/kg")
    # 5 g × 20000 mg/kg = (5/1000 kg) × 20000 = 100 mg = same n_in as 2 wt%.
    row = _realistic_row(Ca_starting_content=20000.0)
    assert mb.moles_in(row, "Ca", prof)["value"] == pytest.approx(N_IN)


# --------------------------------------------------------------------------- #
# Incomplete handling — never a partial number shown as real
# --------------------------------------------------------------------------- #
def test_missing_required_term_is_incomplete_not_partial():
    row = _realistic_row()
    del row["Ca_solid_residue"]                  # solid term cannot be computed
    res = mb.closure(row, "Ca", profile=BATCH_PROFILE)
    assert res["status"] == mb.STATUS_INCOMPLETE
    assert res["gap"] is None and res["gap_fraction"] is None
    assert "Ca_solid_residue" in res["missing_fields"]
    assert res["n_solid"] is None                # not a fabricated 0


def test_missing_solid_mass_is_assumed_not_fabricated():
    row = _realistic_row()
    del row["solid_mass_g"]                       # recovered solid mass not recorded
    res = mb.closure(row, "Ca", profile=BATCH_PROFILE)
    assert res["status"] == mb.STATUS_COMPLETE    # assumed solid mass = material mass
    assert any("assumed" in a for a in res["assumptions"])
    # n_solid recomputed with the assumed mass (5 g, not 4 g): 5×0.4×10/M_Ca.
    assert res["n_solid"] == pytest.approx(5.0 * 0.4 * 10.0 / M_CA)


# --------------------------------------------------------------------------- #
# Sanity warnings (validation-surface style)
# --------------------------------------------------------------------------- #
def test_negative_gap_warns_with_culprit():
    # Low starting assay (1 wt%) but high liquid → more recovered than charged.
    res = mb.closure(_realistic_row(Ca_starting_content=1.0), "Ca", profile=BATCH_PROFILE)
    assert res["gap"] < 0
    issues = mb.closure_warnings(res)
    checks = {i["check"] for i in issues}
    assert "mass_balance_negative_gap" in checks
    neg = next(i for i in issues if i["check"] == "mass_balance_negative_gap")
    assert "unit error" in neg["message"]              # names a likely culprit
    assert "mass_balance_over_recovery" in checks       # liquid+solid > input


def test_incomplete_warning_lists_missing():
    row = _realistic_row()
    del row["Ca_mM"]
    issues = mb.closure_warnings(mb.closure(row, "Ca", profile=BATCH_PROFILE))
    assert issues[0]["check"] == "mass_balance_incomplete"
    assert "Ca_mM" in issues[0]["message"]


def test_complete_closure_no_spurious_warnings():
    assert mb.closure_warnings(mb.closure(_realistic_row(), "Ca", profile=BATCH_PROFILE)) == []


# --------------------------------------------------------------------------- #
# Uncertainty propagation vs a hand-checked case
# --------------------------------------------------------------------------- #
def test_uncertainty_propagation_matches_hand_check():
    sigmas = {
        "material_mass_g": 0.05, "Ca_starting_content": 0.1,
        "Ca_mM": 1.0, "liquid_volume_mL": 0.5,
        "Ca_solid_residue": 0.02, "solid_mass_g": 0.04,
    }
    res = mb.closure(_realistic_row(), "Ca", profile=BATCH_PROFILE, sigmas=sigmas)
    assert res["uncertainty"] == "propagated"
    # Hand-computed term sigmas via relative quadrature, then sum in quadrature.
    s_in = N_IN * math.sqrt((0.05 / 5.0) ** 2 + (0.1 / 2.0) ** 2)
    s_liq = N_LIQ * math.sqrt((1.0 / 40.0) ** 2 + (0.5 / 50.0) ** 2)
    s_sol = N_SOL * math.sqrt((0.02 / 0.4) ** 2 + (0.04 / 4.0) ** 2)
    expected = math.sqrt(s_in ** 2 + s_liq ** 2 + s_sol ** 2)
    assert res["gap_sigma"] == pytest.approx(expected)


def test_no_uncertainty_is_unknown_not_zero():
    res = mb.closure(_realistic_row(), "Ca", profile=BATCH_PROFILE)   # no sigmas
    assert res["gap_sigma"] is None
    assert res["uncertainty"] == "unknown"           # never implied to be zero


# --------------------------------------------------------------------------- #
# FLY_ASH_PROFILE (does not opt in) is unchanged
# --------------------------------------------------------------------------- #
def test_fly_ash_profile_mass_balance_off_and_unchanged():
    # The feature is OFF for the default profile (empty mass_balance_elements).
    assert profiles.FLY_ASH_PROFILE.mass_balance_elements == ()
    assert mb.is_enabled(profiles.FLY_ASH_PROFILE) is False
    # No batch column leaked into the fly-ash variable columns (NUMERIC untouched).
    vc = set(profiles.FLY_ASH_PROFILE.variable_columns)
    assert not (vc & set(config.BATCH_REACTION_COLUMNS))
    assert tuple(config.EXPERIMENTAL_NUMERIC_COLUMNS) == profiles.FLY_ASH_PROFILE.variable_columns


def test_closure_records_empty_for_non_optin_profile():
    data = pd.DataFrame([_realistic_row()])
    assert mb.closure_records(data, profiles.FLY_ASH_PROFILE) == []   # off → no records
    recs = mb.closure_records(data, BATCH_PROFILE)                    # opted in → records
    assert len(recs) == 4 and {r["element"] for r in recs} == {"Ca", "Si", "Al", "Fe"}
