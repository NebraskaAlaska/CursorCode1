"""Class C fly ash dissolution-workbook parser (special-case import, no ML).

The generic :mod:`import_mapping` importer assumes a single rectangular table.
The lab's dissolution workbook is **not** rectangular: it has an ``ICP OES`` sheet
with stacked *element blocks* (Calcium / Silicon / Aluminum), each split into a
``mmol/l`` and a ``mg/L`` sub-section whose columns are the leaching conditions
``NaOH-OA`` / ``NaOH-PF`` / ``NaOH-GS`` and whose rows are reaction times; and a
separate ``pH`` sheet of sample rows like ``0.5M NaOH-OA-10`` (plus HCl rows).

This module normalises that workbook into the canonical measured-release schema
(:data:`config.EXPERIMENTAL_RELEASE_COLUMNS`) by:

* parsing the ``pH`` sheet for ``<conc>M <leachant>-<code>-<time>`` sample labels,
* parsing the ``ICP OES`` sheet block-by-block (marker-based, not fixed cell
  coordinates, so small layout shifts still parse), preferring ``mmol/l`` values
  and falling back to ``mg/L`` → mM via :data:`calculations.ATOMIC_MASSES`,
* joining chemistry onto the NaOH pH rows by ``(condition_code, time_min)``,
* keeping HCl rows pH-only and acid-tagged (never forced into ``NaOH_M``), and
* leaving operator-supplied metadata (date, temperature, L/S, CO2, initial pH,
  fly-ash type) as importer *defaults* rather than parsing them.

It is pure (reads only the source handed in) and reuses the same unit-conversion,
leachant/provenance columns, and acid note as :mod:`import_mapping`, so the saved
rows are shaped exactly like a generic import.

.. note::
   The detection is built to the documented structure and a synthetic fixture
   (``tests/test_dissolution_workbook.py``). A real workbook may need the markers
   (sheet names, element/unit/condition labels) tuned — they are module constants.
"""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd

from . import config
from . import import_mapping as im
from . import scenarios
from .calculations import ATOMIC_MASSES

# Sheet names (matched case-insensitively, substring-tolerant).
SHEET_ICP_TOKENS = ("icp", "oes")
SHEET_PH_TOKENS = ("ph",)

# Element block name -> schema column / element key for conversion.
ELEMENT_TO_COLUMN: dict[str, str] = {
    "calcium": "Ca_mM",
    "silicon": "Si_mM",
    "silica": "Si_mM",
    "aluminum": "Al_mM",
    "aluminium": "Al_mM",
}
COLUMN_TO_ELEMENT: dict[str, str] = {"Ca_mM": "Ca", "Si_mM": "Si", "Al_mM": "Al"}

CONDITION_CODES = ("OA", "PF", "GS")
FLY_ASH_DEFAULT = "Class C fly ash"

# Metadata the importer lets the user fill once for every row (NOT parsed).
DEFAULT_FILL_FIELDS = [
    "experiment_date",
    "temperature_C",
    "liquid_solid_ratio",
    "CO2_condition",
    "initial_pH",
    "fly_ash_type",
]

# A sample label like "0.5M NaOH-OA-10" or "0.5 M HCL-OA-10".
LABEL_RE = re.compile(
    r"([0-9]*\.?[0-9]+)\s*M\s+(NaOH|KOH|HCl|HCL|HNO3|H2SO4)\s*[-_ ]\s*"
    r"(OA|PF|GS)\s*[-_ ]\s*([0-9]+)",
    re.IGNORECASE,
)

CONDITION_RE = re.compile(r"(?<![A-Z])(OA|PF|GS)(?![A-Z])")

# Chemistry not present in this workbook (warned about so blanks aren't read as 0).
ABSENT_CHEMISTRY = ["Fe_mM", "Na_mM", "K_mM", "Sc_ppb", "total_REE_ppb"]

EXTRA_CONDITION_COLUMN = f"{im.EXTRA_COLUMN_PREFIX}condition_code"
# Optional derived fields from the now-known OA/PF/GS cover meanings.
EXTRA_COVER_COLUMN = f"{im.EXTRA_COLUMN_PREFIX}cover_condition"
EXTRA_CO2_EXPOSURE_COLUMN = f"{im.EXTRA_COLUMN_PREFIX}CO2_exposure_level"


class DissolutionWorkbookError(Exception):
    """Raised when the workbook is missing the expected ICP OES / pH sheets."""


# --------------------------------------------------------------------------- #
# Small cell helpers
# --------------------------------------------------------------------------- #
def _is_blank(v) -> bool:
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):  # pragma: no cover
        pass
    return str(v).strip() == ""


