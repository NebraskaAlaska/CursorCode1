"""Load lab data from CSV/Excel, normalise it, and build blank templates."""

from __future__ import annotations

import io
import os
import re
from typing import Union

import pandas as pd

from . import config

# Accept either a path, a file-like object (Streamlit upload), or raw bytes.
FileLike = Union[str, os.PathLike, io.IOBase, bytes]


def _normalise_name(name: str) -> str:
    """Lower-case, trim, and collapse whitespace/punctuation in a column name.

    This makes header matching forgiving of minor formatting differences
    ("Sample ID", "sample_id", "sample-id" all map to "sample_id").
    """
    cleaned = str(name).strip().lower()
    cleaned = re.sub(r"[^\w]+", "_", cleaned)   # non-word chars -> underscore
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned


# Map a case-insensitive normalised header back to its canonical mixed-case name
# (e.g. "compressive_strength_mpa" -> "compressive_strength_MPa"). Expected columns
# use mixed case for unit suffixes (MPa, pH, uS_cm); lowercasing alone would break
# matching against the rest of the pipeline.
_CANONICAL_BY_LOWER = {c.lower(): c for c in config.EXPECTED_COLUMNS}


def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` with normalised column names.

    Headers are lower-cased and de-punctuated for forgiving matching, then mapped
    back to the canonical mixed-case name where one exists, so that columns like
    ``Compressive Strength (MPa)`` resolve to ``compressive_strength_MPa``.
    """
    df = df.copy()
    df.columns = [
        _CANONICAL_BY_LOWER.get(_normalise_name(c), _normalise_name(c))
        for c in df.columns
    ]
    return df


def coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce known numeric and date columns to proper dtypes.

    Non-parseable values become ``NaN`` (numbers) or ``NaT`` (dates) rather than
    raising, so that downstream validation can flag them instead of crashing.
    """
    df = df.copy()
    for col in config.NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in config.DATE_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    # Trim stray whitespace in text columns and normalise data_status casing.
    for col in config.TEXT_COLUMNS:
        if col in df.columns:
            df[col] = df[col].apply(
                lambda v: v.strip() if isinstance(v, str) else v
            )
    if "data_status" in df.columns:
        df["data_status"] = df["data_status"].apply(
            lambda v: v.strip().lower() if isinstance(v, str) and v.strip() else v
        )
    return df


def ensure_expected_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add any missing expected columns (as empty) so downstream code is safe.

    Existing extra columns are preserved; expected columns are ordered first.
    """
    df = df.copy()
    for col in config.EXPECTED_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    ordered = config.EXPECTED_COLUMNS + [
        c for c in df.columns if c not in config.EXPECTED_COLUMNS
    ]
    return df[ordered]


def _read_raw(source: FileLike, filename: str | None = None) -> pd.DataFrame:
    """Read a CSV or Excel source into a raw DataFrame based on its extension."""
    name = filename
    if name is None and isinstance(source, (str, os.PathLike)):
        name = str(source)
    elif name is None and hasattr(source, "name"):
        name = source.name  # Streamlit UploadedFile exposes .name

    ext = os.path.splitext(name or "")[1].lower()
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(source, engine="openpyxl")
    # Default to CSV for ".csv" and unknown extensions.
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    return pd.read_csv(source)


def load_data(source: FileLike, filename: str | None = None) -> pd.DataFrame:
    """Load, normalise, type-coerce, and shape lab data from CSV/Excel.

    Args:
        source: A file path, file-like object (e.g. Streamlit upload), or bytes.
        filename: Optional explicit name used to detect the file type.

    Returns:
        A cleaned DataFrame with normalised column names, coerced types, and all
        expected columns present.
    """
    raw = _read_raw(source, filename)
    df = normalise_columns(raw)
    df = coerce_types(df)
    df = ensure_expected_columns(df)
    return df


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def make_template_dataframe(include_example: bool = True) -> pd.DataFrame:
    """Build a blank template DataFrame with all expected columns.

    Args:
        include_example: If True, include one illustrative example row.
    """
    df = pd.DataFrame(columns=config.EXPECTED_COLUMNS)
    if include_example:
        example = {
            "sample_id": "S001",
            "mix_id": "MIX-A",
            "specimen_id": "MIX-A-01",
            "test_id": "T0001",
            "date_cast": "2026-01-10",
            "date_tested": "2026-02-07",
            "curing_age_days": 28,
            "mix_type": "fly_ash_cement_blend",
            "fly_ash_mass_g": 300,
            "cement_mass_g": 700,
            "water_mass_g": 400,
            "red_mud_mass_g": 0,
            "sand_mass_g": 2000,
            "additive_type": "none",
            "specimen_shape": "cube",
            "length_mm": 50,
            "width_mm": 50,
            "diameter_mm": "",
            "loaded_area_mm2": 2500,
            "peak_load_kN": 75,
            "flow_mm": 180,
            "setting_time_min": 240,
            "compressive_strength_MPa": 30.0,
            "leachate_pH": 11.5,
            "leachate_conductivity_uS_cm": 1200,
            "data_status": "tested",
            "visual_notes": "no visible cracks",
            "photo_path": "",
        }
        df = pd.DataFrame([example], columns=config.EXPECTED_COLUMNS)
    return df


def template_csv_bytes(include_example: bool = True) -> bytes:
    """Return the template as CSV bytes (for download buttons)."""
    return make_template_dataframe(include_example).to_csv(index=False).encode("utf-8")


def template_excel_bytes(include_example: bool = True) -> bytes:
    """Return the template as XLSX bytes (for download buttons)."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        make_template_dataframe(include_example).to_excel(
            writer, index=False, sheet_name="lab_data"
        )
    return buffer.getvalue()


def write_templates(directory: str) -> dict[str, str]:
    """Write CSV and Excel templates into ``directory``.

    Returns a mapping of format -> written path.
    """
    os.makedirs(directory, exist_ok=True)
    csv_path = os.path.join(directory, "flyash_template.csv")
    xlsx_path = os.path.join(directory, "flyash_template.xlsx")
    with open(csv_path, "wb") as fh:
        fh.write(template_csv_bytes())
    with open(xlsx_path, "wb") as fh:
        fh.write(template_excel_bytes())
    return {"csv": csv_path, "xlsx": xlsx_path}


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Encode a DataFrame as UTF-8 CSV bytes (for export buttons)."""
    return df.to_csv(index=False).encode("utf-8")
