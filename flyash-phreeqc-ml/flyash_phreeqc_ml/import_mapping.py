"""Flexible experimental-file import: read messy CSV/Excel into the app schema.

This is an **ingest helper**, not chemistry and not ML. It lets the app accept a
raw lab workbook (``.csv`` / ``.xlsx`` / ``.xls``), list its sheets, *suggest* how
the uploaded columns map onto the canonical measured-release schema
(:data:`config.EXPERIMENTAL_RELEASE_COLUMNS`), convert chemistry columns from
mg/L / ppm / ppb to mM using the atomic masses already documented in
:mod:`flyash_phreeqc_ml.calculations`, and build a schema-aligned frame that
preserves provenance (file/sheet/row/timestamp) and keeps unknown extra columns
rather than dropping them silently.

Everything here is pure (no Streamlit, no file-system side effects beyond reading
the source the caller hands in) so it can be unit-tested in isolation. The app
(`app.py`) is the only place that wires these functions to widgets and to
:func:`run_manager.save_lab_dataframe`.

Design rules that keep the data honest:

* The mg/L→mM conversion is the *same* formula the audit tab documents
  (``mM = mg/L / atomic_mass``) — it reuses :data:`calculations.ATOMIC_MASSES`,
  never a second copy.
* Sc and total REE stay in ppb by default — they have no atomic mass here and the
  schema column is ``*_ppb``.
* Acid (HCl) leaching rows are **not** forced into ``NaOH_M``: when a row's
  leachant looks like an acid, ``NaOH_M`` is blanked, ``leachant``/``acid_M`` are
  recorded, and an ``import_warning`` flags that PHREEQC mapping needs a matching
  acid simulation.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config, profiles, units
from .calculations import ATOMIC_MASSES  # back-compat re-export (== units.MOLAR_MASSES)


class ImportMappingError(Exception):
    """Raised for an unsupported file type or an otherwise unreadable source."""


# --------------------------------------------------------------------------- #
# Schema targets
# --------------------------------------------------------------------------- #
# The app columns an upload can be mapped onto. The canonical release schema plus
# two optional acid/leachant columns (kept after the canonical ones, so existing
# tooling that reads the fixed schema is unaffected — see run_manager).
LEACHANT_COLUMN = "leachant"
ACID_M_COLUMN = "acid_M"
MAPPING_TARGETS = list(config.EXPERIMENTAL_RELEASE_COLUMNS) + [LEACHANT_COLUMN, ACID_M_COLUMN]

# Schema chemistry columns that can be unit-converted, mapped to their element key
# in ATOMIC_MASSES. Sc/REE are intentionally absent (kept in ppb).
CHEM_ELEMENT_COLUMNS: dict[str, str] = {
    "Ca_mM": "Ca",
    "Si_mM": "Si",
    "Al_mM": "Al",
    "Fe_mM": "Fe",
    "Na_mM": "Na",
    "K_mM": "K",
}

# Accepted concentration units for a chemistry column (mg/L and ppm are treated as
# equivalent for dilute aqueous solutions). The authoritative per-column contract is
# the dataset profile's ``accepted_units`` (see :func:`accepted_units_for`); this list
# is the UI's default option set and the fallback.
UNIT_OPTIONS = ["mM", "mg/L", "ppm", "ppb"]
DEFAULT_UNIT = units.UNIT_MM

# Columns that count as a measured value when classifying a row.
PH_VALUE_COLUMNS = ["initial_pH", "final_pH"]
CHEM_VALUE_COLUMNS = list(CHEM_ELEMENT_COLUMNS.keys())
MEASURED_VALUE_COLUMNS = (
    PH_VALUE_COLUMNS + ["conductivity_mS_cm"] + CHEM_VALUE_COLUMNS + ["Sc_ppb", "total_REE_ppb"]
)

# Provenance columns appended to every imported frame so the source is never lost.
PROVENANCE_COLUMNS = [
    "original_file_name",
    "original_sheet_name",
    "original_row_number",
    "import_timestamp",
    "import_warning",
    "units_assumed",
]

# Prefix for preserved unknown/extra source columns.
EXTRA_COLUMN_PREFIX = "extra__"

# Per-converted-column provenance companions (the authority defines the suffixes).
# Captured as module-level names so they are usable inside build_schema_frame, whose
# ``units`` parameter (a dict) shadows the units *module*.
ORIG_VALUE_SUFFIX = units.ORIG_VALUE_SUFFIX
ORIG_UNIT_SUFFIX = units.ORIG_UNIT_SUFFIX
CONVERSION_ID_SUFFIX = units.CONVERSION_ID_SUFFIX
CONVERSION_PROVENANCE_SUFFIXES = units.CONVERSION_PROVENANCE_SUFFIXES


def _convert_column_with_meta(series: pd.Series, element: str, unit: str):
    """Module-scope wrapper around :func:`units.convert_series` (returns series + meta)."""
    return units.convert_series(series, unit, units.UNIT_MM, element)


def conversion_provenance_columns(converted_columns) -> list[str]:
    """All companion column names for an iterable of converted columns (flat order)."""
    out: list[str] = []
    for col in converted_columns:
        out.extend(f"{col}{s}" for s in CONVERSION_PROVENANCE_SUFFIXES)
    return out


def is_conversion_provenance_column(name: str) -> bool:
    """True if ``name`` is a conversion-provenance companion (so it is *not* unknown)."""
    return units.is_conversion_provenance_column(name)

# Note stamped on rows that were imported as acid leaching.
ACID_IMPORT_NOTE = (
    "Imported as acid leaching row; PHREEQC mapping may require matching acid simulation."
)

# Legacy CO2_condition migration to the cup-cover vocabulary (OA/PF/GS). ``open``
# (and atmospheric synonyms) map to OA; ``sealed`` is deliberately NOT auto-mapped to
# a cover — we cannot know which cup cover it was — so it is left as-is and flagged for
# the user to resolve.
_LEGACY_CO2_OPEN = {"open", "atm", "atmospheric", "atmospheric/open", "open air"}
_LEGACY_CO2_AMBIGUOUS = {"sealed", "sealed-like", "closed"}
CO2_OPEN_TO_OA_NOTE = "CO2_condition 'open' interpreted as OA (open air)."
CO2_SEALED_AMBIGUOUS_NOTE = (
    "CO2_condition 'sealed' is ambiguous under the cup-cover vocabulary — set OA / PF / GS "
    "(or a model label no_CO2) to resolve; not auto-mapped (we can't know which cover it was)."
)


def normalize_co2_condition(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Migrate legacy CO2_condition values to the cup-cover vocabulary.

    Returns ``(new_series, warnings_series)`` (positional index). Legacy ``open`` →
    ``OA`` with a note; ``sealed`` is left unchanged and **flagged** (PF vs GS is not
    knowable). Cup-cover / model labels and blanks pass through unchanged.
    """
    new: list = []
    warns: list = []
    for v in series:
        if _is_blank(v):
            new.append(v)
            warns.append("")
            continue
        token = str(v).strip().lower()
        if token in _LEGACY_CO2_OPEN:
            new.append("OA")
            warns.append(CO2_OPEN_TO_OA_NOTE)
        elif token in _LEGACY_CO2_AMBIGUOUS:
            new.append(v)  # leave it; do NOT guess a cover
            warns.append(CO2_SEALED_AMBIGUOUS_NOTE)
        else:
            new.append(v)
            warns.append("")
    idx = series.index
    return pd.Series(new, index=idx, dtype=object), pd.Series(warns, index=idx, dtype=object)


