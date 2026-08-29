"""Generate a clean experiment plan (a run sheet) for an experiment run.

The plan is built from four small experiment *sets* (a time series, a NaOH
concentration series, a CO2-control pair, and a replicate check). Each set is a
Cartesian product of its varied factors; every combination becomes one row with a
deterministic ``sample_id``. Rows are de-duplicated on ``sample_id`` so the same
physical condition is not scheduled twice — unless the replicate number differs.

The output CSV carries the planning columns plus all blank measurement columns, so
the same file can be printed as a bench sheet and filled in directly at the bench.
This module computes the table only; the script ``scripts/06_generate_experiment_plan.py``
owns the file path.
"""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
import re
from itertools import product
from pathlib import Path

import pandas as pd

from .. import config

# Fixed fly-ash type for the planned matrix. Kept as a constant (not a magic
# string) so it is easy to change for a future material.
DEFAULT_FLY_ASH_TYPE = "CFA"

# The planning columns that come *before* the standard release columns. The full
# plan = these + the measurement columns (left blank, to be filled at the bench).
PLAN_LEADING_COLUMNS = ["sample_id", "experiment_set", "replicate"]

# Measurement / metadata columns reused verbatim from the release schema, minus
# the ones we already place up front. Column names match the canonical release
# schema exactly (``fly_ash_type``), so the plan can be filled in and re-read by
# the Phase-2 parser without renaming.
_RELEASE_TAIL = [
    "experiment_date",
    "fly_ash_type",
    "NaOH_M",
    "time_min",
    "temperature_C",
    "liquid_solid_ratio",
    "CO2_condition",
    "initial_pH",
    "final_pH",
    "conductivity_mS_cm",
    "Ca_mM",
    "Si_mM",
    "Al_mM",
    "Fe_mM",
    "Na_mM",
    "K_mM",
    "Sc_ppb",
    "total_REE_ppb",
    "filtration_notes",
    "precipitate_observed",
    "notes",
]

PLAN_COLUMNS = PLAN_LEADING_COLUMNS + _RELEASE_TAIL

GENERIC_MEASUREMENT_COLUMNS = [
    "measured_response",
    "measured_unit",
    "measurement_method",
    "measurement_qc_status",
    "measurement_notes",
]


class ExperimentPlanError(ValueError):
    """A user-defined design is incomplete, unsafe, or exceeds its explicit cap."""


class ExperimentPlanLimitError(ExperimentPlanError):
    """The requested plan exceeds the supplied maximum-run cap."""

# --------------------------------------------------------------------------- #
# Experiment-set definitions
# --------------------------------------------------------------------------- #
# Each set lists, per factor, the value(s) to sweep. Scalars are held constant;
# lists are expanded via a Cartesian product. Defaults fill the unspecified knobs.
# CO2 is controlled by the cup cover (OA = open air, PF = plastic flap, GS = glass).
# OA is the default; the cover-control set sweeps all three covers.
_SET_DEFAULTS = {
    "NaOH_M": 0.5,
    "time_min": 60,
    "temperature_C": 25,
    "liquid_solid_ratio": 5,
    "CO2_condition": "OA",
    "replicate": 1,
}

EXPERIMENT_SETS: dict[str, dict] = {
    "time_series": {"time_min": [10, 20, 40, 60, 90, 120]},
    "naoh_series": {"NaOH_M": [0, 0.1, 0.25, 0.5, 1.0]},
    "co2_control": {"CO2_condition": ["OA", "PF", "GS"]},
    "replicate_check": {"replicate": [1, 2, 3]},
}

# Factors that take part in the sample_id / Cartesian expansion.
_FACTORS = ["NaOH_M", "time_min", "temperature_C", "liquid_solid_ratio", "CO2_condition", "replicate"]


