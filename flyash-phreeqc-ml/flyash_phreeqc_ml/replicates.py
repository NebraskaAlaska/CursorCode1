"""Replicate-aware mapping layer (no chemistry, no ML).

In this project PHREEQC ``sol1/sol2/sol3`` are **replicate batches of one
experimental condition**, not time points. So a measured row should be understood
as *(experimental condition, replicate batch)* rather than mapped sample-by-sample
to a PHREEQC solution number. This module adds that layer on top of the existing
sample → PHREEQC mapping without changing any chemistry:

* :func:`condition_key` collapses a measured row's metadata (leachant, molarity,
  OA/PF/GS code, time, L/S, CO2, temperature) into one stable grouping key,
* :func:`replicate_id` reads the replicate/batch number from a ``sample_id``,
* :func:`replicate_summary` reports mean ± std per condition,
* :func:`expand_condition_mapping` lets one ``condition_key → PHREEQC`` link be
  inherited by every replicate row (so the existing per-sample pipeline still
  works), and
* :func:`collision_report` / :func:`condition_mean_comparison` /
  :func:`individual_replicate_comparison` make the mapping safety checks and the
  results comparison replicate-aware (same-condition replicates sharing a PHREEQC
  row is *expected*, not a collision).

Everything is pure (operates on DataFrames/dicts handed in). Predictions come from
the scenario manifest the app already builds; nothing here recomputes PHREEQC.
"""
from __future__ import annotations

import re

import pandas as pd

from . import profiles, scenarios

# Measured columns summarised / compared, mapped to their manifest prediction col.
VALUE_COLUMNS = ["final_pH", "Ca_mM", "Si_mM", "Al_mM"]
PREDICTION_COLUMN = {
    "final_pH": "predicted_pH",
    "Ca_mM": "predicted_Ca_mM",
    "Si_mM": "predicted_Si_mM",
    "Al_mM": "predicted_Al_mM",
}
# Short element label used for phreeqc_/residual_ columns (matches Phase-2 naming:
# residual_pH / residual_Ca / residual_Si / residual_Al).
RESIDUAL_LABEL = {"final_pH": "pH", "Ca_mM": "Ca", "Si_mM": "Si", "Al_mM": "Al"}

CONDITION_KEY_COLUMN = "condition_key"
REPLICATE_ID_COLUMN = "replicate_id"

REPLICATE_SUMMARY_COLUMNS = (
    [CONDITION_KEY_COLUMN, "number_of_replicates", "replicate_ids"]
    + [f"{stat}_{col}" for col in VALUE_COLUMNS for stat in ("mean", "std")]
)

