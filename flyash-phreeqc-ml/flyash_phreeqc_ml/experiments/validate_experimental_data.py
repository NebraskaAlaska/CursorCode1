"""Feature 2 — validate a filled experimental-release CSV before Phase 2.

The point is to catch data-entry mistakes early (impossible pH, negative
concentrations, duplicate or empty sample ids, an unknown CO2 label) and to flag
soft problems as *warnings* rather than hard failures, so a partially-filled sheet
still produces a useful report.

The core entry point :func:`validate_experimental_df` takes an in-memory
DataFrame and returns a tidy report (one row per issue). :func:`validate_experimental_dir`
loops over the measured CSVs in a directory. The script ``07_validate_experimental_data.py``
owns the output path.

Severities
----------
``error``   — a real problem that would corrupt the analysis (fix before Phase 2).
``warning`` — worth checking, but not necessarily wrong (e.g. missing dilution note).
``ok``      — emitted once when a source passes with no errors or warnings.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .. import config

VALIDATION_REPORT_COLUMNS = ["source", "severity", "check", "column", "n_affected", "message"]

# Concentration columns that must be non-negative when present.
_CONCENTRATION_COLUMNS = ["Ca_mM", "Si_mM", "Al_mM", "Fe_mM", "Na_mM", "K_mM", "Sc_ppb", "total_REE_ppb"]
# Chemistry columns we expect to carry the actual measured signal.
_CHEMISTRY_COLUMNS = ["Ca_mM", "Si_mM", "Al_mM", "Fe_mM", "Na_mM", "K_mM", "Sc_ppb", "total_REE_ppb"]
# pH columns to range-check (0..14).
_PH_COLUMNS = ["initial_pH", "final_pH"]


def _numeric(df: pd.DataFrame, column: str) -> pd.Series | None:
    """Return *column* coerced to numeric, or None if the column is absent."""
    if column not in df.columns:
        return None
    return pd.to_numeric(df[column], errors="coerce")


def validate_experimental_df(df: pd.DataFrame, *, source: str = "<dataframe>") -> list[dict]:
    """Validate one already-loaded experimental-release frame.

    Returns a list of report-row dicts (keys = :data:`VALIDATION_REPORT_COLUMNS`).
    The frame may hold strings (as read with ``dtype=str``) or already-typed
    values — numeric checks coerce as needed, so either form works.
    """
    issues: list[dict] = []

    def add(severity: str, check: str, message: str, column: str = "", n_affected: int = 0) -> None:
        issues.append(
            {
                "source": source,
                "severity": severity,
                "check": check,
                "column": column,
                "n_affected": int(n_affected),
                "message": message,
            }
        )

    # --- required columns exist -------------------------------------------- #
    required = list(config.EXPERIMENTAL_RELEASE_COLUMNS)
    missing = [c for c in required if c not in df.columns]
    if missing:
        add("error", "required_columns", f"missing required column(s): {missing}",
            column=", ".join(missing), n_affected=len(missing))

    # --- sample_id present, non-empty, unique ------------------------------ #
    if "sample_id" in df.columns:
        ids = df["sample_id"].astype("string").str.strip()
        empty = int(ids.isna().sum() + (ids == "").sum())
        if empty:
            add("error", "sample_id_empty", f"{empty} row(s) have a blank sample_id",
                column="sample_id", n_affected=empty)
        non_empty = ids[(ids != "") & ids.notna()]
        dup = int(non_empty.duplicated(keep=False).sum())
        if dup:
            offenders = sorted(set(non_empty[non_empty.duplicated(keep=False)]))
            add("error", "sample_id_unique", f"{dup} row(s) share a duplicated sample_id: {offenders}",
                column="sample_id", n_affected=dup)

    # --- numeric range / sign checks --------------------------------------- #
    naoh = _numeric(df, "NaOH_M")
    if naoh is not None:
        bad = int((naoh < 0).sum())
        if bad:
            add("error", "NaOH_M_nonnegative", f"{bad} row(s) have negative NaOH_M",
                column="NaOH_M", n_affected=bad)

    time_min = _numeric(df, "time_min")
    if time_min is not None:
        bad = int((time_min <= 0).sum())
        if bad:
            add("error", "time_min_positive", f"{bad} row(s) have non-positive time_min",
                column="time_min", n_affected=bad)

    ls = _numeric(df, "liquid_solid_ratio")
    if ls is not None:
        bad = int((ls <= 0).sum())
        if bad:
            add("error", "liquid_solid_ratio_positive",
                f"{bad} row(s) have non-positive liquid_solid_ratio",
                column="liquid_solid_ratio", n_affected=bad)

    temp = _numeric(df, "temperature_C")
    if temp is not None:
        bad = int(((temp < 0) | (temp > 100)).sum())
        if bad:
            add("warning", "temperature_range",
                f"{bad} row(s) have temperature_C outside 0-100 C",
                column="temperature_C", n_affected=bad)

    # --- CO2 condition vocabulary ------------------------------------------ #
    if "CO2_condition" in df.columns:
        allowed = set(config.CO2_CONDITION_ALLOWED)
        vals = df["CO2_condition"].astype("string").str.strip()
        present = vals[(vals != "") & vals.notna()]
        bad_mask = ~present.isin(allowed)
        bad = int(bad_mask.sum())
        if bad:
            offenders = sorted(set(present[bad_mask]))
            add("error", "CO2_condition_vocab",
                f"{bad} row(s) use a CO2_condition not in {sorted(allowed)}: {offenders}",
                column="CO2_condition", n_affected=bad)

    # --- pH range ---------------------------------------------------------- #
    for col in _PH_COLUMNS:
        series = _numeric(df, col)
        if series is not None:
            bad = int(((series < 0) | (series > 14)).sum())
            if bad:
                add("error", "pH_range", f"{bad} row(s) have {col} outside 0-14",
                    column=col, n_affected=bad)

    # --- conductivity non-negative ----------------------------------------- #
    cond = _numeric(df, "conductivity_mS_cm")
    if cond is not None:
        bad = int((cond < 0).sum())
        if bad:
            add("error", "conductivity_nonnegative",
                f"{bad} row(s) have negative conductivity_mS_cm",
                column="conductivity_mS_cm", n_affected=bad)

    # --- concentrations non-negative --------------------------------------- #
    for col in _CONCENTRATION_COLUMNS:
        series = _numeric(df, col)
        if series is not None:
            bad = int((series < 0).sum())
            if bad:
                add("error", "concentration_nonnegative",
                    f"{bad} row(s) have negative {col}", column=col, n_affected=bad)

    # --- soft warnings ----------------------------------------------------- #
    present_chem = [c for c in _CHEMISTRY_COLUMNS if c in df.columns]
    if present_chem and not df.empty:
        chem = df[present_chem].apply(pd.to_numeric, errors="coerce")
        if not chem.notna().any().any():
            add("warning", "all_chemistry_blank",
                "no measured chemistry values found (all Ca/Si/Al/Fe/Na/K/Sc/REE blank)",
                column=", ".join(present_chem), n_affected=len(df))

    final_ph = _numeric(df, "final_pH")
    if final_ph is not None and not df.empty:
        missing_ph = int(final_ph.isna().sum())
        if missing_ph:
            add("warning", "final_pH_missing", f"{missing_ph} row(s) are missing final_pH",
                column="final_pH", n_affected=missing_ph)

    # Dilution factor: warn if there is neither a dilution-ish column nor a
    # "dilution" mention in the notes — a common ICP bookkeeping omission.
    has_dilution_col = any("dilution" in str(c).lower() for c in df.columns)
    has_dilution_note = False
    if "notes" in df.columns and not df.empty:
        notes = df["notes"].astype("string").str.lower()
        has_dilution_note = bool(notes.str.contains("dilut", na=False).any())
    if not has_dilution_col and not has_dilution_note:
        add("warning", "dilution_factor_absent",
            "no dilution-factor column and no 'dilution' mention in notes — "
            "confirm ICP dilution was recorded",
            column="notes", n_affected=len(df))

    if not issues:
        add("ok", "all_checks", "no errors or warnings")
    return issues


def _measured_csv_paths(directory: Path) -> list[Path]:
    """Measured-release CSVs in *directory* (skips template / map / plan)."""
    skip = set(config.EXPERIMENTAL_NON_DATA_FILES)
    return [p for p in sorted(directory.glob("*.csv")) if p.name not in skip]


def validate_experimental_dir(directory: str | Path | None = None) -> pd.DataFrame:
    """Validate every measured-release CSV in *directory*; return a combined report.

    Each file is read raw (as strings) so the column-presence check sees the file
    exactly as written. If the directory has no measured files, a single
    informational row is returned.
    """
    directory = Path(directory) if directory is not None else config.EXPERIMENTAL_ICP_DIR
    rows: list[dict] = []

    paths = _measured_csv_paths(directory) if directory.exists() else []
    if not paths:
        rows.append(
            {
                "source": str(directory),
                "severity": "warning",
                "check": "no_measured_files",
                "column": "",
                "n_affected": 0,
                "message": "no filled experimental-release CSV found (only template/map/plan)",
            }
        )
        return pd.DataFrame(rows, columns=VALIDATION_REPORT_COLUMNS)

    for path in paths:
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=True, skipinitialspace=True)
            df.columns = [str(c).strip() for c in df.columns]
        except Exception as exc:  # unreadable file -> report, don't crash
            rows.append(
                {
                    "source": path.name,
                    "severity": "error",
                    "check": "read_csv",
                    "column": "",
                    "n_affected": 0,
                    "message": f"could not read file: {exc}",
                }
            )
            continue
        rows.extend(validate_experimental_df(df, source=path.name))

    return pd.DataFrame(rows, columns=VALIDATION_REPORT_COLUMNS)
