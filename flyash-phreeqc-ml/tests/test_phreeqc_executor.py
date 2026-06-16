"""Tests for the gated PHREEQC execution layer (``simulation.phreeqc_executor``).

The real PHREEQC binary is always **mocked** — no binary / network needed (one optional
integration test runs only when a real binary + database are configured). Coverage:

* PHREEQC missing → graceful structured status, never a crash;
* execution happens **only** on the explicit ``execute_preview`` call;
* files are written **only** to a safe workspace (never ``data/raw`` / the source tree);
* a failed / timed-out run returns a structured error;
* the parser handles a missing SELECTED_OUTPUT file safely;
* running never touches the scientific result-path CSVs;
* generated simulation files are gitignored.
"""
from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pandas as pd
import pytest

import flyash_phreeqc_ml as pkg
from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.simulation import phreeqc_executor as E

PKG_DIR = Path(pkg.__file__).resolve().parent
REAL_PQO = config.RAW_DIR / "PHREEQC outputs" / "L-S_5_atmCO2.pqo"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _preview(text="SOLUTION 1\n    pH 13\nEND\n", sid="SIM-001"):
    return types.SimpleNamespace(scenario_id=sid, phreeqc_input_text=text)


def _fake_exe_db(tmp_path):
    exe = tmp_path / "phreeqc"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    db = tmp_path / "cemdata.dat"
    db.write_text("# fake db")
    return str(exe), str(db)


def _ok_run_writing(out_text):
    """A fake subprocess.run that writes ``out_text`` to the output arg and returns rc 0."""
    def _run(cmd, **kw):
        Path(cmd[2]).write_text(out_text)
        return types.SimpleNamespace(returncode=0, stdout="done", stderr="")
    return _run


# --------------------------------------------------------------------------- #
# Missing PHREEQC → graceful (no crash)
# --------------------------------------------------------------------------- #
def test_missing_phreeqc_is_graceful(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", None)
    res = E.execute_preview(_preview(), workdir=tmp_path, exe="definitely_not_a_real_binary_xyz")
    assert res.status == E.STATUS_MISSING
    assert res.error_message == E.NOT_CONFIGURED_MESSAGE
    assert res.input_path is None                  # nothing written when it can't run
    assert list(tmp_path.iterdir()) == []


def test_check_availability_reports_missing(monkeypatch):
    monkeypatch.setattr(config, "PHREEQC_EXE_PATH", "no_such_phreeqc_binary_zzz")
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", None)
    av = E.check_availability()
    assert not av.can_run
    assert "not configured" in av.message
    assert av.smoke_ok is None                     # no smoke attempted by default


def test_smoke_false_when_missing(monkeypatch):
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", None)
    assert E.smoke_test(exe="nope_binary", database=None) is False


# --------------------------------------------------------------------------- #
# Execution happens ONLY on the explicit call
# --------------------------------------------------------------------------- #
def test_no_execution_without_explicit_call(monkeypatch, tmp_path):
    calls = []

    def _tripwire(cmd, **kw):
        calls.append(cmd)
        Path(cmd[2]).write_text("TITLE ok\n")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(E.subprocess, "run", _tripwire)
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(config, "PHREEQC_EXE_PATH", exe)
    monkeypatch.setattr(config, "PHREEQC_DATABASE_PATH", db)

    # availability check + parsing must NOT execute PHREEQC
    E.check_availability(run_smoke=False)
    E.parse_outputs(E.ExecutionResult("SIM", E.STATUS_FAILED))
    assert calls == []

    # only the explicit execute call runs it
    E.execute_preview(_preview(), workdir=tmp_path / "ws", exe=exe, database=db)
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Safe workspace
# --------------------------------------------------------------------------- #
def test_writes_only_to_given_workspace(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(E.subprocess, "run", _ok_run_writing("TITLE ok\n"))
    ws = tmp_path / "sims"
    res = E.execute_preview(_preview(sid="SIM-007"), workdir=ws, exe=exe, database=db)
    assert res.status == E.STATUS_SUCCESS
    written = sorted(p.name for p in ws.iterdir())
    assert written == ["SIM_007.pqi", "SIM_007.pqo"]
    assert Path(res.input_path).parent == ws.resolve() or Path(res.input_path).parent == ws


@pytest.mark.parametrize("bad_root", ["raw", "processed", "package"])
def test_unsafe_workspace_refused(monkeypatch, tmp_path, bad_root):
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(E.subprocess, "run", _ok_run_writing("ok"))
    target = {"raw": config.RAW_DIR, "processed": config.PROCESSED_DIR,
              "package": config.PACKAGE_DIR}[bad_root] / "evil_sim_dir"
    res = E.execute_preview(_preview(), workdir=target, exe=exe, database=db)
    assert res.status == E.STATUS_FAILED
    assert "refusing" in (res.error_message or "")
    assert not target.exists()                     # never created the forbidden dir


def test_assert_safe_workspace_allows_outputs(monkeypatch):
    # the default workspace is under outputs/ and must be allowed
    assert E.assert_safe_workspace(E.default_workspace())
    with pytest.raises(ValueError):
        E.assert_safe_workspace(config.RAW_DIR / "x")


# --------------------------------------------------------------------------- #
# Failure / timeout → structured error
# --------------------------------------------------------------------------- #
def test_failed_run_returns_structured_error(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)

    def _bad(cmd, **kw):
        Path(cmd[2]).write_text("ERROR: did not converge\n")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="ERROR: boom")

    monkeypatch.setattr(E.subprocess, "run", _bad)
    res = E.execute_preview(_preview(), workdir=tmp_path / "ws", exe=exe, database=db)
    assert res.status == E.STATUS_FAILED
    assert "did not converge" in res.error_message
    assert res.stderr_tail and res.runtime_seconds is not None
    # parsing a failed result is safe
    parsed = E.parse_outputs(res)
    assert parsed.parse_status == E.PARSE_FAILED


def test_timeout_returns_structured(monkeypatch, tmp_path):
    exe, db = _fake_exe_db(tmp_path)

    def _slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))

    monkeypatch.setattr(E.subprocess, "run", _slow)
    res = E.execute_preview(_preview(), workdir=tmp_path / "ws", exe=exe, database=db, timeout=1)
    assert res.status == E.STATUS_TIMEOUT
    assert "timed out" in res.error_message.lower()


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_parse_handles_missing_selected_output(monkeypatch, tmp_path):
    out = tmp_path / "x.pqo"
    out.write_text("(pretend pqo)")
    results = pd.DataFrame([{"state": "batch", "pH": 12.5, "pe": 8.0, "mol_Ca": 0.001,
                             "mol_Si": 0.002}])
    sat = pd.DataFrame([{"state": "batch", "phase": "Calcite", "SI": -0.3}])
    monkeypatch.setattr(E, "parse_pqo_file", lambda p: ["rec"])
    monkeypatch.setattr(E, "records_to_frames", lambda recs: (results, sat, pd.DataFrame()))
    res = E.ExecutionResult("SIM", E.STATUS_SUCCESS, output_path=str(out),
                            selected_output_path=None)
    parsed = E.parse_outputs(res)
    assert parsed.parse_status == E.PARSE_PARSED
    assert parsed.pH == 12.5 and parsed.pe == 8.0
    assert parsed.element_totals_mM == {"Ca": 1.0, "Si": 2.0}      # molality ×1000 → mM
    assert any("SELECTED_OUTPUT" in w for w in parsed.warnings)


