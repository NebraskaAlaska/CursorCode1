"""The generalization layer: a second, non-fly-ash DatasetProfile must drive the
whole condition-grouping → suggestion-table → mapping-status → inclusion chain with
synthetic frames, proving no fly-ash constants (OA/PF/GS, leachant, NaOH, ICP cols)
are required. Fly-ash defaults are exercised by the rest of the suite.
"""
from __future__ import annotations

import pandas as pd

from flyash_phreeqc_ml import mapping_table, profiles, replicates
from flyash_phreeqc_ml.compare import comparison_inclusion
from flyash_phreeqc_ml.viz import measured_overview as mo

# A minimal alternate dataset: a soil-moisture demo with WET/DRY treatments, a "day"
# time column, and a different variable set — nothing fly-ash specific.
WET_DRY_PROFILE = profiles.DatasetProfile(
    name="soil moisture demo",
    id_column="sample_id",
    time_column="day",
    condition_column="treatment",
    condition_codes={
        "WET": {"description": "watered", "caution": ""},
        "DRY": {"description": "droughted", "caution": ""},
    },
    variable_columns=("yield_g", "moisture_pct"),
    overview_variables=("yield_g", "moisture_pct"),
    important_fields=("treatment", "day"),
    tolerances={},
    comparison_variable_spec={"yield_g": ("yield_g", "model_yield_g")},
    grouping="generic",
)

MANIFEST = pd.DataFrame([
    {"phreeqc_record_key": "m1", "scenario_label": "model WET day1", "state": "batch"},
])


def _data() -> pd.DataFrame:
    return pd.DataFrame([
        {"sample_id": "WET-d1-R1", "treatment": "WET", "day": 1, "yield_g": 10.0, "moisture_pct": 40},
        {"sample_id": "WET-d1-R2", "treatment": "WET", "day": 1, "yield_g": 12.0, "moisture_pct": 42},
        {"sample_id": "DRY-d1-R1", "treatment": "DRY", "day": 1, "yield_g": 5.0, "moisture_pct": 20},
        {"sample_id": "DRY-d1-R2", "treatment": "DRY", "day": 1, "yield_g": 4.0, "moisture_pct": 18},
    ])


# --------------------------------------------------------------------------- #
# Profile construction
# --------------------------------------------------------------------------- #
def test_fly_ash_profile_references_config():
    p = profiles.FLY_ASH_PROFILE
    assert p.grouping == "fly_ash"
    assert set(p.condition_codes) == {"OA", "PF", "GS"}        # from config dict
    assert "final_pH" in p.overview_variables
    assert p.comparison_variable_spec["final_pH"] == ("final_pH", "phreeqc_pH")
    assert profiles.PHREEQC_PROFILE.name == "PHREEQC"


# --------------------------------------------------------------------------- #
# Condition grouping (generic profile)
# --------------------------------------------------------------------------- #
def test_condition_grouping_uses_profile_fields():
    ann = replicates.annotate(_data(), WET_DRY_PROFILE)
    keys = set(ann[replicates.CONDITION_KEY_COLUMN])
    assert keys == {"treatment=WET_day=1", "treatment=DRY_day=1"}  # grouped by treatment+day


def test_replicate_summary_generic_profile():
    summary = replicates.replicate_summary(_data(), WET_DRY_PROFILE)
    assert len(summary) == 2
    assert set(summary["number_of_replicates"]) == {2}
    assert "mean_yield_g" in summary.columns and "mean_final_pH" not in summary.columns


# --------------------------------------------------------------------------- #
# Suggestion table + mapping status (generic profile)
# --------------------------------------------------------------------------- #
def test_suggestion_table_generic_profile():
    table = mapping_table.build_suggestion_table(_data(), MANIFEST, None, profile=WET_DRY_PROFILE)
    assert len(table) == 2
    assert (table["n_replicates"] == 2).all()
    assert (table["phreeqc_record_key"] == "m1").all()
    # Every status is one of the canonical four; none requires fly-ash metadata.
    assert set(table["mapping_status"]) <= set(replicates.MAPPING_STATUS_DEFINITIONS)


def test_mapping_status_generic_profile_is_exact():
    sample = {"sample_id": "WET-d1-R1", "treatment": "WET", "day": 1}
    scenario = {"phreeqc_record_key": "m1", "state": "batch"}
    assert replicates.mapping_status(sample, scenario, WET_DRY_PROFILE) == replicates.MAPPING_STATUS_EXACT
    # And sample_condition_code reads the profile's condition column + codes (WET/DRY).
    from flyash_phreeqc_ml import scenarios
    assert scenarios.sample_condition_code(sample, WET_DRY_PROFILE) == "WET"


# --------------------------------------------------------------------------- #
# Inclusion counts (generic profile)
# --------------------------------------------------------------------------- #
def test_inclusion_counts_generic_profile():
    data = _data()
    mapping = pd.DataFrame([{"sample_id": s, "phreeqc_record_key": "m1"}
                            for s in data["sample_id"]])
    comp = data.copy()
    comp["phreeqc_record_key"] = "m1"
    comp["model_yield_g"] = 9.0
    inc = comparison_inclusion(data, mapping, comp, "yield_g",
                               manifest=MANIFEST, profile=WET_DRY_PROFILE)
    assert inc["n_total"] == 4
    assert inc["rows_plotted"] + len(inc["excluded"]) == inc["n_total"]  # partition holds
    assert inc["rows_plotted"] == 4                                      # all comparable
    assert inc["unique_predictions_used"] == 1


