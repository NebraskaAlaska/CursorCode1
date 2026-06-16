"""Small-sweep PHREEQC execution — run a confirmed simulation matrix scenario-by-scenario.

A thin, prototype-scale orchestration layer over the single-scenario
:mod:`phreeqc_executor`. It runs each reviewed input preview in turn, parses each result, and
assembles a structured batch table + plot-ready frames. Like the executor it wraps, it is
deliberately constrained and off the scientific result path:

* **Prototype scale only** — at most :data:`DEFAULT_MAX_SCENARIOS` scenarios per sweep
  (extra scenarios are dropped with a flag, never silently). Hundreds/thousands of runs need
  a dedicated batch/optimization workflow (future work — see :data:`LARGE_SWEEP_MESSAGE`).
* **Never automatic / never crashes** — a caller runs it explicitly; one failed scenario
  never stops the batch, and every per-scenario outcome is a structured status.
* **Off the result path** — it imports the safe executor (+ pandas), never an AI helper, a
  comparison/residual/mapping module, or the Match-tab runner. Its outputs are **simulation
  results, not validated predictions**, and it writes nothing outside the executor's safe
  ``outputs/simulations/`` workspace.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from . import phreeqc_executor as _exec

# --------------------------------------------------------------------------- #
# Limits + labels
# --------------------------------------------------------------------------- #
DEFAULT_MAX_SCENARIOS = 20

LARGE_SWEEP_MESSAGE = ("This prototype supports small confirmed sweeps. Large/adaptive search "
                       "requires a dedicated batch/optimization workflow.")
SWEEP_OUTPUT_LABEL = ("Generated from PHREEQC execution of reviewed simulation inputs. "
                      "Not validated against measured data.")

# Sweep x-axis preference (the task's detection order).
SWEEP_AXIS_PRIORITY = ("leachant_concentration_M", "time_min", "temperature_C")
_KEY_SI_COUNT = 3


# --------------------------------------------------------------------------- #
# Per-scenario + batch result containers
# --------------------------------------------------------------------------- #
@dataclass
class BatchScenarioResult:
    """One scenario's execution + parsed outputs (parsed is None unless the run succeeded)."""

    scenario_id: str
    execution: _exec.ExecutionResult
    parsed: object | None = None         # ParsedSimulation | None

    @property
    def status(self) -> str:
        return self.execution.status

    @property
    def parse_status(self):
        return self.parsed.parse_status if self.parsed is not None else None

    @property
    def pH(self):
        return self.parsed.pH if self.parsed is not None else None

    @property
    def pe(self):
        return self.parsed.pe if self.parsed is not None else None

    @property
    def runtime_seconds(self):
        return self.execution.runtime_seconds

    @property
    def element_totals_mM(self) -> dict:
        return dict(self.parsed.element_totals_mM) if self.parsed is not None else {}

    @property
    def saturation_indices(self) -> list:
        return list(self.parsed.saturation_indices) if self.parsed is not None else []

    @property
    def warnings(self) -> list:
        out: list[str] = []
        if self.execution.error_message and self.status != _exec.STATUS_SUCCESS:
            out.append(self.execution.error_message)
        if self.parsed is not None:
            out += list(self.parsed.warnings)
        return out


@dataclass
class BatchResult:
    """The outcome of running a (capped) list of previews."""

    results: list = field(default_factory=list)      # list[BatchScenarioResult]
    requested: int = 0
    max_scenarios: int = DEFAULT_MAX_SCENARIOS
    truncated: bool = False

    @property
    def executed(self) -> int:
        return len(self.results)

    @property
    def n_success(self) -> int:
        return sum(1 for r in self.results if r.status == _exec.STATUS_SUCCESS)

    def status_counts(self) -> dict:
        return dict(Counter(r.status for r in self.results))


# --------------------------------------------------------------------------- #
# Run a sweep (explicit; never automatic)
# --------------------------------------------------------------------------- #
def run_batch(previews, *, max_scenarios: int = DEFAULT_MAX_SCENARIOS, exe: str | None = None,
              database: str | None = None, timeout: float | None = None,
              on_progress=None) -> BatchResult:
    """Run each preview through the safe executor; collect a structured batch result.

    Caps the run at ``max_scenarios`` (excess previews are dropped with ``truncated=True``).
    One scenario failing never stops the batch — each per-scenario outcome is a structured
    status (``success`` / ``failed`` / ``timeout`` / ``phreeqc_missing``; ``parse_failed`` is
    captured in the parsed status). ``on_progress(i, total, scenario_id, status)`` is called
    after each scenario when given. Never raises.
    """
    previews = list(previews or [])
    requested = len(previews)
    to_run = previews[:max(0, int(max_scenarios))]
    truncated = requested > len(to_run)
    total = len(to_run)

    results: list[BatchScenarioResult] = []
    for i, pv in enumerate(to_run, start=1):
        sid = getattr(pv, "scenario_id", f"SIM-{i:03d}")
        try:
            execution = _exec.execute_preview(pv, exe=exe, database=database, timeout=timeout)
            parsed = (_exec.parse_outputs(execution)
                      if execution.status == _exec.STATUS_SUCCESS else None)
        except Exception as exc:                          # noqa: BLE001 — never stop the batch
            execution = _exec.ExecutionResult(
                sid, _exec.STATUS_FAILED, error_message=f"{type(exc).__name__}: {exc}")
            parsed = None
        results.append(BatchScenarioResult(sid, execution, parsed))
        if on_progress is not None:
            try:
                on_progress(i, total, sid, execution.status)
            except Exception:                             # noqa: BLE001 — UI callback is best-effort
                pass

    return BatchResult(results=results, requested=requested,
                       max_scenarios=int(max_scenarios), truncated=truncated)


