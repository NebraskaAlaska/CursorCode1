"""Tests for the consolidated measured-condition → model suggestion table.

The Match tab is automatic-first: one suggestion row per measured condition,
classified into the four mapping statuses, with an ``already_mapped`` flag and
bulk-accept filtering. These tests pin that construction (per status), the
already-mapped flag, the exact-only bulk filter, and the ``override=true`` tag on
a manually-overridden unsafe mapping. Synthetic data only.
"""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml import config, mapping_table, replicates, run_manager

# A tiny synthetic manifest: one batch scenario, L/S 5, open CO2 (no time/condition
# columns, exactly like the real manifest built from phreeqc_results.csv).
MANIFEST = pd.DataFrame([{
    "phreeqc_record_key": "f|sim1|batch|sol1",
    "scenario_label": "L/S 5 open — batch sol1",
    "state": "batch",
    "liquid_solid_ratio": 5.0,
    "CO2_condition": "open",
    "temperature_C": float("nan"),
    "NaOH_M": float("nan"),
}])


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _status_for(rows: list[dict], manifest=MANIFEST) -> str:
    table = mapping_table.build_suggestion_table(_df(rows), manifest)
    assert len(table) == 1
    return table.iloc[0]["mapping_status"]


# --------------------------------------------------------------------------- #
# Table construction per status
# --------------------------------------------------------------------------- #
def test_exact_status_when_no_unconfirmable_metadata():
    # NaOH, L/S 5, open CO2, no time / OA-PF-GS / NaOH_M the model can't confirm.
    rows = [{"sample_id": "S1", "leachant": "NaOH",
             "liquid_solid_ratio": 5, "CO2_condition": "open", "final_pH": 13.0}]
    assert _status_for(rows) == replicates.MAPPING_STATUS_EXACT


def test_scenario_level_when_experiment_specifies_time():
    # Adding a measured time the model does not specify caps it to scenario-level.
    rows = [{"sample_id": "S1", "leachant": "NaOH", "time_min": 10,
             "liquid_solid_ratio": 5, "CO2_condition": "open", "final_pH": 13.0}]
    assert _status_for(rows) == replicates.MAPPING_STATUS_SCENARIO


def test_unsafe_when_acid_leachant():
    rows = [{"sample_id": "A1", "leachant": "HCl", "acid_M": 0.5,
             "liquid_solid_ratio": 5, "CO2_condition": "open", "final_pH": 3.2}]
    assert _status_for(rows) == replicates.MAPPING_STATUS_UNSAFE


def test_needs_new_simulation_when_no_candidates():
    rows = [{"sample_id": "S1", "leachant": "NaOH",
             "liquid_solid_ratio": 5, "CO2_condition": "open", "final_pH": 13.0}]
    table = mapping_table.build_suggestion_table(_df(rows), pd.DataFrame())  # empty manifest
    assert len(table) == 1
    row = table.iloc[0]
    assert row["mapping_status"] == replicates.MAPPING_STATUS_NEEDS_NEW
    assert row["phreeqc_record_key"] == ""


def test_replicates_collapse_to_one_row_with_count():
    rows = [{"sample_id": f"S1-R{i}", "leachant": "NaOH", "liquid_solid_ratio": 5,
             "CO2_condition": "open", "final_pH": 13.0} for i in (1, 2, 3)]
    table = mapping_table.build_suggestion_table(_df(rows), MANIFEST)
    assert len(table) == 1                     # one condition, three replicates
    assert int(table.iloc[0]["n_replicates"]) == 3


def test_empty_data_gives_empty_table():
    table = mapping_table.build_suggestion_table(pd.DataFrame(), MANIFEST)
    assert table.empty
    assert list(table.columns) == mapping_table.SUGGESTION_TABLE_COLUMNS


