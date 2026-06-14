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

from flyash_phreeqc_ml import (config, mapping_table, report, run_manager, scenarios)
from flyash_phreeqc_ml.compare import compare_measured_vs_phreeqc
from flyash_phreeqc_ml.compare import inclusion as I
from flyash_phreeqc_ml import phreeqc_runner


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
