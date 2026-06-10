"""Tests for the replicate-aware mapping layer.

Cover condition_key generation, replicate_id parsing, replicate grouping +
mean/std, condition-level mapping inheritance, the optional replicate→solution
mapping, the replicate-aware collision rules (same-condition replicates are not a
collision; different conditions sharing a PHREEQC row are), and the condition
mean comparison + run_manager condition-mapping storage.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from flyash_phreeqc_ml import config, replicates as rep, run_manager, scenarios


# --------------------------------------------------------------------------- #
# Feature 1 — condition_key + replicate_id
# --------------------------------------------------------------------------- #
def test_condition_key_example():
    sample = {
        "sample_id": "0.5M-NaOH-OA-10min-R1", "leachant": "NaOH", "NaOH_M": 0.5,
        "time_min": 10, "liquid_solid_ratio": 5, "CO2_condition": "open",
        "extra__condition_code": "OA",
    }
    assert rep.condition_key(sample) == "NaOH0.5M_OA_10min_LS5_open"


def test_condition_key_acid_uses_acid_M_and_drops_naoh():
    sample = {"sample_id": "0.5M-HCl-OA-10min", "leachant": "HCl", "NaOH_M": "",
              "acid_M": 0.5, "time_min": 10, "liquid_solid_ratio": 5, "CO2_condition": "open"}
    key = rep.condition_key(sample)
    assert key.startswith("HCl0.5M_OA_10min")


def test_replicate_id_parsing():
    assert rep.parse_replicate_id("0.5M-NaOH-OA-10min-R1") == "R1"
    assert rep.parse_replicate_id("cond_rep2") == "R2"
    assert rep.parse_replicate_id("sample-batch3") == "R3"
    assert rep.parse_replicate_id("replicate 1") == "R1"
    assert rep.parse_replicate_id("0.5M-NaOH-OA-10min") == ""


def test_replicate_id_prefers_explicit_field():
    assert rep.replicate_id({"sample_id": "x-R1", "replicate_id": "R9"}) == "R9"


def test_infer_replicate_ids_warns_when_missing():
    df = pd.DataFrame([
        {"sample_id": "0.5M-NaOH-OA-10min", "leachant": "NaOH", "NaOH_M": 0.5,
         "time_min": 10, "liquid_solid_ratio": 5, "CO2_condition": "open"},
        {"sample_id": "0.5M-NaOH-OA-10min", "leachant": "NaOH", "NaOH_M": 0.5,
         "time_min": 10, "liquid_solid_ratio": 5, "CO2_condition": "open"},
    ])
    ann, warnings = rep.infer_replicate_ids(df)
    assert sorted(ann["replicate_id"]) == ["R1", "R2"]
    assert warnings and "row order" in warnings[0]


# --------------------------------------------------------------------------- #
# Feature 2 — replicate summary (grouping + mean/std)
# --------------------------------------------------------------------------- #
def _three_replicates():
    base = {"leachant": "NaOH", "NaOH_M": 0.5, "time_min": 10, "liquid_solid_ratio": 5,
            "CO2_condition": "open", "extra__condition_code": "OA"}
    return pd.DataFrame([
        {**base, "sample_id": "0.5M-NaOH-OA-10min-R1", "final_pH": 13.0, "Ca_mM": 2.0,
         "Si_mM": 1.0, "Al_mM": 0.5},
        {**base, "sample_id": "0.5M-NaOH-OA-10min-R2", "final_pH": 13.2, "Ca_mM": 2.2,
         "Si_mM": 1.1, "Al_mM": 0.6},
        {**base, "sample_id": "0.5M-NaOH-OA-10min-R3", "final_pH": 13.1, "Ca_mM": 2.1,
         "Si_mM": 1.2, "Al_mM": 0.7},
    ])


def test_replicate_summary_groups_and_stats():
    summary = rep.replicate_summary(_three_replicates())
    assert list(summary.columns) == rep.REPLICATE_SUMMARY_COLUMNS
    assert len(summary) == 1
    row = summary.iloc[0]
    assert row["condition_key"] == "NaOH0.5M_OA_10min_LS5_open"
    assert row["number_of_replicates"] == 3
    assert row["replicate_ids"] == "R1, R2, R3"
    assert row["mean_final_pH"] == pytest.approx(13.1)
    assert row["std_Ca_mM"] == pytest.approx(pd.Series([2.0, 2.2, 2.1]).std(ddof=1))


def test_replicate_summary_single_replicate_std_is_nan():
    df = _three_replicates().iloc[[0]]
    row = rep.replicate_summary(df).iloc[0]
    assert row["number_of_replicates"] == 1
    assert math.isnan(row["std_final_pH"])


# --------------------------------------------------------------------------- #
# Feature 3/4 — condition mapping inheritance + replicate→solution
# --------------------------------------------------------------------------- #
def test_expand_condition_mapping_inherited_by_all_replicates():
    df = _three_replicates()
    cmap = {"NaOH0.5M_OA_10min_LS5_open": "file|sim1|batch|sol1"}
    expanded = rep.expand_condition_mapping(df, cmap)
    assert list(expanded.columns) == ["sample_id", "phreeqc_record_key"]
    assert len(expanded) == 3
    assert set(expanded["phreeqc_record_key"]) == {"file|sim1|batch|sol1"}


def test_replicate_record_key_swaps_solution():
    assert rep.replicate_record_key("file|sim1|batch|sol1", 2) == "file|sim1|batch|sol2"


def test_expand_replicate_solution_mapping():
    df = _three_replicates()
    cmap = {"NaOH0.5M_OA_10min_LS5_open": "file|sim1|batch|sol1"}
    rs = {"R1": 1, "R2": 2, "R3": 3}
    expanded = rep.expand_replicate_solution_mapping(df, cmap, rs)
    keys = dict(zip(expanded["sample_id"], expanded["phreeqc_record_key"]))
    assert keys["0.5M-NaOH-OA-10min-R2"].endswith("sol2")
    assert keys["0.5M-NaOH-OA-10min-R3"].endswith("sol3")


# --------------------------------------------------------------------------- #
# Feature 7 — replicate-aware collisions
# --------------------------------------------------------------------------- #
def test_same_condition_replicates_not_a_collision():
    df = _three_replicates()
    mapping = pd.DataFrame([
        {"sample_id": "0.5M-NaOH-OA-10min-R1", "phreeqc_record_key": "K"},
        {"sample_id": "0.5M-NaOH-OA-10min-R2", "phreeqc_record_key": "K"},
        {"sample_id": "0.5M-NaOH-OA-10min-R3", "phreeqc_record_key": "K"},
    ])
    warns = rep.collision_report(df, mapping)
    assert not any(w["type"] == "cross_condition_collision" for w in warns)


def test_different_conditions_same_record_warns():
    df = pd.DataFrame([
        {"sample_id": "0.5M-NaOH-OA-10min-R1", "leachant": "NaOH", "NaOH_M": 0.5,
         "time_min": 10, "liquid_solid_ratio": 5, "CO2_condition": "open"},
        {"sample_id": "0.5M-NaOH-PF-60min-R1", "leachant": "NaOH", "NaOH_M": 0.5,
         "time_min": 60, "liquid_solid_ratio": 5, "CO2_condition": "open"},
    ])
    mapping = pd.DataFrame([
        {"sample_id": "0.5M-NaOH-OA-10min-R1", "phreeqc_record_key": "K"},
        {"sample_id": "0.5M-NaOH-PF-60min-R1", "phreeqc_record_key": "K"},
    ])
    warns = rep.collision_report(df, mapping)
    assert any(w["type"] == "cross_condition_collision" for w in warns)


def test_acid_mapped_to_naoh_scenario_warns():
    df = pd.DataFrame([
        {"sample_id": "0.5M-HCl-OA-10min", "leachant": "HCl", "acid_M": 0.5,
         "time_min": 10, "liquid_solid_ratio": 5, "CO2_condition": "open"},
    ])
    mapping = pd.DataFrame([{"sample_id": "0.5M-HCl-OA-10min", "phreeqc_record_key": "K"}])
    warns = rep.collision_report(df, mapping)
    assert any(w["type"] == "acid_to_naoh" for w in warns)


# --------------------------------------------------------------------------- #
# Feature 5/6 — condition mean comparison
# --------------------------------------------------------------------------- #
def test_condition_mean_comparison_residuals_and_warning():
    df = _three_replicates()
    cmap = {"NaOH0.5M_OA_10min_LS5_open": "K"}
    manifest = pd.DataFrame([{
        "phreeqc_record_key": "K", "predicted_pH": 12.6, "predicted_Ca_mM": 2.0,
        "predicted_Si_mM": 1.0, "predicted_Al_mM": 0.4,
    }])
    comp = rep.condition_mean_comparison(df, cmap, manifest)
    row = comp.iloc[0]
    assert row["n_replicates"] == 3
    assert row["residual_pH"] == pytest.approx(13.1 - 12.6)
    assert row["warning"] == ""  # 3 replicates, mapped

    # single replicate -> n<2 warning
    one = rep.condition_mean_comparison(df.iloc[[0]], cmap, manifest)
    assert "n_replicates<2" in one.iloc[0]["warning"]


# --------------------------------------------------------------------------- #
# run_manager condition-mapping storage
# --------------------------------------------------------------------------- #
@pytest.fixture()
def lab_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    run_manager.create_run("rep lab", "lab_experiment")
    run_manager.save_lab_dataframe("rep lab", _three_replicates(), mode="replace")
    return "rep lab"


def test_condition_mapping_crud_and_apply(lab_run):
    assert not run_manager.has_condition_mapping(lab_run)
    run_manager.add_condition_mapping(lab_run, "NaOH0.5M_OA_10min_LS5_open", "file|sim1|batch|sol1")
    assert run_manager.has_condition_mapping(lab_run)

    # apply expands the one condition link to all three replicate samples.
    path = run_manager.apply_condition_mapping(lab_run)
    sample_map = pd.read_csv(path)
    assert len(sample_map) == 3
    assert set(sample_map["phreeqc_record_key"]) == {"file|sim1|batch|sol1"}


def test_apply_condition_mapping_requires_a_mapping(lab_run):
    with pytest.raises(run_manager.RunManagerError):
        run_manager.apply_condition_mapping(lab_run)


def test_condition_mapping_persists_notes(lab_run):
    # Optional free-text notes round-trip in the condition map but never reach the
    # per-sample map the comparison step reads.
    run_manager.add_condition_mapping(
        lab_run, "NaOH0.5M_OA_10min_LS5_open", "file|sim1|batch|sol1",
        notes="covered cup; CO2 reduced",
    )
    cmap = run_manager.read_condition_mapping(lab_run)
    assert "notes" in cmap.columns
    assert cmap.iloc[0]["notes"] == "covered cup; CO2 reduced"
    sample_map = pd.read_csv(run_manager.apply_condition_mapping(lab_run))
    assert "notes" not in sample_map.columns  # pipeline schema stays 2-column


# --------------------------------------------------------------------------- #
# Mapping status + conditions-needing-simulation (presentation logic)
# --------------------------------------------------------------------------- #
def test_mapping_status_classification():
    naoh = {"sample_id": "0.5M-NaOH-OA-10min", "leachant": "NaOH", "NaOH_M": 0.5,
            "time_min": 10, "CO2_condition": "open"}
    hcl = {"sample_id": "0.5M-HCl-OA-10min", "leachant": "HCl", "acid_M": 0.5,
           "time_min": 10, "CO2_condition": "open"}
    scenario_no_meta = {"CO2_condition": "atm_CO2"}  # PHREEQC: no time/condition/NaOH

    # No scenario -> needs new simulation.
    assert rep.mapping_status(naoh, None) == rep.MAPPING_STATUS_NEEDS_NEW
    # Acid sample on a NaOH scenario -> unsafe.
    assert rep.mapping_status(hcl, scenario_no_meta) == rep.MAPPING_STATUS_UNSAFE
    # NaOH with known time but PHREEQC lacks it -> scenario-level only.
    assert rep.mapping_status(naoh, scenario_no_meta) == rep.MAPPING_STATUS_SCENARIO
    # Exact only when nothing is missing (no extra experimental metadata to confirm).
    bare = {"sample_id": "x", "leachant": "NaOH", "CO2_condition": "open"}
    assert rep.mapping_status(bare, {"CO2_condition": "atm_CO2"}) == rep.MAPPING_STATUS_EXACT


def test_pf_cover_capped_at_scenario_level():
    # PF is a reduced-CO2 cover that is NOT confirmed airtight. Against a reduced-family
    # model scenario it is compatible but unconfirmed -> at most scenario-level, never
    # exact; against an atmospheric scenario it is a CO2 conflict -> unsafe.
    pf = {"sample_id": "0.5M-NaOH-PF-10min", "leachant": "NaOH", "CO2_condition": "PF"}
    assert rep.mapping_status(pf, {"CO2_condition": "low_CO2"}) == rep.MAPPING_STATUS_SCENARIO
    assert rep.mapping_status(pf, {"CO2_condition": "atm_CO2"}) == rep.MAPPING_STATUS_UNSAFE
    gs = {"sample_id": "0.5M-NaOH-GS-10min", "leachant": "NaOH", "CO2_condition": "GS"}
    assert rep.mapping_status(gs, {"CO2_condition": "no_CO2"}) == rep.MAPPING_STATUS_SCENARIO


def test_oa_open_air_can_be_exact():
    # OA (open air) is directly represented by an atmospheric-CO2 model scenario, so it
    # can reach exact; against a reduced scenario it is a conflict -> unsafe.
    oa = {"sample_id": "0.5M-NaOH-OA-10min", "leachant": "NaOH", "CO2_condition": "OA"}
    assert rep.mapping_status(oa, {"CO2_condition": "atm_CO2"}) == rep.MAPPING_STATUS_EXACT
    assert rep.mapping_status(oa, {"CO2_condition": "low_CO2"}) == rep.MAPPING_STATUS_UNSAFE


def test_overall_mapping_status_not_all_exact():
    df = _three_replicates()
    mapping = pd.DataFrame([
        {"sample_id": "0.5M-NaOH-OA-10min-R1", "phreeqc_record_key": "K"},
    ])
    manifest = pd.DataFrame([{"phreeqc_record_key": "K", "CO2_condition": "atm_CO2"}])
    status = rep.overall_mapping_status(df, mapping, manifest)
    assert status["all_exact"] is False           # time_min known, PHREEQC lacks it
    assert status["counts"][rep.MAPPING_STATUS_SCENARIO] >= 1
    assert status["n_unmapped"] == 2              # the other two replicates unmapped


def test_conditions_needing_simulation_columns_and_reason():
    df = _three_replicates()
    needed = rep.conditions_needing_simulation(df, {}, pd.DataFrame())
    assert list(needed.columns) == rep.CONDITIONS_NEEDED_COLUMNS
    assert len(needed) == 1                        # the one condition, unmapped
    row = needed.iloc[0]
    assert row["condition_code"] == "OA"
    assert "no mapping" in row["reason_needed"]
