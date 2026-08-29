"""Tests for the on-demand PHREEQC runner (Prompt 11).

The actual PHREEQC execution is always mocked — no binary/network needed. Coverage:

* template generation (golden-file `.pqi` snippets + the assumptions banner);
* the OA/PF/GS → CO₂ scenario mapping;
* ingest tagging (generated / source_condition_key / generated_at + metadata);
* the generated OA→exact, PF/GS→scenario-level mapping statuses (the design check);
* error propagation (not-configured, run failure, timeout).

Plus one optional integration test that runs only when a real PHREEQC + database
are configured (skipped otherwise).
"""
from __future__ import annotations

import re
import types
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from flyash_phreeqc_ml import config, phreeqc_runner as pr, profiles, replicates, scenarios

FIXTURES = Path(__file__).parent / "fixtures"


def _oa_condition() -> dict:
    return {
        "sample_id": "CFA-NaOH0.5M-LS5-10min-OA-R1", "leachant": "NaOH",
        "NaOH_M": 0.5, "liquid_solid_ratio": 5.0, "temperature_C": 25.0,
        "time_min": 10.0, "CO2_condition": "OA", "final_pH": 13.1,
    }


# --------------------------------------------------------------------------- #
# Template generation
# --------------------------------------------------------------------------- #
def test_build_single_input_atm_golden():
    text, assumptions = pr.build_single_input(
        0.5, 5.0, 25.0, "atm_CO2", ph=13.1, time_min=10.0, label="CFA-test")
    assert text == (FIXTURES / "golden_atm_co2.pqi").read_text(encoding="utf-8")
    # Assumptions are surfaced (not buried) — stock + counter-ion always present.
    assert any("stock" in a for a in assumptions)
    assert any("Cl" in a for a in assumptions)


def test_build_single_input_low_and_none():
    low, _ = pr.build_single_input(1.0, 5.0, 25.0, "low")     # short alias
    assert "CO2(g)    -3.37   0.001" in low
    none, _ = pr.build_single_input(1.0, 5.0, 25.0, "none")
    assert "no CO2(g) phase" in none
    # No *active* CO2(g) line in the sealed scenario (only the explanatory comment).
    active = "\n".join(re.sub(r"#.*", "", ln) for ln in none.splitlines())
    assert "CO2(g)" not in active


def test_build_single_input_pH_and_temp_assumed_when_missing():
    text, assumptions = pr.build_single_input(0.5, 5.0, None, "atm_CO2", ph=None)
    assert f"pH        {config.ASSUMED_PH:.2f}" in text
    assert f"{config.ASSUMED_TEMPERATURE_C:.2f}" in text
    assert any("pH assumed" in a for a in assumptions)
    assert any("temperature assumed" in a for a in assumptions)


# --------------------------------------------------------------------------- #
# OA/PF/GS CO₂ mapping
# --------------------------------------------------------------------------- #
def test_build_input_oa_single_atm_variant():
    outs = pr.build_input(_oa_condition(), profiles.FLY_ASH_PROFILE)
    assert [o.model_label for o in outs] == ["atm_CO2"]
    assert outs[0].metadata["CO2_condition"] == "atm_CO2"
    assert outs[0].metadata["NaOH_M"] == 0.5 and outs[0].metadata["time_min"] == 10.0


@pytest.mark.parametrize("cover", ["PF", "GS"])
def test_build_input_covered_two_variants(cover):
    c = _oa_condition()
    c["sample_id"] = c["sample_id"].replace("OA", cover)
    c["CO2_condition"] = cover
    outs = pr.build_input(c, profiles.FLY_ASH_PROFILE)
    assert sorted(o.model_label for o in outs) == ["low_CO2", "no_CO2"]


def test_build_input_acid_blocked():
    c = _oa_condition()
    c.update({"leachant": "HCl", "acid_M": 1.0, "NaOH_M": None})
    assert pr.build_input(c, profiles.FLY_ASH_PROFILE) == []
    assert pr.generation_blocked_reason(c) is not None


