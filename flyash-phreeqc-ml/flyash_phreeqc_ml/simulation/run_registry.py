"""Simulation **run registry** — provenance for Simulate-tab PHREEQC executions.

When a user runs PHREEQC from the Simulate tab (single scenario or a small sweep), this
module lets them *save* the whole run with a full provenance chain, so every predicted
value can be traced back to: the experiment text → the parsed scenario → the confirmed
assumptions → the material profile → the generated PHREEQC input → the executable/database
used → the output files → the parser status → the warnings.

Hard boundaries (these are **simulation** records, not validation results):

* It is **off the scientific result path** — it imports no AI module and no comparison /
  residual / mapping module, and it never writes to ``data/raw``, ``data/processed``, the
  source tree, or any measured/validation CSV. Records live only under
  ``outputs/simulation_runs/`` (gitignored).
* It **never stores secrets** — no API key (the app never exposes one), and it does **not**
  persist the raw AI response. It records the *structured* scenario + the executable/database
  **paths** (paths, not contents).
* A saved run is clearly a **simulation result, not validated** — every record carries that
  label, and these runs are kept separate from measured-data validation runs.

Storage layout (one folder per run, under :data:`config.SIMULATION_RUNS_DIR`)::

    outputs/simulation_runs/<run_id>/
        run_metadata.json          # provenance scalars + lists + per-scenario records
        assumptions_warnings.json  # assumptions + warnings (incl. missing-profile warning)
        scenario_matrix.csv        # the plan matrix that was run
        parsed_results.csv         # the per-scenario parsed result table
        inputs/<scenario_id>.pqi   # the exact reviewed inputs (copied for self-containment)
"""
from __future__ import annotations

import io
import json
import math
import numbers
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .. import __version__ as APP_VERSION
from .. import config
from ..security.identity import IdentityContext, ROLE_MEMBER, current_identity
from ..storage_scope import StorageScope, StorageScopeError
from . import batch_executor as _batch
from . import phreeqc_executor as _exec

# File names written per run.
RUN_METADATA_FILE = "run_metadata.json"
ASSUMPTIONS_WARNINGS_FILE = "assumptions_warnings.json"
SCENARIO_MATRIX_FILE = "scenario_matrix.csv"
PARSED_RESULTS_FILE = "parsed_results.csv"
INPUTS_SUBDIR = "inputs"
MAX_RUN_METADATA_BYTES = 8 * 1024 * 1024
MAX_RUN_AUXILIARY_BYTES = 4 * 1024 * 1024
MAX_RUN_TABLE_ROWS = 10_000
MAX_RUN_TABLE_COLUMNS = 2_000
MAX_COPIED_INPUT_BYTES = 25 * 1024 * 1024
MAX_EXPORTED_BUNDLE_BYTES = 100 * 1024 * 1024
MAX_LISTED_RUNS = 10_000

_RESOURCE_IDENTITY_FIELDS = (
    "phreeqc_version",
    "executable_sha256",
    "database_resource_id",
    "database_version",
    "database_sha256",
    "environment_identity_hash",
    "container_image_digest",
    "resource_manifest_version",
    "runtime_resource_id",
    "runtime_installation_id",
    "database_installation_id",
    "runtime_manifest_sha256",
    "resource_catalog_hash",
    "resource_catalog_generation",
    "resource_bootstrap_result_sha256",
    "knowledge_pack_hash",
    "source_manifest_sha256",
    "app_version",
    "app_vcs_ref",
    "resource_identity_status",
)

# Standing honesty label stamped into every saved record.
SIM_RUN_LABEL = ("Simulation run — PHREEQC execution of reviewed inputs. Not validated "
                 "against measured data.")
MISSING_PROFILE_WARNING = ("No material profile was selected — material composition was not "
                           "included, so the predicted dissolution is structural only "
                           "(composition is never invented).")

_SECRET_VALUE_RE = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._~+/=-]+|\bsk-[A-Za-z0-9_-]{8,}|"
    r"\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+)", re.IGNORECASE)
_SECRET_KEY_RE = re.compile(
    r"(^|_)(api_?key|token|authorization|cookie|password|secret|credential)(_|$)",
    re.IGNORECASE)


# --------------------------------------------------------------------------- #
# JSON safety (no NaN/Inf, numpy → python, never a secret)
# --------------------------------------------------------------------------- #
def _json_safe(obj):
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, numbers.Integral):
        return int(obj)
    if isinstance(obj, numbers.Real):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    return str(obj)


