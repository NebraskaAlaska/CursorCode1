"""Tests for the one-click validation report (report.py).

Pins (per the spec): the report builds on a synthetic e2e-style run; the MANIFEST
hashes verify; a STALE header appears when the comparison is stale; the
unsafe/preliminary banner wording matches the Prompt-4 validity rules; and
needed_simulations.csv columns match the Prompt-11 condition fields. Synthetic only.
"""
from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from flyash_phreeqc_ml import (attribution, config, mapping_table, profiles, report,
                               run_manager, scenarios, units)
from flyash_phreeqc_ml.compare import compare_measured_vs_phreeqc
from flyash_phreeqc_ml.compare import inclusion as I
from flyash_phreeqc_ml import phreeqc_runner
from flyash_phreeqc_ml.ai import literature


def _phreeqc() -> pd.DataFrame:
    return pd.DataFrame([
        {"record_key": "f|sim1|batch|sol1", "source_file": "L-S_5_atmCO2.pqo",
         "simulation": 1, "state": "batch", "solution_number": 1, "pH": 12.9,
         "mol_Ca": 0.002, "mol_Si": 0.001, "mol_Al": 0.0005, "temperature_c": 25},
    ])


def _measured() -> pd.DataFrame:
    rows = []
    for i, ph in enumerate([13.5, 13.6, 13.4], start=1):     # exact (no time)
        rows.append({"sample_id": f"SYNTH-EXACT-R{i}", "fly_ash_type": "Class C fly ash",
                     "leachant": "NaOH", "NaOH_M": "", "acid_M": "", "CO2_condition": "OA",
                     "liquid_solid_ratio": 5, "temperature_C": 25, "final_pH": ph})
    for i in range(1, 4):                                    # scenario-level (time_min=10)
        rows.append({"sample_id": f"SYNTH-SCN-R{i}", "fly_ash_type": "Class C fly ash",
                     "leachant": "NaOH", "NaOH_M": "", "acid_M": "", "CO2_condition": "OA",
                     "liquid_solid_ratio": 5, "temperature_C": 25, "time_min": 10,
                     "final_pH": 13.0})
    for i in range(1, 4):                                    # unmapped -> needs new sim
        rows.append({"sample_id": f"SYNTH-NEW-R{i}", "fly_ash_type": "Class C fly ash",
                     "leachant": "NaOH", "NaOH_M": "", "acid_M": "", "CO2_condition": "OA",
                     "liquid_solid_ratio": 5, "temperature_C": 25, "time_min": 20,
                     "final_pH": 12.5})
    return pd.DataFrame(rows)


@pytest.fixture()
def built(tmp_path, monkeypatch):
    """A lab run with exact + scenario-level mappings (→ preliminary), then a report."""
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    proc = tmp_path / "processed"
    proc.mkdir()
    monkeypatch.setattr(config, "PROCESSED_DIR", proc)
    pheq = _phreeqc()
    pheq.to_csv(proc / config.PHREEQC_RESULTS_CSV, index=False)
    manifest = scenarios.build_scenario_manifest(pheq)

    run = "rep_run"
    run_manager.create_run(run, "lab_experiment")
    run_manager.save_lab_dataframe(run, _measured(), mode="replace")
    data = run_manager.read_data_file(run)

    table = mapping_table.build_suggestion_table(data, manifest, None)
    for _, r in table.iterrows():
        if r["mapping_status"] in mapping_table.SELECTABLE_STATUSES:  # exact + scenario-level
            run_manager.add_condition_mapping(run, r["condition_key"], r["phreeqc_record_key"],
                                              mapping_status=r["mapping_status"])
    run_manager.apply_condition_mapping(run)
    sm = run_manager.read_mapping(run)
    comp = compare_measured_vs_phreeqc(data, pheq, mapping=sm)
    cp = run_manager.comparison_path(run)
    cp.parent.mkdir(parents=True, exist_ok=True)
    comp.to_csv(cp, index=False)
    run_manager.write_comparison_meta(run)

    out = report.build_report(run)
    return {"run": run, "out": out}