# Cell texts that mean "no measurement" — treated as missing, never as 0 or a pH.
_MISSING_TOKENS = {"-", "--", "–", "—", "n/a", "na", "nd", "bdl", "n.d."}


def _to_number(v) -> float | None:
    if _is_blank(v):
        return None
    s = str(v).strip()
    if s.lower() in _MISSING_TOKENS:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _to_int(v) -> int | None:
    f = _to_number(v)
    if f is None:
        return None
    if abs(f - round(f)) > 1e-9:
        return None
    return int(round(f))


def _norm_text(v) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(v).lower())


def _match_condition(v) -> str | None:
    if _is_blank(v):
        return None
    m = CONDITION_RE.search(str(v).upper())
    return m.group(1) if m else None


def _match_unit(v) -> str | None:
    """Return ``"mmol"`` or ``"mg"`` for a unit-label cell, else None."""
    if _is_blank(v):
        return None
    low = str(v).lower().replace(" ", "")
    if "mmol" in low:
        return "mmol"
    if "mg/l" in low or low in ("mgl", "mg"):
        return "mg"
    return None


def _first_number_after(row_values, start_col: int) -> float | None:
    """First numeric cell strictly to the right of ``start_col`` in a row."""
    for c in range(start_col + 1, len(row_values)):
        n = _to_number(row_values[c])
        if n is not None:
            return n
    return None


def _guess_time_col(arr, header_row: int, first_cond_col: int, n_cols: int) -> int | None:
    """Find the time column: a labelled one, else the column left of the block."""
    for c in range(n_cols):
        t = _norm_text(arr[header_row][c])
        if "time" in t or t == "min" or "timemin" in t:
            return c
    if first_cond_col > 0:
        return first_cond_col - 1
    return None


def _find_header_col(arr, n_rows: int, n_cols: int, wanted: tuple[str, ...]) -> int | None:
    """First column (row-major scan) whose header cell normalises to ``wanted``."""
    for r in range(n_rows):
        for c in range(n_cols):
            if _norm_text(arr[r][c]) in wanted:
                return c
    return None


def _unit_column_map(arr, n_rows: int, n_cols: int) -> dict[int, str]:
    """Map each column to its unit group from a global ``mg/L`` / ``mmol/l`` row.

    The real workbook puts the unit labels once in the top row, each anchoring a
    horizontal group of condition columns. Each unit anchor owns the columns from
    itself up to (but not including) the next anchor. Empty if no unit labels
    exist (callers then treat values as already-mM).
    """
    anchors: dict[int, str] = {}
    for r in range(n_rows):
        for c in range(n_cols):
            u = _match_unit(arr[r][c])
            if u and c not in anchors:
                anchors[c] = u
    if not anchors:
        return {}
    cols_sorted = sorted(anchors)
    mapping: dict[int, str] = {}
    for i, ucol in enumerate(cols_sorted):
        end = cols_sorted[i + 1] if i + 1 < len(cols_sorted) else n_cols
        for c in range(ucol, end):
            mapping[c] = anchors[ucol]
    return mapping


# --------------------------------------------------------------------------- #
# Sheet location
# --------------------------------------------------------------------------- #
def _as_excelfile(source) -> pd.ExcelFile:
    if isinstance(source, pd.ExcelFile):
        return source
    try:
        return pd.ExcelFile(source)
    except ImportError as exc:  # pragma: no cover - optional engine
        raise DissolutionWorkbookError(
            "Reading this workbook needs an Excel engine ('openpyxl' for .xlsx, "
            "'xlrd' for .xls)."
        ) from exc


def find_workbook_sheets(xl: pd.ExcelFile) -> tuple[str, str]:
    """Return ``(icp_sheet, ph_sheet)`` names from a workbook's sheets.

    Matches case-insensitively: the ICP sheet contains 'icp' or 'oes'; the pH
    sheet is named 'ph' (exact, normalised). Raises if either is missing.
    """
    icp = ph = None
    for name in xl.sheet_names:
        low = name.lower()
        norm = _norm_text(name)
        if icp is None and any(tok in low for tok in SHEET_ICP_TOKENS):
            icp = name
        if ph is None and norm == "ph":
            ph = name
    if icp is None or ph is None:
        raise DissolutionWorkbookError(
            "Expected an 'ICP OES' sheet and a 'pH' sheet; found "
            f"{list(xl.sheet_names)}."
        )
    return icp, ph