# --------------------------------------------------------------------------- #
# File reading + sheet selection
# --------------------------------------------------------------------------- #
def file_kind(filename: str) -> str:
    """Return ``"csv"`` or ``"excel"`` from a filename's extension.

    Raises :class:`ImportMappingError` for anything else.
    """
    ext = Path(str(filename)).suffix.lower()
    if ext == ".csv":
        return "csv"
    if ext in (".xlsx", ".xls"):
        return "excel"
    raise ImportMappingError(
        f"unsupported file type {ext!r}; expected .csv, .xlsx or .xls"
    )


def list_excel_sheets(source) -> list[str]:
    """List sheet names in an Excel workbook (path or file-like / bytes buffer)."""
    try:
        return list(pd.ExcelFile(source).sheet_names)
    except ImportError as exc:  # pragma: no cover - depends on optional engine
        raise ImportMappingError(
            "Reading .xls needs the 'xlrd' package (pip install xlrd); "
            ".xlsx needs 'openpyxl'."
        ) from exc


def read_tabular(source, *, kind: str, sheet: str | int | None = None,
                 header: int = 0) -> pd.DataFrame:
    """Read a CSV or one Excel sheet into a DataFrame.

    ``kind`` is ``"csv"`` or ``"excel"`` (see :func:`file_kind`). For Excel, pass a
    specific ``sheet`` name/index; ``header`` is the 0-based header row.
    """
    try:
        if kind == "csv":
            return pd.read_csv(source, header=header)
        if kind == "excel":
            return pd.read_excel(source, sheet_name=sheet, header=header)
    except ImportError as exc:  # pragma: no cover - optional engine missing
        raise ImportMappingError(
            "Reading this file needs an Excel engine ('openpyxl' for .xlsx, "
            "'xlrd' for .xls). Install it or export the sheet to CSV."
        ) from exc
    raise ImportMappingError(f"unknown kind {kind!r}; expected 'csv' or 'excel'")