def _fmt_number(value) -> str:
    """Format a numeric factor compactly for use inside a sample_id.

    ``0.5 -> "0.5"``, ``1.0 -> "1"``, ``0 -> "0"``, ``5 -> "5"`` — trailing zeros
    are dropped so ids stay short and stable.
    """
    return f"{float(value):g}"


def make_sample_id(
    *,
    naoh_m: float,
    liquid_solid_ratio: float,
    time_min: float,
    co2_condition: str,
    replicate: int,
) -> str:
    """Build the canonical sample id for one experimental condition.

    Format: ``CFA-NaOH{NaOH_M}M-LS{liquid_solid_ratio}-{time_min}min-{CO2}-R{replicate}``.
    Two conditions that differ only in replicate number get distinct ids; two
    otherwise-identical conditions get the *same* id (so they de-duplicate).
    """
    return (
        f"CFA-NaOH{_fmt_number(naoh_m)}M"
        f"-LS{_fmt_number(liquid_solid_ratio)}"
        f"-{_fmt_number(time_min)}min"
        f"-{co2_condition}"
        f"-R{int(replicate)}"
    )


def _expand_set(set_name: str, spec: dict, experiment_date: str | None) -> list[dict]:
    """Expand one experiment-set definition into a list of plan rows."""
    # Resolve each factor to a list (scalars -> single-element lists).
    factor_values: dict[str, list] = {}
    for factor in _FACTORS:
        value = spec.get(factor, _SET_DEFAULTS[factor])
        factor_values[factor] = list(value) if isinstance(value, (list, tuple)) else [value]

    rows: list[dict] = []
    for combo in product(*(factor_values[f] for f in _FACTORS)):
        values = dict(zip(_FACTORS, combo))
        sample_id = make_sample_id(
            naoh_m=values["NaOH_M"],
            liquid_solid_ratio=values["liquid_solid_ratio"],
            time_min=values["time_min"],
            co2_condition=values["CO2_condition"],
            replicate=values["replicate"],
        )
        row = {col: "" for col in PLAN_COLUMNS}
        row.update(
            {
                "sample_id": sample_id,
                "experiment_set": set_name,
                "replicate": values["replicate"],
                "experiment_date": experiment_date or "",
                "fly_ash_type": DEFAULT_FLY_ASH_TYPE,
                "NaOH_M": values["NaOH_M"],
                "time_min": values["time_min"],
                "temperature_C": values["temperature_C"],
                "liquid_solid_ratio": values["liquid_solid_ratio"],
                "CO2_condition": values["CO2_condition"],
            }
        )
        rows.append(row)
    return rows


def _all_planned_rows(experiment_date: str | None = None) -> list[dict]:
    """Every planned row across all sets, *before* de-duplication (definition order)."""
    rows: list[dict] = []
    for set_name, spec in EXPERIMENT_SETS.items():
        rows.extend(_expand_set(set_name, spec, experiment_date))
    return rows


def build_experiment_plan(experiment_date: str | None = None) -> pd.DataFrame:
    """Build the full experiment plan as a DataFrame.

    Rows from all four sets are concatenated in definition order, then
    de-duplicated on ``sample_id`` keeping the first occurrence — so a condition
    that appears in more than one set is scheduled once (and attributed to the
    first set that introduced it), while distinct replicates are preserved.
    """
    raw = pd.DataFrame(_all_planned_rows(experiment_date), columns=PLAN_COLUMNS)
    return raw.drop_duplicates(subset="sample_id", keep="first").reset_index(drop=True)


def plan_dedup_stats(experiment_date: str | None = None) -> dict:
    """Build the plan and report what de-duplication did.

    Returns a dict with ``n_raw`` (rows before dedup), ``n_unique`` (after),
    ``n_removed``, the per-set raw counts (``raw_per_set``), and the final
    ``plan`` DataFrame. The summary distinguishes how many conditions each set
    *requested* from how many survive once cross-set duplicates are dropped.
    """
    raw = pd.DataFrame(_all_planned_rows(experiment_date), columns=PLAN_COLUMNS)
    plan = raw.drop_duplicates(subset="sample_id", keep="first").reset_index(drop=True)
    return {
        "n_raw": len(raw),
        "n_unique": len(plan),
        "n_removed": len(raw) - len(plan),
        "raw_per_set": raw.groupby("experiment_set").size().sort_index(),
        "plan": plan,
    }


