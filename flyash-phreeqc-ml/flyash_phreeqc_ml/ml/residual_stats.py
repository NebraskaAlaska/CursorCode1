"""Per-element / per-condition **systematic-bias** estimates (statistics, not learning).

This is the *first* "residual correction" step the project takes, and it is
deliberately the most modest one: plain descriptive statistics of the
``measured − model`` residuals, computed **only** over mappings the inclusion
module classifies as ``exact``. No model is trained, no sklearn is used — this is
pandas/numpy arithmetic that must read honestly at ``n = 5`` rather than
impressively.

What it produces
----------------
:func:`bias_table` returns one row per ``(element, condition_key)`` plus a pooled
``(element, all-conditions)`` row, each with the number of exact-mapped pairs, the
mean residual, its sample std and standard error, and a ``sufficient`` flag (the
count reached ``min_n``). Below ``min_n`` the row is kept with ``sufficient=False``
so the UI can say *"insufficient exact pairs (k of N needed)"* instead of showing a
number that would over-claim.

Sign convention (inherited from the comparison): ``residual = measured − model``.
A **positive** mean means the measurement is higher than the model, i.e. the model
**under**predicts; a negative mean means it **over**predicts. :func:`describe_bias_row`
turns that into the caption wording.

Honesty guard
-------------
Only ``exact`` rows are counted; scenario-level, unsafe and unmapped rows are
excluded (their inclusion status is taken from the single source of truth, the
:mod:`flyash_phreeqc_ml.compare.inclusion` status join). Synthetic/demo rows are
never counted even if a test harness mapped them ``exact``. And every rendering of
the table carries :data:`NON_CLAIM_LINE` — these numbers describe *this dataset's*
exact comparisons, they are not a general correction model.
"""
from __future__ import annotations

import math

import pandas as pd

from .. import config, profiles, replicates

# Minimum exact-mapped pairs before a mean bias is shown as a number (not a guess).
DEFAULT_MIN_N = 5

# Label for the pooled "every condition together" row in the bias table.
ALL_CONDITIONS = "(all conditions)"

# Source-type tag stamped on synthetic_demo rows (see run_manager.SYNTHETIC_SOURCE_TAG).
# Re-declared here to keep this module free of any I/O-layer import; kept in sync.
SYNTHETIC_SOURCE_TAG = "synthetic_demo"

BIAS_TABLE_COLUMNS = [
    "element", "condition_key", "n_exact_pairs",
    "mean_residual", "std", "sem", "sufficient", "unit",
]

# Shown whenever the bias table renders. The single most important non-claim.
NON_CLAIM_LINE = (
    "Bias estimates describe this dataset's exact-mapped comparisons; "
    "they are not a general correction model."
)