# Replicate / batch token in a sample_id: R1, rep_2, batch-3, replicate 1, …
_REPLICATE_RE = re.compile(r"(?:^|[-_ ])(?:R|REP|REPLICATE|BATCH)\s*[-_]?\s*(\d+)\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _to_float(value):
    return scenarios._to_float(value)


def _num(value) -> str:
    """Compact numeric token: ``0.5``→``0.5``, ``5.0``→``5``, blank for missing."""
    f = _to_float(value)
    if f is None:
        return ""
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return f"{f:g}"


def _is_blank(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):  # pragma: no cover
        pass
    return str(value).strip() == ""


def _is_acid(leachant) -> bool:
    s = str(leachant or "").strip().lower()
    return "hcl" in s or "acid" in s or "hno3" in s or "h2so4" in s


# --------------------------------------------------------------------------- #
# Feature 1 — condition_key + replicate_id
# --------------------------------------------------------------------------- #
def condition_key(sample: dict, profile=None) -> str:
    """Stable grouping key for one experimental condition (replicate-independent).

    With no profile (or the fly-ash profile) the bespoke fly-ash key is built (leachant
    + molarity + OA/PF/GS cover + time + L/S + CO2 + temperature). Any other
    :class:`profiles.DatasetProfile` falls back to a generic key built from that
    profile's ``important_fields``, so the same grouping works for another dataset
    without fly-ash columns.
    """
    profile = profile or profiles.default_dataset_profile()
    if getattr(profile, "grouping", "generic") == "fly_ash":
        return _fly_ash_condition_key(sample)
    return _generic_condition_key(sample, profile)


def _generic_condition_key(sample: dict, profile) -> str:
    """Profile-driven key: ``field=value`` joined over the profile's important fields."""
    parts: list[str] = []
    for f in profile.important_fields:
        v = sample.get(f)
        if _is_blank(v):
            continue
        tok = _num(v) or str(v).strip()
        parts.append(f"{f}={tok}")
    return "_".join(parts) if parts else "unknown_condition"


def _fly_ash_condition_key(sample: dict) -> str:
    """The original fly-ash condition key (unchanged behaviour)."""
    leachant = str(sample.get("leachant", "") or "").strip()
    acid = _is_acid(leachant)
    conc = _num(sample.get("acid_M") if acid else sample.get("NaOH_M"))
    lead = leachant or (("HCl" if acid else "NaOH") if conc else "")
    code = scenarios.sample_condition_code(sample) or ""
    time = _num(sample.get("time_min"))
    ls = _num(sample.get("liquid_solid_ratio"))
    co2 = str(sample.get("CO2_condition", "") or "").strip()
    temp = _num(sample.get("temperature_C"))

    parts: list[str] = []
    if lead or conc:
        parts.append(f"{lead}{conc}M" if conc else lead)
    if code:
        parts.append(code)
    if time:
        parts.append(f"{time}min")
    if ls:
        parts.append(f"LS{ls}")
    # CO2_condition now holds the cup-cover code (OA/PF/GS) for experiment rows,
    # which is already captured by `code` above — only append CO2_condition when it
    # is a distinct (model-side, e.g. atm_CO2/low_CO2/no_CO2) label, so the key never
    # duplicates the cover code.
    if (co2 and co2.lower() not in ("nan", "unknown")
            and co2.strip().upper() not in ("OA", "PF", "GS")):
        parts.append(co2)
    if temp:
        parts.append(f"T{temp}C")
    return "_".join(parts) if parts else "unknown_condition"


def parse_replicate_id(sample_id) -> str:
    """Canonical ``R<n>`` from a sample_id (R1 / rep2 / batch3 / replicate 1), else ''."""
    m = _REPLICATE_RE.search(str(sample_id or ""))
    return f"R{int(m.group(1))}" if m else ""


def replicate_id(sample: dict) -> str:
    """Replicate id from an explicit ``replicate_id`` field, else parsed from sample_id."""
    explicit = sample.get("replicate_id")
    if not _is_blank(explicit):
        return str(explicit).strip()
    return parse_replicate_id(sample.get("sample_id"))


def annotate(df: pd.DataFrame, profile=None) -> pd.DataFrame:
    """Return a copy of ``df`` with ``condition_key`` and ``replicate_id`` columns.

    ``profile`` defaults to the fly-ash profile, so existing callers are unchanged.
    """
    if df is None or df.empty:
        out = pd.DataFrame(columns=list(df.columns) if df is not None else [])
        out[CONDITION_KEY_COLUMN] = []
        out[REPLICATE_ID_COLUMN] = []
        return out
    out = df.copy()
    records = out.to_dict("records")
    out[CONDITION_KEY_COLUMN] = [condition_key(r, profile) for r in records]
    out[REPLICATE_ID_COLUMN] = [replicate_id(r) for r in records]
    return out


def infer_replicate_ids(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Fill blank replicate ids by within-condition order, returning warnings.

    Only rows whose ``replicate_id`` could not be parsed are numbered (R1, R2, …)
    per ``condition_key`` in row order. Each affected condition raises a warning,
    since order-based replicate numbers are a guess, not data.
    """
    ann = annotate(df)
    warnings: list[str] = []
    if ann.empty:
        return ann, warnings
    for ck, g in ann.groupby(CONDITION_KEY_COLUMN):
        blanks = g[g[REPLICATE_ID_COLUMN].apply(_is_blank)]
        if blanks.empty:
            continue
        existing = {r for r in g[REPLICATE_ID_COLUMN] if not _is_blank(r)}
        n = 0
        for idx in blanks.index:
            n += 1
            while f"R{n}" in existing:
                n += 1
            ann.at[idx, REPLICATE_ID_COLUMN] = f"R{n}"
            existing.add(f"R{n}")
        warnings.append(
            f"condition '{ck}': {len(blanks)} row(s) had no replicate id in the sample_id; "
            "assigned R-numbers by row order (verify this is correct)."
        )
    return ann, warnings


# --------------------------------------------------------------------------- #
# Feature 2 — replicate summary
# --------------------------------------------------------------------------- #
def replicate_summary(df: pd.DataFrame, profile=None) -> pd.DataFrame:
    """Per-condition replicate count, ids, and mean ± std of the value columns.

    ``std`` uses ddof=1, so a single-replicate condition has ``std = NaN`` (you
    cannot estimate spread from one batch). ``profile`` selects the grouping (and,
    for a non-fly-ash profile, the value columns); it defaults to the fly-ash profile
    so existing callers are unchanged.
    """
    ann = annotate(df, profile)
    # Fly-ash default keeps the canonical pH/Ca/Si/Al stat columns; a non-fly-ash
    # profile summarises its own variable columns instead.
    if profile is None or getattr(profile, "grouping", "generic") == "fly_ash":
        value_cols = VALUE_COLUMNS
        columns = REPLICATE_SUMMARY_COLUMNS
    else:
        value_cols = [c for c in profile.variable_columns if c in (df.columns if df is not None else [])]
        columns = ([CONDITION_KEY_COLUMN, "number_of_replicates", "replicate_ids"]
                   + [f"{stat}_{c}" for c in value_cols for stat in ("mean", "std")])
    if ann.empty:
        return pd.DataFrame(columns=columns)

    rows: list[dict] = []
    for ck, g in ann.groupby(CONDITION_KEY_COLUMN, sort=True):
        reps = sorted({r for r in g[REPLICATE_ID_COLUMN] if not _is_blank(r)})
        row = {
            CONDITION_KEY_COLUMN: ck,
            "number_of_replicates": int(len(g)),
            "replicate_ids": ", ".join(reps),
        }
        for col in value_cols:
            vals = pd.to_numeric(g[col], errors="coerce") if col in g.columns else pd.Series(dtype=float)
            row[f"mean_{col}"] = vals.mean() if vals.notna().any() else float("nan")
            row[f"std_{col}"] = vals.std(ddof=1) if vals.notna().sum() >= 2 else float("nan")
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


# --------------------------------------------------------------------------- #
# Feature 3 — condition-level mapping inheritance
# --------------------------------------------------------------------------- #
def _mapping_dict(condition_map) -> dict[str, str]:
    """Accept a dict or a [condition_key, phreeqc_record_key] frame -> dict."""
    if condition_map is None:
        return {}
    if isinstance(condition_map, dict):
        return {str(k).strip(): str(v).strip() for k, v in condition_map.items()
                if not _is_blank(k) and not _is_blank(v)}
    out: dict[str, str] = {}
    if isinstance(condition_map, pd.DataFrame) and CONDITION_KEY_COLUMN in condition_map.columns:
        for _, r in condition_map.iterrows():
            ck = str(r.get(CONDITION_KEY_COLUMN, "")).strip()
            key = str(r.get("phreeqc_record_key", "")).strip()
            if ck and key and key.lower() != "nan":
                out[ck] = key
    return out


def expand_condition_mapping(df: pd.DataFrame, condition_map) -> pd.DataFrame:
    """Expand one ``condition_key → record_key`` map to a per-sample mapping.

    Every replicate row whose ``condition_key`` is mapped inherits that PHREEQC
    record_key. Output columns match the sample mapping the pipeline already reads:
    ``[sample_id, phreeqc_record_key]``.
    """
    cmap = _mapping_dict(condition_map)
    ann = annotate(df)
    rows: list[dict] = []
    if not ann.empty and "sample_id" in ann.columns:
        for _, r in ann.iterrows():
            sid = str(r.get("sample_id", "")).strip()
            key = cmap.get(str(r.get(CONDITION_KEY_COLUMN, "")).strip())
            if sid and key:
                rows.append({"sample_id": sid, "phreeqc_record_key": key})
    return pd.DataFrame(rows, columns=["sample_id", "phreeqc_record_key"])


# --------------------------------------------------------------------------- #
# Feature 4 — replicate -> PHREEQC solution number
# --------------------------------------------------------------------------- #
def replicate_record_key(base_record_key: str, solution_number) -> str:
    """Swap the ``solN`` segment of a PHREEQC record_key for a target solution.

    ``file|sim1|batch|sol1`` + ``2`` -> ``file|sim1|batch|sol2``. Used by the
    advanced replicate→solution mapping (R1→sol1, R2→sol2, …).
    """
    parts = str(base_record_key).split("|")
    if parts and parts[-1].lower().startswith("sol"):
        parts[-1] = f"sol{_num(solution_number) or solution_number}"
    return "|".join(parts)


def expand_replicate_solution_mapping(df: pd.DataFrame, condition_map,
                                      replicate_solution: dict) -> pd.DataFrame:
    """Per-sample mapping where each replicate points at its own PHREEQC solution.

    Starts from the condition-level mapping (so the scenario/file is chosen once),
    then for any sample whose ``replicate_id`` is in ``replicate_solution`` (e.g.
    ``{"R1": 1, "R2": 2}``) swaps the record_key's ``solN`` to that solution.
    """
    cmap = _mapping_dict(condition_map)
    rep_sol = {str(k).strip().upper(): v for k, v in (replicate_solution or {}).items()}
    ann = annotate(df)
    rows: list[dict] = []
    if not ann.empty and "sample_id" in ann.columns:
        for _, r in ann.iterrows():
            sid = str(r.get("sample_id", "")).strip()
            base = cmap.get(str(r.get(CONDITION_KEY_COLUMN, "")).strip())
            if not sid or not base:
                continue
            rep = str(r.get(REPLICATE_ID_COLUMN, "")).strip().upper()
            key = replicate_record_key(base, rep_sol[rep]) if rep in rep_sol else base
            rows.append({"sample_id": sid, "phreeqc_record_key": key})
    return pd.DataFrame(rows, columns=["sample_id", "phreeqc_record_key"])


# --------------------------------------------------------------------------- #
# Feature 5/6 — comparison (condition mean & individual replicate)
# --------------------------------------------------------------------------- #
def _manifest_predictions(manifest: pd.DataFrame) -> dict[str, dict]:
    if manifest is None or manifest.empty or "phreeqc_record_key" not in manifest.columns:
        return {}
    out: dict[str, dict] = {}
    for _, r in manifest.iterrows():
        out[str(r.get("phreeqc_record_key", "")).strip()] = r.to_dict()
    return out


def condition_mean_comparison(df: pd.DataFrame, condition_map, manifest: pd.DataFrame) -> pd.DataFrame:
    """Condition-level comparison: replicate mean ± std vs PHREEQC, with residuals.

    For every mapped condition, joins the replicate-mean of pH/Ca/Si/Al to the
    mapped PHREEQC scenario's prediction and computes ``residual = mean − PHREEQC``.
    Flags conditions with fewer than two replicates (no spread estimate).
    """
    summary = replicate_summary(df)
    cmap = _mapping_dict(condition_map)
    preds = _manifest_predictions(manifest)

    cols = (["condition_key", "n_replicates", "phreeqc_record_key"]
            + [f"{s}_{c}" for c in VALUE_COLUMNS for s in ("mean", "std")]
            + [f"phreeqc_{RESIDUAL_LABEL[c]}" for c in VALUE_COLUMNS]
            + [f"residual_{RESIDUAL_LABEL[c]}" for c in VALUE_COLUMNS]
            + ["warning"])
    rows: list[dict] = []
    for _, s in summary.iterrows():
        ck = s[CONDITION_KEY_COLUMN]
        rk = cmap.get(ck, "")
        pred = preds.get(rk, {})
        row = {"condition_key": ck, "n_replicates": int(s["number_of_replicates"]),
               "phreeqc_record_key": rk}
        for c in VALUE_COLUMNS:
            label = RESIDUAL_LABEL[c]
            mean = _to_float(s[f"mean_{c}"])
            pcol = _to_float(pred.get(PREDICTION_COLUMN[c]))
            row[f"mean_{c}"] = s[f"mean_{c}"]
            row[f"std_{c}"] = s[f"std_{c}"]
            row[f"phreeqc_{label}"] = pcol if pcol is not None else float("nan")
            row[f"residual_{label}"] = (mean - pcol) if (mean is not None and pcol is not None) else float("nan")
        warn = []
        if int(s["number_of_replicates"]) < 2:
            warn.append("n_replicates<2 (no std)")
        if not rk:
            warn.append("condition not mapped")
        row["warning"] = "; ".join(warn)
        rows.append(row)
    return pd.DataFrame(rows, columns=cols)


def individual_replicate_comparison(df: pd.DataFrame, sample_mapping: pd.DataFrame,
                                    manifest: pd.DataFrame) -> pd.DataFrame:
    """Per-replicate comparison: each measured row vs its mapped PHREEQC solution."""
    ann = annotate(df)
    preds = _manifest_predictions(manifest)
    smap: dict[str, str] = {}
    if sample_mapping is not None and not sample_mapping.empty and "sample_id" in sample_mapping.columns:
        for _, m in sample_mapping.iterrows():
            smap[str(m.get("sample_id", "")).strip()] = str(m.get("phreeqc_record_key", "")).strip()

    cols = (["sample_id", "condition_key", "replicate_id", "phreeqc_record_key"]
            + [c for c in VALUE_COLUMNS]
            + [f"phreeqc_{RESIDUAL_LABEL[c]}" for c in VALUE_COLUMNS]
            + [f"residual_{RESIDUAL_LABEL[c]}" for c in VALUE_COLUMNS])
    rows: list[dict] = []
    if not ann.empty and "sample_id" in ann.columns:
        for _, r in ann.iterrows():
            sid = str(r.get("sample_id", "")).strip()
            rk = smap.get(sid, "")
            pred = preds.get(rk, {})
            row = {"sample_id": sid, "condition_key": r.get(CONDITION_KEY_COLUMN, ""),
                   "replicate_id": r.get(REPLICATE_ID_COLUMN, ""), "phreeqc_record_key": rk}
            for c in VALUE_COLUMNS:
                label = RESIDUAL_LABEL[c]
                meas = _to_float(r.get(c))
                pcol = _to_float(pred.get(PREDICTION_COLUMN[c]))
                row[c] = r.get(c, "")
                row[f"phreeqc_{label}"] = pcol if pcol is not None else float("nan")
                row[f"residual_{label}"] = (meas - pcol) if (meas is not None and pcol is not None) else float("nan")
            rows.append(row)
    return pd.DataFrame(rows, columns=cols)


# --------------------------------------------------------------------------- #
# Feature 7 — replicate-aware mapping safety
# --------------------------------------------------------------------------- #
def collision_report(df: pd.DataFrame, sample_mapping: pd.DataFrame,
                     manifest: pd.DataFrame | None = None) -> list[dict]:
    """Replicate-aware mapping warnings (pure).

    Same-condition replicates sharing one PHREEQC scenario is **expected** and not
    flagged. Warnings are raised only for:

    * a PHREEQC scenario shared by **different** condition_keys (real collision),
    * an **acid** (HCl) condition mapped to a (NaOH) PHREEQC scenario,
    * time / OA-PF-GS condition metadata the PHREEQC scenario cannot confirm
      (when a ``manifest`` is given).
    """
    warnings: list[dict] = []
    ann = annotate(df)
    if ann.empty or "sample_id" not in ann.columns or sample_mapping is None or sample_mapping.empty:
        return warnings

    by_sample = {str(r.get("sample_id", "")).strip(): r for _, r in ann.iterrows()}
    smap: list[tuple[str, str]] = []
    for _, m in sample_mapping.iterrows():
        sid = str(m.get("sample_id", "")).strip()
        key = str(m.get("phreeqc_record_key", "")).strip()
        if sid and key and key.lower() != "nan":
            smap.append((sid, key))

    # record_key -> set of condition_keys mapped to it.
    record_conditions: dict[str, set[str]] = {}
    preds = _manifest_predictions(manifest) if manifest is not None else {}
    for sid, key in smap:
        row = by_sample.get(sid)
        if row is None:
            continue
        ck = str(row.get(CONDITION_KEY_COLUMN, "")).strip()
        record_conditions.setdefault(key, set()).add(ck)

        if _is_acid(row.get("leachant")):
            warnings.append({
                "type": "acid_to_naoh",
                "sample_id": sid,
                "message": f"acid (HCl) condition '{ck}' is mapped to PHREEQC scenario "
                           f"'{key}' — PHREEQC scenarios here are NaOH; an acid simulation is needed.",
            })
        if preds:
            align = scenarios._metadata_alignment(row.to_dict(), preds.get(key, {}))
            for note in align["metadata_notes"]:
                warnings.append({"type": "metadata", "sample_id": sid, "message": note})

    for key, conds in record_conditions.items():
        if len(conds) > 1:
            warnings.append({
                "type": "cross_condition_collision",
                "phreeqc_record_key": key,
                "message": f"PHREEQC scenario '{key}' is mapped by {len(conds)} different "
                           f"conditions ({', '.join(sorted(conds))}) — these are not replicates "
                           "of one condition, so the comparison would mix conditions.",
            })
    return warnings


# --------------------------------------------------------------------------- #
# Mapping status: exact / scenario-level only / unsafe / needs new simulation
# --------------------------------------------------------------------------- #
MAPPING_STATUS_EXACT = "exact"
MAPPING_STATUS_SCENARIO = "scenario-level only"
MAPPING_STATUS_UNSAFE = "unsafe"
MAPPING_STATUS_NEEDS_NEW = "needs new simulation"

# Presentation-facing definitions, worded generically (measured data ↔ model
# prediction) so the workflow is not specific to one experiment. PHREEQC is the
# current model implementation, surfaced in the advanced views.
MAPPING_STATUS_DEFINITIONS = {
    MAPPING_STATUS_EXACT:
        "Measured record and model prediction metadata match.",
    MAPPING_STATUS_SCENARIO:
        "Broad match, but important metadata are missing.",
    MAPPING_STATUS_UNSAFE:
        "Known metadata conflict between measured data and model prediction.",
    MAPPING_STATUS_NEEDS_NEW:
        "No suitable model/simulation result exists.",
}


def mapping_status(sample: dict, scenario: dict | None, profile=None) -> str:
    """Classify one measured-record→model-prediction mapping into the four statuses.

    * no model prediction → **needs new simulation**;
    * acid (HCl) sample on a (NaOH) scenario, or opposite CO2 families → **unsafe**;
    * prediction aligns broadly but the model lacks time / condition code / concentration
      → **scenario-level only**;
    * otherwise → **exact** (rare with the current PHREEQC files, which is the honest point).

    ``profile`` selects the condition vocab the alignment uses; it defaults to the
    fly-ash profile so existing callers are unchanged. The fly-ash-specific acid/CO2
    conflict checks simply do not fire for a dataset that lacks those columns.
    """
    if not scenario:
        return MAPPING_STATUS_NEEDS_NEW
    if _is_acid(sample.get("leachant")):
        return MAPPING_STATUS_UNSAFE
    sf = scenarios.co2_family(sample.get("CO2_condition"))
    mf = scenarios.co2_family(scenario.get("CO2_condition"))
    if sf != scenarios.UNKNOWN and mf != scenarios.UNKNOWN and sf != mf:
        return MAPPING_STATUS_UNSAFE
    align = scenarios._metadata_alignment(sample, scenario, profile)
    if align["metadata_notes"]:
        return MAPPING_STATUS_SCENARIO
    return MAPPING_STATUS_EXACT


def overall_mapping_status(df: pd.DataFrame, sample_mapping: pd.DataFrame,
                           manifest: pd.DataFrame | None = None) -> dict:
    """Aggregate mapping status across all measured samples (pure).

    Returns ``{"counts": {status: n}, "n_mapped": int, "n_unmapped": int,
    "all_exact": bool, "overall": <worst status present>}``. ``all_exact`` is True
    only when there is at least one mapped row and every mapped row is *exact*.
    """
    counts = {MAPPING_STATUS_EXACT: 0, MAPPING_STATUS_SCENARIO: 0,
              MAPPING_STATUS_UNSAFE: 0, MAPPING_STATUS_NEEDS_NEW: 0}
    ann = annotate(df)
    if ann.empty or "sample_id" not in ann.columns:
        return {"counts": counts, "n_mapped": 0, "n_unmapped": 0,
                "all_exact": False, "overall": MAPPING_STATUS_NEEDS_NEW}

    preds = _manifest_predictions(manifest) if manifest is not None else {}
    smap: dict[str, str] = {}
    if sample_mapping is not None and not sample_mapping.empty and "sample_id" in sample_mapping.columns:
        for _, m in sample_mapping.iterrows():
            sid = str(m.get("sample_id", "")).strip()
            key = str(m.get("phreeqc_record_key", "")).strip()
            if sid and key and key.lower() != "nan":
                smap[sid] = key

    n_mapped = 0
    for _, r in ann.iterrows():
        sid = str(r.get("sample_id", "")).strip()
        key = smap.get(sid)
        scenario = preds.get(key) if key else None
        if key:
            n_mapped += 1
        status = mapping_status(r.to_dict(), scenario)
        counts[status] += 1

    n_unmapped = counts[MAPPING_STATUS_NEEDS_NEW]
    # worst status present, by severity order.
    for status in (MAPPING_STATUS_UNSAFE, MAPPING_STATUS_NEEDS_NEW,
                   MAPPING_STATUS_SCENARIO, MAPPING_STATUS_EXACT):
        if counts[status]:
            overall = status
            break
    else:
        overall = MAPPING_STATUS_NEEDS_NEW
    all_exact = n_mapped > 0 and counts[MAPPING_STATUS_EXACT] == n_mapped
    return {"counts": counts, "n_mapped": n_mapped, "n_unmapped": n_unmapped,
            "all_exact": all_exact, "overall": overall}


CONDITIONS_NEEDED_COLUMNS = [
    "condition_key", "leachant", "NaOH_M", "acid_M", "time_min", "condition_code",
    "liquid_solid_ratio", "CO2_condition", "reason_needed",
]


def conditions_needing_simulation(df: pd.DataFrame, condition_map,
                                  manifest: pd.DataFrame | None = None) -> pd.DataFrame:
    """Presentation table of conditions whose mapping is not exact (or missing).

    One row per ``condition_key`` that is unmapped, mapped *unsafe*ly, or only
    *scenario-level*; with the condition metadata and a ``reason_needed``. Exact
    conditions are omitted.
    """
    ann = annotate(df)
    if ann.empty or CONDITION_KEY_COLUMN not in ann.columns:
        return pd.DataFrame(columns=CONDITIONS_NEEDED_COLUMNS)
    cmap = _mapping_dict(condition_map)
    preds = _manifest_predictions(manifest) if manifest is not None else {}

    rows: list[dict] = []
    for ck, g in ann.groupby(CONDITION_KEY_COLUMN, sort=True):
        rep = g.iloc[0].to_dict()
        key = cmap.get(ck)
        scenario = preds.get(key) if key else None
        status = mapping_status(rep, scenario)
        if status == MAPPING_STATUS_EXACT:
            continue
        reason = {
            MAPPING_STATUS_NEEDS_NEW: "no mapping / no suitable model-simulation result",
            MAPPING_STATUS_UNSAFE: "unsafe mapping (e.g. acid leachant mapped to a NaOH/CO2 scenario)",
            MAPPING_STATUS_SCENARIO: "scenario-level only (model lacks exact time / condition code / concentration)",
        }[status]
        rows.append({
            "condition_key": ck,
            "leachant": rep.get("leachant", ""),
            "NaOH_M": rep.get("NaOH_M", ""),
            "acid_M": rep.get("acid_M", ""),
            "time_min": rep.get("time_min", ""),
            "condition_code": scenarios.sample_condition_code(rep) or "",
            "liquid_solid_ratio": rep.get("liquid_solid_ratio", ""),
            "CO2_condition": rep.get("CO2_condition", ""),
            "reason_needed": reason,
        })
    return pd.DataFrame(rows, columns=CONDITIONS_NEEDED_COLUMNS)