def write_experiment_plan(
    path: str | Path | None = None,
    experiment_date: str | None = None,
) -> tuple[Path, dict]:
    """Generate the plan, write it to *path* (default location), and report stats.

    Returns ``(path, stats)`` where ``stats`` is the :func:`plan_dedup_stats` dict
    (its ``plan`` key holds the written DataFrame).
    """
    if path is None:
        path = config.EXPERIMENTAL_ICP_DIR / config.EXPERIMENT_PLAN_CSV
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    stats = plan_dedup_stats(experiment_date=experiment_date)
    stats["plan"].to_csv(path, index=False)
    return path, stats


def summarize_plan(df: pd.DataFrame) -> pd.Series:
    """Count of *unique* samples per experiment set (post-dedup)."""
    return df.groupby("experiment_set").size().sort_index()


def _json_key(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ExperimentPlanError(f"plan value is not finite JSON-safe data: {exc}") from exc


def _validated_value(value, label: str):
    if isinstance(value, bool):
        raise ExperimentPlanError(f"{label} must not be a boolean")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ExperimentPlanError(f"{label} must be finite")
        return value
    if value is None:
        raise ExperimentPlanError(f"{label} must not be missing")
    if isinstance(value, str):
        if not value.strip():
            raise ExperimentPlanError(f"{label} must not be blank")
        return value.strip()
    # JSON-safe lists/dicts can be explicit categorical levels, but still receive
    # deterministic finite validation. They are never interpreted scientifically.
    _json_key(value)
    return value


def _validated_mapping(values, label: str) -> dict:
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ExperimentPlanError(f"{label} must be an object")
    out = {}
    for raw_name, raw_value in values.items():
        name = str(raw_name or "").strip()
        if not name:
            raise ExperimentPlanError(f"{label} contains a blank field name")
        if name in {"sample_id", "replicate", "design_mode", *GENERIC_MEASUREMENT_COLUMNS}:
            raise ExperimentPlanError(f"{label} field {name!r} is reserved")
        out[name] = _validated_value(raw_value, f"{label}.{name}")
    return out


def _validated_factors(factors) -> tuple[dict[str, list], int]:
    if not isinstance(factors, dict) or not factors:
        raise ExperimentPlanError("generic design requires at least one explicit factor")
    cleaned: dict[str, list] = {}
    raw_count = 1
    for raw_name in sorted(factors, key=lambda item: str(item)):
        name = str(raw_name or "").strip()
        if not name:
            raise ExperimentPlanError("factor name must not be blank")
        if name in {"sample_id", "replicate", "design_mode", *GENERIC_MEASUREMENT_COLUMNS}:
            raise ExperimentPlanError(f"factor name {name!r} is reserved")
        raw_levels = factors[raw_name]
        if not isinstance(raw_levels, (list, tuple)) or not raw_levels:
            raise ExperimentPlanError(f"factor {name!r} requires a non-empty list of levels")
        levels = [_validated_value(value, f"factors.{name}") for value in raw_levels]
        raw_count *= len(levels)
        seen = set()
        unique = []
        for value in levels:
            key = _json_key(value)
            if key not in seen:
                seen.add(key)
                unique.append(value)
        cleaned[name] = unique
    return cleaned, raw_count


def _safe_prefix(value: str | None) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "SAMPLE").strip()).strip("-")
    if not text:
        text = "SAMPLE"
    return text[:24].upper()