def element_specs(profile=None) -> list[tuple[str, str, str]]:
    """``(element_label, residual_column, unit)`` for each comparable variable.

    The ICP elements come from ``config.RESIDUAL_ELEMENTS`` (mM); pH is appended
    with its own unit. ``profile`` is accepted for symmetry with the rest of the
    pipeline; the fly-ash residual columns are the default.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    specs = [(el, f"residual_{el}", "mM") for el in config.RESIDUAL_ELEMENTS]
    specs.append(("pH", "residual_pH", "pH units"))
    return specs


def _coerce_statuses(statuses) -> dict[str, str]:
    """Accept a dict / Series / [sample_id, mapping_status] frame → ``{sample_id: status}``."""
    if statuses is None:
        return {}
    if isinstance(statuses, dict):
        return {str(k).strip(): v for k, v in statuses.items()}
    if isinstance(statuses, pd.Series):
        return {str(k).strip(): v for k, v in statuses.items()}
    if isinstance(statuses, pd.DataFrame):
        if {"sample_id", "mapping_status"}.issubset(statuses.columns):
            return {str(r["sample_id"]).strip(): r["mapping_status"]
                    for _, r in statuses.iterrows()}
        raise ValueError("statuses DataFrame needs columns ['sample_id', 'mapping_status']")
    raise TypeError(f"unsupported statuses type: {type(statuses)!r}")


def exact_mask(comparison_df: pd.DataFrame, statuses) -> pd.Series:
    """Boolean row mask: status is ``exact`` **and** the row is not synthetic/demo."""
    status_map = _coerce_statuses(statuses)
    sid = comparison_df["sample_id"].astype(str).str.strip()
    mask = sid.map(lambda s: status_map.get(s) == replicates.MAPPING_STATUS_EXACT).astype(bool)
    if "source_type" in comparison_df.columns:
        not_synthetic = (
            comparison_df["source_type"].astype(str).str.strip() != SYNTHETIC_SOURCE_TAG
        )
        mask = mask & not_synthetic
    return mask


def _with_condition_key(df: pd.DataFrame, profile) -> pd.DataFrame:
    """Ensure a ``condition_key`` column exists (annotate from metadata if absent)."""
    if replicates.CONDITION_KEY_COLUMN in df.columns:
        return df
    return replicates.annotate(df, profile)


def _stat_row(element: str, condition_key: str, series: pd.Series,
              unit: str, min_n: int) -> dict:
    """One bias-table row: count, mean, sample std (ddof=1), sem, sufficiency."""
    vals = pd.to_numeric(series, errors="coerce").dropna()
    n = int(len(vals))
    mean = float(vals.mean()) if n else float("nan")
    std = float(vals.std(ddof=1)) if n >= 2 else float("nan")
    sem = float(std / math.sqrt(n)) if n >= 2 else float("nan")
    return {
        "element": element,
        "condition_key": condition_key,
        "n_exact_pairs": n,
        "mean_residual": mean,
        "std": std,
        "sem": sem,
        "sufficient": bool(n >= min_n),
        "unit": unit,
    }


def bias_table(comparison_df: pd.DataFrame, statuses, min_n: int = DEFAULT_MIN_N,
               *, profile=None) -> pd.DataFrame:
    """Per-element, per-condition mean-residual bias over **exact** mappings only.

    Parameters
    ----------
    comparison_df: the per-run comparison CSV (measured joined to model predictions,
        carrying ``residual_<X>`` columns). ``condition_key`` is derived from the
        row metadata if not already present.
    statuses: the inclusion module's status join (``{sample_id: mapping_status}``,
        a Series, or a ``[sample_id, mapping_status]`` frame). Only ``exact`` rows
        are counted.
    min_n: the count at/above which a row is ``sufficient`` (default 5).

    Returns one row per ``(element, condition_key)`` and one pooled
    ``(element, ALL_CONDITIONS)`` row, in ``BIAS_TABLE_COLUMNS``. An element with no
    exact, non-null residual contributes no rows.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    if (comparison_df is None or comparison_df.empty
            or "sample_id" not in comparison_df.columns):
        return pd.DataFrame(columns=BIAS_TABLE_COLUMNS)

    df = _with_condition_key(comparison_df.copy(), profile)
    exact = df[exact_mask(df, statuses).values].copy()

    rows: list[dict] = []
    for element, residual_col, unit in element_specs(profile):
        if residual_col not in exact.columns:
            continue
        sub = pd.DataFrame({
            "condition_key": exact[replicates.CONDITION_KEY_COLUMN].astype(str),
            "residual": pd.to_numeric(exact[residual_col], errors="coerce"),
        }).dropna(subset=["residual"])
        if sub.empty:
            continue
        for ck, grp in sub.groupby("condition_key", sort=True):
            rows.append(_stat_row(element, ck, grp["residual"], unit, min_n))
        rows.append(_stat_row(element, ALL_CONDITIONS, sub["residual"], unit, min_n))

    return pd.DataFrame(rows, columns=BIAS_TABLE_COLUMNS)