def _dump_json(obj) -> str:
    return json.dumps(_json_safe(obj), indent=2, allow_nan=False)


def _assert_no_credentials(obj, *, depth: int = 0) -> None:
    """Refuse credential-like material before a run bundle reaches disk."""
    if depth > 12:
        raise ValueError("run provenance nesting exceeds the safe limit")
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _SECRET_KEY_RE.search(str(key)):
                raise ValueError("run provenance contains a credential-like field")
            _assert_no_credentials(value, depth=depth + 1)
    elif isinstance(obj, (list, tuple, set)):
        for value in obj:
            _assert_no_credentials(value, depth=depth + 1)
    elif isinstance(obj, str) and _SECRET_VALUE_RE.search(obj):
        raise ValueError("run provenance contains credential-like content")


def _bounded_csv(rows: list) -> str:
    table = pd.DataFrame(rows or [])
    if table.shape[0] > MAX_RUN_TABLE_ROWS or table.shape[1] > MAX_RUN_TABLE_COLUMNS:
        raise ValueError("run table exceeds the registry shape limit")
    text = table.to_csv(index=False)
    if len(text.encode("utf-8")) > MAX_RUN_AUXILIARY_BYTES:
        raise ValueError("run table exceeds the registry size limit")
    return text


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass
class SimulationScenarioRecord:
    """One scenario's input-side provenance (metadata + the input it produced)."""

    scenario_id: str
    metadata: dict = field(default_factory=dict)     # leachant/conc/time/temp/L:S/material
    input_status: str | None = None                  # the input-preview status
    input_path: str | None = None

    def to_dict(self) -> dict:
        return {"scenario_id": self.scenario_id, "metadata": self.metadata,
                "input_status": self.input_status, "input_path": self.input_path}


@dataclass
class SimulationOutputRecord:
    """One scenario's output-side provenance (execution + parsed values)."""

    scenario_id: str
    status: str
    parse_status: str | None = None
    pH: float | None = None
    pe: float | None = None
    element_totals_mM: dict = field(default_factory=dict)
    saturation_indices: list = field(default_factory=list)
    output_path: str | None = None
    selected_output_path: str | None = None
    runtime_seconds: float | None = None
    executed_at: str | None = None
    input_sha256: str | None = None
    error_message: str | None = None
    warnings: list = field(default_factory=list)
    resource_identity: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id, "status": self.status,
            "parse_status": self.parse_status, "pH": self.pH, "pe": self.pe,
            "element_totals_mM": self.element_totals_mM,
            "saturation_indices": self.saturation_indices,
            "output_path": self.output_path, "selected_output_path": self.selected_output_path,
            "runtime_seconds": self.runtime_seconds, "executed_at": self.executed_at,
            "input_sha256": self.input_sha256, "error_message": self.error_message,
            "warnings": list(self.warnings), "resource_identity": self.resource_identity,
        }


