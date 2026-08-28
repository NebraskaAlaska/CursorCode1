"""On-demand PHREEQC simulation runner (Prompt 11) — make "needs new simulation"
actionable.

Given a measured condition that has no matching model scenario, this module can:

1. :func:`build_input` — template a ``.pqi`` from the condition's metadata
   (leachant molarity, L/S ratio, temperature, CO₂ cup cover). The cup-cover
   semantics map to PHREEQC's ``CO2(g)`` encoding decoded from the real
   ``data/raw`` files: **OA → atmospheric CO₂**; **PF/GS → reduced-CO₂**, for which
   we generate *both* a low-CO₂ and a no-CO₂ variant because the true exchange rate
   is unconfirmed. Solution chemistry the metadata can't supply (fly-ash release
   Si/Al/Ca, the charge-balance counter-ion, sometimes pH/temperature) comes from a
   configurable **assumed** stock block, written as visible comments — never buried.
2. :func:`run` — execute the PHREEQC **CLI** (user-supplied binary + database;
   neither is committed), capturing stdout/stderr, with a hard timeout so it can
   never hang, raising a typed error carrying the PHREEQC error text on failure.
3. :func:`ingest` — parse the ``.pqo`` with the existing :mod:`pqo_parser`, append
   to ``data/processed/phreeqc_results.csv`` tagged ``generated=true`` + the source
   ``condition_key`` + a timestamp, and regenerate the scenario manifest — so
   generated scenarios are distinguishable from hand-built ones everywhere and can
   be offered for mapping.

This is plumbing: no ML, no chemistry beyond templating the documented encoding.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import config, profiles, replicates, scenarios
from .parsers.pqo_parser import parse_pqo_file, records_to_frames


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #
class PhreeqcRunnerError(Exception):
    """Base class for runner failures."""


class PhreeqcNotConfiguredError(PhreeqcRunnerError):
    """PHREEQC executable and/or database are not configured/available."""


class PhreeqcRunError(PhreeqcRunnerError):
    """PHREEQC ran but failed (non-zero exit, timeout, error text, or no output)."""


_SETUP_HELP = (
    "PHREEQC is not configured. Install the PHREEQC CLI and supply the database:\n"
    "  • set PHREEQC_EXE to the `phreeqc` binary (or put it on PATH), and\n"
    "  • set PHREEQC_DATABASE to your CEMDATA18 .dat file (not shipped — user-supplied).\n"
    "The app and pipeline work fully without this; only on-demand simulation needs it."
)


# --------------------------------------------------------------------------- #
# Generated input
# --------------------------------------------------------------------------- #
_GENERATED_INPUT_SEAL = object()


@dataclass(frozen=True)
class GeneratedInput:
    """One templated ``.pqi`` for a (condition, CO₂ scenario) pair."""

    model_label: str            # atm_CO2 / low_CO2 / no_CO2
    condition_code: str         # OA / PF / GS / unknown
    source_condition_key: str
    pqi_text: str
    assumptions: tuple = ()
    metadata: dict = field(default_factory=dict)  # NaOH_M, L/S, CO2_condition, temp, time
    basename: str = "gen"       # safe file stem (drives the .pqo record_key prefix)
    scenario_id: str | None = None
    _builder_text_hash: str | None = field(default=None, repr=False, compare=False)
    _builder_identity_hash: str | None = field(default=None, repr=False, compare=False)
    _builder_seal: object = field(default=None, repr=False, compare=False)

    @property
    def builder_issued(self) -> bool:
        return bool(
            self._builder_seal is _GENERATED_INPUT_SEAL
            and self._builder_text_hash == hashlib.sha256(self.pqi_text.encode("utf-8")).hexdigest()
            and self._builder_identity_hash == _generated_identity_hash(
                model_label=self.model_label, condition_code=self.condition_code,
                source_condition_key=self.source_condition_key, pqi_text=self.pqi_text,
                assumptions=self.assumptions, metadata=self.metadata,
                basename=self.basename, scenario_id=self.scenario_id)
            and self.basename == _safe_stem(self.basename)
            and self.scenario_id
        )


def _generated_identity_hash(**values) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _issue_generated_input(*, model_label: str, condition_code: str,
                           source_condition_key: str, pqi_text: str, assumptions=(),
                           metadata=None, basename: str, scenario_id: str) -> GeneratedInput:
    """Construct the immutable legacy-builder output carrying its original exact-text seal."""
    text = str(pqi_text)
    values = {
        "model_label": str(model_label), "condition_code": str(condition_code),
        "source_condition_key": str(source_condition_key), "pqi_text": text,
        "assumptions": tuple(assumptions or ()), "metadata": dict(metadata or {}),
        "basename": _safe_stem(basename), "scenario_id": str(scenario_id),
    }
    return GeneratedInput(
        **values,
        _builder_text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        _builder_identity_hash=_generated_identity_hash(**values),
        _builder_seal=_GENERATED_INPUT_SEAL,
    )


def is_trusted_generated_input(value) -> bool:
    """True only for an intact object mechanically issued by this deterministic builder."""
    return isinstance(value, GeneratedInput) and value.builder_issued


# --------------------------------------------------------------------------- #
# Templating
# --------------------------------------------------------------------------- #
def _fmt(x, nd: int = 5) -> str:
    return f"{float(x):.{nd}f}"


def _safe_stem(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", str(text)).strip("_") or "condition"


def build_single_input(naoh_m: float, liquid_solid_ratio: float, temperature_C: float,
                       co2_scenario: str, *, ph: float | None = None,
                       time_min: float | None = None, label: str = "condition",
                       material_inputs: dict | None = None,
                       candidate_phases=None,
                       selected_output_elements=None,
                       ) -> tuple[str, list[str]]:
    """Template one ``.pqi`` text + its list of assumptions for one CO₂ scenario.

    ``co2_scenario`` is a model label (``atm_CO2``/``low_CO2``/``no_CO2``) or a short
    alias (``atm``/``low``/``none``). The DATABASE line is intentionally omitted — the
    database is supplied to the CLI at run time (:func:`run`), so the same text works
    regardless of where CEMDATA18 lives.

    Optional **mass-balance attribution** extras (Prompt 24; all additive — when a
    parameter is ``None`` the output is byte-identical to the base template):

    * ``material_inputs`` — ``{element: mol_per_L}`` of dissolved batch material added to
      the SOLUTION (overriding the assumed stock for those elements), flagged as an
      assumption in the header.
    * ``candidate_phases`` — extra precipitate phases added to EQUILIBRIUM_PHASES (each
      ``Phase 0 0``, allowed to precipitate), skipping any already present.
    * ``selected_output_elements`` — emit, per element, moles in solution and per phase
      moles via SELECTED_OUTPUT/USER_PUNCH (so the attribution can read them back).
    """
    scenario = config.CO2_SCENARIO_ALIASES.get(str(co2_scenario), str(co2_scenario))
    if scenario not in config.CO2_SCENARIO_ENCODING:
        raise PhreeqcRunnerError(f"Unknown CO2 scenario: {co2_scenario!r}")
    si, reservoir = config.CO2_SCENARIO_ENCODING[scenario]

    stock = config.ASSUMED_STOCK_SOLUTION
    assumptions: list[str] = [
        "fly-ash release Si/Al/Ca taken from the configured ASSUMED stock solution "
        f"(Si={stock['Si']}, Al={stock['Al']}, Ca={stock['Ca']} mol/L) — not measured",
        "charge balanced with Cl as the counter-ion (the real anion is not measured)",
    ]
    ph_val = ph
    if ph_val is None:
        ph_val = config.ASSUMED_PH
        assumptions.append(f"pH assumed {config.ASSUMED_PH} (no measured pH on this condition)")
    temp_val = temperature_C
    if temp_val is None:
        temp_val = config.ASSUMED_TEMPERATURE_C
        assumptions.append(f"temperature assumed {config.ASSUMED_TEMPERATURE_C} °C")
    co2_desc = {
        "atm_CO2": "atmospheric CO2(g) equilibration (open air)",
        "low_CO2": "reduced CO2(g): atmospheric target but a tiny depletable reservoir",
        "no_CO2": "no CO2(g) phase (sealed — no atmospheric exchange)",
    }[scenario]

    lines: list[str] = []
    lines.append("# Generated PHREEQC input — templated from a measured condition's metadata.")
    lines.append("# DATABASE is supplied to the PHREEQC CLI at run time (not hard-coded here).")
    lines.append("# ASSUMED values (not measured) — shown so they are never buried:")
    for a in assumptions:
        lines.append(f"#   - {a}")
    lines.append(f"# CO2 scenario: {scenario} — {co2_desc}")
    lines.append(f"# L/S ratio (metadata, not a SOLUTION field): {liquid_solid_ratio}")
    lines.append(f"# source condition: {label}")
    lines.append("")
    lines.append(f"TITLE Generated: NaOH {_fmt(naoh_m)} M, L/S {liquid_solid_ratio}, {scenario}")
    lines.append("")
    lines.append("SOLUTION 1 generated")
    lines.append(f"    temp      {_fmt(temp_val, 2)}")
    lines.append(f"    pH        {_fmt(ph_val, 2)}")
    lines.append("    units     mol/l")
    lines.append(f"    density   {_fmt(config.ASSUMED_DENSITY, 1)}")
    lines.append(f"    Na        {_fmt(naoh_m)}")
    lines.append(f"    Si        {_fmt(stock['Si'])}   # ASSUMED stock")
    lines.append(f"    Al        {_fmt(stock['Al'])}   # ASSUMED stock")
    lines.append(f"    Ca        {_fmt(stock['Ca'])}   # ASSUMED stock")
    # Dissolved batch material (mass-balance attribution) overrides the stock for the
    # given elements; PHREEQC uses the later definition. Flagged, never buried.
    if material_inputs:
        for el, mol_l in material_inputs.items():
            lines.append(f"    {el}        {_fmt(mol_l)}   # dissolved batch material (mol/L)")
        assumptions.append(
            "dissolved batch material added to SOLUTION (mol/L): "
            + ", ".join(f"{el}={float(v):g}" for el, v in material_inputs.items())
            + " — derived from the measured liquid + closure gap, overrides the stock")
    lines.append("    Cl        0 charge   # ASSUMED counter-ion (charge balance)")
    lines.append("")
    lines.append("EQUILIBRIUM_PHASES 1")
    if si is not None:
        lines.append(f"    CO2(g)    {_fmt(si, 2)}   {reservoir:g}")
    else:
        lines.append("    # no CO2(g) phase — sealed scenario")
    lines.append("    Cal           0      0")
    lines.append("    Portlandite   0      0")
    if candidate_phases:
        present = {"Cal", "Portlandite", "CO2(g)"}
        for ph_name in candidate_phases:
            if ph_name not in present:
                lines.append(f"    {ph_name:<13} 0      0   # candidate precipitate")
                present.add(ph_name)
    lines.append("")
    if selected_output_elements:
        lines.extend(_selected_output_block(selected_output_elements, candidate_phases or []))
        lines.append("")
    lines.append("USE solution 1")
    lines.append("USE equilibrium_phases 1")
    lines.append("END")
    lines.append("")
    return "\n".join(lines), assumptions


# Column-name conventions the attribution reads back from the selected output.
def sol_moles_column(element: str) -> str:
    return f"{element}_sol_mol"


def phase_moles_column(phase: str) -> str:
    return f"phase_{phase}_mol"


def _selected_output_block(elements, phases) -> list[str]:
    """SELECTED_OUTPUT + USER_PUNCH emitting per-element solution moles + per-phase moles.

    ``TOT("El") * (mass of water)`` gives moles in solution; ``EQUI("Phase")`` gives
    moles of each precipitated phase. The attribution reads these columns back.
    """
    headings: list[str] = []
    punch: list[str] = []
    n = 10
    for el in elements:
        headings.append(sol_moles_column(el))
        punch.append(f"  {n} PUNCH TOT(\"{el}\") * TOT(\"water\")")
        n += 10
    for ph_name in phases:
        headings.append(phase_moles_column(ph_name))
        punch.append(f"  {n} PUNCH EQUI(\"{ph_name}\")")
        n += 10
    return (["SELECTED_OUTPUT", "    -reset false", "    -high_precision true",
             "USER_PUNCH", "    -headings  " + "  ".join(headings)] + punch)


def generation_blocked_reason(condition: dict, profile=None) -> str | None:
    """Why a condition can't be templated (or ``None`` if it can).

    Acid (HCl) leaching has no CEMDATA NaOH/CO₂ analogue, so we don't fabricate one.
    """
    leachant = str(condition.get("leachant", "")).strip().lower()
    if "hcl" in leachant or "acid" in leachant or replicates._is_acid(condition.get("leachant")):
        return ("Acid (HCl) leaching is not represented by the CEMDATA NaOH/CO₂ "
                "workflow — no simulation is generated for it.")
    if _condition_naoh(condition) is None:
        return "No NaOH molarity on this condition — cannot template a leachant solution."
    return None


def _condition_naoh(condition: dict) -> float | None:
    for key in ("NaOH_M", "concentration", "naoh_m"):
        v = scenarios._to_float(condition.get(key))
        if v is not None:
            return v
    return None


def build_input(condition: dict, profile=None, template=None) -> list[GeneratedInput]:
    """Template the ``.pqi`` variants for one measured condition.

    Returns one :class:`GeneratedInput` for an OA condition (``atm_CO2``) or two for a
    covered PF/GS condition (``low_CO2`` + ``no_CO2``); ``[]`` when the condition can't
    be templated (see :func:`generation_blocked_reason`). ``template`` is accepted for
    API symmetry/future use; the built-in template is used when it is ``None``.
    """
    profile = profile or profiles.default_dataset_profile()
    if generation_blocked_reason(condition, profile):
        return []

    naoh = _condition_naoh(condition)
    ls = scenarios._to_float(condition.get("liquid_solid_ratio"))
    temp = scenarios._to_float(condition.get("temperature_C"))
    time_min = scenarios._to_float(condition.get("time_min"))
    ph = (scenarios._to_float(condition.get("final_pH"))
          or scenarios._to_float(condition.get("initial_pH")))
    code = scenarios.sample_condition_code(condition, profile) or "unknown"
    ckey = replicates.condition_key(condition, profile)

    labels = config.COVER_TO_CO2_SCENARIOS.get(code, ["atm_CO2"])
    extra_assumption = ([] if code in config.COVER_TO_CO2_SCENARIOS
                        else [f"condition code {code!r} not recognised — assuming atmospheric CO2"])

    out: list[GeneratedInput] = []
    for model_label in labels:
        text, assumptions = build_single_input(
            naoh, ls if ls is not None else float("nan"),
            temp, model_label, ph=ph, time_min=time_min, label=ckey,
        )
        out.append(_issue_generated_input(
            model_label=model_label,
            condition_code=code,
            source_condition_key=ckey,
            pqi_text=text,
            assumptions=tuple(assumptions + extra_assumption),
            metadata={
                "NaOH_M": naoh,
                "liquid_solid_ratio": ls,
                "CO2_condition": model_label,   # the MODEL-side CO₂ label
                "temperature_C": temp,
                "time_min": time_min,
            },
            basename=f"gen_{_safe_stem(ckey)}_{model_label}",
            scenario_id=f"{ckey}|{model_label}",
        ))
    return out


def build_design_input(naoh_m: float, liquid_solid_ratio: float, temperature_C: float,
                       co2_scenario: str, *, sample_id: str) -> GeneratedInput:
    """Issue one deterministic, provenance-sealed input for the surrogate-design workflow."""
    text, assumptions = build_single_input(
        naoh_m, liquid_solid_ratio, temperature_C, co2_scenario, label=str(sample_id))
    scenario = config.CO2_SCENARIO_ALIASES.get(str(co2_scenario), str(co2_scenario))
    return _issue_generated_input(
        model_label=scenario, condition_code="design", source_condition_key=str(sample_id),
        pqi_text=text, assumptions=assumptions,
        metadata={"NaOH_M": float(naoh_m), "liquid_solid_ratio": float(liquid_solid_ratio),
                  "CO2_condition": scenario, "temperature_C": float(temperature_C)},
        basename=str(sample_id), scenario_id=str(sample_id))


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def _resolve_exe(exe: str | None) -> str:
    exe = exe or config.PHREEQC_EXE_PATH
    found = shutil.which(exe) or (exe if Path(exe).is_file() else None)
    if not found:
        raise PhreeqcNotConfiguredError(f"PHREEQC executable {exe!r} not found.\n{_SETUP_HELP}")
    return found


def _resolve_database(database: str | None) -> Path:
    database = database or config.PHREEQC_DATABASE_PATH
    if not database:
        raise PhreeqcNotConfiguredError(_SETUP_HELP)
    path = Path(database)
    if not path.is_file():
        raise PhreeqcNotConfiguredError(
            f"PHREEQC database not found at {path}.\n{_SETUP_HELP}")
    return path


def is_configured() -> bool:
    """True when both the PHREEQC executable and database resolve (no run attempted)."""
    try:
        _resolve_exe(None)
        _resolve_database(None)
        return True
    except PhreeqcNotConfiguredError:
        return False


# Phases the generated ``.pqi`` requires, in CEMDATA18 naming. A standard database such as
# the shipped ``phreeqc.dat`` defines ``Calcite`` / ``Portlandite`` but NOT the CEMDATA
# abbreviation ``Cal`` — so it would error on this input rather than run it.
REQUIRED_DATABASE_PHASES = ("Cal", "Portlandite")


def database_defines_phases(phases, database: str | None = None) -> bool:
    """True if the configured (or given) PHREEQC database defines every name in ``phases``.

    Delegates to :mod:`simulation.database_compatibility` (the single source of truth for
    "does this database define these phases"). Used to gate CEMDATA18-specific integration
    tests — the runner templates CEMDATA phase names that other databases lack. Never raises.
    """
    from .simulation import database_compatibility as _dbcompat
    return _dbcompat.database_defines_phases(phases, database)


def is_cemdata_compatible(database: str | None = None) -> bool:
    """True when the configured database defines the CEMDATA18 phases the runner templates.

    A run-gating check: even with a PHREEQC binary + *some* database configured, the
    on-demand runner's input only runs on a CEMDATA18-compatible database.
    """
    return database_defines_phases(REQUIRED_DATABASE_PHASES, database)


def capture_compatible_environment(availability):
    """Return one already-resolved legacy environment, or fail with preview-only guidance.

    This helper performs no environment lookup.  UI callers use it to retain the single exact
    identity they displayed before constructing confirmations for a whole input set.
    """
    from .simulation import phreeqc_run_contract as _contract
    environment = getattr(availability, "environment_identity", None)
    if (availability is None or not getattr(availability, "can_run", False)
            or not isinstance(environment, _contract.ExecutionEnvironmentIdentity)):
        message = getattr(availability, "message", None) or _SETUP_HELP
        raise PhreeqcNotConfiguredError(
            message + " Preview remains available; no review, confirmation, workspace, "
            "ingestion, or subprocess will occur.")
    if not is_cemdata_compatible(environment.database.resolved_path):
        required = ", ".join(REQUIRED_DATABASE_PHASES)
        raise PhreeqcNotConfiguredError(
            "The configured database is not compatible with this legacy CEMDATA workflow "
            f"because it does not define {required}. Preview remains available; select a "
            "compatible database and review its identity before execution.")
    return environment


def run(generated_input: GeneratedInput, workdir, *, basename: str | None = None,
        exe: str | None = None,
        database: str | None = None, timeout: float | None = None,
        confirmation=None) -> Path:
    """Run one intact deterministic :class:`GeneratedInput`; return its ``.pqo`` path.

    Writes ``<basename>.pqi`` + ``<basename>.pqo`` under ``workdir`` and invokes the
    CLI as ``phreeqc <input> <output> <database>``. Raises
    :class:`PhreeqcNotConfiguredError` when the exe/database are missing, and
    :class:`PhreeqcRunError` (carrying the PHREEQC error text) on timeout, non-zero
    exit, missing output, or an ``ERROR`` line in the output.
    """
    if not is_trusted_generated_input(generated_input):
        raise PhreeqcRunError(
            "PHREEQC execution blocked: input is not an intact deterministic GeneratedInput.")
    expected_basename = generated_input.basename
    if basename is not None and _safe_stem(basename) != expected_basename:
        raise PhreeqcRunError(
            "PHREEQC execution blocked: basename differs from the reviewed generated input.")
    basename = expected_basename
    from .simulation import phreeqc_executor as _executor
    from .simulation import phreeqc_run_contract as _contract
    preview = _contract.generated_text_preview(generated_input)
    availability = _executor.check_availability(exe=exe, database=database)
    readiness = _contract.assess_execution(preview, confirmation, availability)
    if not availability.can_run or availability.environment_identity is None:
        raise PhreeqcNotConfiguredError(availability.message + "\n" + _SETUP_HELP)
    if not readiness.can_execute:
        raise PhreeqcRunError("PHREEQC execution blocked: " + readiness.message)

    # Execute the exact resolved files whose content identities passed assess_execution.
    # The configured values may have been relative paths; carrying those earlier strings into
    # a subprocess with ``cwd=workdir`` could select an unrelated decoy file.
    exe_path = availability.executable_path
    db_path = availability.database_path
    if not exe_path or not db_path or not Path(exe_path).is_absolute() or not Path(db_path).is_absolute():
        raise PhreeqcRunError(
            "PHREEQC execution blocked: verified executable/database paths are not absolute.")
    if not is_cemdata_compatible(db_path):
        required = ", ".join(REQUIRED_DATABASE_PHASES)
        raise PhreeqcNotConfiguredError(
            "The legacy generated-condition runner requires a CEMDATA-compatible database "
            f"defining {required}. Select a compatible database, review its identity, and "
            "confirm again; no PHREEQC run was started.")
    input_text = confirmation.phreeqc_input_text
    timeout = config.PHREEQC_RUN_TIMEOUT_S if timeout is None else timeout

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    in_path = workdir / f"{basename}.pqi"
    out_path = workdir / f"{basename}.pqo"
    in_path.write_text(input_text, encoding="utf-8")

    try:
        proc = subprocess.run(
            [exe_path, str(in_path), str(out_path), str(db_path)],
            capture_output=True, text=True, timeout=timeout, cwd=str(workdir),
        )
    except subprocess.TimeoutExpired as exc:
        raise PhreeqcRunError(f"PHREEQC timed out after {timeout:g}s.") from exc

    out_text = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""
    error_lines = [ln.strip() for ln in (proc.stdout + "\n" + proc.stderr + "\n" + out_text).splitlines()
                   if "ERROR" in ln.upper()]
    if proc.returncode != 0 or not out_path.exists() or error_lines:
        detail = "\n".join(error_lines[:20]) or (proc.stderr.strip() or proc.stdout.strip()
                                                 or f"exit code {proc.returncode}")
        raise PhreeqcRunError(f"PHREEQC run failed:\n{detail}")
    return out_path


def review_input(generated_input: GeneratedInput):
    """Review only an intact deterministic builder-issued input; raw text is rejected."""
    from .simulation import phreeqc_run_contract as _contract
    return _contract.review_preview(_contract.generated_text_preview(generated_input))


def confirm_reviewed_input(reviewed, *, exe: str | None = None, database: str | None = None,
                           expected_environment=None):
    """Bind a reviewed legacy snapshot to one exact executable/database identity.

    When ``expected_environment`` is supplied, its resolved files are re-hashed immediately and
    must still have the same identity.  The confirmation remains bound to that expected identity;
    a later lookup can never silently substitute a different executable or database.
    """
    from .simulation import phreeqc_executor as _executor
    from .simulation import phreeqc_run_contract as _contract
    if expected_environment is not None:
        if not isinstance(expected_environment, _contract.ExecutionEnvironmentIdentity):
            raise PhreeqcNotConfiguredError(
                "Expected PHREEQC execution environment is invalid; review and confirm again.")
        exe = expected_environment.executable.resolved_path
        database = expected_environment.database.resolved_path

    availability = _executor.check_availability(exe=exe, database=database)
    if not availability.can_run or availability.environment_identity is None:
        raise PhreeqcNotConfiguredError(availability.message + "\n" + _SETUP_HELP)
    current_environment = availability.environment_identity
    if (expected_environment is not None
            and current_environment.identity_hash != expected_environment.identity_hash):
        raise PhreeqcNotConfiguredError(
            "The PHREEQC executable or thermodynamic database changed after review; "
            "execution was blocked. Review the environment identity and confirm again.")
    if not is_cemdata_compatible(current_environment.database.resolved_path):
        required = ", ".join(REQUIRED_DATABASE_PHASES)
        raise PhreeqcNotConfiguredError(
            "The legacy generated-condition runner requires a CEMDATA-compatible database "
            f"defining {required}; review and select a compatible database before confirming.")
    binding = expected_environment if expected_environment is not None else current_environment
    return _contract.confirm_reviewed(reviewed, binding)


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def ingest(pqo_path, run_name: str | None = None, *, condition_key: str = "",
           generated_at: str | None = None, metadata: dict | None = None,
           results_path: Path | None = None) -> list[str]:
    """Parse a generated ``.pqo`` and append its rows to ``phreeqc_results.csv``.

    Tags every appended row with ``generated=True``, the source ``condition_key``,
    and a timestamp, plus the generated-scenario metadata columns (so the manifest
    can use the exact condition metadata). De-duplicates on ``record_key`` (a
    re-generated scenario replaces its prior rows), then regenerates the scenario
    manifest. Returns the appended ``record_key`` values.

    ``run_name`` is accepted for call-site symmetry; the shared results CSV is the
    single source the manifest + mapping read, so generated rows live there (tagged)
    rather than in a per-run file.
    """
    generated_at = generated_at or _now_iso()
    metadata = metadata or {}
    records = parse_pqo_file(pqo_path)
    results, _sat, _asm = records_to_frames(records)
    if results.empty:
        return []

    results[config.GENERATED_FLAG_COLUMN] = True
    results[config.GENERATED_SOURCE_COLUMN] = condition_key
    results[config.GENERATED_AT_COLUMN] = generated_at
    for meta_key, col in config.GENERATED_META_COLUMNS.items():
        results[col] = metadata.get(meta_key)

    results_path = results_path or (config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV)
    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)

    if results_path.exists():
        existing = pd.read_csv(results_path)
        if config.GENERATED_FLAG_COLUMN not in existing.columns:
            existing[config.GENERATED_FLAG_COLUMN] = False
        combined = pd.concat([existing, results], ignore_index=True)
        if "record_key" in combined.columns:
            combined = combined.drop_duplicates(subset=["record_key"], keep="last")
    else:
        combined = results
    combined.to_csv(results_path, index=False)

    # Regenerate the manifest so the generated scenario is immediately mappable.
    scenarios.write_scenario_manifest(results_path)
    return list(results["record_key"].astype(str)) if "record_key" in results.columns else []