# --------------------------------------------------------------------------- #
# already_mapped flag
# --------------------------------------------------------------------------- #
def test_already_mapped_flag_from_existing_mapping():
    rows = [
        {"sample_id": "S1", "leachant": "NaOH", "liquid_solid_ratio": 5,
         "CO2_condition": "open", "final_pH": 13.0},
        {"sample_id": "S2", "leachant": "NaOH", "liquid_solid_ratio": 10,
         "CO2_condition": "open", "final_pH": 12.5},
    ]
    ck1 = replicates.condition_key(rows[0])
    existing = pd.DataFrame([{"condition_key": ck1, "phreeqc_record_key": "f|sim1|batch|sol1"}])
    table = mapping_table.build_suggestion_table(_df(rows), MANIFEST, existing)
    by_ck = {r["condition_key"]: r for _, r in table.iterrows()}
    assert by_ck[ck1]["already_mapped"] is True
    other = [v for k, v in by_ck.items() if k != ck1][0]
    assert other["already_mapped"] is False


# --------------------------------------------------------------------------- #
# Bulk-accept filtering (exact only)
# --------------------------------------------------------------------------- #
def test_exact_suggestions_filters_to_exact_with_candidate():
    rows = [
        {"sample_id": "S1", "leachant": "NaOH", "liquid_solid_ratio": 5,
         "CO2_condition": "open", "final_pH": 13.0},                       # exact
        {"sample_id": "S2", "leachant": "NaOH", "time_min": 10, "liquid_solid_ratio": 5,
         "CO2_condition": "open", "final_pH": 12.0},                       # scenario-level
        {"sample_id": "A1", "leachant": "HCl", "acid_M": 0.5, "liquid_solid_ratio": 5,
         "CO2_condition": "open", "final_pH": 3.0},                        # unsafe
    ]
    table = mapping_table.build_suggestion_table(_df(rows), MANIFEST)
    exact = mapping_table.exact_suggestions(table)
    assert set(exact["mapping_status"]) == {replicates.MAPPING_STATUS_EXACT}
    assert len(exact) == 1
    assert (exact["phreeqc_record_key"].astype(str).str.strip() != "").all()
    # Unsafe is not in the bulk set and not in the selectable set.
    assert replicates.MAPPING_STATUS_UNSAFE not in mapping_table.SELECTABLE_STATUSES


def test_needs_new_simulation_helper_matches_status_column():
    rows = [{"sample_id": "S1", "leachant": "NaOH", "liquid_solid_ratio": 5,
             "CO2_condition": "open", "final_pH": 13.0}]
    table = mapping_table.build_suggestion_table(_df(rows), pd.DataFrame())
    nn = mapping_table.needs_new_simulation(table)
    # The helper count agrees with the table's status column (one source of truth).
    assert len(nn) == int((table["mapping_status"] == replicates.MAPPING_STATUS_NEEDS_NEW).sum())
    assert len(nn) == 1


# --------------------------------------------------------------------------- #
# Override tagging through the condition-mapping save path
# --------------------------------------------------------------------------- #
@pytest.fixture()
def lab_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    run_manager.create_run("ovr lab", "lab_experiment")
    data = pd.DataFrame([{"sample_id": "S1", "leachant": "HCl", "acid_M": 0.5,
                          "liquid_solid_ratio": 5, "CO2_condition": "open", "final_pH": 3.0}])
    run_manager.save_lab_dataframe("ovr lab", data, mode="replace")
    return "ovr lab"


def test_override_defaults_false(lab_run):
    run_manager.add_condition_mapping(lab_run, "HCl0.5M_LS5_open", "f|sim1|batch|sol1")
    cmap = run_manager.read_condition_mapping(lab_run)
    assert "override" in cmap.columns
    assert bool(cmap.iloc[0]["override"]) is False


def test_override_true_is_recorded(lab_run):
    run_manager.add_condition_mapping(
        lab_run, "HCl0.5M_LS5_open", "f|sim1|batch|sol1",
        notes="confirmed unsafe override", override=True,
    )
    cmap = run_manager.read_condition_mapping(lab_run)
    assert bool(cmap.iloc[0]["override"]) is True
    # The override flag never leaks into the per-sample map the pipeline reads.
    sample_map = pd.read_csv(run_manager.apply_condition_mapping(lab_run))
    assert "override" not in sample_map.columns
    assert list(sample_map.columns) == ["sample_id", "phreeqc_record_key"]