@dataclass
class SimulationRunRecord:
    """The full provenance of one saved Simulate execution (single scenario or sweep)."""

    run_id: str
    created_at: str
    user_label: str | None = None
    original_experiment_text: str | None = None
    desired_outputs_text: str | None = None
    parser_source: str | None = None
    scenario_json: dict = field(default_factory=dict)
    assumptions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    material_profile_summary: dict | None = None
    material_profile_verification_status: str | None = None
    matrix_rows: list = field(default_factory=list)
    phreeqc_input_paths: list = field(default_factory=list)
    phreeqc_output_paths: list = field(default_factory=list)
    phreeqc_database_path: str | None = None
    phreeqc_executable_path: str | None = None
    scientific_resource_identities: list = field(default_factory=list)
    execution_status_summary: dict = field(default_factory=dict)
    parsed_result_table: list = field(default_factory=list)
    plot_axis: str | None = None
    notes: str | None = None
    refinement: dict | None = None                       # parent-run / objective / refined-sweep provenance
    target_match: dict | None = None                     # inverse-search: target spec / grid / best candidate
    agent_provenance: dict | None = None                 # AI-assistant transcript summary + action trace
    scenarios: list = field(default_factory=list)        # list[SimulationScenarioRecord]
    outputs: list = field(default_factory=list)          # list[SimulationOutputRecord]
    app_version: str = APP_VERSION

    @property
    def n_scenarios(self) -> int:
        return len(self.outputs)

    @property
    def n_success(self) -> int:
        return int(self.execution_status_summary.get(_exec.STATUS_SUCCESS, 0))

    def to_metadata_dict(self) -> dict:
        """The ``run_metadata.json`` payload (the big tables live in their own CSVs)."""
        return {
            "label_note": SIM_RUN_LABEL,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "app_version": self.app_version,
            "user_label": self.user_label,
            "notes": self.notes,
            "original_experiment_text": self.original_experiment_text,
            "desired_outputs_text": self.desired_outputs_text,
            "parser_source": self.parser_source,
            "scenario_json": self.scenario_json,
            "material_profile_summary": self.material_profile_summary,
            "material_profile_verification_status": self.material_profile_verification_status,
            "phreeqc_executable_path": self.phreeqc_executable_path,
            "phreeqc_database_path": self.phreeqc_database_path,
            "scientific_resource_identities": list(self.scientific_resource_identities),
            "phreeqc_input_paths": list(self.phreeqc_input_paths),
            "phreeqc_output_paths": list(self.phreeqc_output_paths),
            "execution_status_summary": self.execution_status_summary,
            "plot_axis": self.plot_axis,
            "refinement": self.refinement,
            "target_match": self.target_match,
            "agent_provenance": self.agent_provenance,
            "n_scenarios": self.n_scenarios,
            "scenarios": [s.to_dict() for s in self.scenarios],
            "outputs": [o.to_dict() for o in self.outputs],
            "files": {
                "parsed_results": PARSED_RESULTS_FILE,
                "scenario_matrix": SCENARIO_MATRIX_FILE,
                "assumptions_warnings": ASSUMPTIONS_WARNINGS_FILE,
            },
        }

    def assumptions_warnings_dict(self) -> dict:
        return {"assumptions": list(self.assumptions), "warnings": list(self.warnings)}

    def summary(self) -> dict:
        """A compact list-view summary (no big tables)."""
        leachant = self.scenario_json.get("leachant_type") if self.scenario_json else None
        material = (self.scenario_json.get("material_name") if self.scenario_json else None) or (
            (self.material_profile_summary or {}).get("material_name"))
        counts = self.execution_status_summary or {}
        return {
            "run_id": self.run_id, "user_label": self.user_label,
            "created_at": self.created_at, "n_scenarios": self.n_scenarios,
            "n_success": int(counts.get(_exec.STATUS_SUCCESS, 0)),
            "n_failed": sum(int(v) for k, v in counts.items() if k != _exec.STATUS_SUCCESS),
            "material": material, "leachant": leachant, "sweep_axis": self.plot_axis,
        }


# --------------------------------------------------------------------------- #
# Run-id generation
# --------------------------------------------------------------------------- #
def _slug(text: str, n: int = 24) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "-", str(text or "")).strip("-").lower()[:n]


def generate_run_id(now, label: str | None = None) -> str:
    """A filesystem-safe run id from a timestamp (+ optional label slug).

    ``now`` may be a ``datetime`` (the app passes ``datetime.now()``) or any string. Kept
    out of the registry's save path so saving is pure/testable and time is injected.
    """
    stamp = now.strftime("%Y%m%d-%H%M%S") if hasattr(now, "strftime") else _slug(str(now), 32)
    s = _slug(label)
    return f"sim-{stamp}" + (f"-{s}" if s else "")


def _safe_run_id(run_id: str) -> str:
    """Sanitise a run id so it can never traverse out of the registry folder."""
    cleaned = re.sub(r"[^0-9A-Za-z._-]", "_", str(run_id or "")).strip("._-")
    return cleaned or "run"


def _validated_run_id(run_id: str) -> str:
    """Return an already-safe run id; never reinterpret a path probe as another id."""
    raw = str(run_id or "")
    safe = _safe_run_id(raw)
    if not raw or len(raw) > 160 or raw != safe:
        raise ValueError("run id must contain only letters, numbers, '.', '_' or '-'")
    return safe