# --------------------------------------------------------------------------- #
# Manifest tagging + the generated-scenario mapping statuses (design check)
# --------------------------------------------------------------------------- #
def _generated_results_row(co2_label="atm_CO2") -> pd.DataFrame:
    g = config.GENERATED_META_COLUMNS
    return pd.DataFrame([{
        "record_key": "gen_x.pqo|sim1|batch|sol1", "source_file": "gen_x.pqo",
        "simulation": 1, "state": "batch", "solution_number": 1, "pH": 12.9,
        "mol_Ca": 0.001, "mol_Si": 0.002, "mol_Al": 0.0001,
        config.GENERATED_FLAG_COLUMN: True,
        config.GENERATED_SOURCE_COLUMN: "NaOH0.5M_OA_10min_LS5",
        config.GENERATED_AT_COLUMN: "2026-01-01T00:00:00",
        g["NaOH_M"]: 0.5, g["liquid_solid_ratio"]: 5.0, g["CO2_condition"]: co2_label,
        g["temperature_C"]: 25.0, g["time_min"]: 10.0,
    }])


def test_manifest_marks_generated_with_exact_metadata():
    man = scenarios.build_scenario_manifest(_generated_results_row("atm_CO2"))
    row = man.iloc[0]
    assert bool(row["generated"]) is True
    assert row["source_condition_key"] == "NaOH0.5M_OA_10min_LS5"
    assert float(row["NaOH_M"]) == 0.5 and float(row["time_min"]) == 10.0
    assert row["CO2_condition"] == "atm_CO2" and float(row["liquid_solid_ratio"]) == 5.0


def test_generated_oa_reaches_exact_pf_scenario_level():
    # An OA condition mapped to a generated atm_CO2 scenario → EXACT (time + NaOH align).
    sample = _oa_condition()
    scn_atm = {"phreeqc_record_key": "k", "state": "batch", "CO2_condition": "atm_CO2",
               "NaOH_M": 0.5, "time_min": 10.0, "liquid_solid_ratio": 5.0, "temperature_C": 25.0}
    assert replicates.mapping_status(sample, scn_atm) == replicates.MAPPING_STATUS_EXACT
    # A PF condition mapped to a generated low_CO2 scenario → scenario-level only
    # (the cup-cover stays unconfirmed; Prompt-5 cap), NOT unsafe (both reduced family).
    sample_pf = {**sample, "sample_id": sample["sample_id"].replace("OA", "PF"),
                 "CO2_condition": "PF"}
    scn_low = {**scn_atm, "CO2_condition": "low_CO2"}
    assert replicates.mapping_status(sample_pf, scn_low) == replicates.MAPPING_STATUS_SCENARIO


def test_generated_scenarios_flow_to_designed_statuses_end_to_end():
    """Full path: generated results → manifest → suggestion table → mapping status.

    A generated atm_CO2 scenario lets an OA condition reach *exact*; a generated
    low_CO2 scenario lets a PF condition reach *scenario-level only* (Prompt-5 cap).
    This is the design check the prompt asks to verify.
    """
    from flyash_phreeqc_ml import mapping_table
    results = pd.concat([_generated_results_row("atm_CO2").assign(
                             record_key="gen_OA.pqo|sim1|batch|sol1", source_file="gen_OA.pqo"),
                         _generated_results_row("low_CO2").assign(
                             record_key="gen_PF.pqo|sim1|batch|sol1", source_file="gen_PF.pqo",
                             **{config.GENERATED_SOURCE_COLUMN: "NaOH0.5M_PF_10min_LS5"})],
                        ignore_index=True)
    manifest = scenarios.build_scenario_manifest(results)
    data = pd.DataFrame([
        {"sample_id": "CFA-NaOH0.5M-LS5-10min-OA-R1", "leachant": "NaOH", "NaOH_M": 0.5,
         "liquid_solid_ratio": 5.0, "temperature_C": 25.0, "time_min": 10.0,
         "CO2_condition": "OA", "final_pH": 12.9},
        {"sample_id": "CFA-NaOH0.5M-LS5-10min-PF-R1", "leachant": "NaOH", "NaOH_M": 0.5,
         "liquid_solid_ratio": 5.0, "temperature_C": 25.0, "time_min": 10.0,
         "CO2_condition": "PF", "final_pH": 12.9},
    ])
    table = mapping_table.build_suggestion_table(data, manifest, None)
    status = {ck.split("_")[1]: st for ck, st in
              zip(table["condition_key"], table["mapping_status"])}
    assert status["OA"] == replicates.MAPPING_STATUS_EXACT
    assert status["PF"] == replicates.MAPPING_STATUS_SCENARIO


