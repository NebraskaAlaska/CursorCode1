"""End-to-end lock test for the full measured-data → model-comparison workflow.

Drives the pipeline through ``run_manager`` directly (no Streamlit) so a later
refactor cannot silently break it:

    create lab run → save synthetic measured data (4 conditions covering all four
    mapping statuses) → build suggestion table → accept per the rules (bulk-exact +
    selected scenario-level; verify unsafe is refused) → expand condition mapping →
    generate the per-run comparison (Prompt-1 path) → check comparison_meta.json +
    comparison_is_current → run comparison_inclusion and assert counts / exclusion
    reasons / residual signs / collapse / validity → mutate the data CSV and assert
    comparison_is_current flips with the right reason.

A second, unit-level pass runs grouping → suggestion → inclusion through the
alternate (non-fly-ash) profile from the generalization layer. All synthetic; no
network; small frames so the whole module runs in well under a minute.
"""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml import config, mapping_table, profiles, replicates, run_manager, scenarios
from flyash_phreeqc_ml.compare import comparison_inclusion, compare_measured_vs_phreeqc
from flyash_phreeqc_ml.compare import inclusion as I

RUN = "synth e2e run"


# --------------------------------------------------------------------------- #
# Synthetic inputs
# --------------------------------------------------------------------------- #
def _measured() -> pd.DataFrame:
    """Four conditions (3 replicates each) designed to hit all four statuses.

    * NaOH + OA, no time/NaOH_M  -> exact (OA is open-air, atmospheric-CO2 match)
    * NaOH + OA + time_min=10    -> scenario-level only (model can't confirm time)
    * HCl  + OA (acid)           -> unsafe (acid on a NaOH/CO2 scenario)
    * NaOH + OA + time_min=20    -> a 4th condition we leave unmapped (needs a new
                                    simulation; verified at the status level below)
    """
    rows = []

    def add(cond, n, **over):
        for i in range(1, n + 1):
            row = {"sample_id": f"SYNTH-{cond}-R{i}", "fly_ash_type": "Class C fly ash",
                   "leachant": "NaOH", "NaOH_M": "", "acid_M": "", "CO2_condition": "OA",
                   "liquid_solid_ratio": 5, "temperature_C": "", "initial_pH": "13.8"}
            row.update(over)
            rows.append(row)

    # exact: measured pH well above the model's 12.9 -> positive residuals.
    for i, ph in enumerate([13.5, 13.6, 13.4], start=1):
        rows.append({"sample_id": f"SYNTH-EXACT-R{i}", "fly_ash_type": "Class C fly ash",
                     "leachant": "NaOH", "NaOH_M": "", "acid_M": "", "CO2_condition": "OA",
                     "liquid_solid_ratio": 5, "temperature_C": "", "initial_pH": "13.8",
                     "final_pH": ph})
    add("SCN", 3, time_min=10, final_pH=13.0)
    add("ACID", 3, leachant="HCl", acid_M=0.5, final_pH=3.2)
    add("NEW", 3, time_min=20, final_pH=12.5)
    return pd.DataFrame(rows)


def _phreeqc_results() -> pd.DataFrame:
    """Minimal phreeqc_results frame: two batch scenarios (atmospheric + reduced)."""
    return pd.DataFrame([
        {"record_key": "f|sim1|batch|sol1", "source_file": "L-S_5_atmCO2.pqo",
         "simulation": 1, "state": "batch", "solution_number": 1, "pH": 12.9,
         "mol_Ca": 0.002, "mol_Si": 0.001, "mol_Al": 0.0005, "temperature_c": 25},
        {"record_key": "f|sim1|batch|sol2", "source_file": "L-S_5_lowCO2.pqo",
         "simulation": 1, "state": "batch", "solution_number": 2, "pH": 12.7,
         "mol_Ca": 0.0015, "mol_Si": 0.0008, "mol_Al": 0.0004, "temperature_c": 25},
    ])


