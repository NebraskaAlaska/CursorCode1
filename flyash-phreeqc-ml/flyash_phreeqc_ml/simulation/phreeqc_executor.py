"""Deterministic PHREEQC **execution** layer for confirmed Simulate-tab input previews.

This is the first layer that actually *runs* PHREEQC, deliberately separated from the
planning layer and from the scientific result path:

* It runs **only when called explicitly** (a user clicks "Run" after reviewing the input).
  Nothing here runs automatically after AI parsing or preview generation.
* **AI does not write or modify PHREEQC input** — it executes the *exact* reviewed text from
  :class:`simulation.phreeqc_input_builder.PhreeqcInputPreview`; this module never edits it.
* It writes only to a **safe simulation workspace** (``outputs/simulations/`` by default, or a
  caller-supplied run dir) — never ``data/raw``, never the source tree, never the processed
  pipeline CSVs. Generated files are gitignored.
* It is **off the scientific result path**: it imports no AI module, no comparison/residual/
  mapping module, and it writes nothing to the comparison CSVs or measured data. Its outputs
  are **simulation results, not validated predictions**.
* It **never crashes the app**: a missing binary, a failed run, or a timeout returns a typed,
  structured :class:`ExecutionResult` (status ``phreeqc_missing`` / ``failed`` / ``timeout``)
  rather than raising.

Parsing reuses the existing :mod:`parsers.pqo_parser` (the reliable ``.pqo`` output) plus the
optional :mod:`parsers.selected_output_parser` (a ``SELECTED_OUTPUT`` table when produced).
"""
from __future__ import annotations

import datetime as _dt
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..parsers.pqo_parser import parse_pqo_file, records_to_frames
from ..parsers.selected_output_parser import parse_selected_output

# --------------------------------------------------------------------------- #
# Status vocabularies
# --------------------------------------------------------------------------- #
STATUS_NOT_RUN = "not_run"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_MISSING = "phreeqc_missing"
STATUS_TIMEOUT = "timeout"

PARSE_PARSED = "parsed"
PARSE_NO_SELECTED_OUTPUT = "no_selected_output"
PARSE_PARTIAL = "partial"
PARSE_FAILED = "parse_failed"

# Standing wording (single source).
NOT_CONFIGURED_MESSAGE = ("PHREEQC execution is not configured. You can still review and "
                          "download the input preview.")
SIM_OUTPUT_LABEL = ("Generated from PHREEQC execution of the reviewed simulation input. "
                    "Not validated against measured data.")

# A minimal, harmless smoke job — equilibrate pure water (no database-specific phases).
SMOKE_INPUT = "TITLE smoke test\nSOLUTION 1\n    pH 7\n    temp 25\n    units mol/kgw\nEND\n"

_TAIL_LINES = 40             # how many trailing stdout/stderr lines to keep
_MOLALITY_TO_MM = config.PHREEQC_MOLALITY_TO_MM


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #
@dataclass
class PhreeqcAvailability:
    """What is configured / present for PHREEQC execution (no run unless ``run_smoke``)."""

    executable_configured: bool
    database_configured: bool
    executable_found: bool
    database_found: bool
    executable_path: str | None
    database_path: str | None
    message: str
    smoke_ok: bool | None = None        # None = smoke not attempted

    @property
    def can_run(self) -> bool:
        return self.executable_found and self.database_found


@dataclass
class ExecutionResult:
    """Structured outcome of one PHREEQC execution attempt (never an exception)."""

    scenario_id: str
    status: str
    input_path: str | None = None
    output_path: str | None = None
    selected_output_path: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error_message: str | None = None
    runtime_seconds: float | None = None
    timestamp: str | None = None
    phreeqc_executable: str | None = None
    database_path: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id, "status": self.status,
            "input_path": self.input_path, "output_path": self.output_path,
            "selected_output_path": self.selected_output_path,
            "error_message": self.error_message, "runtime_seconds": self.runtime_seconds,
            "timestamp": self.timestamp, "phreeqc_executable": self.phreeqc_executable,
            "database_path": self.database_path,
        }


