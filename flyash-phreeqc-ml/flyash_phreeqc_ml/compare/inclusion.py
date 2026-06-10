"""Explicit inclusion logic for the measured-vs-model comparison (one source).

The comparison display must be honest about *which* measured rows actually make it
into a plot and *why* the rest are excluded. All of that is decided **here**, in the
single function :func:`comparison_inclusion`. The plots and the counts panel consume
its output — they must never re-derive their own filters.

For a chosen ``variable`` it classifies every measured row into exactly one of:

* **plotted** — mapped, status acceptable (exact / scenario-level by default; unsafe
  only when ``include_unsafe``), and both a measured value and a model prediction
  exist; or
* **excluded**, with exactly one reason, in this priority order:
  ``no saved mapping`` → ``mapping is unsafe (excluded by default)`` →
  ``model prediction missing this variable`` → ``measured value missing/non-numeric``.

It also joins the four mapping statuses (via :func:`replicates.mapping_status`) onto
the rows, flags the scenario-level *collapse* case, and picks one overall validity
status by explicit, documented rules (see ``docs/comparison_inclusion.md``). The
``valid`` status is the **only** one that implies the model was validated.

Pure — no Streamlit, no plotting. Operates on the frames handed in.
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from .. import profiles, replicates

# variable -> (measured_col, model_prediction_col). The fly-ash spec is the default,
# sourced from the dataset profile (the single source of truth) so another dataset can
# pass its own profile without editing this module.
VARIABLE_SPEC = profiles.FLY_ASH_PROFILE.comparison_variable_spec

# Exclusion reasons (exactly one per excluded row), in priority order.
REASON_NO_MAPPING = "no saved mapping"
REASON_UNSAFE = "mapping is unsafe (excluded by default)"
REASON_NO_PREDICTION = "model prediction missing this variable"
REASON_NO_MEASURED = "measured value missing/non-numeric"
REASONS = [REASON_NO_MAPPING, REASON_UNSAFE, REASON_NO_PREDICTION, REASON_NO_MEASURED]

EXCLUDED_COLUMNS = ["sample_id", "condition_key", "phreeqc_record_key",
                    "mapping_status", "reason"]
PLOTTED_COLUMNS = ["sample_id", "condition_key", "phreeqc_record_key", "mapping_status",
                   "measured", "predicted", "residual", "flagged"]

# Overall validity statuses. Only VALID implies the model was validated.
VALIDITY_VALID = "valid"
VALIDITY_PRELIMINARY = "preliminary"
VALIDITY_SINGLE_SAMPLE = "single-sample"
VALIDITY_UNSAFE = "unsafe"
VALIDITY_NEEDS_NEW = "needs new simulations"
VALIDITY_NONE = "nothing to compare"

# st.* method names for the app to render each validity line at the right severity.
VALIDITY_SEVERITY = {
    VALIDITY_VALID: "success",
    VALIDITY_PRELIMINARY: "warning",
    VALIDITY_SINGLE_SAMPLE: "warning",
    VALIDITY_UNSAFE: "error",
    VALIDITY_NEEDS_NEW: "error",
    VALIDITY_NONE: "info",
}

DEFAULT_MIN_VALID_ROWS = 3
# Collapse triggers when plotted rows reuse few predictions: ratio of distinct
# predictions to plotted rows <= COLLAPSE_RATIO, or any one prediction reused >=
# COLLAPSE_REUSE times.
COLLAPSE_RATIO = 0.5
COLLAPSE_REUSE = 3

COLLAPSE_MESSAGE = (
    "Many measured rows map to few model predictions — this comparison is scenario-level; "
    "new simulations are likely needed for per-condition validation."
)


def _num(value):
    """Best-effort float, or None for blank/non-numeric/NaN."""
    n = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return None if pd.isna(n) else float(n)


def _blank_key(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):  # pragma: no cover
        pass
    s = str(value).strip()
    return s == "" or s.lower() == "nan"


def _validity_message(validity: str, *, rows_plotted: int, min_valid_rows: int,
                      n_unsafe_plotted: int) -> str:
    if validity == VALIDITY_VALID:
        return (f"Validated comparison: all {rows_plotted} plotted mapping(s) are exact "
                f"(≥{min_valid_rows} rows).")
    if validity == VALIDITY_PRELIMINARY:
        return ("Preliminary comparison — non-exact (scenario-level) mappings are included, "
                "so this is a workflow check, not model validation.")
    if validity == VALIDITY_SINGLE_SAMPLE:
        return ("Single-sample comparison — only one mapped condition is plotted; "
                "not a trend or model validation.")
    if validity == VALIDITY_UNSAFE:
        return (f"Unsafe mapping(s) toggled in ({n_unsafe_plotted}) and shown in red — these are "
                "known metadata conflicts, not valid model comparisons.")
    if validity == VALIDITY_NEEDS_NEW:
        return ("Nothing is plottable for this variable yet — conditions need mappings or new "
                "simulations (see the Match PHREEQC tab).")
    return "No measured/predicted pairs are available for this variable."


def comparison_inclusion(data: pd.DataFrame, mapping, comparison_df: pd.DataFrame,
                         variable: str, *, manifest=None, include_unsafe: bool = False,
                         min_valid_rows: int = DEFAULT_MIN_VALID_ROWS, profile=None) -> dict:
    """Decide what is plotted vs excluded for ``variable`` (the only inclusion logic).

    Parameters
    ----------
    data: the run's measured rows (used for condition_key + the sample metadata that
        drives :func:`replicates.mapping_status`).
    mapping: the run's per-sample ``sample_id -> phreeqc_record_key`` map.
    comparison_df: the per-run comparison CSV (measured joined to model predictions).
    variable: a key of the profile's comparison variable spec (e.g. ``final_pH`` / ``Ca_mM``).
    manifest: the model scenario manifest (so each row's status can be classified).
    include_unsafe: when True, unsafe mappings are plotted (flagged), not excluded.
    profile: a :class:`profiles.DatasetProfile` selecting the variable spec + grouping;
        defaults to the fly-ash profile so existing callers are unchanged.

    Returns a dict with the counts, a ``plotted`` frame, an ``excluded`` frame (one
    reason each — the two partition the comparison rows so counts always add up), the
    per-row status counts, the collapse flag/message, and the overall ``validity``.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    spec = profile.comparison_variable_spec.get(variable)

    # Per-sample condition_key + metadata (for status), from the run's own data.
    ann = (replicates.annotate(data, profile)
           if data is not None and not data.empty else pd.DataFrame())
    ck_by_id: dict[str, str] = {}
    sample_by_id: dict[str, dict] = {}
    if not ann.empty and "sample_id" in ann.columns:
        for _, r in ann.iterrows():
            sid = str(r.get("sample_id", "")).strip()
            if sid:
                ck_by_id[sid] = r.get(replicates.CONDITION_KEY_COLUMN, "")
                sample_by_id[sid] = r.to_dict()

    preds: dict[str, dict] = {}
    if manifest is not None and not manifest.empty and "phreeqc_record_key" in manifest.columns:
        for _, r in manifest.iterrows():
            preds[str(r.get("phreeqc_record_key", "")).strip()] = r.to_dict()

    smap: dict[str, str] = {}
    if mapping is not None and not mapping.empty and "sample_id" in mapping.columns:
        for _, m in mapping.iterrows():
            sid = str(m.get("sample_id", "")).strip()
            key = str(m.get("phreeqc_record_key", "")).strip()
            if sid and key and key.lower() != "nan":
                smap[sid] = key

    if comparison_df is None:
        comparison_df = pd.DataFrame()

    plotted_rows: list[dict] = []
    excluded_rows: list[dict] = []
    reason_counts = {r: 0 for r in REASONS}
    status_counts = {s: 0 for s in (
        replicates.MAPPING_STATUS_EXACT, replicates.MAPPING_STATUS_SCENARIO,
        replicates.MAPPING_STATUS_UNSAFE, replicates.MAPPING_STATUS_NEEDS_NEW)}
    n_measured = n_mapped = n_pred = 0

    for _, row in comparison_df.iterrows():
        rd = row.to_dict()
        sid = str(rd.get("sample_id", "")).strip()
        ck = ck_by_id.get(sid, "")

        # Saved mapping: prefer the mapping table, fall back to the comparison's key.
        rk = smap.get(sid, "")
        if not rk and not _blank_key(rd.get("phreeqc_record_key")):
            rk = str(rd.get("phreeqc_record_key")).strip()
        mapped = bool(rk)

        # Status via the single classifier. Reconstruct a minimal scenario if the
        # manifest lacks the key (so acid/CO2 checks still run).
        scenario = preds.get(rk) if rk else None
        if rk and scenario is None:
            scenario = {"state": rd.get("phreeqc_state", ""),
                        "source_file": rd.get("phreeqc_source_file", ""),
                        "CO2_condition": ""}
        sample = sample_by_id.get(sid, rd)
        status = replicates.mapping_status(sample, scenario, profile)
        status_counts[status] = status_counts.get(status, 0) + 1

        measured = _num(rd.get(spec[0])) if spec else _num(rd.get(variable))
        predicted = _num(rd.get(spec[1])) if spec else None
        if measured is not None:
            n_measured += 1
        if mapped:
            n_mapped += 1
        if mapped and predicted is not None:
            n_pred += 1

        # Exactly one outcome, in priority order.
        if not mapped:
            reason = REASON_NO_MAPPING
        elif status == replicates.MAPPING_STATUS_UNSAFE and not include_unsafe:
            reason = REASON_UNSAFE
        elif predicted is None:
            reason = REASON_NO_PREDICTION
        elif measured is None:
            reason = REASON_NO_MEASURED
        else:
            reason = None

        if reason is None:
            plotted_rows.append({
                "sample_id": sid, "condition_key": ck, "phreeqc_record_key": rk,
                "mapping_status": status, "measured": measured, "predicted": predicted,
                "residual": measured - predicted,
                "flagged": status == replicates.MAPPING_STATUS_UNSAFE,
            })
        else:
            reason_counts[reason] += 1
            excluded_rows.append({
                "sample_id": sid, "condition_key": ck, "phreeqc_record_key": rk,
                "mapping_status": status, "reason": reason,
            })

    plotted = pd.DataFrame(plotted_rows, columns=PLOTTED_COLUMNS)
    excluded = pd.DataFrame(excluded_rows, columns=EXCLUDED_COLUMNS)

    n_total = int(len(comparison_df))
    rows_plotted = int(len(plotted))
    rk_used = plotted["phreeqc_record_key"].tolist() if not plotted.empty else []
    unique_pred = len(set(rk_used))
    max_reuse = max(Counter(rk_used).values()) if rk_used else 0
    n_unsafe_plotted = int(plotted["flagged"].sum()) if not plotted.empty else 0

    collapse = rows_plotted >= 2 and (
        (unique_pred / rows_plotted) <= COLLAPSE_RATIO or max_reuse >= COLLAPSE_REUSE)

    plotted_statuses = set(plotted["mapping_status"]) if not plotted.empty else set()
    if rows_plotted == 0:
        validity = VALIDITY_NEEDS_NEW if n_measured > 0 else VALIDITY_NONE
    elif include_unsafe and replicates.MAPPING_STATUS_UNSAFE in plotted_statuses:
        validity = VALIDITY_UNSAFE
    elif rows_plotted == 1:
        validity = VALIDITY_SINGLE_SAMPLE
    elif plotted_statuses == {replicates.MAPPING_STATUS_EXACT} and rows_plotted >= min_valid_rows:
        validity = VALIDITY_VALID
    else:
        validity = VALIDITY_PRELIMINARY

    return {
        "variable": variable,
        "include_unsafe": bool(include_unsafe),
        "n_total": n_total,
        "measured_rows_available": int(n_measured),
        "rows_with_mapping": int(n_mapped),
        "rows_prediction_available": int(n_pred),
        "rows_plotted": rows_plotted,
        "unique_predictions_used": int(unique_pred),
        "max_prediction_reuse": int(max_reuse),
        "unmapped_rows": int(n_total - n_mapped),
        "plotted": plotted,
        "excluded": excluded,
        "reason_counts": reason_counts,
        "status_counts": status_counts,
        "n_unsafe_plotted": n_unsafe_plotted,
        "collapse_warning": bool(collapse),
        "collapse_message": COLLAPSE_MESSAGE if collapse else "",
        "validity": validity,
        "validity_message": _validity_message(
            validity, rows_plotted=rows_plotted, min_valid_rows=min_valid_rows,
            n_unsafe_plotted=n_unsafe_plotted),
        "validity_severity": VALIDITY_SEVERITY[validity],
        "min_valid_rows": int(min_valid_rows),
    }