# --------------------------------------------------------------------------- #
# Builds + structure
# --------------------------------------------------------------------------- #
def test_report_builds_full_bundle(built):
    out = built["out"]
    assert out.is_dir() and out.name.startswith(report.REPORT_DIR_PREFIX)
    for name in ("report.html", "MANIFEST.json", "measured_clean.csv",
                 "model_predictions_used.csv", "mapping_table.csv", "residuals.csv",
                 "excluded_rows.csv", "needed_simulations.csv", "audit_log.jsonl"):
        assert (out / name).exists(), f"missing {name}"
    # At least one figure PNG was embedded + written.
    assert any(p.suffix == ".png" for p in out.iterdir())


def test_html_is_self_contained(built):
    html = (built["out"] / "report.html").read_text(encoding="utf-8")
    assert "<style>" in html                      # inline CSS
    assert "data:image/png;base64," in html       # embedded images, no external refs
    assert "validity:" in html


# --------------------------------------------------------------------------- #
# MANIFEST hashes verify
# --------------------------------------------------------------------------- #
def test_manifest_hashes_verify(built):
    out = built["out"]
    man = json.loads((out / "MANIFEST.json").read_text())
    assert man["app_version"] and man["generated_at"]
    assert man["files"]
    for entry in man["files"]:
        p = out / entry["file"]
        assert p.exists()
        assert hashlib.sha256(p.read_bytes()).hexdigest() == entry["sha256"]
        assert p.stat().st_size == entry["size"]
    # The manifest never lists itself.
    assert all(e["file"] != report.MANIFEST_FILENAME for e in man["files"])


# --------------------------------------------------------------------------- #
# Validity banner wording == Prompt-4 rules
# --------------------------------------------------------------------------- #
def test_preliminary_banner_matches_validity_rules(built):
    html = (built["out"] / "report.html").read_text(encoding="utf-8")
    man = json.loads((built["out"] / "MANIFEST.json").read_text())
    # exact + scenario-level plotted together -> preliminary (Prompt-4 rule).
    assert man["overall_validity"] == I.VALIDITY_PRELIMINARY
    assert "validity: preliminary" in html
    assert report.NOT_VALIDATED_BANNER.format(status="preliminary") in html


def test_banner_wording_constant_for_each_status():
    # The standing banner is parameterised by the validity status verbatim.
    for status in (I.VALIDITY_PRELIMINARY, I.VALIDITY_UNSAFE, I.VALIDITY_NEEDS_NEW):
        msg = report.NOT_VALIDATED_BANNER.format(status=status)
        assert msg == (f"This comparison is {status} — it is a workflow check, "
                       "not model validation.")


