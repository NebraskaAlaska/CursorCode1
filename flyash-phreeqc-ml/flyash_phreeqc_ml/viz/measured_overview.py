"""Measured-data overview — the *first* plot family (data-only, no model).

This prepares the data for a "measured data only — no model comparison" view that
renders from a run's **own measured rows alone**, before (and without) any
sample→PHREEQC mapping or ``phreeqc_results.csv``. It is the counterpart to the
comparison plots in :mod:`compare_plots`, which need measured + model + a mapping.

The module is **pure** — it does the data preparation and returns plain
DataFrames/scalars in a dict; the Streamlit/matplotlib rendering lives in ``app.py``.
It reuses :func:`replicates.annotate` for the condition/replicate grouping so the
overview groups exactly the same way the rest of the app does, and generalises
:func:`replicates.replicate_summary`'s mean ± std (ddof=1, NaN for a single
replicate) to *any* measured variable.
"""
from __future__ import annotations

import pandas as pd

from .. import profiles, replicates

# Candidate measured variables, in display order, sourced from the fly-ash dataset
# profile (the single source of truth). Only those actually present with numeric data
# are offered (see :func:`available_variables`) — empty columns are never listed.
OVERVIEW_VARIABLES = list(profiles.FLY_ASH_PROFILE.overview_variables)

# Default measured-time column (overridden by a profile's ``time_column``).
TIME_COLUMN = profiles.FLY_ASH_PROFILE.time_column or "time_min"

# Tidy plot-frame columns (``time_min`` is appended only when a numeric time exists).
PLOT_COLUMNS = ["sample_id", "condition_key", "replicate_id", "value"]
EXCLUDED_COLUMNS = ["sample_id", "condition_key", "value_raw", "reason"]
GROUP_STAT_COLUMNS = ["condition_key", "n", "mean", "std", "sem"]


def _is_blank(value) -> bool:
    """A cell is blank if NaN/None or empty after stripping (reuse-friendly)."""
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):  # pragma: no cover - non-scalar guard
        pass
    return str(value).strip() == ""


def _has_numeric(series: pd.Series) -> bool:
    return bool(pd.to_numeric(series, errors="coerce").notna().any())


def available_variables(data: pd.DataFrame, profile=None) -> list[str]:
    """Measured variables present in ``data`` with at least one numeric value.

    The candidate list + order come from the dataset ``profile`` (fly ash by default:
    ``final_pH`` first, then the ICP columns). Columns that are absent or
    all-blank/non-numeric are omitted, so the user never picks an empty variable.
    """
    if data is None or data.empty:
        return []
    candidates = (profile or profiles.FLY_ASH_PROFILE).overview_variables
    return [c for c in candidates if c in data.columns and _has_numeric(data[c])]


def _empty_overview(variable: str) -> dict:
    return {
        "variable": variable,
        "plot": pd.DataFrame(columns=PLOT_COLUMNS),
        "excluded": pd.DataFrame(columns=EXCLUDED_COLUMNS),
        "group_stats": pd.DataFrame(columns=GROUP_STAT_COLUMNS),
        "has_time": False,
        "n_shown": 0,
        "n_excluded": 0,
        "n_conditions": 0,
        "replicate_counts": {},
    }


def prepare_overview(data: pd.DataFrame, variable: str, profile=None) -> dict:
    """Prepare the measured-data overview for one ``variable`` (pure, no plotting).

    Returns a dict with:

    * ``plot`` — tidy frame (``sample_id, condition_key, replicate_id, value`` plus
      ``time_min`` when a numeric time exists) of the rows that have a usable value;
    * ``excluded`` — rows dropped because the value is blank/missing or non-numeric,
      each with a human ``reason`` (so the counts always add up:
      ``n_shown + n_excluded == rows with a sample_id``);
    * ``group_stats`` — per-condition ``n / mean / std / sem`` (std ddof=1; ``sem =
      std/√n``; both NaN for a single replicate, never a fake 0) of the kept values;
    * ``has_time`` — whether a numeric ``time_min`` column is present (the app uses
      this to choose a time x-axis vs condition categories);
    * ``n_shown`` / ``n_excluded`` / ``n_conditions`` / ``replicate_counts``.

    Needs nothing but the run's own data — no mapping, no model results.
    """
    if data is None or data.empty or variable not in data.columns:
        return _empty_overview(variable)

    profile = profile or profiles.FLY_ASH_PROFILE
    time_col = profile.time_column or TIME_COLUMN
    ann = replicates.annotate(data, profile)
    has_time = time_col in ann.columns and _has_numeric(ann[time_col])

    plot_rows: list[dict] = []
    excluded_rows: list[dict] = []
    for _, r in ann.iterrows():
        sid = "" if _is_blank(r.get("sample_id")) else str(r.get("sample_id")).strip()
        ck = r.get(replicates.CONDITION_KEY_COLUMN, "")
        raw = r.get(variable)
        if _is_blank(raw):
            excluded_rows.append({"sample_id": sid, "condition_key": ck,
                                  "value_raw": "", "reason": "missing value (blank)"})
            continue
        num = pd.to_numeric(pd.Series([raw]), errors="coerce").iloc[0]
        if pd.isna(num):
            excluded_rows.append({"sample_id": sid, "condition_key": ck,
                                  "value_raw": str(raw),
                                  "reason": f"non-numeric value: {str(raw)!r}"})
            continue
        row = {
            "sample_id": sid,
            "condition_key": ck,
            "replicate_id": r.get(replicates.REPLICATE_ID_COLUMN, ""),
            "value": float(num),
        }
        if has_time:
            row[time_col] = pd.to_numeric(pd.Series([r.get(time_col)]),
                                          errors="coerce").iloc[0]
        plot_rows.append(row)

    plot_cols = PLOT_COLUMNS + ([time_col] if has_time else [])
    plot = pd.DataFrame(plot_rows, columns=plot_cols)
    excluded = pd.DataFrame(excluded_rows, columns=EXCLUDED_COLUMNS)

    # Per-condition mean ± std of the kept values (generalises replicate_summary).
    if plot.empty:
        group_stats = pd.DataFrame(columns=GROUP_STAT_COLUMNS)
        replicate_counts: dict = {}
    else:
        grp = plot.groupby("condition_key")["value"]
        stats = grp.agg(["count", "mean"]).reset_index()
        stats["std"] = grp.std(ddof=1).reset_index(drop=True)
        # SEM = std / √n (NaN where std is NaN, i.e. a single replicate — never a fake 0).
        stats["sem"] = stats["std"] / stats["count"].pow(0.5)
        group_stats = stats.rename(columns={"count": "n"})[GROUP_STAT_COLUMNS]
        replicate_counts = {str(k): int(v) for k, v in grp.count().items()}

    return {
        "variable": variable,
        "plot": plot,
        "excluded": excluded,
        "group_stats": group_stats,
        "has_time": bool(has_time),
        "n_shown": int(len(plot)),
        "n_excluded": int(len(excluded)),
        "n_conditions": int(plot["condition_key"].nunique()) if not plot.empty else 0,
        "replicate_counts": replicate_counts,
    }