# --------------------------------------------------------------------------- #
# Fuzzy column-mapping suggestions
# --------------------------------------------------------------------------- #
def _norm(name) -> str:
    """Normalise a header for fuzzy matching: lower-case, alphanumerics only."""
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


# Hand-written aliases per schema target. The canonical name is implicitly an
# alias too. Kept transparent and conservative — these are rules, not learning.
COLUMN_SYNONYMS: dict[str, list[str]] = {
    "sample_id": ["sample", "sample id", "id", "sampleid", "sample name"],
    "experiment_date": ["date", "experiment date", "run date"],
    "fly_ash_type": ["fly ash type", "ash type", "flyash", "fly ash", "sample type"],
    "NaOH_M": ["NaOH", "base concentration", "naoh conc", "naoh molarity", "alkali"],
    "time_min": ["time", "reaction time", "duration", "time min", "contact time"],
    "temperature_C": ["temperature", "temp", "temp c", "temperature c"],
    "liquid_solid_ratio": ["L/S", "LS ratio", "ls", "l s ratio", "liquid solid ratio",
                            "liquid to solid", "ls_ratio"],
    "CO2_condition": ["CO2", "co2 condition", "atmosphere", "co2 atmosphere"],
    "initial_pH": ["initial pH", "ph initial", "starting ph", "ph start", "ph0"],
    "final_pH": ["pH", "final pH", "ph final", "end ph", "measured ph"],
    "conductivity_mS_cm": ["conductivity", "ec", "cond", "conductivity ms cm"],
    "Ca_mM": ["Ca", "calcium"],
    "Si_mM": ["Si", "silicon", "silica"],
    "Al_mM": ["Al", "aluminium", "aluminum"],
    "Fe_mM": ["Fe", "iron"],
    "Na_mM": ["Na", "sodium"],
    "K_mM": ["K", "potassium"],
    "Sc_ppb": ["Sc", "scandium"],
    "total_REE_ppb": ["total REE", "REE", "rare earth", "total ree", "sum ree"],
    "filtration_notes": ["filtration", "filter notes", "filtration note"],
    "precipitate_observed": ["precipitate", "precip", "precipitate observed"],
    "notes": ["note", "comment", "comments", "remark", "remarks", "observation"],
    LEACHANT_COLUMN: ["leachant", "reagent", "solution type", "leaching agent", "lixiviant"],
    ACID_M_COLUMN: ["acid concentration", "hcl", "hcl m", "acid molarity", "acid conc"],
}


def _alias_set(target: str) -> set[str]:
    """Normalised aliases for a target, including its own canonical name."""
    aliases = {_norm(target)}
    for alias in COLUMN_SYNONYMS.get(target, []):
        aliases.add(_norm(alias))
    return aliases