def test_ingest_tags_generated_rows(monkeypatch, tmp_path):
    res = pd.DataFrame([{"record_key": "gen_x.pqo|sim1|batch|sol1", "source_file": "gen_x.pqo",
                         "state": "batch", "pH": 12.9, "mol_Ca": 0.001}])
    monkeypatch.setattr(pr, "parse_pqo_file", lambda p: ["rec"])
    monkeypatch.setattr(pr, "records_to_frames", lambda recs: (res.copy(), None, None))
    monkeypatch.setattr(pr.scenarios, "write_scenario_manifest", lambda *a, **k: None)

    results_path = tmp_path / "phreeqc_results.csv"
    keys = pr.ingest(tmp_path / "gen_x.pqo", condition_key="NaOH0.5M_OA",
                     generated_at="2026-01-01T00:00:00",
                     metadata={"NaOH_M": 0.5, "liquid_solid_ratio": 5.0,
                               "CO2_condition": "atm_CO2", "temperature_C": 25.0, "time_min": 10.0},
                     results_path=results_path)
    df = pd.read_csv(results_path)
    assert keys == ["gen_x.pqo|sim1|batch|sol1"]
    assert bool(df["generated"].iloc[0]) is True
    assert df["source_condition_key"].iloc[0] == "NaOH0.5M_OA"
    assert df["generated_at"].iloc[0] == "2026-01-01T00:00:00"
    assert float(df["gen_NaOH_M"].iloc[0]) == 0.5
    assert df["gen_CO2_condition"].iloc[0] == "atm_CO2"


def test_ingest_dedupes_and_preserves_hand_built(monkeypatch, tmp_path):
    results_path = tmp_path / "phreeqc_results.csv"
    # pre-existing hand-built row (no generated column)
    pd.DataFrame([{"record_key": "hand.pqo|sim1|batch|sol1", "state": "batch", "pH": 13.0}]
                 ).to_csv(results_path, index=False)
    res = pd.DataFrame([{"record_key": "gen.pqo|sim1|batch|sol1", "state": "batch", "pH": 12.5}])
    monkeypatch.setattr(pr, "parse_pqo_file", lambda p: ["r"])
    monkeypatch.setattr(pr, "records_to_frames", lambda recs: (res.copy(), None, None))
    monkeypatch.setattr(pr.scenarios, "write_scenario_manifest", lambda *a, **k: None)
    pr.ingest(tmp_path / "g.pqo", condition_key="c", results_path=results_path)
    df = pd.read_csv(results_path)
    assert set(df["record_key"]) == {"hand.pqo|sim1|batch|sol1", "gen.pqo|sim1|batch|sol1"}
    hand = df[df["record_key"] == "hand.pqo|sim1|batch|sol1"].iloc[0]
    assert bool(hand["generated"]) is False  # back-filled for the hand-built row


# --------------------------------------------------------------------------- #
# Error propagation
# --------------------------------------------------------------------------- #
def _fake_exe_db(tmp_path):
    exe = tmp_path / "phreeqc"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    db = tmp_path / "cemdata.dat"
    db.write_text(
        "PHASES\nCal\n\tCaCO3 = CO3-2 + Ca+2\n"
        "Portlandite\n\tCa(OH)2 = Ca+2 + 2 OH-\n")
    return str(exe), str(db)


def _confirmation(generated_input, exe, database):
    return pr.confirm_reviewed_input(
        pr.review_input(generated_input), exe=exe, database=database)


def _valid_input(basename="gen"):
    return pr.build_design_input(0.5, 5.0, 25.0, "atm_CO2", sample_id=basename)


def test_run_not_configured_without_database(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", None)
    with pytest.raises(pr.PhreeqcNotConfiguredError):
        pr.run(_valid_input(), tmp_path, database=None, exe=str(tmp_path / "phreeqc_missing"))


def test_run_failure_propagates_phreeqc_error(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    generated = _valid_input("bad")

    def fake_run(cmd, **kw):
        Path(cmd[2]).write_text("ERROR: simulation did not converge\n")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="ERROR: boom")

    monkeypatch.setattr(pr.subprocess, "run", fake_run)
    with pytest.raises(pr.PhreeqcRunError) as exc:
        pr.run(generated, tmp_path, exe=exe, database=db, basename="bad",
               confirmation=_confirmation(generated, exe, db))
    assert "did not converge" in str(exc.value)


def test_run_timeout_raises(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    generated = _valid_input("slow")

    def fake_run(cmd, **kw):
        raise pr.subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))

    monkeypatch.setattr(pr.subprocess, "run", fake_run)
    with pytest.raises(pr.PhreeqcRunError) as exc:
        pr.run(generated, tmp_path, exe=exe, database=db, timeout=1, basename="slow",
               confirmation=_confirmation(generated, exe, db))
    assert "timed out" in str(exc.value).lower()