# --------------------------------------------------------------------------- #
# pH sheet
# --------------------------------------------------------------------------- #
def parse_ph_sheet(grid: pd.DataFrame) -> list[dict]:
    """Parse the pH sheet into ``[{leachant, condition_code, time_min, conc,
    final_pH, source_label}]``.

    Pass A reads explicit ``<conc>M <leachant>-<code>-<time>`` labels (pH is the
    first number to the right). Pass B reads a NaOH pH *matrix* (condition headers
    across, time down) for any ``(code, time)`` not already found, defaulting the
    concentration to the modal one from Pass A.
    """
    arr = grid.values
    n_rows, n_cols = grid.shape
    rows: list[dict] = []
    seen: set[tuple[str, str, int]] = set()

    # The label list has a "pH" header column (e.g. Sample | Time (min) | pH);
    # read pH from there, NOT the first number after the label (that is Time).
    ph_col = _find_header_col(arr, n_rows, n_cols, ("ph",))

    # Pass A — explicit labels.
    for r in range(n_rows):
        for c in range(n_cols):
            cell = arr[r][c]
            if _is_blank(cell):
                continue
            m = LABEL_RE.search(str(cell))
            if not m:
                continue
            conc, leach, code, time = m.groups()
            code = code.upper()
            time_i = int(time)
            if ph_col is not None and ph_col != c:
                ph = _to_number(arr[r][ph_col])
            else:
                ph = _first_number_after(arr[r], c)
            leach_norm = "HCl" if leach.upper().startswith("HC") else leach
            rows.append({
                "leachant": leach_norm,
                "condition_code": code,
                "time_min": time_i,
                "conc": conc,
                "final_pH": ph,
                "source_label": str(cell).strip(),
            })
            seen.add((leach_norm.upper(), code, time_i))

    # Modal concentration from explicit NaOH labels (for the matrix fallback).
    naoh_concs = [r["conc"] for r in rows if r["leachant"].upper().startswith("NA")]
    modal_conc = max(set(naoh_concs), key=naoh_concs.count) if naoh_concs else ""

    # Pass B — NaOH pH matrix (condition headers across, time down).
    for r in range(n_rows):
        header_cols = {}
        for c in range(n_cols):
            code = _match_condition(arr[r][c])
            if code:
                header_cols[c] = code
        if len(header_cols) < 2:
            continue
        time_col = _guess_time_col(arr, r, min(header_cols), n_cols)
        if time_col is None:
            continue
        for rr in range(r + 1, n_rows):
            t = _to_int(arr[rr][time_col])
            if t is None:
                continue
            for c, code in header_cols.items():
                ph = _to_number(arr[rr][c])
                if ph is None:
                    continue
                key = ("NAOH", code, t)
                if key in seen:
                    continue
                rows.append({
                    "leachant": "NaOH",
                    "condition_code": code,
                    "time_min": t,
                    "conc": modal_conc,
                    "final_pH": ph,
                    "source_label": f"{modal_conc}M NaOH-{code}-{t}".strip("M "),
                })
                seen.add(key)
    return rows


