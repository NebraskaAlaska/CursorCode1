"""Tests for the append-only audit log (audit.py) + its instrumentation.

Pins (per the spec): append-only behaviour, the event schema (every event carries
timestamp / event_type / app_version), instrumentation firing for import → map →
compare on a synthetic run, and the reader tolerating an unknown future event_type
(and malformed lines). Synthetic data only.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from flyash_phreeqc_ml import audit, config, run_manager


@pytest.fixture()
def run(tmp_path, monkeypatch):
    """A fresh lab run under a temp experiments root."""
    monkeypatch.setattr(config, "EXPERIMENT_RUNS_DIR", tmp_path)
    run_manager.create_run("audit_run", "lab_experiment")
    return "audit_run"


# --------------------------------------------------------------------------- #
# Schema + append-only
# --------------------------------------------------------------------------- #
def test_event_schema_has_required_fields(run):
    assert audit.log_event(run, "custom_event", {"a": 1}) is True
    df = audit.read_audit(run)
    assert list(df.columns) == audit.AUDIT_COLUMNS
    row = df.iloc[0]
    assert row["event_type"] == "custom_event"
    assert row["timestamp"]                      # ISO timestamp present
    assert row["app_version"]                    # package version present
    assert row["payload"] == {"a": 1}


def test_append_only_accumulates_in_order(run):
    audit.log_event(run, "first", {"n": 1})
    audit.log_event(run, "second", {"n": 2})
    audit.log_event(run, "third", {"n": 3})
    df = audit.read_audit(run)
    assert list(df["event_type"]) == ["first", "second", "third"]
    # The writer only ever appends — each call adds exactly one line.
    raw = audit.audit_log_path(run).read_text().strip().splitlines()
    assert len(raw) == 3


def test_no_edit_or_delete_api():
    # The module must not expose any mutation/removal of past events.
    for forbidden in ("delete_event", "edit_event", "remove_event", "clear_audit",
                      "update_event", "truncate"):
        assert not hasattr(audit, forbidden)


def test_logging_failure_warns_not_crashes(monkeypatch, run):
    # Force the writer to fail; it must warn and return False, never raise.
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(audit, "audit_log_path", boom)
    with pytest.warns(UserWarning):
        ok = audit.log_event(run, "x", {})
    assert ok is False


# --------------------------------------------------------------------------- #
# Reader tolerance
# --------------------------------------------------------------------------- #
def test_reader_tolerates_unknown_future_event_type(run):
    # A future version writes an event_type this version never defined.
    audit.log_event(run, "some_future_event_v9", {"new_field": [1, 2, 3]})
    df = audit.read_audit(run)
    assert "some_future_event_v9" in set(df["event_type"])
    assert df.iloc[0]["payload"] == {"new_field": [1, 2, 3]}


def test_reader_skips_malformed_lines(run):
    audit.log_event(run, "good", {"n": 1})
    with audit.audit_log_path(run).open("a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
        fh.write(json.dumps({"timestamp": "t", "event_type": "good2"}) + "\n")
    with pytest.warns(UserWarning):
        df = audit.read_audit(run)
    # The bad line is skipped; the valid ones (incl. one missing payload) survive.
    assert list(df["event_type"]) == ["good", "good2"]
    assert df.iloc[1]["payload"] == {}     # missing field filled with a default


def test_read_audit_empty_when_no_log(run):
    df = audit.read_audit(run)
    assert df.empty
    assert list(df.columns) == audit.AUDIT_COLUMNS


# --------------------------------------------------------------------------- #
# Instrumentation: import -> map -> compare on a synthetic run
# --------------------------------------------------------------------------- #
def test_instrumentation_import_map_compare(run, monkeypatch):
    import numpy as np
    from flyash_phreeqc_ml import import_mapping as im
    from flyash_phreeqc_ml.ml import residual_stats
    from flyash_phreeqc_ml import scenarios

    # --- import: save a lab frame (with a unit conversion) -> import event ---
    raw = pd.DataFrame({"sample_id": ["X0", "X1"], "Ca": [40.078, 80.156],
                        "leachant": ["NaOH", "NaOH"], "NaOH_M": [0.5, 0.5],
                        "liquid_solid_ratio": [5, 5], "CO2_condition": ["OA", "OA"],
                        "final_pH": [13.0, 13.0]})
    mapping = im.suggest_column_mapping(raw.columns)
    transformed = im.build_schema_frame(raw, mapping, {"Ca_mM": "mg/L"},
                                        import_timestamp="2026-06-08T00:00:00")
    run_manager.save_lab_dataframe(run, transformed, mode="replace")

    # --- map: accept a condition mapping -> mapping_accepted event ---
    run_manager.add_condition_mapping(run, "NaOH0.5M_OA_LS5", "k1",
                                      mapping_status="exact")

    # --- compare: drive the script-05 per-run writer directly -> events ---
    import importlib.util
    import sys
    from pathlib import Path
    script = Path(config.PROJECT_ROOT) / "scripts" / "05_compare_experimental.py"
    sys.path.insert(0, str(script.parent))
    spec = importlib.util.spec_from_file_location("script05", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    measured = run_manager.read_data_file(run)
    comp = measured.copy()
    comp["phreeqc_record_key"] = "k1"
    comp["phreeqc_pH"] = 13.1
    comp["residual_pH"] = pd.to_numeric(comp["final_pH"]) - comp["phreeqc_pH"]
    comp["phreeqc_Ca_mM"] = 0.8
    comp["residual_Ca"] = pd.to_numeric(comp["Ca_mM"]) - comp["phreeqc_Ca_mM"]
    sample_map = pd.DataFrame([{"sample_id": s, "phreeqc_record_key": "k1"}
                               for s in measured["sample_id"]])
    pheq = pd.DataFrame([{"record_key": "k1", "state": "batch", "pH": 13.1,
                          "mol_Ca": 0.0008}])

    mod._write_per_run_outputs(run, comp, measured, sample_map, pheq)

    # --- assert the trail captured the whole chain ---
    df = audit.read_audit(run)
    types = list(df["event_type"])
    assert audit.EVENT_IMPORT in types
    assert audit.EVENT_MAPPING_ACCEPTED in types
    assert audit.EVENT_COMPARISON_GENERATED in types
    assert audit.EVENT_INCLUSION in types

    imp = df[df["event_type"] == audit.EVENT_IMPORT].iloc[0]["payload"]
    assert imp["mode"] == "replace" and imp["n_rows"] == 2
    assert imp["conversions"].get("Ca_mM") == "mgL_to_mM"   # conversion id, not a value

    acc = df[df["event_type"] == audit.EVENT_MAPPING_ACCEPTED].iloc[0]["payload"]
    assert acc["condition_key"] == "NaOH0.5M_OA_LS5"
    assert acc["phreeqc_record_key"] == "k1"
    assert acc["mapping_status"] == "exact"

    gen = df[df["event_type"] == audit.EVENT_COMPARISON_GENERATED].iloc[0]["payload"]
    # References the Prompt-1 stamp's hashes rather than duplicating the stamp.
    assert gen["meta_file"] == run_manager.COMPARISON_META_FILENAME
    assert "sources" in gen


def test_no_measured_values_in_import_payload(run):
    from flyash_phreeqc_ml import import_mapping as im
    raw = pd.DataFrame({"sample_id": ["X0"], "Ca": [123.456],
                        "final_pH": [13.21]})
    mapping = im.suggest_column_mapping(raw.columns)
    transformed = im.build_schema_frame(raw, mapping, {"Ca_mM": "mg/L"},
                                        import_timestamp="2026-06-08T00:00:00")
    run_manager.save_lab_dataframe(run, transformed, mode="replace")
    payload = audit.read_audit(run).iloc[0]["payload"]
    blob = json.dumps(payload)
    # Column names + ids + counts only — never the measured numbers.
    assert "123.456" not in blob
    assert "13.21" not in blob
    assert "Ca_mM" in blob           # column name is fine