# --------------------------------------------------------------------------- #
# Database-capability gate (pins the CEMDATA-compatibility check itself)
# --------------------------------------------------------------------------- #
def test_database_defines_phases_distinguishes_cal_from_calcite(tmp_path):
    # a standard database defines Calcite + Portlandite but NOT the CEMDATA name "Cal"
    std = tmp_path / "phreeqc.dat"
    std.write_text("PHASES\nCalcite\n\tCaCO3 = CO3-2 + Ca+2\nPortlandite\n\tCa(OH)2 = Ca+2 + 2 OH-\n")
    assert pr.database_defines_phases(["Portlandite"], database=str(std))
    assert not pr.database_defines_phases(["Cal"], database=str(std))   # "Calcite" must not match
    assert not pr.is_cemdata_compatible(database=str(std))

    # a CEMDATA-like database that DOES define Cal + Portlandite at column 0
    cem = tmp_path / "cemdata18.dat"
    cem.write_text("PHASES\nCal\n\tCaCO3 = CO3-2 + Ca+2\nPortlandite\n\tCa(OH)2 = Ca+2 + 2 OH-\n")
    assert pr.is_cemdata_compatible(database=str(cem))


def test_cemdata_compatible_false_without_a_real_file(tmp_path):
    assert not pr.is_cemdata_compatible(database="")                     # unset
    assert not pr.is_cemdata_compatible(database=str(tmp_path / "missing.dat"))


def test_arbitrary_text_with_every_old_marker_is_rejected_without_workspace(monkeypatch,
                                                                            tmp_path):
    raw = ("# Generated PHREEQC input\nSOLUTION 1\nNa 1\nSi 1 # ASSUMED stock\n"
           "Al 1\nCa 1\nEQUILIBRIUM_PHASES 1\nEND\n")
    calls = []
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(pr.PhreeqcRunError):
        pr.run(raw, tmp_path / "ws")
    with pytest.raises(Exception):
        pr.review_input(raw)
    assert calls == [] and not (tmp_path / "ws").exists()


def test_modified_builder_output_is_rejected_without_workspace(monkeypatch, tmp_path):
    generated = _valid_input("modified")
    modified = replace(
        generated,
        pqi_text=generated.pqi_text + "EQUILIBRIUM_PHASES 2\n    Gypsum 0 0\n")
    calls = []
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(Exception):
        pr.review_input(modified)
    with pytest.raises(pr.PhreeqcRunError):
        pr.run(modified, tmp_path / "ws")
    assert calls == [] and not (tmp_path / "ws").exists()


def test_real_deterministic_builder_output_is_reviewable_and_environment_bound(tmp_path):
    generated = _valid_input("valid")
    exe, database = _fake_exe_db(tmp_path)
    reviewed = pr.review_input(generated)
    confirmation = pr.confirm_reviewed_input(reviewed, exe=exe, database=database)
    assert reviewed.phreeqc_input_text == generated.pqi_text
    assert reviewed.execution_basename == generated.basename
    assert confirmation.execution_environment.database.resolved_path == str(Path(database).resolve())


def test_expected_environment_cannot_rebind_after_same_path_replacement(tmp_path):
    generated = _valid_input("expected-environment")
    exe, database = _fake_exe_db(tmp_path)
    reviewed = pr.review_input(generated)
    from flyash_phreeqc_ml.simulation import phreeqc_executor
    expected = phreeqc_executor.check_availability(
        exe=exe, database=database).environment_identity
    assert expected is not None

    Path(database).write_text(
        "PHASES\nCal\n changed reaction\nPortlandite\n changed reaction\n# replacement\n")
    with pytest.raises(pr.PhreeqcNotConfiguredError) as exc:
        pr.confirm_reviewed_input(reviewed, expected_environment=expected)
    assert "changed after review" in str(exc.value).lower()


