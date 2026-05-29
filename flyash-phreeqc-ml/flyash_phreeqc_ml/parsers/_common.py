"""Small shared helpers used by more than one parser."""
from __future__ import annotations

import re
from typing import Optional

# Matches PHREEQC-style numbers: 1.23, -4, 5.6e-04, 1E+02, etc.
_NUMBER_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def is_number(token: str) -> bool:
    """True if *token* is a plain numeric literal (no units, no trailing text)."""
    return bool(_NUMBER_RE.match(token.strip()))


def to_float(token: str) -> Optional[float]:
    """Parse *token* to float, returning None if it is not numeric."""
    token = token.strip()
    if not is_number(token):
        return None
    try:
        return float(token)
    except ValueError:
        return None