# --------------------------------------------------------------------------- #
# Build a record from the app's session objects (defensive; pure)
# --------------------------------------------------------------------------- #
def build_run_record(*, run_id: str, created_at: str, batch, matrix=None, scenario=None,
                     parse_result=None, material_profile=None, previews=None,
                     experiment_text: str | None = None, desired_outputs_text: str | None = None,
                     label: str | None = None, notes: str = "",
                     refinement: dict | None = None,
                     target_match: dict | None = None,
                     agent_provenance: dict | None = None) -> SimulationRunRecord:
    """Assemble a :class:`SimulationRunRecord` from whatever the Simulate tab has in hand.

    ``batch`` is a :class:`batch_executor.BatchResult` (a single run is wrapped as a
    one-scenario batch by the caller). Everything else is optional and recorded if present;
    a **missing material profile is recorded as a warning**, never silently ignored.
    ``target_match`` (when the run came from an inverse target search) carries the target
    spec / search parameters / candidate grid / best candidate — a **simulation search
    record, not validation** (kept distinct from measured-data validation runs).
    ``agent_provenance`` (when the run was driven by the AI assistant) carries the **agent
    transcript summary + action trace + confirmed assumptions + the not-validated label** —
    a sanitised, key-free dict that **never** holds raw model responses, secrets, or measured
    data (the assistant never stores those). Additive: it does not change any scientific field.
    """
    scenario_json = {}
    if scenario is not None:
        try:
            scenario_json = dict(scenario.to_flat_dict())
        except Exception:                                  # noqa: BLE001
            scenario_json = {}

    parser_source = getattr(parse_result, "source", None) or "manual"
    assumptions = _collect_assumptions(parse_result)
    warnings = _collect_warnings(parse_result, material_profile)

    mp_summary = None
    mp_status = None
    if material_profile is not None:
        try:
            mp_summary = material_profile.summary()
        except Exception:                                  # noqa: BLE001
            mp_summary = {"material_name": getattr(material_profile, "material_name", None)}
        mp_status = getattr(material_profile, "verification_status", None)

    matrix_rows = matrix.to_dict("records") if (matrix is not None
                                                and hasattr(matrix, "to_dict")) else []
    table_df = _batch.build_result_table(batch, matrix)
    parsed_table = table_df.to_dict("records") if not table_df.empty else []
    plot_axis = _batch.detect_sweep_axis(matrix)[1]

    preview_status = {getattr(p, "scenario_id", None): getattr(p, "status", None)
                      for p in (previews or [])}

    scenario_records: list[SimulationScenarioRecord] = []
    output_records: list[SimulationOutputRecord] = []
    input_paths: list[str] = []
    output_paths: list[str] = []
    resource_identities: list[dict] = []
    exe_path = db_path = None
    meta_by_id = {str(r.get("scenario_id")): r for r in matrix_rows}

    for r in batch.results:
        ex = r.execution
        sid = r.scenario_id
        if ex.input_path:
            input_paths.append(ex.input_path)
        if ex.output_path:
            output_paths.append(ex.output_path)
        exe_path = exe_path or ex.phreeqc_executable
        db_path = db_path or ex.database_path
        resource_identity = {
            key: getattr(ex, key, None) for key in _RESOURCE_IDENTITY_FIELDS
            if getattr(ex, key, None) is not None
        }
        if resource_identity and resource_identity not in resource_identities:
            resource_identities.append(resource_identity)
        scenario_records.append(SimulationScenarioRecord(
            scenario_id=sid, metadata=meta_by_id.get(str(sid), {}),
            input_status=preview_status.get(sid), input_path=ex.input_path))
        output_records.append(SimulationOutputRecord(
            scenario_id=sid, status=r.status, parse_status=r.parse_status, pH=r.pH, pe=r.pe,
            element_totals_mM=r.element_totals_mM, saturation_indices=r.saturation_indices,
            output_path=ex.output_path, selected_output_path=ex.selected_output_path,
            runtime_seconds=r.runtime_seconds, executed_at=ex.timestamp,
            input_sha256=ex.input_hash, error_message=ex.error_message,
            warnings=r.warnings, resource_identity=resource_identity))

    return SimulationRunRecord(
        run_id=run_id, created_at=created_at, user_label=label or None,
        original_experiment_text=experiment_text, desired_outputs_text=desired_outputs_text,
        parser_source=parser_source, scenario_json=scenario_json, assumptions=assumptions,
        warnings=warnings, material_profile_summary=mp_summary,
        material_profile_verification_status=mp_status, matrix_rows=matrix_rows,
        phreeqc_input_paths=input_paths, phreeqc_output_paths=output_paths,
        phreeqc_database_path=db_path, phreeqc_executable_path=exe_path,
        scientific_resource_identities=resource_identities,
        execution_status_summary=batch.status_counts(), parsed_result_table=parsed_table,
        plot_axis=plot_axis, notes=notes or None, refinement=(dict(refinement) if refinement
                                                              else None),
        target_match=(dict(target_match) if target_match else None),
        agent_provenance=(dict(agent_provenance) if agent_provenance else None),
        scenarios=scenario_records, outputs=output_records)


