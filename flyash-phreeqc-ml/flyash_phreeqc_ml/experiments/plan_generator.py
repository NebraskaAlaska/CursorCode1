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