# --------------------------------------------------------------------------- #
# Result table + sweep detection (pure; plot-ready)
# --------------------------------------------------------------------------- #
def _key_si_string(sis: list, top: int = _KEY_SI_COUNT) -> str:
    """A compact 'Phase:SI; …' string of the most saturated/undersaturated phases."""
    ranked = sorted((s for s in sis if s.get("SI") is not None),
                    key=lambda s: abs(float(s["SI"])), reverse=True)
    return "; ".join(f"{s['phase']}:{float(s['SI']):.2f}" for s in ranked[:top])


def batch_elements(batch: BatchResult) -> list:
    """Sorted union of elements seen across all scenarios' parsed totals."""
    seen: list[str] = []
    for r in batch.results:
        for el in r.element_totals_mM:
            if el not in seen:
                seen.append(el)
    return sorted(seen)


def build_result_table(batch: BatchResult, matrix=None) -> pd.DataFrame:
    """One row per scenario with metadata (joined from ``matrix``), status, parse status,
    pH / pe, per-element totals (``<El>_mM``), key saturation indices, runtime, warnings."""
    meta: dict[str, dict] = {}
    if matrix is not None and hasattr(matrix, "to_dict"):
        for row in matrix.to_dict("records"):
            meta[str(row.get("scenario_id"))] = row

    elements = batch_elements(batch)
    rows: list[dict] = []
    for r in batch.results:
        m = meta.get(str(r.scenario_id), {})
        row = {
            "scenario_id": r.scenario_id,
            "leachant_type": m.get("leachant_type"),
            "leachant_concentration_M": m.get("leachant_concentration_M"),
            "time_min": m.get("time_min"),
            "temperature_C": m.get("temperature_C"),
            "status": r.status,
            "parse_status": r.parse_status,
            "pH": r.pH,
            "pe": r.pe,
        }
        for el in elements:
            row[f"{el}_mM"] = r.element_totals_mM.get(el)
        row["key_SI"] = _key_si_string(r.saturation_indices)
        row["runtime_seconds"] = (round(r.runtime_seconds, 3)
                                  if r.runtime_seconds is not None else None)
        row["warnings"] = "; ".join(r.warnings)
        rows.append(row)
    return pd.DataFrame(rows)


def detect_sweep_axis(matrix) -> tuple:
    """Pick the sweep x-axis: concentration → time → temperature (whichever *varies*), else
    ``(None, "scenario_id")``. Returns ``(column_or_None, axis_label)``."""
    if matrix is None or not hasattr(matrix, "columns"):
        return None, "scenario_id"
    for col in SWEEP_AXIS_PRIORITY:
        if col in matrix.columns:
            vals = pd.to_numeric(matrix[col], errors="coerce").dropna().unique()
            if len(vals) > 1:
                return col, col
    return None, "scenario_id"


def sweep_plot_frame(table: pd.DataFrame, axis_col: str | None, value_col: str) -> pd.DataFrame:
    """A tidy ``(x, y)`` frame for one value column over successful scenarios, sorted by x.

    Drops rows with a missing y. When ``axis_col`` is None (no varying numeric sweep
    parameter) the x is the ``scenario_id`` (categorical, original order preserved).
    """
    if table is None or table.empty or value_col not in table.columns:
        return pd.DataFrame(columns=["x", "y", "scenario_id"])
    ok = table[table["status"] == _exec.STATUS_SUCCESS].copy()
    if ok.empty:
        return pd.DataFrame(columns=["x", "y", "scenario_id"])
    if axis_col and axis_col in ok.columns:
        ok["x"] = pd.to_numeric(ok[axis_col], errors="coerce")
        out = ok[["x", value_col, "scenario_id"]].rename(columns={value_col: "y"})
        out = out.dropna(subset=["y"]).sort_values("x")
    else:
        out = ok[["scenario_id", value_col]].rename(columns={value_col: "y"})
        out["x"] = out["scenario_id"]
        out = out.dropna(subset=["y"])
    return out[["x", "y", "scenario_id"]].reset_index(drop=True)
