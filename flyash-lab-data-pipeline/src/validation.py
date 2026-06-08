"""Data-quality validation for fly ash lab data.

Each rule appends issue dicts of the form::

    {"row": <0-based index or None>, "column": <name or None>,
     "severity": "error" | "warning", "code": <slug>, "message": <text>}

Severity policy (per project requirements):
    * Missing strength is a *warning* (specimens may not have reached curing age).
    * Duplicate ``sample_id`` is only an *error* when ``specimen_id``/``test_id``
      is also duplicated; otherwise it is a warning.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from . import calculations, config


def _issue(severity, code, message, row=None, column=None) -> dict:
    return {
        "row": row,
        "column": column,
        "severity": severity,
        "code": code,
        "message": message,
    }


def _is_blank(value) -> bool:
    """True if a value is NaN/NaT/None or an empty/whitespace string."""
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(value, str) and not value.strip():
        return True
    return False


def validate(df: pd.DataFrame, stats: Optional[pd.DataFrame] = None) -> list[dict]:
    """Run all validation rules and return a list of issue dicts.

    Args:
        df: Lab data (ideally after type coercion / derived columns).
        stats: Optional precomputed strength statistics; if omitted it is
            computed here for the high-CV check.
    """
    issues: list[dict] = []

    # --- sample_id: missing ---
    for idx, value in df.get("sample_id", pd.Series(dtype=object)).items():
        if _is_blank(value):
            issues.append(_issue("warning", "missing_sample_id",
                                 "Missing sample_id.", row=idx, column="sample_id"))

    # --- duplicate ids (sample_id only an error if specimen/test also dup) ---
    issues.extend(_duplicate_id_issues(df))

    # --- mix_id: missing (error — mix design is the core grouping key) ---
    for idx, value in df.get("mix_id", pd.Series(dtype=object)).items():
        if _is_blank(value):
            issues.append(_issue("error", "missing_mix_id",
                                 "Missing mix_id.", row=idx, column="mix_id"))

    # --- masses: negative ---
    for col in config.MASS_COLUMNS:
        if col not in df.columns:
            continue
        for idx, value in df[col].items():
            if pd.notna(value) and value < 0:
                issues.append(_issue("error", "negative_mass",
                                     f"Negative mass in {col} ({value}).",
                                     row=idx, column=col))

    # --- zero binder mass ---
    fly = df.get("fly_ash_mass_g")
    cem = df.get("cement_mass_g")
    red = df.get("red_mud_mass_g")
    if fly is not None and cem is not None and red is not None:
        binder = fly.fillna(0) + cem.fillna(0) + red.fillna(0)
        for idx, value in binder.items():
            if value == 0:
                issues.append(_issue("error", "zero_binder",
                                     "Total binder mass is zero (fly ash + cement + red mud).",
                                     row=idx, column="total_binder_mass_g"))

    # --- water/binder ratio out of plausible range ---
    issues.extend(_wb_ratio_issues(df))

    # --- pH outside 0-14 ---
    if "leachate_pH" in df.columns:
        for idx, value in df["leachate_pH"].items():
            if pd.notna(value) and (value < config.PH_MIN or value > config.PH_MAX):
                issues.append(_issue("error", "ph_out_of_range",
                                     f"leachate_pH {value} outside {config.PH_MIN}-{config.PH_MAX}.",
                                     row=idx, column="leachate_pH"))

    # --- missing curing age ---
    if "curing_age_days" in df.columns:
        for idx, value in df["curing_age_days"].items():
            if _is_blank(value):
                issues.append(_issue("warning", "missing_curing_age",
                                     "Missing curing_age_days.",
                                     row=idx, column="curing_age_days"))

    # --- missing strength (WARNING, not error) ---
    if "compressive_strength_MPa" in df.columns:
        status = df.get("data_status")
        for idx, value in df["compressive_strength_MPa"].items():
            if _is_blank(value):
                st = status.get(idx) if status is not None else None
                note = ""
                if isinstance(st, str) and st.lower() == "pending":
                    note = " (status pending — may not have reached curing age)"
                issues.append(_issue("warning", "missing_strength",
                                     f"Missing compressive_strength_MPa{note}.",
                                     row=idx, column="compressive_strength_MPa"))

    # --- very high coefficient of variation (per mix/age group) ---
    if stats is None:
        stats = calculations.strength_statistics(df)
    for _, srow in stats.iterrows():
        cv = srow.get("cv_percent")
        if pd.notna(cv) and cv > config.CV_HIGH_PCT:
            issues.append(_issue(
                "warning", "high_cv",
                f"High strength CV ({cv:.1f}%) for mix {srow['mix_id']} "
                f"at {srow['curing_age_days']} days (n={int(srow['n'])}).",
                column="compressive_strength_MPa"))

    # --- conductivity present but no unit context ---
    issues.extend(_conductivity_unit_issues(df))

    # --- unknown data_status values ---
    if "data_status" in df.columns:
        for idx, value in df["data_status"].items():
            if isinstance(value, str) and value.strip() and \
                    value.strip().lower() not in config.DATA_STATUS_VALUES:
                issues.append(_issue("warning", "unknown_data_status",
                                     f"Unrecognised data_status '{value}'. Expected one of "
                                     f"{config.DATA_STATUS_VALUES}.",
                                     row=idx, column="data_status"))

    return issues


def _duplicate_id_issues(df: pd.DataFrame) -> list[dict]:
    """Flag duplicate ids.

    * Duplicate specimen_id -> error.
    * Duplicate test_id -> error.
    * Duplicate sample_id -> error only if that row's specimen_id or test_id is
      *also* duplicated; otherwise a warning.
    """
    out: list[dict] = []

    def _dup_mask(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series(False, index=df.index)
        s = df[col]
        non_blank = s.apply(lambda v: not _is_blank(v))
        return s.duplicated(keep=False) & non_blank

    specimen_dup = _dup_mask("specimen_id")
    test_dup = _dup_mask("test_id")
    sample_dup = _dup_mask("sample_id")

    for idx in df.index[specimen_dup]:
        out.append(_issue("error", "duplicate_specimen_id",
                          f"Duplicate specimen_id '{df.at[idx, 'specimen_id']}'.",
                          row=idx, column="specimen_id"))
    for idx in df.index[test_dup]:
        out.append(_issue("error", "duplicate_test_id",
                          f"Duplicate test_id '{df.at[idx, 'test_id']}'.",
                          row=idx, column="test_id"))
    for idx in df.index[sample_dup]:
        also_dup = bool(specimen_dup.get(idx, False) or test_dup.get(idx, False))
        severity = "error" if also_dup else "warning"
        suffix = (" (and specimen_id/test_id also duplicated)" if also_dup
                  else " (specimen_id/test_id still distinct)")
        out.append(_issue(severity, "duplicate_sample_id",
                          f"Duplicate sample_id '{df.at[idx, 'sample_id']}'{suffix}.",
                          row=idx, column="sample_id"))
    return out


def _wb_ratio_issues(df: pd.DataFrame) -> list[dict]:
    """Flag implausible water/binder ratios."""
    out: list[dict] = []
    needed = {"fly_ash_mass_g", "cement_mass_g", "red_mud_mass_g", "water_mass_g"}
    if not needed.issubset(df.columns):
        return out
    binder = (df["fly_ash_mass_g"].fillna(0)
              + df["cement_mass_g"].fillna(0)
              + df["red_mud_mass_g"].fillna(0))
    for idx in df.index:
        b = binder.at[idx]
        w = df.at[idx, "water_mass_g"]
        if b and b > 0 and pd.notna(w):
            ratio = w / b
            if ratio < config.WB_RATIO_MIN or ratio > config.WB_RATIO_MAX:
                out.append(_issue(
                    "warning", "wb_ratio_out_of_range",
                    f"Water/binder ratio {ratio:.2f} outside plausible "
                    f"{config.WB_RATIO_MIN}-{config.WB_RATIO_MAX}.",
                    row=idx, column="water_binder_ratio"))
    return out


def _conductivity_unit_issues(df: pd.DataFrame) -> list[dict]:
    """Flag conductivity values when the expected unit column is absent.

    The canonical column name encodes the unit (``leachate_conductivity_uS_cm``).
    If a conductivity value exists but no such unit-bearing column is present in
    the data, flag a single dataset-level warning that units are unconfirmed.
    """
    out: list[dict] = []
    # Detect a conductivity-like column whose name does not encode a unit.
    unitless = [c for c in df.columns
                if "conductivity" in c and "us_cm" not in c.lower() and "ms_cm" not in c.lower()]
    if unitless:
        out.append(_issue("warning", "missing_conductivity_unit",
                          f"Conductivity column(s) {unitless} have no unit in the name; "
                          "confirm values are in uS/cm.", column=unitless[0]))
    return out


def validation_summary(issues: list[dict]) -> dict:
    """Summarise issues into counts by severity and by code."""
    by_severity: dict[str, int] = {}
    by_code: dict[str, int] = {}
    for it in issues:
        by_severity[it["severity"]] = by_severity.get(it["severity"], 0) + 1
        by_code[it["code"]] = by_code.get(it["code"], 0) + 1
    return {
        "total": len(issues),
        "errors": by_severity.get("error", 0),
        "warnings": by_severity.get("warning", 0),
        "by_code": by_code,
    }


def issues_dataframe(issues: list[dict]) -> pd.DataFrame:
    """Convert issues to a DataFrame for display/export."""
    if not issues:
        return pd.DataFrame(columns=["severity", "code", "row", "column", "message"])
    df = pd.DataFrame(issues)
    return df[["severity", "code", "row", "column", "message"]]