def test_parse_failed_is_safe(monkeypatch, tmp_path):
    out = tmp_path / "x.pqo"
    out.write_text("garbage")

    def _boom(p):
        raise ValueError("not a pqo")

    monkeypatch.setattr(E, "parse_pqo_file", _boom)
    parsed = E.parse_outputs(E.ExecutionResult("SIM", E.STATUS_SUCCESS, output_path=str(out)))
    assert parsed.parse_status == E.PARSE_FAILED
    assert parsed.warnings


def test_parse_partial_when_only_pH(monkeypatch, tmp_path):
    out = tmp_path / "x.pqo"
    out.write_text("x")
    results = pd.DataFrame([{"state": "batch", "pH": 12.0}])      # no mol_ columns
    monkeypatch.setattr(E, "parse_pqo_file", lambda p: ["r"])
    monkeypatch.setattr(E, "records_to_frames", lambda recs: (results, pd.DataFrame(),
                                                              pd.DataFrame()))
    parsed = E.parse_outputs(E.ExecutionResult("SIM", E.STATUS_SUCCESS, output_path=str(out)))
    assert parsed.parse_status == E.PARSE_PARTIAL
    assert "element totals" in parsed.missing


# --------------------------------------------------------------------------- #
# Result path is untouched
# --------------------------------------------------------------------------- #
def test_execution_does_not_touch_result_path_csv(monkeypatch, tmp_path):
    results_csv = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    before = results_csv.stat().st_mtime if results_csv.exists() else None
    exe, db = _fake_exe_db(tmp_path)
    monkeypatch.setattr(E.subprocess, "run", _ok_run_writing("TITLE ok\n"))
    E.execute_preview(_preview(), workdir=tmp_path / "ws", exe=exe, database=db)
    after = results_csv.stat().st_mtime if results_csv.exists() else None
    assert before == after                         # the comparison CSV is never written/updated


# --------------------------------------------------------------------------- #
# Optional real-PHREEQC-output parse (no binary needed — parses a shipped .pqo)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not REAL_PQO.exists(), reason="shipped sample .pqo not present")
def test_parse_real_pqo_output():
    res = E.ExecutionResult("SIM", E.STATUS_SUCCESS, output_path=str(REAL_PQO))
    parsed = E.parse_outputs(res)
    assert parsed.parse_status in (E.PARSE_PARSED, E.PARSE_PARTIAL)
    assert parsed.pH is not None
    assert parsed.element_totals_mM            # real pqo carries element totals


# --------------------------------------------------------------------------- #
# Optional true integration (real binary + database)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not E.is_configured(),
                    reason="no real PHREEQC binary + PHREEQC_DATABASE configured")
def test_integration_real_execution(tmp_path):  # pragma: no cover - env-dependent
    pv = _preview(text=E.SMOKE_INPUT, sid="SMOKE")
    res = E.execute_preview(pv, workdir=tmp_path / "ws")
    assert res.status == E.STATUS_SUCCESS


# --------------------------------------------------------------------------- #
# .gitignore protects generated simulation files
# --------------------------------------------------------------------------- #
def test_gitignore_protects_simulation_outputs():
    import shutil as _sh
    if not _sh.which("git"):
        pytest.skip("git not available")
    rel = "outputs/simulations/SIM-001.pqo"
    proc = subprocess.run(["git", "check-ignore", rel], cwd=str(config.PROJECT_ROOT),
                          capture_output=True, text=True)
    if proc.returncode == 128:
        pytest.skip("not inside a git work tree")
    assert proc.returncode == 0, f"{rel!r} is NOT gitignored (check-ignore rc={proc.returncode})"
