"""Parser for PHREEQC ``SELECTED_OUTPUT`` tables.

When a ``.pqi`` requests ``SELECTED_OUTPUT``, PHREEQC writes a clean, machine-readable
table (whitespace- or tab-delimited) with a header row and one row per solution
state. This is far easier to consume than the verbose ``.pqo`` and is the preferred
source whenever it exists.

The current raw dataset does not ship the generated ``.out`` file, but the revised
input requests one, so this parser is ready for when it is produced. It auto-detects
tab vs. whitespace delimiting and strips PHREEQC's leading/trailing blank columns.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def parse_selected_output(path: str | Path) -> pd.DataFrame:
    """Read a SELECTED_OUTPUT file into a tidy DataFrame.

    Handles both tab-delimited and fixed/space-delimited variants. Column names are
    stripped of surrounding whitespace; fully empty leading/trailing columns
    (artifacts of PHREEQC's formatting) are dropped.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")

    # Decide delimiter from the header line.
    first_line = text.splitlines()[0] if text.splitlines() else ""
    sep = "\t" if "\t" in first_line else r"\s+"

    df = pd.read_csv(path, sep=sep, engine="python")
    df.columns = [str(c).strip() for c in df.columns]

    # Drop columns that are entirely empty / unnamed (PHREEQC pads with blanks).
    df = df.loc[:, [c for c in df.columns if c and not c.startswith("Unnamed")]]
    df = df.dropna(axis=1, how="all")

    df.insert(0, "source_file", path.name)
    return df
