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