ATM_KEY = "f|sim1|batch|sol1"  # atmospheric scenario every OA condition should match


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Point run storage + processed dir at a throwaway tree; write phreeqc_results."""
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    processed = tmp_path / "processed"
    processed.mkdir()
    monkeypatch.setattr(config, "PROCESSED_DIR", processed)
    phreeqc = _phreeqc_results()
    phreeqc.to_csv(processed / config.PHREEQC_RESULTS_CSV, index=False)
    return {"tmp": tmp_path, "phreeqc": phreeqc,
            "manifest": scenarios.build_scenario_manifest(phreeqc)}


# --------------------------------------------------------------------------- #
# The full workflow
# --------------------------------------------------------------------------- #
def test_full_workflow_lock(env):
    manifest = env["manifest"]
    measured = _measured()

    # 1) create run + save measured data --------------------------------- #
    run_manager.create_run(RUN, "lab_experiment")
    run_manager.save_lab_dataframe(RUN, measured, mode="replace")
    data = run_manager.read_data_file(RUN)
    assert len(data) == 12

    # 2) suggestion table covers exact / scenario-level / unsafe --------- #
    table = mapping_table.build_suggestion_table(data, manifest, None)
    by_key = {r["condition_key"]: r for _, r in table.iterrows()}
    rep = {c: replicates.condition_key(g.iloc[0].to_dict())
           for c, g in data.assign(_c=[s.split("-")[1] for s in data["sample_id"]]).groupby("_c")}
    assert by_key[rep["EXACT"]]["mapping_status"] == replicates.MAPPING_STATUS_EXACT
    assert by_key[rep["SCN"]]["mapping_status"] == replicates.MAPPING_STATUS_SCENARIO
    assert by_key[rep["ACID"]]["mapping_status"] == replicates.MAPPING_STATUS_UNSAFE

    # The 4th status — "needs new simulation" — is only produced when there is *no*
    # candidate (empty manifest), and by mapping_status when no scenario is linked.
    empty_table = mapping_table.build_suggestion_table(data, pd.DataFrame(), None)
    assert set(empty_table["mapping_status"]) == {replicates.MAPPING_STATUS_NEEDS_NEW}
    new_rep = data[data["sample_id"].str.contains("NEW")].iloc[0].to_dict()
    assert replicates.mapping_status(new_rep, None) == replicates.MAPPING_STATUS_NEEDS_NEW

    # 3) accept per the rules: bulk-exact + the selected scenario-level row #
    exact_rows = mapping_table.exact_suggestions(table)
    assert list(exact_rows["condition_key"]) == [rep["EXACT"]]      # only the exact one
    for _, r in exact_rows.iterrows():
        run_manager.add_condition_mapping(RUN, r["condition_key"], r["phreeqc_record_key"])
    # selected scenario-level (SCN); NEW is deliberately left unmapped.
    scn_row = by_key[rep["SCN"]]
    assert scn_row["mapping_status"] in mapping_table.SELECTABLE_STATUSES
    run_manager.add_condition_mapping(RUN, scn_row["condition_key"], scn_row["phreeqc_record_key"])

    # verify unsafe is refused by the accept rules (not bulk-exact, not selectable).
    acid_row = by_key[rep["ACID"]]
    assert acid_row["condition_key"] not in set(exact_rows["condition_key"])
    assert acid_row["mapping_status"] not in mapping_table.SELECTABLE_STATUSES

    # 4) expand condition mapping -> per-sample map ---------------------- #
    run_manager.apply_condition_mapping(RUN)
    sample_map = run_manager.read_mapping(RUN)
    assert len(sample_map) == 6                                     # exact(3) + scn(3)
    assert set(sample_map["phreeqc_record_key"]) == {ATM_KEY}

    # 5) generate the per-run comparison (Prompt-1 path) ---------------- #
    comparison = compare_measured_vs_phreeqc(data, env["phreeqc"], mapping=sample_map)
    comp_path = run_manager.comparison_path(RUN)
    comp_path.parent.mkdir(parents=True, exist_ok=True)
    comparison.to_csv(comp_path, index=False)
    run_manager.write_comparison_meta(RUN)

    # 6) provenance stamp + freshness ----------------------------------- #
    meta = run_manager.read_comparison_meta(RUN)
    assert meta["run_name"] == RUN and meta["run_type"] == "lab_experiment"
    assert all(meta["sources"][k] for k in ("data", "mapping", "phreeqc_results"))
    current, reasons = run_manager.comparison_is_current(RUN)
    assert current is True and reasons == []

    # 7) inclusion: counts / reasons / residual sign / collapse / validity #
    comp_df = pd.read_csv(comp_path)
    inc = comparison_inclusion(data, sample_map, comp_df, "final_pH", manifest=manifest)
    assert inc["n_total"] == 12
    assert inc["rows_plotted"] == 6                                 # exact + scenario-level
    assert inc["rows_plotted"] + len(inc["excluded"]) == inc["n_total"]   # partition holds
    # the 6 unmapped rows (acid + new) are excluded for "no saved mapping".
    assert inc["reason_counts"][I.REASON_NO_MAPPING] == 6
    # residual sign: measured − model; an exact row at 13.6 vs model 12.9 → +0.7.
    plotted = inc["plotted"]
    assert (plotted["residual"] >= 0).all() and (plotted["residual"] > 0).any()
    top = plotted.loc[plotted["measured"].idxmax()]
    assert top["residual"] == pytest.approx(top["measured"] - top["predicted"])
    assert top["residual"] == pytest.approx(13.6 - 12.9)
    # collapse: 6 plotted rows all map to one prediction.
    assert inc["unique_predictions_used"] == 1 and inc["collapse_warning"] is True
    # overall validity is preliminary (scenario-level mappings are included).
    assert inc["validity"] == I.VALIDITY_PRELIMINARY

    # overall mapping status: the unmapped acid+new conditions count as needs-new.
    overall = replicates.overall_mapping_status(data, sample_map, manifest)
    assert overall["counts"][replicates.MAPPING_STATUS_NEEDS_NEW] == 6
    assert overall["all_exact"] is False

    # 8) mutate the data CSV -> comparison_is_current flips with the reason #
    run_manager.append_lab_row(RUN, {"sample_id": "SYNTH-EXTRA-R1", "final_pH": "13.0",
                                     "leachant": "NaOH", "CO2_condition": "OA",
                                     "liquid_solid_ratio": 5})
    current2, reasons2 = run_manager.comparison_is_current(RUN)
    assert current2 is False
    assert any("run data CSV" in r for r in reasons2)


# --------------------------------------------------------------------------- #
# Second pass — alternate (non-fly-ash) profile at the unit level
# --------------------------------------------------------------------------- #
WET_DRY_PROFILE = profiles.DatasetProfile(
    name="soil moisture demo",
    time_column="day",
    condition_column="treatment",
    condition_codes={"WET": {"description": "watered", "caution": ""},
                     "DRY": {"description": "droughted", "caution": ""}},
    variable_columns=("yield_g",),
    overview_variables=("yield_g",),
    important_fields=("treatment", "day"),
    comparison_variable_spec={"yield_g": ("yield_g", "model_yield_g")},
    grouping="generic",
)

_ALT_MANIFEST = pd.DataFrame([
    {"phreeqc_record_key": "m1", "scenario_label": "model day1", "state": "batch"},
])


def _alt_data() -> pd.DataFrame:
    return pd.DataFrame([
        {"sample_id": "WET-d1-R1", "treatment": "WET", "day": 1, "yield_g": 10.0},
        {"sample_id": "WET-d1-R2", "treatment": "WET", "day": 1, "yield_g": 12.0},
        {"sample_id": "DRY-d1-R1", "treatment": "DRY", "day": 1, "yield_g": 5.0},
        {"sample_id": "DRY-d1-R2", "treatment": "DRY", "day": 1, "yield_g": 4.0},
    ])


def test_alternate_profile_grouping_suggestion_inclusion():
    data = _alt_data()

    # grouping: by treatment + day (no fly-ash fields involved).
    ann = replicates.annotate(data, WET_DRY_PROFILE)
    assert set(ann[replicates.CONDITION_KEY_COLUMN]) == {"treatment=WET_day=1", "treatment=DRY_day=1"}

    # suggestion table: one row per condition, every status canonical.
    table = mapping_table.build_suggestion_table(data, _ALT_MANIFEST, None, profile=WET_DRY_PROFILE)
    assert len(table) == 2
    assert set(table["mapping_status"]) <= set(replicates.MAPPING_STATUS_DEFINITIONS)

    # inclusion: counts partition; all rows comparable against the model column.
    mapping = pd.DataFrame([{"sample_id": s, "phreeqc_record_key": "m1"} for s in data["sample_id"]])
    comp = data.copy()
    comp["phreeqc_record_key"] = "m1"
    comp["model_yield_g"] = 9.0
    inc = comparison_inclusion(data, mapping, comp, "yield_g",
                               manifest=_ALT_MANIFEST, profile=WET_DRY_PROFILE)
    assert inc["n_total"] == 4
    assert inc["rows_plotted"] + len(inc["excluded"]) == inc["n_total"]
    assert inc["rows_plotted"] == 4
