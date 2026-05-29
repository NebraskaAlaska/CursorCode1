"""Parser for PHREEQC **input** files (``.pqi``).

We extract two things that are useful as ML features / join keys:

1. ``SOLUTION`` blocks  -> composition of each input solution
   (number, optional label, temp, pH, units, density, element concentrations).
2. ``EQUILIBRIUM_PHASES`` blocks -> the pure phases each solution is equilibrated
   against (e.g. ``CO2(g)``, calcite, portlandite) with their target SI and amount.

The grammar is line-oriented and forgiving: comments (``#``) and inline comments
are stripped, keywords are matched case-insensitively, and unknown keywords are
ignored rather than raising.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from ._common import to_float

# Keywords that introduce a new top-level block.
_BLOCK_KEYWORDS = {
    "SOLUTION",
    "EQUILIBRIUM_PHASES",
    "SELECTED_OUTPUT",
    "SOLUTION_SPREAD",
    "REACTION",
    "USE",
    "SAVE",
    "END",
    "TITLE",
    "DATABASE",
}

# Per-solution scalar keywords (everything else inside SOLUTION is an element).
_SOLUTION_SCALARS = {"temp", "ph", "pe", "units", "density", "redox", "temperature"}


def _strip_comment(line: str) -> str:
    """Remove ``#`` comments (PHREEQC's comment marker) and trailing whitespace."""
    return line.split("#", 1)[0].rstrip()


def _split_keyword(line: str) -> tuple[str, str]:
    """Return (first_token_upper, remainder) for a stripped, non-empty line."""
    parts = line.strip().split(None, 1)
    head = parts[0].upper()
    rest = parts[1] if len(parts) > 1 else ""
    return head, rest


def parse_pqi_file(path: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Parse one ``.pqi`` file.

    Returns
    -------
    (solutions_df, phases_df)
        ``solutions_df`` has one row per (file, solution number, element) in long
        form plus the solution scalars repeated; we also pivot a wide view in the
        dataset builder. ``phases_df`` has one row per (file, equilibrium-phases
        block, phase).
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")

    solution_rows: list[dict] = []
    phase_rows: list[dict] = []

    current_block: Optional[str] = None  # "SOLUTION" or "EQUILIBRIUM_PHASES"
    block_number: Optional[int] = None
    block_label: str = ""
    # Scalars accumulated for the current SOLUTION block.
    sol_scalars: dict = {}

    def flush_nothing() -> None:
        """SOLUTION rows are emitted as elements are read, so nothing to flush."""

    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue

        head, rest = _split_keyword(line)

        if head in _BLOCK_KEYWORDS:
            # Starting a new block ends the previous one.
            if head == "SOLUTION":
                current_block = "SOLUTION"
                tokens = rest.split(None, 1)
                block_number = int(tokens[0]) if tokens and tokens[0].isdigit() else None
                block_label = tokens[1].strip() if len(tokens) > 1 else ""
                sol_scalars = {
                    "source_file": path.name,
                    "solution_number": block_number,
                    "solution_label": block_label,
                }
            elif head == "EQUILIBRIUM_PHASES":
                current_block = "EQUILIBRIUM_PHASES"
                tokens = rest.split()
                block_number = int(tokens[0]) if tokens and tokens[0].isdigit() else None
                block_label = " ".join(tokens[1:]) if len(tokens) > 1 else ""
            else:
                # USE / SAVE / END / TITLE / DATABASE / SELECTED_OUTPUT etc.
                current_block = None
                block_number = None
            continue

        # --- inside a block --------------------------------------------------
        if current_block == "SOLUTION":
            key = head.lower()
            if key in _SOLUTION_SCALARS:
                value = rest.strip()
                num = to_float(value.split()[0]) if value else None
                # store both numeric (if parseable) and raw (for 'units')
                sol_scalars[key if key != "temperature" else "temp"] = (
                    num if num is not None else value
                )
            else:
                # Element line, e.g. "Na  0.50000" or "Cl  0 charge".
                tokens = line.split()
                element = tokens[0]
                conc = to_float(tokens[1]) if len(tokens) > 1 else None
                qualifier = " ".join(tokens[2:]) if len(tokens) > 2 else ""
                row = dict(sol_scalars)
                row.update(
                    element=element,
                    concentration=conc,
                    qualifier=qualifier,
                )
                solution_rows.append(row)

        elif current_block == "EQUILIBRIUM_PHASES":
            tokens = line.split()
            phase = tokens[0]
            target_si = to_float(tokens[1]) if len(tokens) > 1 else None
            amount = to_float(tokens[2]) if len(tokens) > 2 else None
            phase_rows.append(
                dict(
                    source_file=path.name,
                    block_number=block_number,
                    phase=phase,
                    target_si=target_si,
                    amount=amount,
                )
            )

    flush_nothing()
    solutions_df = pd.DataFrame(solution_rows)
    phases_df = pd.DataFrame(phase_rows)
    return solutions_df, phases_df


def parse_all_pqi(paths: Iterable[str | Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Parse and concatenate every ``.pqi`` file in *paths*."""
    sol_frames, phase_frames = [], []
    for p in paths:
        sol, ph = parse_pqi_file(p)
        if not sol.empty:
            sol_frames.append(sol)
        if not ph.empty:
            phase_frames.append(ph)

    solutions = (
        pd.concat(sol_frames, ignore_index=True) if sol_frames else pd.DataFrame()
    )
    phases = pd.concat(phase_frames, ignore_index=True) if phase_frames else pd.DataFrame()
    return solutions, phases