def _condition_id(prefix: str, condition: dict, replicate: int, experiment_date: str | None) -> str:
    identity = {"condition": condition, "experiment_date": experiment_date or "",
                "replicate": replicate}
    digest = hashlib.sha256(_json_key(identity).encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}-R{replicate}"


def build_generic_factor_plan(
    *,
    material_id: str,
    goal: str,
    factors: dict,
    fixed_conditions: dict | None,
    replicates: int,
    max_run_count: int,
    controls: list | None = None,
    sample_prefix: str | None = None,
    experiment_date: str | None = None,
) -> dict:
    """Build a deterministic user-defined advisory plan with no material defaults.

    Duplicate factor levels are removed transparently before replicate expansion. The
    maximum-run cap is a refusal boundary, never a silent truncation. Measurement columns
    are present and blank because this function plans work; it produces no outcomes.
    """
    material = str(material_id or "").strip()
    research_goal = str(goal or "").strip()
    if not material:
        raise ExperimentPlanError("active material identity is required")
    if not research_goal:
        raise ExperimentPlanError("research goal is required")
    if isinstance(replicates, bool) or not isinstance(replicates, int) or replicates < 1:
        raise ExperimentPlanError("replicates must be a positive integer")
    if isinstance(max_run_count, bool) or not isinstance(max_run_count, int) or max_run_count < 1:
        raise ExperimentPlanError("maximum run count must be a positive integer")

    factor_levels, raw_condition_count = _validated_factors(factors)
    fixed = _validated_mapping(fixed_conditions, "fixed_conditions")
    collisions = set(factor_levels) & set(fixed)
    if collisions:
        raise ExperimentPlanError(
            f"factor and fixed-condition names overlap: {sorted(collisions)}")

    factor_names = list(factor_levels)
    conditions = [dict(zip(factor_names, values, strict=True))
                  for values in product(*(factor_levels[name] for name in factor_names))]
    # The level-level dedup above normally makes these unique; this second identity
    # guard keeps nested JSON-equivalent values honest.
    unique_conditions = []
    seen_conditions = set()
    for condition in conditions:
        combined = {**fixed, **condition}
        key = _json_key(combined)
        if key not in seen_conditions:
            seen_conditions.add(key)
            unique_conditions.append({"control": "", "conditions": combined})

    cleaned_controls = []
    for index, raw in enumerate(controls or []):
        if isinstance(raw, str):
            name = raw.strip()
            conditions_value = {}
        elif isinstance(raw, dict):
            name = str(raw.get("name") or raw.get("control") or "").strip()
            conditions_value = _validated_mapping(raw.get("conditions") or {},
                                                  f"controls[{index}].conditions")
        else:
            raise ExperimentPlanError("controls must be names or objects with name/conditions")
        if not name:
            raise ExperimentPlanError("every control requires an explicit name")
        cleaned_controls.append({"control": name, "conditions": {**fixed, **conditions_value}})

    planned_conditions = [*unique_conditions, *cleaned_controls]
    requested_runs = len(planned_conditions) * replicates
    if requested_runs > max_run_count:
        raise ExperimentPlanLimitError(
            f"requested {requested_runs} runs exceeds the explicit cap of {max_run_count}")

    prefix = _safe_prefix(sample_prefix)
    rows = []
    for condition_index, item in enumerate(planned_conditions, start=1):
        for replicate in range(1, replicates + 1):
            row = {
                "sample_id": _condition_id(
                    prefix, {"control": item["control"], **item["conditions"]},
                    replicate, experiment_date),
                "design_mode": "generic_user_defined",
                "material_id": material,
                "goal": research_goal,
                "condition_index": condition_index,
                "replicate": replicate,
                "control": item["control"],
                "experiment_date": str(experiment_date or ""),
                **item["conditions"],
            }
            row.update({column: "" for column in GENERIC_MEASUREMENT_COLUMNS})
            rows.append(row)

    leading = ["sample_id", "design_mode", "material_id", "goal", "condition_index",
               "replicate", "control", "experiment_date"]
    condition_columns = sorted(set(fixed) | set(factor_levels)
                               | {key for item in cleaned_controls
                                  for key in item["conditions"]})
    columns = [*leading, *condition_columns, *GENERIC_MEASUREMENT_COLUMNS]
    frame = pd.DataFrame(rows).reindex(columns=columns, fill_value="").fillna("")
    return {
        "mode": "generic_user_defined",
        "goal": research_goal,
        "material_id": material,
        "factors": factor_levels,
        "fixed_conditions": fixed,
        "controls": cleaned_controls,
        "replicates": replicates,
        "max_run_count": max_run_count,
        "raw_condition_count": raw_condition_count,
        "unique_factor_conditions": len(unique_conditions),
        "duplicate_conditions_removed": raw_condition_count - len(unique_conditions),
        "control_conditions": len(cleaned_controls),
        "run_count": len(frame),
        "measurement_columns": list(GENERIC_MEASUREMENT_COLUMNS),
        "plan": frame,
        "assumptions": [
            "User supplied every factor level and fixed condition.",
            "This deterministic full-factorial plan is advisory and makes no optimum or outcome claim.",
            "Measurement columns are intentionally blank until physical experiments are run.",
        ],
    }