def suggest_column_mapping(uploaded_columns) -> dict[str, str | None]:
    """Suggest a ``{schema_target: uploaded_column_or_None}`` mapping.

    Two passes so an exact name match always wins over a looser alias, and each
    uploaded column is assigned to at most one target. Targets with no match map to
    ``None`` ("leave blank").
    """
    uploaded = [str(c) for c in uploaded_columns]
    norm_to_original: dict[str, str] = {}
    for col in uploaded:
        norm_to_original.setdefault(_norm(col), col)

    mapping: dict[str, str | None] = {t: None for t in MAPPING_TARGETS}
    used: set[str] = set()

    # Pass 1: exact normalised match to the canonical target name.
    for target in MAPPING_TARGETS:
        nt = _norm(target)
        if nt in norm_to_original and norm_to_original[nt] not in used:
            mapping[target] = norm_to_original[nt]
            used.add(norm_to_original[nt])

    # Pass 2: alias match for still-unmapped targets.
    for target in MAPPING_TARGETS:
        if mapping[target] is not None:
            continue
        for alias in _alias_set(target):
            original = norm_to_original.get(alias)
            if original is not None and original not in used:
                mapping[target] = original
                used.add(original)
                break

    return mapping


def unmapped_columns(raw_df: pd.DataFrame, mapping: dict[str, str | None]) -> list[str]:
    """Uploaded columns not used by any mapping target (preserved as extras)."""
    used = {v for v in mapping.values() if v}
    return [c for c in raw_df.columns if c not in used]


