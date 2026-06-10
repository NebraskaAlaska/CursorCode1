"""Tests for the PHREEQC scenario manifest + rule-based mapping assistant."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from flyash_phreeqc_ml import config, replicates, scenarios


# --------------------------------------------------------------------------- #
# Feature 1 — metadata inference from filenames
# --------------------------------------------------------------------------- #
def test_infer_ls_ratio_from_filename():
    meta = scenarios.infer_metadata_from_filename("L-S_5_atmCO2.pqo")
    assert meta["liquid_solid_ratio"] == 5.0


def test_infer_co2_atm_low_no():
    assert scenarios.infer_metadata_from_filename("L-S_5_atmCO2.pqo")["CO2_condition"] == "atm_CO2"
    assert scenarios.infer_metadata_from_filename("L-S_5_lowCO2.pqo")["CO2_condition"] == "low_CO2"
    assert scenarios.infer_metadata_from_filename("L-S_5_noCO2.pqo")["CO2_condition"] == "no_CO2"


def test_infer_unknown_when_no_tokens():
    meta = scenarios.infer_metadata_from_filename("Solution with alkalinity.pqo")
    assert meta["liquid_solid_ratio"] is None
    assert meta["CO2_condition"] == scenarios.UNKNOWN


def test_co2_family_and_compatibility():
    # OA / atm_CO2 are atmospheric; PF/GS and low_CO2/no_CO2 are reduced (never "sealed").
    assert scenarios.co2_family("OA") == scenarios.ATMOSPHERIC
    assert scenarios.co2_family("atm_CO2") == scenarios.ATMOSPHERIC
    assert scenarios.co2_family("PF") == scenarios.REDUCED
    assert scenarios.co2_family("GS") == scenarios.REDUCED
    assert scenarios.co2_family("low_CO2") == scenarios.REDUCED
    assert scenarios.co2_family("no_CO2") == scenarios.REDUCED
    assert scenarios.co2_family("") == scenarios.UNKNOWN
    # atmospheric vs reduced are NOT compatible; unknown is compatible with anything.
    assert scenarios.co2_compatible("OA", "atm_CO2") is True
    assert scenarios.co2_compatible("PF", "low_CO2") is True       # same (reduced) family
    assert scenarios.co2_compatible("OA", "low_CO2") is False      # atmospheric vs reduced
    assert scenarios.co2_compatible("PF", "atm_CO2") is False
    assert scenarios.co2_compatible("unknown", "no_CO2") is True


# --------------------------------------------------------------------------- #
# Manifest build
# --------------------------------------------------------------------------- #
def _results_frame():
    return pd.DataFrame([
        {"record_key": "L-S_5_atmCO2.pqo|sim1|batch|sol1", "source_file": "L-S_5_atmCO2.pqo",
         "simulation": 1, "state": "batch", "solution_number": 1, "pH": 9.63,
         "temperature_c": 25.0, "mol_Ca": 0.00236, "mol_Si": 0.000314, "mol_Al": 0.000739},
        {"record_key": "L-S_5_atmCO2.pqo|sim1|initial|sol1", "source_file": "L-S_5_atmCO2.pqo",
         "simulation": 1, "state": "initial", "solution_number": 1, "pH": 13.1,
         "temperature_c": 25.0, "mol_Ca": 0.00236, "mol_Si": 0.000314, "mol_Al": 0.000740},
    ])


def test_build_manifest_columns_and_conversion():
    manifest = scenarios.build_scenario_manifest(_results_frame())
    assert list(manifest.columns) == scenarios.MANIFEST_COLUMNS
    batch = manifest.iloc[0]
    # molality * 1000 -> mM
    assert batch["predicted_Ca_mM"] == pytest.approx(0.00236 * config.PHREEQC_MOLALITY_TO_MM)
    assert batch["liquid_solid_ratio"] == 5.0
    assert batch["CO2_condition"] == "atm_CO2"
    assert batch["metadata_quality"] == "good"
    # mol_Fe absent in input -> predicted Fe is NaN (unavailable, not zero).
    assert math.isnan(batch["predicted_Fe_mM"])


def test_build_manifest_empty():
    assert scenarios.build_scenario_manifest(pd.DataFrame()).empty


# --------------------------------------------------------------------------- #
# Feature 3 — scoring
# --------------------------------------------------------------------------- #
def _sample(**over):
    base = {"sample_id": "S1", "liquid_solid_ratio": 5.0,
            "CO2_condition": "open", "temperature_C": 25.0}
    base.update(over)
    return base


def _scenario(state="batch", ls=5.0, co2="atm_CO2", temp=25.0):
    return {"phreeqc_record_key": "K", "scenario_label": "lbl", "state": state,
            "liquid_solid_ratio": ls, "CO2_condition": co2, "temperature_C": temp}


def test_score_high_confidence_match():
    res = scenarios.score_scenario(_sample(), _scenario())
    # batch(3)+LS(3)+CO2(2)+temp(1) = 9
    assert res["score"] == 9
    assert res["confidence"] == "high"
    assert "liquid_solid_ratio" in res["matched_fields"]
    assert res["mismatched_fields"] == []


def test_score_low_confidence_match():
    # initial state, wrong L/S, opposite CO2 -> deep negative
    res = scenarios.score_scenario(
        _sample(), _scenario(state="initial", ls=20.0, co2="sealed"))
    assert res["confidence"] == "low"
    assert res["score"] < scenarios.MEDIUM_SCORE


def test_initial_state_penalty_below_batch():
    batch = scenarios.score_scenario(_sample(), _scenario(state="batch"))
    initial = scenarios.score_scenario(_sample(), _scenario(state="initial"))
    # the only difference is state; the -4 vs +3 swing must lower the initial score.
    assert initial["score"] == batch["score"] - 7
    assert "state=initial (starting solution)" in initial["mismatched_fields"]


def test_temperature_unknown_still_scores():
    res = scenarios.score_scenario(_sample(temperature_C=""), _scenario())
    assert "temperature (unknown ok)" in res["matched_fields"]


def test_confidence_bands():
    assert scenarios.confidence_for(9) == "high"
    assert scenarios.confidence_for(5) == "medium"
    assert scenarios.confidence_for(1) == "low"


def test_suggest_mappings_ranks_batch_over_initial():
    manifest = scenarios.build_scenario_manifest(_results_frame())
    top = scenarios.suggest_mappings(_sample(), manifest, top_n=3)
    assert top[0]["suggested_phreeqc_record_key"].endswith("batch|sol1")
    assert top[0]["score"] > top[1]["score"]


def test_suggest_mappings_empty_manifest():
    assert scenarios.suggest_mappings(_sample(), pd.DataFrame()) == []


# --------------------------------------------------------------------------- #
# Feature 5 / 6 — no-good-match + samples needing new simulations
# --------------------------------------------------------------------------- #
def test_no_good_match_detection():
    # a manifest that only offers a conflicting initial-state row -> low confidence
    manifest = pd.DataFrame([
        {"phreeqc_record_key": "K", "scenario_label": "lbl", "state": "initial",
         "liquid_solid_ratio": 20.0, "CO2_condition": "sealed", "temperature_C": 25.0},
    ])
    assert scenarios.best_confidence(_sample(), manifest) == "low"


def test_samples_needing_simulation_flags_unmapped_and_collisions():
    samples = pd.DataFrame([
        {"sample_id": "S1", "NaOH_M": 4, "time_min": 60,
         "liquid_solid_ratio": 5.0, "CO2_condition": "open", "temperature_C": 25.0},
        {"sample_id": "S2", "NaOH_M": 4, "time_min": 60,
         "liquid_solid_ratio": 5.0, "CO2_condition": "open", "temperature_C": 25.0},
        {"sample_id": "S3", "NaOH_M": 4, "time_min": 60,
         "liquid_solid_ratio": 5.0, "CO2_condition": "open", "temperature_C": 25.0},
    ])
    # S1 & S2 collide on K1; S3 has no mapping at all.
    mapping = pd.DataFrame([
        {"sample_id": "S1", "phreeqc_record_key": "K1"},
        {"sample_id": "S2", "phreeqc_record_key": "K1"},
    ])
    manifest = scenarios.build_scenario_manifest(_results_frame())
    needed = scenarios.samples_needing_simulation(samples, mapping, manifest)
    reasons = needed.set_index("sample_id")["reason_new_simulation_needed"].to_dict()
    assert "shares one PHREEQC row with other samples" in reasons["S1"]
    assert "shares one PHREEQC row with other samples" in reasons["S2"]
    assert "no mapping exists" in reasons["S3"]
    assert list(needed.columns) == scenarios._SIM_NEEDED_COLUMNS


def test_samples_needing_simulation_empty_inputs():
    out = scenarios.samples_needing_simulation(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    assert out.empty
    assert list(out.columns) == scenarios._SIM_NEEDED_COLUMNS


# --------------------------------------------------------------------------- #
# Confidence caps when the experiment specifies metadata PHREEQC lacks
# --------------------------------------------------------------------------- #
def test_high_requires_both_sides_time_capped_to_medium():
    # Perfect L/S + CO2 + batch + temp (score 9, base high) BUT the experiment
    # specifies time and PHREEQC does not -> capped to medium.
    res = scenarios.score_scenario(_sample(time_min=60), _scenario())
    assert res["score"] == 9
    assert res["base_confidence"] == "high"
    assert res["confidence"] == "medium"
    assert "time_min" in res["phreeqc_missing"]
    assert any("Experimental time is known, but the selected PHREEQC scenario does not "
               "specify time." == n for n in res["metadata_notes"])


def test_pf_cover_caps_to_scenario_level():
    # PF is a reduced-CO2 cover the model doesn't explicitly represent: even a same-
    # family (reduced) match must cap to medium (scenario-level), never exact-via-CO2.
    res = scenarios.score_scenario(
        _sample(sample_id="0.5M-NaOH-PF-10min", CO2_condition="PF"),
        _scenario(co2="low_CO2"))
    assert res["base_confidence"] == "high"
    assert res["confidence"] == "medium"
    assert "condition_code" in res["phreeqc_missing"]
    assert any("PF" in n and "plastic_flap" in n for n in res["metadata_notes"])


def test_oa_open_air_not_capped_by_cover():
    # OA (open air) is directly represented by an atmospheric-CO2 model scenario, so
    # it is NOT capped by the cover rule and can stay high (exact-able).
    res = scenarios.score_scenario(
        _sample(sample_id="0.5M-NaOH-OA-10min", CO2_condition="OA"),
        _scenario(co2="atm_CO2"))
    assert res["base_confidence"] == "high"
    assert res["confidence"] == "high"
    assert "condition_code" not in res["phreeqc_missing"]


def test_naoh_known_caps_to_medium():
    res = scenarios.score_scenario(_sample(NaOH_M=0.5), _scenario())
    assert res["confidence"] == "medium"
    assert "NaOH_M" in res["phreeqc_missing"]


def test_no_cap_when_experiment_lacks_time_and_condition():
    # The original "perfect match" sample (no time/condition/NaOH) stays high.
    res = scenarios.score_scenario(_sample(), _scenario())
    assert res["confidence"] == "high"
    assert res["metadata_notes"] == []


def test_cap_never_raises_low_to_medium():
    res = scenarios.score_scenario(
        _sample(time_min=60), _scenario(state="initial", ls=20.0, co2="sealed"))
    assert res["confidence"] == "low"   # cap only lowers; a low match stays low


def test_sample_condition_code_sources():
    assert scenarios.sample_condition_code({"extra__condition_code": "PF"}) == "PF"
    assert scenarios.sample_condition_code({"sample_id": "0.5M-NaOH-GS-20min"}) == "GS"
    assert scenarios.sample_condition_code({"notes": "condition_code=OA; foo"}) == "OA"
    assert scenarios.sample_condition_code({"sample_id": "plain_sample"}) is None


def test_cover_condition_and_co2_exposure():
    assert scenarios.cover_condition("OA") == "open_air"
    assert scenarios.cover_condition("pf") == "plastic_flap"
    assert scenarios.cover_condition("GS") == "glass_cover"
    assert scenarios.cover_condition(None) is None
    assert scenarios.cover_condition("ZZ") is None
    # OA = open; PF/GS = reduced (covered) — never "sealed".
    assert scenarios.co2_exposure_level("OA") == "open"
    for code in ("PF", "GS"):
        level = scenarios.co2_exposure_level(code)
        assert level == "reduced" and "sealed" not in level
    assert scenarios.co2_exposure_level(None) is None


# --------------------------------------------------------------------------- #
# Solution descriptions (explain sol1 / sol2 / sol3)
# --------------------------------------------------------------------------- #
def test_describe_solutions_one_row_per_solution():
    long = pd.DataFrame([
        {"source_file": "L-S_5.pqi", "solution_number": 1, "solution_label": None,
         "temp": 25.0, "ph": 13.1, "element": "Na", "concentration": 1.0},
        {"source_file": "L-S_5.pqi", "solution_number": 1, "solution_label": None,
         "temp": 25.0, "ph": 13.1, "element": "Si", "concentration": 0.5},
        {"source_file": "rev.pqi", "solution_number": 2, "solution_label": "L-S solution 2",
         "temp": 25.0, "ph": 12.9, "element": "Na", "concentration": 1.0},
    ])
    desc = scenarios.describe_solutions(long)
    assert list(desc.columns) == scenarios.SOLUTION_DESCRIPTION_COLUMNS
    assert len(desc) == 2  # one row per (file, solution_number)
    labelled = desc[desc["solution_number"] == 2].iloc[0]
    assert labelled["description"] == "L-S solution 2"
    unlabelled = desc[desc["source_file"] == "L-S_5.pqi"].iloc[0]
    assert "no label" in unlabelled["description"]


def test_describe_solutions_empty():
    assert scenarios.describe_solutions(pd.DataFrame()).empty


# --------------------------------------------------------------------------- #
# Decision trace (machine-readable explanation)
# --------------------------------------------------------------------------- #
_REQUIRED_TRACE_KEYS = {"field", "sample_value", "scenario_value", "outcome", "points", "note"}

# (name, sample, scenario, expected mapping_status) — one per status that has a scenario.
_TRACE_CASES = [
    ("exact",
     {"sample_id": "S1", "leachant": "NaOH", "liquid_solid_ratio": 5, "CO2_condition": "OA"},
     {"state": "batch", "liquid_solid_ratio": 5.0, "CO2_condition": "atm_CO2",
      "temperature_C": float("nan")},
     replicates.MAPPING_STATUS_EXACT),
    ("scenario_level",
     {"sample_id": "S1", "leachant": "NaOH", "liquid_solid_ratio": 5, "CO2_condition": "PF",
      "time_min": 10},
     {"state": "batch", "liquid_solid_ratio": 5.0, "CO2_condition": "low_CO2",
      "temperature_C": float("nan")},
     replicates.MAPPING_STATUS_SCENARIO),
    ("unsafe",
     {"sample_id": "A1", "leachant": "HCl", "acid_M": 0.5, "liquid_solid_ratio": 5,
      "CO2_condition": "OA"},
     {"state": "batch", "liquid_solid_ratio": 5.0, "CO2_condition": "atm_CO2",
      "temperature_C": float("nan")},
     replicates.MAPPING_STATUS_UNSAFE),
]


@pytest.mark.parametrize("name,sample,scenario,expected_status", _TRACE_CASES,
                         ids=[c[0] for c in _TRACE_CASES])
def test_trace_shape_sum_reason_and_status(name, sample, scenario, expected_status):
    res = scenarios.score_scenario(sample, scenario)
    trace = res["trace"]
    assert trace, "every scored scenario produces at least one trace entry"
    for e in trace:
        assert _REQUIRED_TRACE_KEYS <= set(e)               # required keys present
        assert e["outcome"] in scenarios.TRACE_OUTCOMES
        assert isinstance(e["points"], int)
    # The integer score equals the sum of the trace points.
    assert res["score"] == sum(e["points"] for e in trace)
    # The reason string is assembled FROM the trace (one code path).
    assert res["reason"] == scenarios.reason_from_trace(trace)
    # The same (sample, scenario) classifies to the expected status.
    assert replicates.mapping_status(sample, scenario) == expected_status


def test_needs_new_simulation_status_without_scenario():
    sample = {"sample_id": "S1", "leachant": "NaOH", "CO2_condition": "OA"}
    assert replicates.mapping_status(sample, None) == replicates.MAPPING_STATUS_NEEDS_NEW


def test_trace_records_fuzzy_normalizations():
    res = scenarios.score_scenario(
        {"leachant": "HCl", "CO2_condition": "open", "liquid_solid_ratio": 5},
        {"state": "batch", "CO2_condition": "atm_CO2", "liquid_solid_ratio": 5.0})
    normalized = {e["field"] for e in res["trace"] if e["outcome"] == "normalized"}
    assert "leachant" in normalized        # HCl -> acid family
    assert "CO2_condition" in normalized   # 'open' -> atmospheric family grouping


def test_confidence_explanation_mentions_band_and_cap():
    capped = scenarios.score_scenario(
        {"sample_id": "S1", "leachant": "NaOH", "CO2_condition": "PF",
         "liquid_solid_ratio": 5, "time_min": 10},
        {"state": "batch", "CO2_condition": "low_CO2", "liquid_solid_ratio": 5.0})
    expl = capped["confidence_explanation"]
    assert f"of max {scenarios.MAX_SCORE}" in expl
    assert "capped to medium" in expl
    assert ("time_min" in expl or "condition_code" in expl)

    uncapped = scenarios.score_scenario(
        {"sample_id": "S1", "leachant": "NaOH", "CO2_condition": "OA", "liquid_solid_ratio": 5},
        {"state": "batch", "CO2_condition": "atm_CO2", "liquid_solid_ratio": 5.0})
    assert "capped" not in uncapped["confidence_explanation"]


def test_reason_assembled_from_trace_with_conflict():
    res = scenarios.score_scenario(
        {"sample_id": "S1", "leachant": "NaOH", "CO2_condition": "OA", "liquid_solid_ratio": 5},
        {"state": "initial", "CO2_condition": "atm_CO2", "liquid_solid_ratio": 5.0})
    assert "CONFLICT: state=initial" in res["reason"]
    assert res["reason"] == scenarios.reason_from_trace(res["trace"])
    # The major-conflict penalty is in the trace but not surfaced as a reason field.
    assert any(e["field"] == "conflict_penalty" for e in res["trace"]) is False  # no L/S or CO2 conflict here
