"""Tests for the Experiment Run Manager (app-level "save files").

These cover the contract the Streamlit save/open layer relies on:
* run names are turned into safe folder names,
* creating a run writes a round-trippable run_config.yaml + data/ + outputs/,
* lab and literature runs write their own CSV with the right columns, and
* literature data can never be written into a lab run's experimental_release.csv
  (the guardrail that keeps measured and literature data separate).
"""
from __future__ import annotations

import pandas as pd
import pytest

from flyash_phreeqc_ml import config, run_manager


@pytest.fixture()
def runs_root(tmp_path, monkeypatch):
    """Point the run manager at a throwaway runs directory."""
    root = tmp_path / "experiments"
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", root)
    return root


# --------------------------------------------------------------------------- #
# Safe naming
# --------------------------------------------------------------------------- #
def test_safe_run_name_slugifies():
    assert run_manager.safe_run_name("2026-06-03 pH-only lab data") == "2026_06_03_ph_only_lab_data"
    assert run_manager.safe_run_name("  Literature Benchmark!! ") == "literature_benchmark"
    assert run_manager.safe_run_name("plastic/fly ash") == "plastic_fly_ash"


def test_safe_run_name_rejects_empty():
    with pytest.raises(run_manager.RunManagerError):
        run_manager.safe_run_name("   ***   ")


# --------------------------------------------------------------------------- #
# Create run + config round-trip
# --------------------------------------------------------------------------- #
def test_create_run_writes_config_and_dirs(runs_root):
    run_manager.create_run(
        "Demo Run", "lab_experiment",
        description="pH-only baseline", notes="bench 1",
        created_at="2026-06-03T09:00:00",
    )
    safe = "demo_run"
    assert (runs_root / safe / "run_config.yaml").exists()
    assert (runs_root / safe / "data").is_dir()
    assert (runs_root / safe / "outputs").is_dir()

    cfg = run_manager.load_run_config(safe)
    assert cfg["run_name"] == "Demo Run"
    assert cfg["run_type"] == "lab_experiment"
    assert cfg["data_source"] == "experimental"
    assert cfg["created_at"] == "2026-06-03T09:00:00"
    assert cfg["description"] == "pH-only baseline"
    assert cfg["notes"] == "bench 1"


def test_config_yaml_round_trips_tricky_text(runs_root):
    # Colons, quotes and commas in free text must survive the tiny YAML writer.
    tricky = 'NaOH 4M: "high pH", see ref [1]'
    run_manager.create_run("r1", "synthetic_demo", description=tricky,
                           created_at="2026-06-03T00:00:00")
    cfg = run_manager.load_run_config("r1")
    assert cfg["description"] == tricky


def test_create_run_rejects_unknown_type(runs_root):
    with pytest.raises(run_manager.RunManagerError):
        run_manager.create_run("x", "not_a_type", created_at="t")


def test_create_run_duplicate_raises(runs_root):
    run_manager.create_run("dup", "lab_experiment", created_at="t")
    with pytest.raises(run_manager.RunManagerError):
        run_manager.create_run("dup", "lab_experiment", created_at="t")


def test_list_runs_sorted(runs_root):
    run_manager.create_run("bravo", "lab_experiment", created_at="t")
    run_manager.create_run("alpha", "synthetic_demo", created_at="t")
    assert run_manager.list_runs() == ["alpha", "bravo"]


# --------------------------------------------------------------------------- #
# Lab CSV
# --------------------------------------------------------------------------- #
def test_lab_run_csv_has_release_columns(runs_root):
    run_manager.create_run("lab1", "lab_experiment", created_at="t")
    # pH-only row: chemistry blank is allowed.
    run_manager.append_lab_row("lab1", {
        "sample_id": "S1", "final_pH": "12.5", "NaOH_M": "4",
    })
    path = run_manager.lab_release_path("lab1")
    assert path.name == "experimental_release.csv"
    df = pd.read_csv(path)
    assert list(df.columns) == config.EXPERIMENTAL_RELEASE_COLUMNS
    assert df["sample_id"].iloc[0] == "S1"
    # blank chemistry stays empty/NaN, not an error.
    assert pd.isna(df["Ca_mM"].iloc[0])