@dataclass
class ParsedSimulation:
    """Basic parsed outputs from a successful run (pH / pe / totals / SI / selected output)."""

    scenario_id: str
    parse_status: str
    pH: float | None = None
    pe: float | None = None
    element_totals_mM: dict = field(default_factory=dict)      # {element: mM}
    saturation_indices: list = field(default_factory=list)     # [{phase, SI}]
    selected_output: object = None                             # DataFrame or None
    warnings: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    n_states: int = 0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _safe_stem(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", str(text)).strip("_") or "scenario"


def _tail(text: str | None, n: int = _TAIL_LINES) -> str:
    if not text:
        return ""
    lines = str(text).splitlines()
    return "\n".join(lines[-n:])


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None       # drop NaN


def default_workspace() -> Path:
    """The default safe execution workspace (``outputs/simulations/``)."""
    return config.SIMULATIONS_DIR


# Directories the executor must never write into.
def _forbidden_roots() -> tuple[Path, ...]:
    return (config.RAW_DIR, config.PROCESSED_DIR, config.PACKAGE_DIR)


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def assert_safe_workspace(path) -> Path:
    """Return the resolved workspace path, or raise ``ValueError`` if it is unsafe.

    Refuses any directory inside ``data/raw``, ``data/processed``, or the package source
    tree — generated simulation files must never land there.
    """
    rp = Path(path).resolve()
    for root in _forbidden_roots():
        if _is_within(rp, root):
            raise ValueError(
                f"refusing to use {rp} as a simulation workspace — it is inside the "
                f"protected directory {root}.")
    return rp


# --------------------------------------------------------------------------- #
# Configuration / availability (never runs PHREEQC unless run_smoke=True)
# --------------------------------------------------------------------------- #
def _resolve_executable(exe: str | None) -> tuple[str | None, bool]:
    """``(resolved_path_or_None, configured)``. ``configured`` = a non-empty exe was set."""
    name = exe or config.PHREEQC_EXE_PATH
    configured = bool(name)
    if not configured:
        return None, False
    found = shutil.which(name) or (name if Path(name).is_file() else None)
    return (str(found) if found else None), configured


def _resolve_database(database: str | None) -> tuple[str | None, bool]:
    db = database if database is not None else config.PHREEQC_DATABASE_PATH
    configured = bool(db)
    if not configured:
        return None, False
    path = Path(db)
    return (str(path) if path.is_file() else None), configured


def check_availability(*, run_smoke: bool = False, exe: str | None = None,
                       database: str | None = None) -> PhreeqcAvailability:
    """Report whether PHREEQC can run. Only runs a tiny smoke job when ``run_smoke`` is True
    **and** both the executable and database are present."""
    exe_path, exe_conf = _resolve_executable(exe)
    db_path, db_conf = _resolve_database(database)
    exe_found = exe_path is not None
    db_found = db_path is not None

    if exe_found and db_found:
        message = "PHREEQC is configured and ready."
    elif not (exe_conf or db_conf):
        message = NOT_CONFIGURED_MESSAGE
    else:
        bits = []
        if not exe_found:
            bits.append("executable not found"
                        if exe_conf else "executable not configured (set PHREEQC_EXE)")
        if not db_found:
            bits.append("database not found"
                        if db_conf else "database not configured (set PHREEQC_DATABASE)")
        message = NOT_CONFIGURED_MESSAGE + "  (" + "; ".join(bits) + ")"

    av = PhreeqcAvailability(
        executable_configured=exe_conf, database_configured=db_conf,
        executable_found=exe_found, database_found=db_found,
        executable_path=exe_path, database_path=db_path, message=message)

    if run_smoke and av.can_run:
        av.smoke_ok = smoke_test(exe=exe, database=database)
    return av


def is_configured() -> bool:
    """True when both the executable and the database resolve (no run attempted)."""
    return check_availability().can_run


# --------------------------------------------------------------------------- #
# The execution primitive (shared by execute_preview + smoke_test)
# --------------------------------------------------------------------------- #
@dataclass
class _RunOutcome:
    returncode: int | None
    stdout: str
    stderr: str
    out_path: Path | None
    selected_output_path: Path | None
    runtime_seconds: float
    timed_out: bool
    error: str | None = None


def _run_phreeqc(input_text: str, workdir: Path, stem: str, exe_path: str, db_path: str,
                 timeout: float) -> _RunOutcome:
    """Write ``<stem>.pqi`` and invoke ``phreeqc <in> <out> <db>``; capture everything.

    Never raises for a PHREEQC-side failure — returns a ``_RunOutcome``. The only way this
    raises is an unexpected internal error, which the caller catches.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    in_path = workdir / f"{stem}.pqi"
    out_path = workdir / f"{stem}.pqo"
    in_path.write_text(input_text, encoding="utf-8")

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [exe_path, str(in_path), str(out_path), str(db_path)],
            capture_output=True, text=True, timeout=timeout, cwd=str(workdir))
    except subprocess.TimeoutExpired as exc:
        return _RunOutcome(None, _as_text(exc.stdout), _as_text(exc.stderr), None, None,
                           time.monotonic() - t0, True,
                           error=f"PHREEQC timed out after {timeout:g}s.")
    runtime = time.monotonic() - t0
    sel = _find_selected_output(workdir, stem, t0)
    return _RunOutcome(proc.returncode, proc.stdout or "", proc.stderr or "",
                       out_path if out_path.exists() else None, sel, runtime, False)


def _as_text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else value.decode("utf-8", "replace")


def _find_selected_output(workdir: Path, stem: str, since: float) -> Path | None:
    """Locate a SELECTED_OUTPUT file if PHREEQC produced one (naming varies by build)."""
    candidates = [workdir / f"{stem}.sel", workdir / "selected.out"]
    for c in candidates:
        if c.exists():
            return c
    hits = sorted(list(workdir.glob("*.sel")) + list(workdir.glob("selected*.out")),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


# PHREEQC flags real errors with the uppercase sentinel ``ERROR`` (e.g. "ERROR: ...").
# Match it case-SENSITIVELY so benign lowercase output like "Percent error" in a perfectly
# good .pqo is not mistaken for a failure.
_ERROR_RE = re.compile(r"\bERROR\b")


def _error_lines(*texts: str) -> list[str]:
    return [ln.strip() for blob in texts for ln in str(blob).splitlines()
            if _ERROR_RE.search(ln)]


# --------------------------------------------------------------------------- #
# Public: execute one confirmed input preview
# --------------------------------------------------------------------------- #
def execute_preview(preview, *, workdir=None, exe: str | None = None,
                    database: str | None = None, timeout: float | None = None,
                    scenario_id: str | None = None) -> ExecutionResult:
    """Run PHREEQC on a confirmed :class:`PhreeqcInputPreview`'s text.

    Returns a structured :class:`ExecutionResult` and **never raises**: a missing binary →
    ``phreeqc_missing``; a non-zero exit / ERROR line / no output → ``failed``; a timeout →
    ``timeout``. The reviewed input text is run **verbatim** — it is never edited here.
    """
    sid = scenario_id or getattr(preview, "scenario_id", "SIM")
    input_text = getattr(preview, "phreeqc_input_text", None)
    ts = _now_iso()

    if not input_text:
        return ExecutionResult(sid, STATUS_FAILED, error_message="No PHREEQC input text to run.",
                               timestamp=ts)

    exe_path, _ = _resolve_executable(exe)
    db_path, _ = _resolve_database(database)
    if exe_path is None or db_path is None:
        return ExecutionResult(
            sid, STATUS_MISSING, error_message=NOT_CONFIGURED_MESSAGE, timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path)

    # Resolve + guard the workspace (default: outputs/simulations/).
    try:
        ws = assert_safe_workspace(workdir if workdir is not None else default_workspace())
    except ValueError as exc:
        return ExecutionResult(sid, STATUS_FAILED, error_message=str(exc), timestamp=ts,
                               phreeqc_executable=exe_path, database_path=db_path)

    stem = _safe_stem(sid)
    timeout = config.PHREEQC_RUN_TIMEOUT_S if timeout is None else timeout
    try:
        outcome = _run_phreeqc(input_text, ws, stem, exe_path, db_path, timeout)
    except Exception as exc:                                  # noqa: BLE001 — never crash
        return ExecutionResult(
            sid, STATUS_FAILED, input_path=str(ws / f"{stem}.pqi"),
            error_message=f"{type(exc).__name__}: {exc}", timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path)

    in_path = str(ws / f"{stem}.pqi")
    sel_path = str(outcome.selected_output_path) if outcome.selected_output_path else None
    if outcome.timed_out:
        return ExecutionResult(
            sid, STATUS_TIMEOUT, input_path=in_path, stdout_tail=_tail(outcome.stdout),
            stderr_tail=_tail(outcome.stderr), error_message=outcome.error,
            runtime_seconds=outcome.runtime_seconds, timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path)

    out_text = (outcome.out_path.read_text(encoding="utf-8", errors="replace")
                if outcome.out_path else "")
    errs = _error_lines(outcome.stdout, outcome.stderr, out_text)
    if outcome.returncode != 0 or outcome.out_path is None or errs:
        detail = "\n".join(errs[:20]) or (outcome.stderr.strip() or outcome.stdout.strip()
                                          or f"PHREEQC exited with code {outcome.returncode}")
        return ExecutionResult(
            sid, STATUS_FAILED, input_path=in_path,
            output_path=str(outcome.out_path) if outcome.out_path else None,
            selected_output_path=sel_path, stdout_tail=_tail(outcome.stdout),
            stderr_tail=_tail(outcome.stderr), error_message=detail,
            runtime_seconds=outcome.runtime_seconds, timestamp=ts,
            phreeqc_executable=exe_path, database_path=db_path)

    return ExecutionResult(
        sid, STATUS_SUCCESS, input_path=in_path, output_path=str(outcome.out_path),
        selected_output_path=sel_path, stdout_tail=_tail(outcome.stdout),
        stderr_tail=_tail(outcome.stderr), runtime_seconds=outcome.runtime_seconds,
        timestamp=ts, phreeqc_executable=exe_path, database_path=db_path)


def smoke_test(*, exe: str | None = None, database: str | None = None,
               timeout: float = 30.0) -> bool:
    """Run a tiny harmless PHREEQC job in a throwaway temp dir. ``True`` iff it succeeds.

    Used only by :func:`check_availability(run_smoke=True)`. Never raises.
    """
    exe_path, _ = _resolve_executable(exe)
    db_path, _ = _resolve_database(database)
    if exe_path is None or db_path is None:
        return False
    try:
        with tempfile.TemporaryDirectory() as td:
            outcome = _run_phreeqc(SMOKE_INPUT, Path(td), "smoke", exe_path, db_path, timeout)
            if outcome.timed_out or outcome.out_path is None or outcome.returncode != 0:
                return False
            out_text = outcome.out_path.read_text(encoding="utf-8", errors="replace")
            return not _error_lines(outcome.stdout, outcome.stderr, out_text)
    except Exception:                                        # noqa: BLE001 — smoke never crashes
        return False


# --------------------------------------------------------------------------- #
# Public: parse the basic outputs of a successful run
# --------------------------------------------------------------------------- #
def parse_outputs(result: ExecutionResult) -> ParsedSimulation:
    """Extract pH / pe / element totals (mM) / saturation indices from a run's ``.pqo``
    (plus the optional SELECTED_OUTPUT table). Never raises — returns ``parse_failed`` /
    ``no_selected_output`` / ``partial`` / ``parsed`` with explicit ``warnings``/``missing``.
    """
    sid = getattr(result, "scenario_id", "SIM")
    if result is None or result.status != STATUS_SUCCESS or not result.output_path:
        return ParsedSimulation(sid, PARSE_FAILED,
                                warnings=["No successful run output to parse."])

    warnings: list[str] = []
    missing: list[str] = []
    try:
        records = parse_pqo_file(result.output_path)
        results, saturation, _assemblage = records_to_frames(records)
    except Exception as exc:                                  # noqa: BLE001
        return ParsedSimulation(sid, PARSE_FAILED,
                                warnings=[f"Could not parse PHREEQC output: "
                                          f"{type(exc).__name__}: {exc}"])
    if results is None or results.empty:
        return ParsedSimulation(sid, PARSE_FAILED,
                                warnings=["PHREEQC output parsed but contained no solution "
                                          "states."])

    # Prefer the post-equilibration ("batch") state — that is what an experiment measures.
    if "state" in results.columns and (results["state"] == "batch").any():
        row = results[results["state"] == "batch"].iloc[-1].to_dict()
    else:
        row = results.iloc[-1].to_dict()
        warnings.append("No post-reaction (batch) state found — using the last solution state.")

    pH = _num(row.get("pH"))
    pe = _num(row.get("pe"))
    totals: dict[str, float] = {}
    for col, val in row.items():
        if str(col).startswith("mol_"):
            v = _num(val)
            if v is not None:
                totals[str(col)[4:]] = v * _MOLALITY_TO_MM       # molality → mM

    sis: list[dict] = []
    if saturation is not None and not saturation.empty:
        sat = saturation
        if "state" in sat.columns and (sat["state"] == "batch").any():
            sat = sat[sat["state"] == "batch"]
        for _, r in sat.iterrows():
            si = _num(r.get("SI"))
            if si is not None and r.get("phase"):
                sis.append({"phase": str(r.get("phase")), "SI": si})

    selected_df = None
    if result.selected_output_path:
        try:
            selected_df = parse_selected_output(result.selected_output_path)
        except Exception as exc:                              # noqa: BLE001
            warnings.append(f"A SELECTED_OUTPUT file was produced but could not be parsed "
                            f"({type(exc).__name__}).")
    else:
        warnings.append("No SELECTED_OUTPUT file was produced — values were read from the "
                        "main .pqo output instead.")

    if pH is None:
        missing.append("pH")
    if not totals:
        missing.append("element totals")
    if not sis:
        missing.append("saturation indices")

    if pH is None and not totals:
        status = PARSE_NO_SELECTED_OUTPUT if selected_df is None else PARSE_PARTIAL
    elif pH is not None and totals:
        status = PARSE_PARSED
    else:
        status = PARSE_PARTIAL

    return ParsedSimulation(
        sid, status, pH=pH, pe=pe, element_totals_mM=totals, saturation_indices=sis,
        selected_output=selected_df, warnings=warnings, missing=missing, n_states=len(results))


def run_and_parse(preview, **kwargs) -> tuple[ExecutionResult, ParsedSimulation | None]:
    """Convenience: execute then parse. Returns ``(result, parsed-or-None)``."""
    result = execute_preview(preview, **kwargs)
    parsed = parse_outputs(result) if result.status == STATUS_SUCCESS else None
    return result, parsed