def test_valid_case_has_no_not_validated_banner(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    proc = tmp_path / "processed"
    proc.mkdir()
    monkeypatch.setattr(config, "PROCESSED_DIR", proc)
    pheq = _phreeqc()
    pheq.to_csv(proc / config.PHREEQC_RESULTS_CSV, index=False)
    manifest = scenarios.build_scenario_manifest(pheq)
    run = "valid_run"
    run_manager.create_run(run, "lab_experiment")
    # Only the exact condition (3 rows) -> all plotted exact -> valid.
    exact = _measured()
    exact = exact[exact["sample_id"].str.contains("EXACT")].reset_index(drop=True)
    run_manager.save_lab_dataframe(run, exact, mode="replace")
    data = run_manager.read_data_file(run)
    table = mapping_table.build_suggestion_table(data, manifest, None)
    for _, r in mapping_table.exact_suggestions(table).iterrows():
        run_manager.add_condition_mapping(run, r["condition_key"], r["phreeqc_record_key"])
    run_manager.apply_condition_mapping(run)
    comp = compare_measured_vs_phreeqc(data, pheq, mapping=run_manager.read_mapping(run))
    cp = run_manager.comparison_path(run)
    cp.parent.mkdir(parents=True, exist_ok=True)
    comp.to_csv(cp, index=False)
    run_manager.write_comparison_meta(run)

    out = report.build_report(run)
    man = json.loads((out / "MANIFEST.json").read_text())
    html = (out / "report.html").read_text(encoding="utf-8")
    assert man["overall_validity"] == I.VALIDITY_VALID
    assert report.NOT_VALIDATED_BANNER.format(status="valid") not in html
    assert "All comparable variables are valid" in html


# --------------------------------------------------------------------------- #
# STALE header when the comparison no longer matches its inputs
# --------------------------------------------------------------------------- #
def test_stale_header_when_comparison_outdated(built):
    run = built["run"]
    # Mutate the run data after the comparison was generated -> stale.
    run_manager.append_lab_row(run, {"sample_id": "SYNTH-EXTRA-R1", "final_pH": "13.0",
                                     "leachant": "NaOH", "CO2_condition": "OA",
                                     "liquid_solid_ratio": 5})
    current, _reasons = run_manager.comparison_is_current(run)
    assert current is False
    out = report.build_report(run)
    html = (out / "report.html").read_text(encoding="utf-8")
    man = json.loads((out / "MANIFEST.json").read_text())
    assert man["stale"] is True
    assert "STALE" in html
    assert "no longer matches" in html


# --------------------------------------------------------------------------- #
# needed_simulations.csv interoperates with Prompt-11 build_input
# --------------------------------------------------------------------------- #
def test_needed_simulations_columns_match_prompt11_fields(built):
    needed = pd.read_csv(built["out"] / "needed_simulations.csv")
    assert list(needed.columns) == report.NEEDED_SIM_COLUMNS
    # The unmapped NEW condition must be listed as needing a simulation.
    assert needed["condition_key"].astype(str).str.contains("20min").any()

    # The CSV columns feed phreeqc_runner.build_input verbatim: the runner reads
    # `concentration` (or NaOH_M), liquid_solid_ratio, temperature_C, time_min,
    # CO2_condition. (The synthetic rows have no molarity, so build_input correctly
    # refuses them — we give one a concentration to prove the field plumbing.)
    row = needed.iloc[0].to_dict()
    condition = {
        "leachant": row["leachant"], "concentration": 0.5,
        "liquid_solid_ratio": row["liquid_solid_ratio"],
        "temperature_C": row["temperature_C"], "time_min": row["time_min"],
        "CO2_condition": row["CO2_condition"], "final_pH": 13.0,
    }
    inputs = phreeqc_runner.build_input(condition)
    assert inputs  # a non-acid condition with a molarity templates at least one .pqi
    assert inputs[0].metadata["liquid_solid_ratio"] == pytest.approx(5.0)

    # And a row with no molarity is refused for the right, explicit reason (not a crash).
    assert "No NaOH molarity" in phreeqc_runner.generation_blocked_reason(
        {"leachant": "NaOH", "concentration": "", "liquid_solid_ratio": 5})


# --------------------------------------------------------------------------- #
# Element recovery (Prompt 25)
# --------------------------------------------------------------------------- #
M_CA = units.MOLAR_MASSES["Ca"]

# A profile that opts into the batch-reaction mass balance + a calcite candidate phase.
BATCH_PROFILE = profiles.DatasetProfile(
    name="batch recovery", grouping="fly_ash",
    mass_balance_elements=("Ca",), starting_content_unit="wt%", solid_residue_unit="wt%",
    mass_balance_candidate_phases={"Calcite": "Ca"}, precipitate_in_measured_solid=False,
    overview_variables=("final_pH",),
    comparison_variable_spec={"final_pH": ("final_pH", "phreeqc_pH")},
    important_fields=("leachant", "NaOH_M", "acid_M", "CO2_condition", "time_min",
                      "liquid_solid_ratio", "temperature_C"))


def _batch_row(naoh, ca_mM, *, ca_start="2.0"):
    """One batch condition row. material 5g, vol 50mL, solid 4g, residue 0.4 wt%."""
    return {"sample_id": f"BATCH-{naoh}", "fly_ash_type": "Class C fly ash",
            "leachant": "NaOH", "NaOH_M": naoh, "acid_M": "", "CO2_condition": "OA",
            "liquid_solid_ratio": 5, "temperature_C": 25, "final_pH": 13.0,
            "material_mass_g": 5.0, "liquid_volume_mL": 50.0, "solid_mass_g": 4.0,
            "Ca_starting_content": ca_start, "Ca_solid_residue": 0.4, "Ca_mM": ca_mM}


def _batch_data():
    # closed: liquid+solid ≈ n_in (gap fraction < 5%);  unexplained: large open gap;
    # literature: NO measured Ca starting assay (filled by a confirmed stand-in).
    return pd.DataFrame([
        _batch_row("0.5", 40.0),                       # gap ≈ 0.096 mmol → closed
        _batch_row("1.0", 20.0),                       # gap ≈ 1.096 mmol → unexplained
        _batch_row("1.5", 20.0, ca_start=""),          # literature stand-in fills the assay
    ])


def _confirm_literature_assay(run):
    """Save + confirm a sourced Ca starting-assay stand-in for the run."""
    cand = literature._candidate_from_dict({
        "value": 2.0, "unit": "wt%", "quantity": "typical Ca content of Class C fly ash",
        "material": "Class C fly ash", "element": "Ca",
        "conditions": {}, "confidence": 0.8,
        "conditions_match": {"matches": True, "mismatch_flags": []},
        "citation": {"doi": "10.1016/j.flyash.2019.05", "title": "Fly ash bulk assays",
                     "year": 2019, "supporting_quote": "Class C fly ash contains ~20 wt% CaO."},
    }, kind="starting_assay")
    literature.save_candidates(run, [cand])
    literature.confirm_value(run, cand.candidate_id)
    return "https://doi.org/10.1016/j.flyash.2019.05"


@pytest.fixture()
def recovery(tmp_path, monkeypatch):
    """A batch run + a confirmed literature assay → a report built with BATCH_PROFILE."""
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path / "experiments")
    proc = tmp_path / "processed"
    proc.mkdir()
    monkeypatch.setattr(config, "PROCESSED_DIR", proc)        # empty → no manifest
    run = "recovery_run"
    run_manager.create_run(run, "lab_experiment")
    run_manager.save_lab_dataframe(run, _batch_data(), mode="replace")
    doi_link = _confirm_literature_assay(run)
    out = report.build_report(run, profile=BATCH_PROFILE)
    return {"run": run, "out": out, "doi_link": doi_link,
            "data": run_manager.read_data_file(run)}


