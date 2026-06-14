"""Named fixtures for the supported-dataset matrix.

Each fixture is one *supported* dataset shape. The matrix test modules
(``test_a_*`` … ``test_f_*``) consume these and each pin a single generality claim;
together they define what "supported" means — the README must not claim more than the
matrix proves. All synthetic; no network.
"""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml import config, profiles, run_manager, scenarios
from flyash_phreeqc_ml.parsers import generic_prediction_parser as gpp


# --------------------------------------------------------------------------- #
# Shared synthetic builders
# --------------------------------------------------------------------------- #
def _phreeqc_results() -> pd.DataFrame:
    """Two batch PHREEQC scenarios: atmospheric (OA-matching) + reduced."""
    return pd.DataFrame([
        {"record_key": "f|sim1|batch|sol1", "source_file": "L-S_5_atmCO2.pqo",
         "simulation": 1, "state": "batch", "solution_number": 1, "pH": 12.9,
         "mol_Ca": 0.002, "mol_Si": 0.001, "mol_Al": 0.0005, "temperature_c": 25},
        {"record_key": "f|sim1|batch|sol2", "source_file": "L-S_5_lowCO2.pqo",
         "simulation": 1, "state": "batch", "solution_number": 2, "pH": 12.7,
         "mol_Ca": 0.0015, "mol_Si": 0.0008, "mol_Al": 0.0004, "temperature_c": 25},
    ])


def _flyash_measured() -> pd.DataFrame:
    """Fly-ash measured rows spanning all four mapping statuses (like the e2e lock)."""
    rows = []
    for i, ph in enumerate([13.5, 13.6, 13.4], start=1):     # exact (OA, no time)
        rows.append({"sample_id": f"EXACT-R{i}", "fly_ash_type": "Class C fly ash",
                     "leachant": "NaOH", "NaOH_M": "", "acid_M": "", "CO2_condition": "OA",
                     "liquid_solid_ratio": 5, "temperature_C": "", "final_pH": ph})
    for i in range(1, 4):                                    # scenario-level (time_min=10)
        rows.append({"sample_id": f"SCN-R{i}", "fly_ash_type": "Class C fly ash",
                     "leachant": "NaOH", "NaOH_M": "", "acid_M": "", "CO2_condition": "OA",
                     "liquid_solid_ratio": 5, "temperature_C": "", "time_min": 10,
                     "final_pH": 13.0})
    for i in range(1, 4):                                    # unsafe (acid)
        rows.append({"sample_id": f"ACID-R{i}", "fly_ash_type": "Class C fly ash",
                     "leachant": "HCl", "NaOH_M": "", "acid_M": 0.5, "CO2_condition": "OA",
                     "liquid_solid_ratio": 5, "temperature_C": "", "final_pH": 3.2})
    return pd.DataFrame(rows)


def _flyash_measured_oa() -> pd.DataFrame:
    """One OA condition, 3 replicates (exact-eligible) with pH + Ca."""
    return pd.DataFrame([
        {"sample_id": f"S{i}", "fly_ash_type": "Class C fly ash", "leachant": "NaOH",
         "NaOH_M": "", "acid_M": "", "CO2_condition": "OA", "liquid_solid_ratio": 5,
         "final_pH": 13.4, "Ca_mM": 2.1} for i in range(3)
    ])