# --------------------------------------------------------------------------- #
# ICP OES sheet
# --------------------------------------------------------------------------- #
def parse_icp_sheet(grid: pd.DataFrame) -> pd.DataFrame:
    """Parse the ICP OES sheet into a long chemistry frame, mmol/l preferred.

    Columns: ``element_col, condition_code, time_min, value_mM, unit_source``.

    The workbook lays the unit groups out **horizontally**: a single top row holds
    the ``mg/L`` and ``mmol/l`` labels, each anchoring a group of condition columns
    (``NaOH-OA/PF/GS``). Each element block (Calcium / Silicon / Aluminum) then has
    one shared header row whose condition columns span *both* unit groups. For each
    condition cell the unit comes from its column (:func:`_unit_column_map`); a
    ``mmol`` reading is used as-is and wins over a ``mg`` one, which is converted to
    mM only as a fallback. ``"-"``/blank cells are skipped (missing, not zero).
    """
    arr = grid.values
    n_rows, n_cols = grid.shape
    col_unit = _unit_column_map(arr, n_rows, n_cols)

    elements: list[tuple[int, str]] = []
    for r in range(n_rows):
        for c in range(n_cols):
            key = _norm_text(arr[r][c])
            if key in ELEMENT_TO_COLUMN:
                elements.append((r, ELEMENT_TO_COLUMN[key]))
                break
    elements.sort()

    records: list[dict] = []
    for i, (erow, schema_col) in enumerate(elements):
        end = elements[i + 1][0] if i + 1 < len(elements) else n_rows
        # Header row = first row in the block carrying condition labels.
        header_row = None
        cond_cols: dict[int, str] = {}
        for r in range(erow, end):
            hc = {c: _match_condition(arr[r][c]) for c in range(n_cols)
                  if _match_condition(arr[r][c])}
            if hc:
                header_row, cond_cols = r, hc
                break
        if header_row is None:
            continue
        time_col = _guess_time_col(arr, header_row, min(cond_cols), n_cols)
        if time_col is None:
            continue

        element = COLUMN_TO_ELEMENT[schema_col]
        for rr in range(header_row + 1, end):
            t = _to_int(arr[rr][time_col])
            if t is None:
                continue
            for c, code in cond_cols.items():
                v = _to_number(arr[rr][c])
                if v is None:
                    continue
                unit = col_unit.get(c, "mmol")  # no unit row -> already mM
                value_mM = im.convert_concentration(v, element, "mg/L") if unit == "mg" else v
                records.append({
                    "element_col": schema_col,
                    "condition_code": code,
                    "time_min": t,
                    "value_mM": value_mM,
                    "unit_source": unit,
                })

    long = pd.DataFrame(
        records, columns=["element_col", "condition_code", "time_min", "value_mM", "unit_source"]
    )
    if long.empty:
        return long
    # Prefer mmol over mg for the same (element, code, time).
    long["_pref"] = (long["unit_source"] == "mmol").astype(int)
    long = (
        long.sort_values("_pref", ascending=False)
        .drop_duplicates(["element_col", "condition_code", "time_min"], keep="first")
        .drop(columns="_pref")
        .reset_index(drop=True)
    )
    return long