def test_recovery_csv_has_every_term_and_provenance(recovery):
    csv = recovery["out"] / "element_recovery.csv"
    assert csv.exists()
    df = pd.read_csv(csv)
    assert list(df.columns) == report.RECOVERY_CSV_COLUMNS
    # Each closure is Ca; provenance flags carry both measured and literature-confirmed.
    provs = set(df["starting_provenance"])
    assert report.CLASS_MEASURED in provs
    assert report.CLASS_LITERATURE in provs


def test_recovery_hits_closed_and_unexplained_offline(recovery):
    df = pd.read_csv(recovery["out"] / "element_recovery.csv")
    statuses = set(df["recovery_status"])
    # Offline (no live PHREEQC) the gap is either within uncertainty (closed) or open
    # (unexplained) — both must appear from the synthetic dataset.
    assert attribution.STATUS_CLOSED in statuses
    assert attribution.STATUS_UNEXPLAINED in statuses


def test_recovery_literature_value_shows_its_doi_link(recovery):
    df = pd.read_csv(recovery["out"] / "element_recovery.csv")
    lit_rows = df[df["starting_provenance"] == report.CLASS_LITERATURE]
    assert not lit_rows.empty
    assert (lit_rows["starting_citation"] == recovery["doi_link"]).all()
    # The clickable DOI/link is rendered in the HTML too.
    html = (recovery["out"] / "report.html").read_text(encoding="utf-8")
    assert recovery["doi_link"] in html
    assert "literature-confirmed stand-in" in html  # narrative provenance phrase


def test_recovery_narrative_and_section_render(recovery):
    html = (recovery["out"] / "report.html").read_text(encoding="utf-8")
    assert "Element recovery" in html
    assert "initially present" in html                 # the generated narrative line
    assert "measured assay" in html                    # a measured-provenance starting amount


def test_manifest_recovery_classification_tags_each_term(recovery):
    man = json.loads((recovery["out"] / "MANIFEST.json").read_text())
    cls = man["recovery_classification"]
    assert cls["n_liquid_mmol"] == report.CLASS_MEASURED
    assert cls["gap_mmol"] == report.CLASS_DERIVED
    assert cls["gap_explained_mmol"] == report.CLASS_MODELED
    assert cls["starting_citation"] == report.CLASS_LITERATURE
    # n_in is measured OR literature-confirmed per row (both categories named).
    assert report.CLASS_MEASURED in cls["n_in_mmol"]
    assert report.CLASS_LITERATURE in cls["n_in_mmol"]