# Alternate (non-fly-ash) dataset + model profiles from the generalization layer.
WET_DRY_PROFILE = profiles.DatasetProfile(
    name="soil moisture demo", time_column="day", condition_column="treatment",
    condition_codes={"WET": {"description": "watered", "caution": ""},
                     "DRY": {"description": "droughted", "caution": ""}},
    variable_columns=("yield_g",), overview_variables=("yield_g",),
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


# --------------------------------------------------------------------------- #
# (a) fly-ash + PHREEQC
# --------------------------------------------------------------------------- #
@pytest.fixture()
def flyash_phreeqc_dataset():
    pheq = _phreeqc_results()
    return {"measured": _flyash_measured(), "phreeqc_results": pheq,
            "manifest": scenarios.build_scenario_manifest(pheq),
            "atm_key": "f|sim1|batch|sol1"}


# --------------------------------------------------------------------------- #
# (b) literature-style fly-ash dataset (separation guarantees)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def literature_runs(tmp_path, monkeypatch):
    """A temp runs root + a literature_benchmark run with one reported row."""
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    run_manager.create_run("lit_run", "literature_benchmark")
    run_manager.create_run("lab_run", "lab_experiment")
    lit_row = {"source_id": "paper2020", "paper_title": "Fly ash leaching",
               "fly_ash_class": "Class C", "leachant": "NaOH", "NaOH_M": 1.0,
               "reported_final_pH": 13.1, "reported_Ca_mM": 2.0}
    run_manager.append_literature_row("lit_run", lit_row)
    return {"lit": "lit_run", "lab": "lab_run", "row": lit_row}


# --------------------------------------------------------------------------- #
# (c) hand-computed residuals
# --------------------------------------------------------------------------- #
@pytest.fixture()
def hand_residual_dataset():
    # Measured vs a model with exactly-known predictions -> residuals are hand-checkable.
    preds = gpp.parse_predictions(pd.DataFrame([
        {"record_key": "K1", "model_name": "HandModel", "pred_pH": 12.5,
         "pred_Ca_mM": 2.0, "leachant": "NaOH", "liquid_solid_ratio": 5,
         "CO2_condition": "OA"}]))
    measured = pd.DataFrame([
        {"sample_id": "H1", "leachant": "NaOH", "NaOH_M": "", "acid_M": "",
         "CO2_condition": "OA", "liquid_solid_ratio": 5, "final_pH": 13.0, "Ca_mM": 2.6}])
    mapping = pd.DataFrame([{"sample_id": "H1", "phreeqc_record_key": "K1"}])
    return {"measured": measured, "mapping": mapping,
            "manifest": scenarios.build_scenario_manifest(preds),
            "expected_residual_pH": 13.0 - 12.5, "expected_residual_Ca": 2.6 - 2.0}


# --------------------------------------------------------------------------- #
# (d) differently-formatted import
# --------------------------------------------------------------------------- #
@pytest.fixture()
def reformatted_upload():
    """A raw upload with reordered/renamed columns and Ca reported in mg/L."""
    raw = pd.DataFrame({
        "Calcium": [100.0, 200.0],          # mg/L, name != Ca_mM
        "pH": [13.0, 12.8],                 # final pH under a different header
        "Sample": ["A1", "A2"],             # sample id under a different header
    })
    return {"raw": raw, "units": {"Ca_mM": "mg/L"}}


# --------------------------------------------------------------------------- #
# (e) alternate non-fly-ash profile
# --------------------------------------------------------------------------- #
@pytest.fixture()
def alternate_profile_dataset():
    return {"profile": WET_DRY_PROFILE, "data": _alt_data(), "manifest": _ALT_MANIFEST}


# --------------------------------------------------------------------------- #
# (f) non-PHREEQC generic prediction CSV
# --------------------------------------------------------------------------- #
@pytest.fixture()
def generic_prediction_dataset():
    csv = pd.DataFrame([
        {"record_key": "M1", "model_name": "ToyThermo", "pred_pH": 12.8,
         "pred_Ca_mM": 1.9, "leachant": "NaOH", "liquid_solid_ratio": 5,
         "CO2_condition": "OA"}])
    parsed = gpp.parse_predictions(csv)
    measured = _flyash_measured_oa()
    mapping = pd.DataFrame([{"sample_id": s, "phreeqc_record_key": "M1"}
                            for s in measured["sample_id"]])
    return {"csv": csv, "parsed": parsed, "measured": measured, "mapping": mapping,
            "manifest": scenarios.build_scenario_manifest(parsed)}


# --------------------------------------------------------------------------- #
# (g) second material (red mud) — batch closure + attribution + recovery
# --------------------------------------------------------------------------- #
def _red_mud_batch() -> pd.DataFrame:
    """One red-mud batch row, all four mass-balance elements complete.

    Ti is the hand-checked element: 10 g material x 5 wt% -> 500 mg / M_Ti = n_in;
    30 mM x 100 mL -> n_liquid; 8 g x 3 wt% -> 240 mg / M_Ti = n_solid.
    """
    return pd.DataFrame([{
        "sample_id": "RM-1", "reagent": "NaOH", "reagent_conc_M": 2.0,
        "liquid_solid_ratio": 10, "material_mass_g": 10.0, "liquid_volume_mL": 100.0,
        "solid_mass_g": 8.0,
        "Ti_starting_content": 5.0, "Ti_solid_residue": 3.0, "Ti_mM": 30.0,
        "V_starting_content": 1.0, "V_solid_residue": 0.5, "V_mM": 4.0,
        "Fe_starting_content": 30.0, "Fe_solid_residue": 28.0, "Fe_mM": 2.0,
        "Al_starting_content": 10.0, "Al_solid_residue": 9.0, "Al_mM": 3.0,
    }])


@pytest.fixture()
def red_mud_batch_dataset():
    return {"profile": profiles.RED_MUD_PROFILE, "material": profiles.RED_MUD_MATERIAL,
            "data": _red_mud_batch()}