def build_cfa_preset_advisory(*, experiment_date: str | None = None,
                              max_run_count: int | None = None) -> dict:
    """Expose the historical CFA leaching plan only as an explicit named preset."""
    stats = plan_dedup_stats(experiment_date=experiment_date)
    if max_run_count is not None:
        if isinstance(max_run_count, bool) or not isinstance(max_run_count, int) \
                or max_run_count < 1:
            raise ExperimentPlanError("maximum run count must be a positive integer")
        if stats["n_unique"] > max_run_count:
            raise ExperimentPlanLimitError(
                f"CFA preset needs {stats['n_unique']} runs, above cap {max_run_count}")
    return {
        "mode": "cfa_leaching_preset",
        "goal": "Class C fly ash NaOH leaching screening preset",
        "material_id": None,
        "raw_condition_count": stats["n_raw"],
        "unique_factor_conditions": stats["n_unique"],
        "duplicate_conditions_removed": stats["n_removed"],
        "run_count": stats["n_unique"],
        "measurement_columns": [column for column in _RELEASE_TAIL
                                if column not in {"experiment_date", "fly_ash_type", "NaOH_M",
                                                  "time_min", "temperature_C",
                                                  "liquid_solid_ratio", "CO2_condition"}],
        "plan": stats["plan"],
        "assumptions": [
            "Explicit CFA/NaOH leaching preset; not a universal material design.",
            "Preset factors are 0.5 M NaOH, 60 min, 25 C, liquid/solid ratio 5, open-air cover unless a named series varies them.",
            "Measurement columns are intentionally blank until physical experiments are run.",
            "The plan is advisory and makes no optimum or guaranteed-result claim.",
        ],
    }


def _spreadsheet_safe(value):
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if not stripped:
        return value
    numeric_text = False
    try:
        numeric_text = Decimal(stripped).is_finite()
    except (InvalidOperation, ValueError):
        pass
    if stripped.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) \
            and not numeric_text:
        return "'" + value
    return value


def plan_to_csv(plan: pd.DataFrame) -> str:
    """Export a plan safely without mutating its internal scientific values."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=[str(column) for column in plan.columns])
    writer.writeheader()
    for row in plan.to_dict(orient="records"):
        writer.writerow({key: _spreadsheet_safe(value) for key, value in row.items()})
    return buffer.getvalue()


def plan_to_json(plan_result: dict) -> str:
    payload = {key: value for key, value in plan_result.items() if key != "plan"}
    payload["rows"] = plan_result["plan"].where(pd.notna(plan_result["plan"]), None).to_dict(
        orient="records")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