def test_recovery_reaches_model_explained_and_partial_with_attribution(recovery):
    """With a (mocked) PHREEQC selected output, attribution moves the status off
    'unexplained' — model-explained when ~all the gap precipitates, partial otherwise."""
    data = recovery["data"]
    # The unexplained condition's key + its measured gap (mmol).
    base = report._recovery_records(data, BATCH_PROFILE, recovery["run"])
    unexpl = next(r for r in base if r["recovery_status"] == attribution.STATUS_UNEXPLAINED
                  and r["closure_status"] == "complete")
    ck, gap = unexpl["condition_key"], unexpl["gap"]
    calcite = phreeqc_runner.phase_moles_column("Calcite")

    explained = report._recovery_records(
        data, BATCH_PROFILE, recovery["run"],
        selected_outputs={ck: {calcite: gap / 1000.0}})        # ~all the gap → mol
    r_exp = next(r for r in explained if r["condition_key"] == ck)
    assert r_exp["recovery_status"] == attribution.STATUS_MODEL_EXPLAINED
    assert r_exp["by_phase"]["Calcite"] == pytest.approx(gap)

    partial = report._recovery_records(
        data, BATCH_PROFILE, recovery["run"],
        selected_outputs={ck: {calcite: (gap * 0.4) / 1000.0}})
    r_part = next(r for r in partial if r["condition_key"] == ck)
    assert r_part["recovery_status"] == attribution.STATUS_PARTIAL


def test_recovery_summary_sorted_by_unexplained_fraction(recovery):
    recs = report._recovery_records(recovery["data"], BATCH_PROFILE, recovery["run"])
    summ = report._recovery_summary(recs)
    assert list(summ.columns) == report.RECOVERY_SUMMARY_COLUMNS
    fracs = pd.to_numeric(summ["unexplained_fraction"], errors="coerce").dropna().tolist()
    assert fracs == sorted(fracs, reverse=True)         # weakest knowledge first


def test_recovery_surfaces_filtration_uncertain():
    """An uncertain element is credited 0 toward the gap but loudly flagged in the report."""
    from flyash_phreeqc_ml import phreeqc_runner as pr
    profile = profiles.DatasetProfile(
        name="batch-uncertain", grouping="fly_ash", mass_balance_elements=("Si",),
        starting_content_unit="wt%", solid_residue_unit="wt%",
        mass_balance_candidate_phases={"SiO2(am)": "Si"},
        precipitate_in_measured_solid=True,
        precipitate_in_measured_solid_overrides={"Si": profiles.PRECIP_UNCERTAIN})
    data = pd.DataFrame([{"sample_id": "U1", "leachant": "NaOH", "NaOH_M": "1.0",
                          "CO2_condition": "OA", "liquid_solid_ratio": 5,
                          "material_mass_g": 5.0, "liquid_volume_mL": 50.0, "solid_mass_g": 4.0,
                          "Si_starting_content": 1.0, "Si_solid_residue": 0.1, "Si_mM": 5.0}])
    from flyash_phreeqc_ml import replicates
    ck = replicates.condition_key(data.iloc[0].to_dict(), profile)
    sel = {pr.phase_moles_column("SiO2(am)"): 5e-4}     # 0.5 mmol Si precipitated
    recs = report._recovery_records(data, profile, selected_outputs={ck: sel})
    si = next(r for r in recs if r["element"] == "Si")
    assert si["filtration_uncertain"] is True
    assert si["filtration_status"] == "uncertain"
    assert si["gap_explained"] == pytest.approx(0.0)              # conservatively 0
    assert si["gap_explained_if_passes"] == pytest.approx(0.5)    # could explain 0.5 if it passes
    # The CSV carries the filtration_status column; the narrative states the uncertainty.
    table = report._recovery_table(recs)
    assert "filtration_status" in table.columns
    assert (table["filtration_status"] == "uncertain").any()
    assert "uncertain" in si["narrative"].lower()
    assert "ultrafiltrate" in si["narrative"].lower()