# --------------------------------------------------------------------------- #
# Measured-data overview (generic profile)
# --------------------------------------------------------------------------- #
def test_measured_overview_generic_profile():
    assert mo.available_variables(_data(), WET_DRY_PROFILE) == ["yield_g", "moisture_pct"]
    ov = mo.prepare_overview(_data(), "yield_g", WET_DRY_PROFILE)
    assert ov["has_time"] is True            # "day" is the profile's time column
    assert ov["n_shown"] == 4
    assert ov["n_conditions"] == 2
    assert "day" in ov["plot"].columns


# --------------------------------------------------------------------------- #
# Material profile (Prompt 28) — resolvers, factory, JSON spec path, quarantine
# --------------------------------------------------------------------------- #
def test_material_resolvers_prefer_material_then_fall_back():
    # Fly ash: a material is attached but mass balance stays OFF (unchanged behaviour).
    assert profiles.FLY_ASH_PROFILE.material.material_id == "class_c_fly_ash"
    assert profiles.mass_balance_elements(profiles.FLY_ASH_PROFILE) == ()
    # Red mud: elements/phases/flag come from the material.
    assert profiles.mass_balance_elements(profiles.RED_MUD_PROFILE) == ("Ti", "V", "Fe", "Al")
    assert profiles.candidate_phases(profiles.RED_MUD_PROFILE)["Anatase"] == "Ti"
    assert profiles.precipitate_in_measured_solid(profiles.RED_MUD_PROFILE) is True
    assert "NaOH" in profiles.default_reagents(profiles.RED_MUD_PROFILE)
    # A material-less profile still resolves from its own fields (back-compat).
    legacy = profiles.DatasetProfile(name="legacy", mass_balance_elements=("Ca",))
    assert profiles.mass_balance_elements(legacy) == ("Ca",)


def test_dataset_profile_from_spec_builds_a_working_profile():
    spec = {
        "material": {
            "material_id": "demo_ore", "display_name": "Demo ore",
            "relevant_elements": ["Fe", "Al"], "mass_balance_elements": ["Fe", "Al"],
            "candidate_phases": {"Hematite": "Fe"}, "precipitate_in_measured_solid": False,
            "default_reagents": ["HCl"],
            "declared_assay": {
                "Fe": {"value": 40.0, "unit": "wt%", "provenance": "literature-confirmed",
                       "citation": "https://doi.org/10.0/demo"}},
        },
        "dataset": {"grouping": "generic", "condition_column": "reagent",
                    "important_fields": ["reagent", "liquid_solid_ratio"]},
    }
    profile = profiles.dataset_profile_from_spec(spec)
    assert profile.material.material_id == "demo_ore"
    assert profiles.mass_balance_elements(profile) == ("Fe", "Al")
    assert profile.important_fields == ("reagent", "liquid_solid_ratio")   # list -> tuple
    fe = profiles.usable_declared_assay(profile, "Fe")
    assert fe is not None and fe.value == 40.0


def test_literature_proposed_declared_assay_is_quarantined():
    spec = {"material": {"material_id": "m", "display_name": "M",
                         "declared_assay": {"Ti": {"value": 5.0, "provenance":
                                                   "literature-proposed", "citation": "url"}}}}
    profile = profiles.dataset_profile_from_spec(spec)
    # The proposed assay is kept for display but is NOT usable in a calculation.
    assert profile.material.declared_assay["Ti"].is_usable is False
    assert profiles.usable_declared_assay(profile, "Ti") is None


def test_spec_validation_rejects_bad_provenance_and_missing_citation():
    import pytest
    with pytest.raises(ValueError):
        profiles.assay_value_from_dict("Fe", {"value": 1.0, "provenance": "guess"})
    with pytest.raises(ValueError):   # literature provenance needs a citation
        profiles.assay_value_from_dict("Fe", {"value": 1.0, "provenance": "literature-confirmed"})
    with pytest.raises(ValueError):   # material needs id + display name
        profiles.material_profile_from_dict({"display_name": "no id"})


def test_load_shipped_red_mud_example_spec_runs_a_closure():
    """The shipped docs/examples JSON loads from disk and drives a real closure."""
    from pathlib import Path

    import pandas as pd

    from flyash_phreeqc_ml import mass_balance, units
    spec_path = Path(__file__).resolve().parents[1] / "docs" / "examples" / "red_mud_material.json"
    profile = profiles.load_dataset_profile(spec_path)
    assert profile.material.material_id == "red_mud"
    assert profiles.mass_balance_elements(profile) == ("Ti", "V", "Fe", "Al")
    assert profiles.precipitate_in_measured_solid(profile) is True
    # Ti declared assay is literature-proposed -> quarantined.
    assert profiles.usable_declared_assay(profile, "Ti") is None

    row = {"material_mass_g": 10.0, "liquid_volume_mL": 100.0, "solid_mass_g": 8.0,
           "Ti_starting_content": 5.0, "Ti_solid_residue": 3.0, "Ti_mM": 30.0}
    c = mass_balance.closure(row, "Ti", profile=profile)
    assert c["status"] == mass_balance.STATUS_COMPLETE
    assert c["n_in"] == 500.0 / units.MOLAR_MASSES["Ti"]