def _collect_assumptions(parse_result) -> list:
    out = []
    for a in getattr(parse_result, "assumptions", None) or []:
        out.append({"field": getattr(a, "field", None),
                    "assumed_value": getattr(a, "assumed_value", None),
                    "reason": getattr(a, "reason", None),
                    "source": getattr(a, "source", None)})
    return out


def _collect_warnings(parse_result, material_profile) -> list:
    warns: list[str] = []
    for w in getattr(parse_result, "warnings", None) or []:
        warns.append(str(w))
    sc = getattr(parse_result, "scenario", None)
    for w in getattr(sc, "warnings", None) or []:
        if str(w) not in warns:
            warns.append(str(w))
    if material_profile is None or not getattr(material_profile, "is_usable", False):
        warns.append(MISSING_PROFILE_WARNING)
    return warns


# --------------------------------------------------------------------------- #
# The registry (save / list / load / export) — safe storage only
# --------------------------------------------------------------------------- #
class SimulationRunRegistry:
    """Reads/writes saved simulation runs under a **safe** generated-output folder."""

    def __init__(self, base_dir=None, *, identity: IdentityContext | None = None):
        principal = identity or current_identity()
        root = Path(base_dir) if base_dir is not None else config.SIMULATION_RUNS_DIR
        try:
            self.storage_scope = StorageScope.for_identity(root, principal)
        except StorageScopeError as exc:
            raise ValueError(str(exc)) from exc
        # Local mode intentionally preserves the historical layout. Hosted mode
        # receives a deterministic opaque tenant root below ``base_dir/tenants``.
        self.base_dir = self.storage_scope.tenant_root

    def run_dir(self, run_id: str) -> Path:
        candidate = self.base_dir / _validated_run_id(run_id)
        root = self.base_dir.resolve(strict=False)
        try:
            candidate.resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise ValueError("run path escapes the scoped registry") from exc
        current = self.base_dir
        for part in candidate.relative_to(self.base_dir).parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise ValueError("symlink-backed run paths are not permitted")
        return candidate

    def _assert_safe(self, path: Path) -> Path:
        # Reuse the executor's single safe-workspace authority (forbids data/raw,
        # data/processed, and the package source tree). Raises ValueError if unsafe.
        return _exec.assert_safe_workspace(path)

    def save_run(self, record: SimulationRunRecord, *, copy_inputs: bool = True) -> Path:
        """Write a run's provenance bundle; return its folder. Refuses an unsafe path."""
        self.storage_scope.identity.require_role(ROLE_MEMBER)
        d = self.run_dir(record.run_id)
        self._assert_safe(d)
        metadata = record.to_metadata_dict()
        metadata["storage_scope"] = {
            "schema_version": 1,
            "tenant_scoped": not self.storage_scope.is_local_compatibility_mode,
        }
        assumptions = record.assumptions_warnings_dict()
        _assert_no_credentials(metadata)
        _assert_no_credentials(assumptions)
        metadata_text = _dump_json(metadata)
        assumptions_text = _dump_json(assumptions)
        matrix_text = _bounded_csv(record.matrix_rows)
        results_text = _bounded_csv(record.parsed_result_table)
        if len(metadata_text.encode("utf-8")) > MAX_RUN_METADATA_BYTES:
            raise ValueError("run metadata exceeds the registry size limit")
        if len(assumptions_text.encode("utf-8")) > MAX_RUN_AUXILIARY_BYTES:
            raise ValueError("run assumptions/warnings exceed the registry size limit")
        input_sources: list[Path] = []
        copied_bytes = 0
        if copy_inputs:
            for value in record.phreeqc_input_paths[:MAX_RUN_TABLE_ROWS]:
                source = Path(value)
                if source.is_symlink() or not source.is_file():
                    continue
                copied_bytes += source.stat().st_size
                if copied_bytes > MAX_COPIED_INPUT_BYTES:
                    raise ValueError("reviewed input copies exceed the registry size limit")
                input_sources.append(source)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        if self.base_dir.is_symlink():
            raise ValueError("registry root must not be a symlink")
        # ``exist_ok=False`` is an atomic no-clobber claim. Concurrent saves can
        # never overwrite a completed/partial run with the same id.
        d.mkdir(exist_ok=False)

        (d / RUN_METADATA_FILE).write_text(metadata_text, encoding="utf-8")
        (d / ASSUMPTIONS_WARNINGS_FILE).write_text(assumptions_text, encoding="utf-8")
        (d / SCENARIO_MATRIX_FILE).write_text(matrix_text, encoding="utf-8")
        (d / PARSED_RESULTS_FILE).write_text(results_text, encoding="utf-8")

        if input_sources:
            inputs_dir = d / INPUTS_SUBDIR
            inputs_dir.mkdir(parents=True, exist_ok=True)
            for src in input_sources:
                try:
                    shutil.copy2(src, inputs_dir / src.name)
                except OSError:
                    pass
        return d

    def list_runs(self) -> list:
        """Summaries of saved runs, newest first. Tolerates malformed folders."""
        if not self.base_dir.exists():
            return []
        out = []
        for index, d in enumerate(self.base_dir.iterdir()):
            if index >= MAX_LISTED_RUNS:
                break
            if d.is_symlink() or not d.is_dir():
                continue
            meta = d / RUN_METADATA_FILE
            if meta.is_symlink() or not meta.is_file() \
                    or meta.stat().st_size > MAX_RUN_METADATA_BYTES:
                continue
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            out.append(_summary_from_metadata(m))
        return sorted(out, key=lambda r: str(r.get("created_at") or ""), reverse=True)

    def load_run(self, run_id: str) -> dict | None:
        """The saved ``run_metadata.json`` as a dict (or None if absent/unreadable)."""
        meta = self.run_dir(run_id) / RUN_METADATA_FILE
        if meta.is_symlink() or not meta.is_file() \
                or meta.stat().st_size > MAX_RUN_METADATA_BYTES:
            return None
        try:
            return json.loads(meta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def load_parsed_results(self, run_id: str):
        path = self.run_dir(run_id) / PARSED_RESULTS_FILE
        return (pd.read_csv(path) if path.is_file() and not path.is_symlink()
                and path.stat().st_size <= MAX_RUN_AUXILIARY_BYTES else None)

    def export_zip(self, run_id: str) -> bytes:
        """Zip a run's whole folder into bytes (for a 'download run package')."""
        d = self.run_dir(run_id)
        buf = io.BytesIO()
        total_bytes = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            if d.is_dir() and not d.is_symlink():
                for p in sorted(d.rglob("*")):
                    if p.is_symlink() or not p.is_file():
                        continue
                    try:
                        p.resolve(strict=True).relative_to(d.resolve(strict=True))
                    except (OSError, ValueError):
                        continue
                    total_bytes += p.stat().st_size
                    if total_bytes > MAX_EXPORTED_BUNDLE_BYTES:
                        raise ValueError("run bundle exceeds the export size limit")
                    zf.write(p, arcname=str(Path(_validated_run_id(run_id)) /
                                             p.relative_to(d)))
        return buf.getvalue()


def _write_csv(rows: list, path: Path) -> None:
    """Backwards-compatible internal helper with the registry's bounded table policy."""
    path.write_text(_bounded_csv(rows), encoding="utf-8")


def _summary_from_metadata(m: dict) -> dict:
    counts = m.get("execution_status_summary") or {}
    n_success = int(counts.get(_exec.STATUS_SUCCESS, 0))
    n_failed = sum(int(v) for k, v in counts.items() if k != _exec.STATUS_SUCCESS)
    scenario = m.get("scenario_json") or {}
    material = scenario.get("material_name") or (
        (m.get("material_profile_summary") or {}).get("material_name"))
    return {
        "run_id": m.get("run_id"), "user_label": m.get("user_label"),
        "created_at": m.get("created_at"), "n_scenarios": m.get("n_scenarios", 0),
        "n_success": n_success, "n_failed": n_failed, "material": material,
        "leachant": scenario.get("leachant_type"), "sweep_axis": m.get("plot_axis"),
    }
