"""CSV serialization guard for durable Phase 3 ICP review outputs.

This module is presentation-only: it never recalculates ICP values or changes the
Phase 1B eligibility decision.  Its only scientific-data behavior is preserving
numeric values verbatim while preventing text cells from being interpreted as
spreadsheet formulas.
"""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from io import StringIO
import json
import math
from numbers import Real
from typing import Any, Iterable, Mapping, Sequence


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


class IcpCsvExportError(ValueError):
    """Raised when a requested ICP export cannot be represented safely."""


def _finite_numeric_text(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    try:
        number = Decimal(text)
    except InvalidOperation:
        return False
    return number.is_finite()


def safe_cell(value: Any) -> Any:
    """Return a CSV cell safe for spreadsheet opening.

    Real numeric values, including negatives, remain numeric.  Numeric text such
    as ``-1.25`` also remains unchanged so a defensive export does not mutate a
    legitimate scientific value.  Formula-like non-numeric text is prefixed with
    an apostrophe, the conventional inert spreadsheet-text marker.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, Real):
        if not math.isfinite(float(value)):
            raise IcpCsvExportError("ICP CSV values must be finite")
        return value
    if isinstance(value, (Mapping, list, tuple)):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise IcpCsvExportError(f"ICP CSV structured value is unsafe: {exc}") from exc
    text = str(value)
    stripped = text.lstrip()
    if stripped.startswith(_FORMULA_PREFIXES) and not _finite_numeric_text(text):
        return "'" + text
    return text


def safe_csv_text(rows: Iterable[Mapping[str, Any]], *,
                  columns: Sequence[str] | None = None) -> str:
    """Serialize mapping rows deterministically with formula-injection guards."""
    records = [dict(row) for row in rows]
    if columns is None:
        headings: list[str] = []
        for row in records:
            for key in row:
                if not isinstance(key, str):
                    raise IcpCsvExportError("ICP CSV column names must be strings")
                name = key
                if name not in headings:
                    headings.append(name)
    else:
        headings = [str(item) for item in columns]
    if len(set(headings)) != len(headings):
        raise IcpCsvExportError("ICP CSV columns must be unique")
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headings, extrasaction="ignore",
                            lineterminator="\n")
    writer.writerow({heading: safe_cell(heading) for heading in headings})
    for row in records:
        writer.writerow({heading: safe_cell(row.get(heading)) for heading in headings})
    return output.getvalue()


def safe_csv_bytes(rows: Iterable[Mapping[str, Any]], *,
                   columns: Sequence[str] | None = None) -> bytes:
    """UTF-8 bytes companion for Streamlit download surfaces."""
    return safe_csv_text(rows, columns=columns).encode("utf-8")


__all__ = ["IcpCsvExportError", "safe_cell", "safe_csv_bytes", "safe_csv_text"]