def test_mismatched_basename_or_scenario_identity_rejected_without_workspace(monkeypatch,
                                                                             tmp_path):
    generated = _valid_input("expected")
    exe, database = _fake_exe_db(tmp_path)
    confirmation = _confirmation(generated, exe, database)
    calls = []
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(pr.PhreeqcRunError):
        pr.run(generated, tmp_path / "ws", basename="different", exe=exe,
               database=database, confirmation=confirmation)
    from flyash_phreeqc_ml.simulation import phreeqc_run_contract as contract
    with pytest.raises(contract.RunContractError):
        contract.generated_text_preview(generated, scenario_id="different-scenario")
    with pytest.raises(contract.RunContractError):
        contract.generated_text_preview(replace(generated, scenario_id="different-scenario"))
    assert calls == [] and not (tmp_path / "ws").exists()


def test_incompatible_legacy_database_blocks_before_workspace_or_subprocess(monkeypatch,
                                                                            tmp_path):
    generated = _valid_input("incompatible")
    exe, _ = _fake_exe_db(tmp_path)
    database = tmp_path / "phreeqc.dat"
    database.write_text(
        "PHASES\nCalcite\n\tCaCO3 = CO3-2 + Ca+2\n"
        "Portlandite\n\tCa(OH)2 = Ca+2 + 2 OH-\n")
    calls = []
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(pr.PhreeqcNotConfiguredError) as exc:
        pr.confirm_reviewed_input(
            pr.review_input(generated), exe=exe, database=str(database))
    assert "cemdata" in str(exc.value).lower()
    assert calls == [] and not (tmp_path / "ws").exists()


def test_run_uses_exact_verified_absolute_paths_not_workdir_decoys(monkeypatch, tmp_path):
    configured_dir = tmp_path / "configured"
    configured_dir.mkdir()
    exe, database = _fake_exe_db(configured_dir)
    workdir = tmp_path / "different-cwd"
    workdir.mkdir()
    (workdir / "phreeqc").write_text("decoy executable")
    (workdir / "cemdata.dat").write_text("decoy database")
    monkeypatch.chdir(configured_dir)

    generated = _valid_input("resolved-paths")
    reviewed = pr.review_input(generated)
    confirmation = pr.confirm_reviewed_input(
        reviewed, exe="phreeqc", database="cemdata.dat")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        Path(cmd[2]).write_text("successful mocked PHREEQC output\n")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pr.subprocess, "run", fake_run)
    out = pr.run(
        generated, workdir, exe="phreeqc", database="cemdata.dat",
        confirmation=confirmation)

    assert out.exists() and len(calls) == 1
    command, kwargs = calls[0]
    assert command[0] == str(Path(exe).resolve())
    assert command[3] == str(Path(database).resolve())
    assert Path(command[0]).is_absolute() and Path(command[3]).is_absolute()
    assert command[0] != str(workdir / "phreeqc")
    assert command[3] != str(workdir / "cemdata.dat")
    assert kwargs["cwd"] == str(workdir)


# --------------------------------------------------------------------------- #
# Optional integration test (real PHREEQC) — runs only with a CEMDATA18-compatible
# database, because the generated input uses CEMDATA phase names (e.g. ``Cal``).
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not (pr.is_configured() and pr.is_cemdata_compatible()),
                    reason="no real PHREEQC binary + CEMDATA18-compatible PHREEQC_DATABASE "
                           "configured (the runner's input needs CEMDATA phases like 'Cal')")
def test_integration_real_phreeqc(tmp_path):  # pragma: no cover - env-dependent
    generated = pr.build_design_input(0.5, 5.0, 25.0, "atm_CO2", sample_id="it")
    reviewed = pr.review_input(generated)
    confirmation = pr.confirm_reviewed_input(reviewed)
    out = pr.run(generated, tmp_path, basename="it", confirmation=confirmation)
    keys = pr.ingest(out, condition_key="it_condition",
                     results_path=tmp_path / "results.csv",
                     metadata={"NaOH_M": 0.5, "liquid_solid_ratio": 5.0,
                               "CO2_condition": "atm_CO2", "temperature_C": 25.0})
    assert keys


def test_configured_runner_requires_reviewed_confirmation(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    generated = _valid_input()
    calls = []
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(pr.PhreeqcRunError) as exc:
        pr.run(generated, tmp_path / "ws", exe=exe, database=db)
    assert "confirmation" in str(exc.value).lower()
    assert calls == []
    assert not (tmp_path / "ws").exists()
