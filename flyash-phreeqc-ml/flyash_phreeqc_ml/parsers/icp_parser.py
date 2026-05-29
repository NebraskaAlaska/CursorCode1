"""Parser for the experimental ICP / mix-design workbook.

The delivered workbook (``CFA + MK design mix_UMass.xlsx``) is a *mix-design
calculator* rather than a tidy data table: a single sheet holds several small
sub-tables (raw-material oxide compositions, activator chemistry, mass balances,
desired molar ratios). It does **not** yet contain measured ICP solution
concentrations (Ca/Si/Al/Fe/REE/Sc release) — those are expected later and will be
wired into Phase 2.

For Phase 1 we therefore do two robust, low-assumption things:

1. ``dump_sheets_raw`` — write every sheet to CSV exactly as a cell grid, so no
   information is lost and the analyst can inspect the real layout in Cursor/Excel.
2. ``extract_oxide_tables`` — best-effort extraction of the clearly-structured
   "Chemical Composition of X" blocks into a tidy long table
   (``material``, ``oxide``, ``weight_pct``). These raw-material compositions are
   useful features (e.g. CFA CaO content) for later modeling.

Everything here is intentionally defensive: if the layout differs, the raw dump
still succeeds and the extraction simply returns fewer rows.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import (
    EXPERIMENTAL_ICP_DIR,
    EXPERIMENTAL_NON_DATA_FILES,
    EXPERIMENTAL_NUMERIC_COLUMNS,
    EXPERIMENTAL_RELEASE_COLUMNS,
    EXPERIMENTAL_TEMPLATE_CSV,
)

# Oxide / component names we recognise as composition-table headers.
_KNOWN_COMPONENTS = {
    "SiO2", "Al2O3", "Fe2O3", "MgO", "CaO", "SO3", "K2O", "Na2O",
    "Moisture", "LOI", "Total", "NaOH", "H2O", "Na2CO3",
}

_TITLE_RE = re.compile(r"chemical composition of\s+(.+)", re.IGNORECASE)


def _read_grid(path: Path) -> dict[str, pd.DataFrame]:
    """Read every sheet as a raw, header-less cell grid (values only)."""
    # header=None keeps the real layout; data is read with formulas evaluated.
    sheets = pd.read_excel(path, sheet_name=None, header=None, engine="openpyxl")
    return sheets


def dump_sheets_raw(path: str | Path, out_dir: str | Path) -> list[Path]:
    """Write each sheet of *path* to ``out_dir`` as ``icp_raw_<sheet>.csv``.

    Returns the list of written file paths.
    """
    path = Path(path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for sheet_name, grid in _read_grid(path).items():
        safe = re.sub(r"[^0-9A-Za-z._-]+", "_", sheet_name).strip("_") or "sheet"
        out_path = out_dir / f"icp_raw_{safe}.csv"
        grid.to_csv(out_path, index=False, header=False)
        written.append(out_path)
    return written


def _clean_component_name(value) -> str | None:
    """Return a normalised component name if *value* looks like an oxide header."""
    if value is None:
        return None
    name = str(value).strip()
    # Trim a trailing footnote-ish space, normalise common variants.
    name_norm = name.replace(" ", "")
    if name_norm in _KNOWN_COMPONENTS:
        return name_norm
    return None


def extract_oxide_tables(path: str | Path) -> pd.DataFrame:
    """Extract 'Chemical Composition of X' blocks into a tidy long table.

    Layout assumption (matches the delivered file): a title cell containing
    ``Chemical Composition of <material>`` sits directly above a header row of
    oxide names, which sits directly above a row of weight-% values. Values are
    matched to oxides by column index, so horizontal offsets are handled.
    """
    path = Path(path)
    rows: list[dict] = []

    for sheet_name, grid in _read_grid(path).items():
        n_rows = grid.shape[0]
        values = grid.values  # ndarray, object dtype

        for r in range(n_rows):
            # Look for a title cell anywhere in this row.
            material = None
            for c in range(grid.shape[1]):
                cell = values[r, c]
                if isinstance(cell, str):
                    m = _TITLE_RE.search(cell)
                    if m:
                        material = m.group(1).strip()
                        break
            if material is None:
                continue

            # Search the next few rows for a header row of known components.
            for header_r in range(r + 1, min(r + 4, n_rows)):
                header_cols: dict[int, str] = {}
                for c in range(grid.shape[1]):
                    comp = _clean_component_name(values[header_r, c])
                    if comp is not None:
                        header_cols[c] = comp
                if len(header_cols) < 2:
                    continue  # not the header row, keep looking

                # Values are expected on a subsequent row (skip blank/label rows).
                for value_r in range(header_r + 1, min(header_r + 3, n_rows)):
                    found_any = False
                    for c, comp in header_cols.items():
                        val = values[value_r, c]
                        if isinstance(val, (int, float, np.integer, np.floating)) and not (
                            isinstance(val, float) and np.isnan(val)
                        ):
                            rows.append(
                                {
                                    "source_sheet": sheet_name,
                                    "material": material,
                                    "oxide": comp,
                                    "weight_pct": float(val),
                                }
                            )
                            found_any = True
                    if found_any:
                        break
                break  # header found; stop scanning rows for this title

    return pd.DataFrame(rows)


def parse_icp_workbook(path: str | Path, out_dir: str | Path) -> dict[str, pd.DataFrame]:
    """High-level entry point used by the pipeline.

    Dumps every sheet to raw CSV (side effect) and returns a dict of tidy frames:

    * ``"oxide_compositions"`` — long table of raw-material oxide weight-%.

    The raw dumps are the authoritative record; the tidy frame is best-effort.
    """
    dump_sheets_raw(path, out_dir)
    return {"oxide_compositions": extract_oxide_tables(path)}


# =========================================================================== #
# Phase 2: measured experimental-release ingestion
# =========================================================================== #
class ExperimentalSchemaError(ValueError):
    """Raised when a measured-experimental-release file does not match the schema."""


def parse_experimental_release(
    path: str | Path,
    *,
    strict: bool = True,
) -> pd.DataFrame:
    """Read a filled ``experimental_release`` CSV (the Monday lab data).

    The expected columns are defined once in
    :data:`flyash_phreeqc_ml.config.EXPERIMENTAL_RELEASE_COLUMNS`. Numeric columns
    are coerced to numbers (blank/garbage -> NaN), ``experiment_date`` is parsed to
    a datetime, and columns are returned in canonical order with a ``source_file``
    provenance column prepended. Unknown extra columns are kept (appended at the
    end) so the lab can add notes without breaking the parser.

    Parameters
    ----------
    strict:
        If True (default), a missing *required* column raises
        :class:`ExperimentalSchemaError`. If False, missing columns are added as
        all-NaN so partially-filled files still load.
    """
    path = Path(path)
    df = pd.read_csv(path, dtype=str, keep_default_na=True, skipinitialspace=True)
    df.columns = [str(c).strip() for c in df.columns]

    expected = list(EXPERIMENTAL_RELEASE_COLUMNS)
    missing = [c for c in expected if c not in df.columns]
    extra = [c for c in df.columns if c not in expected]

    if missing:
        if strict:
            raise ExperimentalSchemaError(
                f"{path.name}: missing required column(s): {missing}"
            )
        for c in missing:
            df[c] = np.nan  # tolerate partially-filled files

    # Coerce numeric columns; non-numeric entries become NaN.
    for col in EXPERIMENTAL_NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Parse the experiment date (invalid -> NaT).
    if "experiment_date" in df.columns:
        df["experiment_date"] = pd.to_datetime(df["experiment_date"], errors="coerce")

    # Canonical column order, keeping any extra columns at the end.
    ordered = expected + extra
    df = df[ordered]
    df.insert(0, "source_file", path.name)
    return df


def load_experimental_release(
    directory: str | Path | None = None,
    *,
    include_template: bool = False,
    strict: bool = True,
) -> pd.DataFrame:
    """Load and concatenate every measured-release CSV in *directory*.

    The blank template file is skipped by default (it has no data rows). Returns an
    empty DataFrame (with the right columns) when no measured files are present —
    this is the normal state until Monday's data arrives.
    """
    directory = Path(directory) if directory is not None else EXPERIMENTAL_ICP_DIR
    if not directory.exists():
        return pd.DataFrame(columns=["source_file"] + EXPERIMENTAL_RELEASE_COLUMNS)

    # Files that are not measured-release data (mapping table; and the blank
    # template unless explicitly included).
    skip = set(EXPERIMENTAL_NON_DATA_FILES)
    if include_template:
        skip.discard(EXPERIMENTAL_TEMPLATE_CSV)

    frames: list[pd.DataFrame] = []
    for csv_path in sorted(directory.glob("*.csv")):
        if csv_path.name in skip:
            continue
        frames.append(parse_experimental_release(csv_path, strict=strict))

    if not frames:
        return pd.DataFrame(columns=["source_file"] + EXPERIMENTAL_RELEASE_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def has_measured_data(df: pd.DataFrame) -> bool:
    """True if *df* contains at least one row with an actual measured value.

    "Measured" means any of the key analytes / pH is populated — a header-only
    template (zero rows) or a file of pure metadata returns False, which the
    pipeline uses to decide whether to run the comparison and plots.
    """
    if df is None or df.empty:
        return False
    signal_cols = [
        c
        for c in ["Ca_mM", "Si_mM", "Al_mM", "Fe_mM", "final_pH"]
        if c in df.columns
    ]
    if not signal_cols:
        return False
    return bool(df[signal_cols].notna().any().any())