def icp_debug_pivots(icp_long: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Per-element time×condition tables of the chosen value_mM (for the debug view)."""
    out: dict[str, pd.DataFrame] = {}
    if icp_long is None or icp_long.empty:
        return out
    for schema_col in ("Ca_mM", "Si_mM", "Al_mM"):
        sub = icp_long[icp_long["element_col"] == schema_col]
        if sub.empty:
            continue
        pivot = (
            sub.pivot_table(index="time_min", columns="condition_code",
                            values="value_mM", aggfunc="first")
            .reset_index()
        )
        out[schema_col] = pivot
    return out


# --------------------------------------------------------------------------- #
# Normalise the whole workbook
# --------------------------------------------------------------------------- #
def normalize_dissolution_workbook(
    source,
    *,
    defaults: dict | None = None,
    include_hcl: bool = True,
    filename: str = "",
    import_timestamp: str | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Normalise the dissolution workbook into the release schema + a report.

    ``defaults`` fills the operator-supplied metadata
    (:data:`DEFAULT_FILL_FIELDS`) on every row; ``fly_ash_type`` defaults to
    ``"Class C fly ash"``. ``include_hcl=False`` drops the acid pH-only rows.
    Returns ``(schema_df, report)`` where ``report`` holds the parse counts and
    warnings for the pre-save preview. Acid rows keep ``NaOH_M`` blank and carry
    :data:`import_mapping.ACID_IMPORT_NOTE`.
    """
    defaults = defaults or {}
    ts = import_timestamp or datetime.now().isoformat(timespec="seconds")
    fly_ash = (defaults.get("fly_ash_type") or "").strip() or FLY_ASH_DEFAULT

    xl = _as_excelfile(source)
    icp_name, ph_name = find_workbook_sheets(xl)
    ph_rows = parse_ph_sheet(xl.parse(ph_name, header=None))
    icp_long = parse_icp_sheet(xl.parse(icp_name, header=None))

    chem_lookup: dict[tuple[str, int], dict[str, float]] = {}
    for rec in icp_long.to_dict("records"):
        chem_lookup.setdefault((rec["condition_code"], rec["time_min"]), {})[
            rec["element_col"]
        ] = rec["value_mM"]

    out_rows: list[dict] = []
    n_naoh = n_hcl = 0
    for pr in ph_rows:
        is_acid = im.is_acid_leachant(pr["leachant"])
        if is_acid and not include_hcl:
            continue
        code, time_i, conc = pr["condition_code"], pr["time_min"], pr["conc"]
        conc_label = f"{conc}M-" if conc else ""
        leach_label = "HCl" if is_acid else "NaOH"
        sample_id = f"{conc_label}{leach_label}-{code}-{time_i}min"

        row = {col: "" for col in config.EXPERIMENTAL_RELEASE_COLUMNS}
        for field in DEFAULT_FILL_FIELDS:
            if field == "fly_ash_type":
                row[field] = fly_ash
            elif not _is_blank(defaults.get(field)):
                row[field] = defaults.get(field)
        row["sample_id"] = sample_id
        row["time_min"] = time_i
        row["final_pH"] = "" if pr["final_pH"] is None else pr["final_pH"]

        note_bits = [f"condition_code={code}", f"source_label={pr['source_label']}"]
        if is_acid:
            n_hcl += 1
            row["NaOH_M"] = ""  # never force acid data into NaOH_M
            row[im.LEACHANT_COLUMN] = "HCl"
            row[im.ACID_M_COLUMN] = conc
            row["import_warning"] = im.ACID_IMPORT_NOTE
            note_bits.append(im.ACID_IMPORT_NOTE)
        else:
            n_naoh += 1
            row["NaOH_M"] = conc
            row[im.LEACHANT_COLUMN] = "NaOH"
            row[im.ACID_M_COLUMN] = ""
            row["import_warning"] = ""
            for schema_col, value in chem_lookup.get((code, time_i), {}).items():
                row[schema_col] = value

        row["notes"] = "; ".join(note_bits)
        row["original_file_name"] = filename
        row["original_sheet_name"] = f"{icp_name} + {ph_name}"
        row["original_row_number"] = ""
        row["import_timestamp"] = ts
        row["units_assumed"] = "ICP: mmol/l preferred, mg/L→mM by atomic mass"
        # The cup cover IS the CO2 condition: store the OA/PF/GS code in
        # CO2_condition (the cup-cover vocabulary), overriding any shared default.
        if code:
            row["CO2_condition"] = code
        row[EXTRA_CONDITION_COLUMN] = code
        # Optional derived fields — OA/PF/GS are known cup-cover / CO2 exposure
        # conditions. Kept as extra columns so they ride through unchanged.
        row[EXTRA_COVER_COLUMN] = scenarios.cover_condition(code) or ""
        row[EXTRA_CO2_EXPOSURE_COLUMN] = scenarios.co2_exposure_level(code) or ""
        out_rows.append(row)

    ordered = (
        list(config.EXPERIMENTAL_RELEASE_COLUMNS)
        + [im.LEACHANT_COLUMN, im.ACID_M_COLUMN]
        + im.PROVENANCE_COLUMNS
        + [EXTRA_CONDITION_COLUMN, EXTRA_COVER_COLUMN, EXTRA_CO2_EXPOSURE_COLUMN]
    )
    schema_df = pd.DataFrame(out_rows, columns=ordered) if out_rows else pd.DataFrame(columns=ordered)

    report = _build_report(schema_df, n_naoh, n_hcl, icp_long)
    return schema_df, report


def _build_report(schema_df: pd.DataFrame, n_naoh: int, n_hcl: int,
                  icp_long: pd.DataFrame) -> dict:
    """Counts + warnings for the pre-save preview (Features 8 & 9)."""
    from . import run_manager  # local import avoids a cycle

    chem_cols = list(COLUMN_TO_ELEMENT.keys())
    if schema_df.empty:
        n_with_ph = n_with_chem = rows_missing_metadata = 0
    else:
        n_with_ph = int(schema_df["final_pH"].apply(lambda v: not _is_blank(v)).sum())
        n_with_chem = int(
            schema_df[chem_cols].apply(lambda r: any(not _is_blank(v) for v in r), axis=1).sum()
        )
        meta_cols = [c for c in run_manager.LAB_REQUIRED_COLUMNS if c in schema_df.columns]
        rows_missing_metadata = int(
            schema_df[meta_cols].apply(lambda r: any(_is_blank(v) for v in r), axis=1).sum()
        )

    warnings = [
        "Fe, Na, K, Sc and total REE are not present in this workbook unless found elsewhere — "
        "those columns stay blank (blank ≠ zero).",
        "OA / PF / GS are cup-cover / CO2 exposure conditions (OA = open air, PF = plastic flap "
        "cover, GS = glass cover) and are written to CO2_condition per row. PF and GS are covered, "
        "reduced-CO2-exchange conditions — not fully sealed unless airtight sealing is confirmed.",
        "HCl rows are acid leaching and must not be mapped to NaOH PHREEQC scenarios.",
    ]
    return {
        "n_naoh": n_naoh,
        "n_hcl": n_hcl,
        "n_with_ph": n_with_ph,
        "n_with_chem": n_with_chem,
        "rows_missing_metadata": rows_missing_metadata,
        "n_icp_chem_points": int(len(icp_long)),
        "warnings": warnings,
        "icp_long": icp_long,
        "icp_debug": icp_debug_pivots(icp_long),
    }