# --------------------------------------------------------------------------- #
# Unit handling
# --------------------------------------------------------------------------- #
def accepted_units_for(column: str, profile=None) -> tuple[str, ...]:
    """The source units the importer accepts for ``column`` (from the dataset profile).

    Falls back to the units.py lab set when the profile does not declare the column.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    declared = getattr(profile, "accepted_units", None) or {}
    return tuple(declared.get(column, units.LAB_CONCENTRATION_SOURCE_UNITS))


def validate_unit(column: str, unit: str, profile=None) -> None:
    """Refuse an undeclared unit (no guess) with the contract message.

    Raises :class:`units.UnknownUnitError` — e.g. *"unit 'g/L' not recognized for Ca;
    supported: mg/L, ppm, ppb, mM"* — when ``unit`` is not in the column's accepted set.
    """
    accepted = accepted_units_for(column, profile)
    if unit not in accepted:
        element = CHEM_ELEMENT_COLUMNS.get(column, column)
        raise units.UnknownUnitError(
            f"unit {unit!r} not recognized for {element}; supported: {', '.join(accepted)}")


def convert_concentration(value: float, element: str, unit: str) -> float:
    """Convert one concentration to mM via the single conversion authority.

    ``mg/L``/``ppm`` ≈ mg/L for dilute aqueous; ``ppb`` = µg/L; ``mM`` is identity.
    Raises :class:`units.UnknownElementError` / :class:`units.UnknownUnitError` — never
    guesses.
    """
    return units.convert(value, unit, units.UNIT_MM, element).value


def convert_series_to_mM(series: pd.Series, element: str, unit: str) -> pd.Series:
    """Vectorised :func:`convert_concentration` over a column (non-numeric → NaN)."""
    converted, _meta = units.convert_series(series, unit, units.UNIT_MM, element)
    return converted


def conversion_provenance_summary(df: pd.DataFrame, *, max_examples: int = 3) -> list[dict]:
    """Per converted column: original→target unit, formula, molar mass, example rows.

    Reads the provenance companions an import wrote, so the import preview and the
    run-data viewer can show *exactly* how each value was converted. Returns ``[]``
    for a frame with no converted columns / no provenance.
    """
    rows: list[dict] = []
    if df is None or df.empty:
        return rows
    for col in CHEM_VALUE_COLUMNS:
        if col not in df.columns:
            continue
        val_col = f"{col}{ORIG_VALUE_SUFFIX}"
        unit_col = f"{col}{ORIG_UNIT_SUFFIX}"
        id_col = f"{col}{CONVERSION_ID_SUFFIX}"
        if not all(c in df.columns for c in (val_col, unit_col, id_col)):
            continue
        element = CHEM_ELEMENT_COLUMNS.get(col)
        from_unit = conv_id = formula = ""
        molar_mass = None
        examples: list[dict] = []
        for i in range(len(df)):
            orig = df[val_col].iloc[i]
            if _is_blank(orig):
                continue
            ounit = df[unit_col].iloc[i]
            if not from_unit:
                from_unit = str(ounit)
                try:
                    meta = units.convert(1.0, str(ounit), units.UNIT_MM, element)
                    conv_id, formula, molar_mass = meta.conversion_id, meta.formula, meta.molar_mass
                except units.UnitConversionError:
                    conv_id = str(df[id_col].iloc[i])
            if len(examples) < max_examples:
                examples.append({"original": orig, "original_unit": ounit,
                                 "converted_mM": df[col].iloc[i]})
        if from_unit:
            rows.append({"column": col, "element": element, "from_unit": from_unit,
                         "to_unit": units.UNIT_MM, "conversion_id": conv_id,
                         "formula": formula, "molar_mass_g_mol": molar_mass,
                         "examples": examples})
    return rows


# --------------------------------------------------------------------------- #
# Acid / leachant classification
# --------------------------------------------------------------------------- #
def is_acid_leachant(value) -> bool:
    """True if a leachant label looks like an acid (HCl, HNO3, …), not a base."""
    s = str(value).strip().lower()
    if not s or s in ("nan", "none"):
        return False
    if "naoh" in s or "koh" in s or "base" in s or "alkali" in s:
        return False
    return "hcl" in s or "acid" in s or "hno3" in s or "h2so4" in s or s == "acidic"


# --------------------------------------------------------------------------- #
# Build a schema-aligned frame
# --------------------------------------------------------------------------- #
def _is_blank(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):  # pragma: no cover - non-scalar guard
        pass
    return str(value).strip() == ""


def units_summary(units: dict[str, str]) -> str:
    """Compact ``Ca_mM=mg/L; Si_mM=ppm`` summary of non-default unit choices."""
    parts = [
        f"{col}={units.get(col, DEFAULT_UNIT)}"
        for col in CHEM_VALUE_COLUMNS
        if units.get(col, DEFAULT_UNIT) != DEFAULT_UNIT
    ]
    return "; ".join(parts)


def build_schema_frame(
    raw_df: pd.DataFrame,
    mapping: dict[str, str | None],
    units: dict[str, str] | None = None,
    *,
    filename: str = "",
    sheet_name: str = "",
    import_timestamp: str | None = None,
    default_leachant: str = "NaOH",
    profile=None,
) -> pd.DataFrame:
    """Transform a raw upload into a release-schema frame with provenance.

    * Canonical schema columns are copied from their mapped source (blank if
      unmapped); chemistry columns are unit-converted to mM via ``units``.
    * **Conversion provenance** — for every converted chemistry column ``X_mM`` the
      original value, original unit, and registry ``conversion_id`` are kept as wide
      companions (``X_mM_orig_value`` / ``_orig_unit`` / ``_conversion_id``), so a
      wrong conversion is auditable later. Values already in mM get
      ``conversion_id == "identity"``. The unit is validated against the dataset
      profile's accepted set first (an undeclared unit is refused, not guessed).
    * ``leachant``/``acid_M`` are filled from their mapped columns or the
      ``default_leachant``; rows whose leachant looks like an acid get ``NaOH_M``
      blanked and an ``import_warning`` (acids are never forced into ``NaOH_M``).
    * Provenance (:data:`PROVENANCE_COLUMNS`) and any unknown source columns
      (prefixed ``extra__``) are appended so nothing is dropped silently.

    Returns a new frame; the input is not modified. (The ``units`` parameter is a
    ``{column: unit}`` dict, not the units module.)
    """
    units = units or {}
    profile = profile or profiles.FLY_ASH_PROFILE
    mapping = {**{t: None for t in MAPPING_TARGETS}, **(mapping or {})}
    n = len(raw_df)
    raw = raw_df.reset_index(drop=True)
    ts = import_timestamp or datetime.now().isoformat(timespec="seconds")

    out = pd.DataFrame(index=range(n))
    conversion_companions: dict[str, object] = {}  # companion col -> values (ordered)
    for col in config.EXPERIMENTAL_RELEASE_COLUMNS:
        src = mapping.get(col)
        if src and src in raw.columns:
            if col in CHEM_ELEMENT_COLUMNS:
                unit_choice = units.get(col, DEFAULT_UNIT)
                validate_unit(col, unit_choice, profile)  # refuse undeclared units
                converted, meta = _convert_column_with_meta(
                    raw[src], CHEM_ELEMENT_COLUMNS[col], unit_choice)
                out[col] = converted.values
                conversion_companions[f"{col}{ORIG_VALUE_SUFFIX}"] = \
                    pd.to_numeric(raw[src], errors="coerce").values
                conversion_companions[f"{col}{ORIG_UNIT_SUFFIX}"] = [unit_choice] * n
                conversion_companions[f"{col}{CONVERSION_ID_SUFFIX}"] = [meta.conversion_id] * n
            else:
                out[col] = raw[src].values
        else:
            out[col] = ""

    # Migrate legacy CO2_condition values to the cup-cover vocabulary (open -> OA;
    # sealed -> flagged, never silently mapped to a cover).
    co2_norm, co2_warn = normalize_co2_condition(out["CO2_condition"])
    out["CO2_condition"] = co2_norm.values

    # Leachant / acid metadata.
    leach_src = mapping.get(LEACHANT_COLUMN)
    if leach_src and leach_src in raw.columns:
        leachant = raw[leach_src].astype(object).where(~raw[leach_src].isna(), default_leachant)
        leachant = leachant.apply(lambda v: default_leachant if _is_blank(v) else v)
    else:
        leachant = pd.Series([default_leachant] * n)
    acid_src = mapping.get(ACID_M_COLUMN)
    acid_m = raw[acid_src] if (acid_src and acid_src in raw.columns) else pd.Series([""] * n)

    is_acid = leachant.apply(is_acid_leachant).to_numpy()
    warnings = pd.Series([""] * n, dtype=object)
    if is_acid.any():
        # Cast to object first: a numeric NaOH_M column can't hold a blank string.
        out["NaOH_M"] = out["NaOH_M"].astype(object)
        out.loc[is_acid, "NaOH_M"] = ""  # never force acid data into NaOH_M
        warnings[is_acid] = ACID_IMPORT_NOTE
    # Merge the CO2 migration notes into import_warning (positional).
    warnings = pd.Series(
        ["; ".join(x for x in (w, c) if x) for w, c in zip(warnings.tolist(), co2_warn.tolist())],
        dtype=object,
    )

    out[LEACHANT_COLUMN] = leachant.to_numpy()
    out[ACID_M_COLUMN] = acid_m.to_numpy() if hasattr(acid_m, "to_numpy") else list(acid_m)

    out["original_file_name"] = filename
    out["original_sheet_name"] = sheet_name
    out["original_row_number"] = [i + 2 for i in range(n)]  # header is row 1
    out["import_timestamp"] = ts
    out["import_warning"] = warnings.to_numpy()
    out["units_assumed"] = units_summary(units)

    # Conversion-provenance companions (recognised columns, not "extra__"/unknown).
    for cname, vals in conversion_companions.items():
        out[cname] = vals

    for col in unmapped_columns(raw, mapping):
        out[f"{EXTRA_COLUMN_PREFIX}{col}"] = raw[col].values

    ordered = (
        list(config.EXPERIMENTAL_RELEASE_COLUMNS)
        + [LEACHANT_COLUMN, ACID_M_COLUMN]
        + PROVENANCE_COLUMNS
        + list(conversion_companions.keys())
        + [c for c in out.columns if c.startswith(EXTRA_COLUMN_PREFIX)]
    )
    return out.reindex(columns=ordered)


# --------------------------------------------------------------------------- #
# Pre-save validation summary
# --------------------------------------------------------------------------- #
def classify_row(row) -> str:
    """Classify a transformed row: chemistry-present / pH-only / incomplete."""
    if any(not _is_blank(row.get(c)) for c in CHEM_VALUE_COLUMNS):
        return "chemistry-present"
    if any(not _is_blank(row.get(c)) for c in PH_VALUE_COLUMNS):
        return "pH-only"
    return "incomplete"


def summarize_import(schema_df: pd.DataFrame, units: dict[str, str] | None = None) -> dict:
    """Pre-save report for the transformed frame (pure, no I/O).

    Returns a dict with: ``n_rows``; ``missing_required_columns``;
    ``rows_missing_required`` (count); ``ph_out_of_range`` (list of
    ``{row, column, value}``); ``blank_sample_ids`` (count);
    ``duplicate_sample_ids`` (list); ``rows_no_measured_values`` (count);
    ``converted_columns`` (``{col: unit}`` for non-mM chem columns);
    ``classifications`` (``{label: count}``).
    """
    from . import run_manager  # local import avoids a module cycle

    units = units or {}
    if schema_df is None or schema_df.empty:
        return {
            "n_rows": 0,
            "missing_required_columns": run_manager.missing_lab_required_columns(
                schema_df if schema_df is not None else pd.DataFrame()
            ),
            "rows_missing_required": 0,
            "ph_out_of_range": [],
            "blank_sample_ids": 0,
            "duplicate_sample_ids": [],
            "rows_no_measured_values": 0,
            "converted_columns": {},
            "classifications": {},
            "co2_unresolved": 0,
        }

    df = schema_df.reset_index(drop=True)
    required = run_manager.LAB_REQUIRED_COLUMNS
    missing_cols = run_manager.missing_lab_required_columns(df)

    present_required = [c for c in required if c in df.columns]
    if present_required:
        row_missing = df[present_required].apply(
            lambda r: any(_is_blank(v) for v in r), axis=1
        )
        rows_missing_required = int(row_missing.sum())
    else:
        rows_missing_required = len(df)

    ph_out: list[dict] = []
    for col in PH_VALUE_COLUMNS:
        if col not in df.columns:
            continue
        vals = pd.to_numeric(df[col], errors="coerce")
        bad = vals.notna() & ((vals < 0) | (vals > 14))
        for idx in df.index[bad]:
            ph_out.append({"row": int(idx) + 1, "column": col, "value": float(vals[idx])})

    if "sample_id" in df.columns:
        sid = df["sample_id"].astype(str).str.strip()
        blank_ids = int(((sid == "") | (sid.str.lower() == "nan")).sum())
        nonblank = sid[(sid != "") & (sid.str.lower() != "nan")]
        dup = sorted(nonblank[nonblank.duplicated(keep=False)].unique().tolist())
    else:
        blank_ids = len(df)
        dup = []

    measured_present = [c for c in MEASURED_VALUE_COLUMNS if c in df.columns]
    if measured_present:
        no_measured = df[measured_present].apply(
            lambda r: all(_is_blank(v) for v in r), axis=1
        )
        rows_no_measured = int(no_measured.sum())
    else:
        rows_no_measured = len(df)

    converted = {
        col: units.get(col, DEFAULT_UNIT)
        for col in CHEM_VALUE_COLUMNS
        if units.get(col, DEFAULT_UNIT) != DEFAULT_UNIT
    }

    labels = df.apply(classify_row, axis=1)
    classifications = {k: int(v) for k, v in labels.value_counts().items()}

    # CO2_condition values still outside the cup-cover vocabulary (e.g. legacy
    # 'sealed' that was flagged, not auto-mapped) — the user must resolve these.
    co2_unresolved = 0
    if "CO2_condition" in df.columns:
        allowed = set(config.CO2_CONDITION_ALLOWED)
        vals = df["CO2_condition"].astype(str).str.strip()
        present = vals[(vals != "") & (vals.str.lower() != "nan")]
        co2_unresolved = int((~present.isin(allowed)).sum())

    return {
        "n_rows": len(df),
        "missing_required_columns": missing_cols,
        "rows_missing_required": rows_missing_required,
        "ph_out_of_range": ph_out,
        "blank_sample_ids": blank_ids,
        "duplicate_sample_ids": dup,
        "rows_no_measured_values": rows_no_measured,
        "converted_columns": converted,
        "classifications": classifications,
        "co2_unresolved": co2_unresolved,
    }