def exact_residuals(comparison_df: pd.DataFrame, statuses, element: str,
                    *, profile=None) -> pd.DataFrame:
    """Tidy ``[sample_id, condition_key, residual]`` for one element, exact rows only.

    The plot-ready companion to :func:`bias_table` — the points a mean±std band is
    drawn over.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    residual_col = f"residual_{element}"
    empty = pd.DataFrame(columns=["sample_id", "condition_key", "residual"])
    if (comparison_df is None or comparison_df.empty
            or "sample_id" not in comparison_df.columns
            or residual_col not in comparison_df.columns):
        return empty
    df = _with_condition_key(comparison_df.copy(), profile)
    exact = df[exact_mask(df, statuses).values]
    if exact.empty:
        return empty
    out = pd.DataFrame({
        "sample_id": exact["sample_id"].astype(str),
        "condition_key": exact[replicates.CONDITION_KEY_COLUMN].astype(str),
        "residual": pd.to_numeric(exact[residual_col], errors="coerce"),
    }).dropna(subset=["residual"]).reset_index(drop=True)
    return out


def bias_direction(mean) -> str | None:
    """``"under"`` if the model underpredicts (mean>0), ``"over"`` if over, else None."""
    if mean is None:
        return None
    try:
        if pd.isna(mean) or float(mean) == 0.0:
            return None
    except (TypeError, ValueError):  # pragma: no cover - non-numeric guard
        return None
    return "under" if float(mean) > 0 else "over"


def describe_bias_row(row, min_n: int = DEFAULT_MIN_N) -> str:
    """Caption for one bias-table row (or the insufficiency message below ``min_n``).

    Wording follows the sign convention: positive mean → model **under**predicts.
    """
    element = row["element"]
    cond = row["condition_key"]
    cond_label = "all conditions" if cond == ALL_CONDITIONS else cond
    n = int(row["n_exact_pairs"])

    if not bool(row["sufficient"]):
        return (f"{element} under {cond_label}: insufficient exact pairs "
                f"({n} of {min_n} needed).")

    mean = float(row["mean_residual"])
    unit = row.get("unit", "mM")
    std = row.get("std")
    std_txt = "—" if std is None or pd.isna(std) else f"{abs(float(std)):.3g}"
    direction = bias_direction(mean)
    if direction is None:
        return (f"Across {n} exact-mapped pairs, the model shows no net bias in "
                f"{element} ({mean:+.3g} {unit}) under {cond_label}.")
    verb = "underpredicts" if direction == "under" else "overpredicts"
    return (f"Across {n} exact-mapped pairs, the model {verb} {element} by "
            f"{abs(mean):.3g} ± {std_txt} {unit} under {cond_label}.")


def sufficient_bias_bands(table: pd.DataFrame) -> dict[str, dict]:
    """Pooled mean±std per element where the all-conditions row is ``sufficient``.

    Drives the shaded residual band: ``{element: {mean, std, sem, n, unit}}``. Only
    elements whose pooled estimate reached ``min_n`` appear — below that there is no
    band to draw.
    """
    out: dict[str, dict] = {}
    if table is None or table.empty:
        return out
    pooled = table[table["condition_key"] == ALL_CONDITIONS]
    for _, r in pooled.iterrows():
        if bool(r["sufficient"]):
            out[r["element"]] = {
                "mean": float(r["mean_residual"]),
                "std": float(r["std"]),
                "sem": float(r["sem"]),
                "n": int(r["n_exact_pairs"]),
                "unit": r.get("unit", "mM"),
            }
    return out


def collect_sample_statuses(data, mapping, comparison_df, *, manifest=None,
                            profile=None) -> dict[str, str]:
    """Build ``{sample_id: mapping_status}`` via the inclusion module (single source).

    Mapping status is variable-independent, so one variable's inclusion partition
    (plotted ∪ excluded) covers every comparison row. Importing the inclusion module
    here keeps the status classifier in exactly one place.
    """
    from ..compare import inclusion  # local import avoids any import-order surprises

    profile = profile or profiles.FLY_ASH_PROFILE
    spec = profile.comparison_variable_spec
    if not spec:
        return {}
    variable = next(iter(spec))
    inc = inclusion.comparison_inclusion(data, mapping, comparison_df, variable,
                                         manifest=manifest, profile=profile)
    out: dict[str, str] = {}
    for frame in (inc.get("plotted"), inc.get("excluded")):
        if frame is not None and not frame.empty:
            for _, r in frame.iterrows():
                sid = str(r.get("sample_id", "")).strip()
                if sid:
                    out[sid] = r.get("mapping_status")
    return out