def test_plastic_composite_uses_release_file(runs_root):
    run_manager.create_run("p1", "plastic_composite", created_at="t")
    run_manager.append_lab_row("p1", {"sample_id": "P1"})
    assert run_manager.lab_release_path("p1").name == "experimental_release.csv"


# --------------------------------------------------------------------------- #
# Literature CSV
# --------------------------------------------------------------------------- #
def test_literature_run_csv_has_literature_columns(runs_root):
    run_manager.create_run("lit1", "literature_benchmark", created_at="t")
    run_manager.append_literature_row("lit1", {
        "source_id": "doe2020", "paper_title": "Leaching of CFA", "year": "2020",
    })
    path = run_manager.literature_path("lit1")
    assert path.name == "literature_benchmark.csv"
    df = pd.read_csv(path)
    assert list(df.columns) == run_manager.LITERATURE_BENCHMARK_COLUMNS
    assert df["source_id"].iloc[0] == "doe2020"


def test_save_literature_dataframe_reorders_to_schema(runs_root):
    run_manager.create_run("lit2", "literature_benchmark", created_at="t")
    raw = pd.DataFrame([{"notes": "n", "source_id": "x", "extra_col": "keep"}])
    path = run_manager.save_literature_dataframe("lit2", raw)
    df = pd.read_csv(path)
    # canonical columns come first, in schema order; extras retained at the end.
    assert list(df.columns)[:len(run_manager.LITERATURE_BENCHMARK_COLUMNS)] == \
        run_manager.LITERATURE_BENCHMARK_COLUMNS
    assert "extra_col" in df.columns


# --------------------------------------------------------------------------- #
# Guardrails: literature data cannot become lab experimental data
# --------------------------------------------------------------------------- #
def test_literature_run_cannot_write_lab_release(runs_root):
    run_manager.create_run("lit3", "literature_benchmark", created_at="t")
    with pytest.raises(run_manager.RunTypeError):
        run_manager.lab_release_path("lit3")
    with pytest.raises(run_manager.RunTypeError):
        run_manager.append_lab_row("lit3", {"sample_id": "S1"})


def test_lab_run_cannot_write_literature_file(runs_root):
    run_manager.create_run("lab2", "lab_experiment", created_at="t")
    with pytest.raises(run_manager.RunTypeError):
        run_manager.literature_path("lab2")


def test_demo_rows_are_tagged_synthetic(runs_root):
    run_manager.create_run("demo1", "synthetic_demo", created_at="t")
    run_manager.append_demo_row("demo1", {"sample_id": "D1", "Ca_mM": "1.0"})
    df = pd.read_csv(run_manager.demo_path("demo1"))
    assert list(df.columns) == run_manager.DEMO_DATA_COLUMNS
    assert (df["source_type"] == run_manager.SYNTHETIC_SOURCE_TAG).all()


# --------------------------------------------------------------------------- #
# Pipeline bridge
# --------------------------------------------------------------------------- #
def test_export_lab_run_to_pipeline(runs_root, tmp_path, monkeypatch):
    dest_dir = tmp_path / "experimental_icp"
    monkeypatch.setattr(config, "EXPERIMENTAL_ICP_DIR", dest_dir)
    run_manager.create_run("lab3", "lab_experiment", created_at="t")
    run_manager.append_lab_row("lab3", {"sample_id": "S1", "final_pH": "12"})
    dest = run_manager.export_lab_run_to_pipeline("lab3")
    assert dest == dest_dir / "experimental_release_manual_entry.csv"
    df = pd.read_csv(dest)
    assert df["sample_id"].iloc[0] == "S1"


def test_export_requires_lab_type_and_data(runs_root, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPERIMENTAL_ICP_DIR", tmp_path / "icp")
    run_manager.create_run("lit4", "literature_benchmark", created_at="t")
    with pytest.raises(run_manager.RunTypeError):
        run_manager.export_lab_run_to_pipeline("lit4")
    # lab run with no data file yet -> RunManagerError
    run_manager.create_run("lab4", "lab_experiment", created_at="t")
    with pytest.raises(run_manager.RunManagerError):
        run_manager.export_lab_run_to_pipeline("lab4")
