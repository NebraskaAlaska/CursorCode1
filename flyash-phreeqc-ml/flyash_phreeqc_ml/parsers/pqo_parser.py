"""Parser for PHREEQC **output** files (``.pqo``).

PHREEQC's text output is a sequence of *simulations*; within each simulation it
prints results for one or more *solution states*. A state is either:

* an **initial solution** calculation (header line ``Initial solution N.``), or
* a **batch-reaction** result (``Reaction step`` / ``Using solution N.``), which
  is the solution *after* equilibration with the pure phases.

For every state PHREEQC prints up to four blocks we care about, each introduced
by a dashed banner line::

    -----------------------------Solution composition------------------------------
    ----------------------------Description of solution----------------------------
    -------------------------------Phase assemblage--------------------------------
    ------------------------------Saturation indices-------------------------------

This parser walks the file line by line, tracks the current (simulation, state,
solution) context, and fills one ``SolutionRecord`` per state. It is deliberately
tolerant: blocks may be missing (e.g. an initial solution has no phase assemblage)
and numeric columns may be printed as ``0`` or ``0.000e+00`` interchangeably.

Three tidy tables come out:

* ``results``     — one wide row per solution state (scalars + element molalities +
                    a compact set of key saturation indices + key phase deltas)
* ``saturation``  — long form: one row per (state, phase) saturation index
* ``assemblage``  — long form: one row per (state, phase) assemblage delta
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from ..config import KEY_PHASES
from ._common import to_float

# --------------------------------------------------------------------------- #
# Banner / marker recognition
# --------------------------------------------------------------------------- #
# A "banner" is a line of dashes optionally wrapping a title, e.g.
# "----Solution composition----" or a bare "------------------".
_BANNER_RE = re.compile(r"^-{3,}(?P<title>.*?)-*$")

_SIM_RE = re.compile(r"Reading input data for simulation\s+(\d+)")
_INITIAL_RE = re.compile(r"^\s*Initial solution\s+(\d+)\.?(?:\s+(.*))?$")
_REACTION_RE = re.compile(r"^\s*Reaction step\s+(\d+)\.?")
_USING_SOL_RE = re.compile(r"^\s*Using solution\s+(\d+)\.?(?:\s+(.*))?$")

# Map a banner title to the block-parsing mode it opens.
_BLOCK_TITLES = {
    "solution composition": "composition",
    "description of solution": "description",
    "phase assemblage": "assemblage",
    "saturation indices": "saturation",
}

# Recognised scalar fields in the "Description of solution" block. The keys are
# normalised (lower-case, no units) substrings; the values are output column names.
_DESCRIPTION_FIELDS = {
    "ph": "pH",
    "pe": "pe",
    "activity of water": "activity_of_water",
    "ionic strength": "ionic_strength",
    "mass of water": "mass_water_kg",
    "total alkalinity": "alkalinity_eq_kg",
    "total co2": "total_co2_mol_kg",
    "total carbon": "total_carbon_mol_kg",
    "temperature": "temperature_c",
    "electrical balance": "electrical_balance_eq",
    "percent error": "percent_error",
}


@dataclass
class SolutionRecord:
    """All extracted scalars/vectors for a single PHREEQC solution state."""

    source_file: str
    simulation: Optional[int]
    state: str  # "initial" or "batch"
    solution_number: Optional[int]
    solution_label: str = ""
    scalars: dict = field(default_factory=dict)        # pH, pe, ionic_strength, ...
    elements: dict = field(default_factory=dict)       # element -> molality
    saturation: dict = field(default_factory=dict)     # phase -> SI
    assemblage: dict = field(default_factory=dict)     # phase -> {initial,final,delta}

    @property
    def key(self) -> str:
        return f"{self.source_file}|sim{self.simulation}|{self.state}|sol{self.solution_number}"


# --------------------------------------------------------------------------- #
# Block parsers (each receives one already-split token list for a data line)
# --------------------------------------------------------------------------- #
def _parse_composition_line(tokens: list[str], rec: SolutionRecord) -> None:
    """'Al  7.402e-04  7.402e-04' -> elements['Al'] = molality (first number)."""
    if len(tokens) < 2:
        return
    molality = to_float(tokens[1])
    if molality is not None:
        rec.elements[tokens[0]] = molality


def _parse_description_line(line: str, rec: SolutionRecord) -> None:
    """'pH  =  13.100  Charge balance' -> scalars['pH'] = 13.1."""
    if "=" not in line:
        return
    label, _, value = line.partition("=")
    label_norm = label.strip().lower()
    value_tok = value.strip().split()
    number = to_float(value_tok[0]) if value_tok else None
    if number is None:
        return
    for needle, column in _DESCRIPTION_FIELDS.items():
        if label_norm.startswith(needle):
            rec.scalars[column] = number
            return


def _parse_assemblage_line(tokens: list[str], rec: SolutionRecord) -> None:
    """'Cal 0.00 -8.48 -8.48 0.000e+00 2.355e-03 2.355e-03'.

    Columns: Phase SI logIAP logK Initial Final Delta (6 numbers after the name).
    """
    if len(tokens) < 7:
        return
    phase = tokens[0]
    nums = [to_float(t) for t in tokens[1:7]]
    if any(n is None for n in nums):
        return
    si, log_iap, log_k, initial, final, delta = nums
    rec.assemblage[phase] = {
        "si": si,
        "log_iap": log_iap,
        "log_k": log_k,
        "initial": initial,
        "final": final,
        "delta": delta,
    }


def _parse_saturation_line(tokens: list[str], rec: SolutionRecord) -> None:
    """'Cal 0.00 -8.48 -8.48 CaCO3'.

    Columns from the right: ... SI logIAP logK formula. The phase name is a single
    token; the formula (last token) has no spaces, so parsing right-to-left is robust
    even for names like 'hemicarbonat10.5' that merge name+number visually.
    """
    if len(tokens) < 5:
        return
    si = to_float(tokens[-4])
    log_iap = to_float(tokens[-3])
    log_k = to_float(tokens[-2])
    if si is None or log_iap is None or log_k is None:
        return  # header / footer line
    phase = " ".join(tokens[:-4])
    rec.saturation[phase] = si


# --------------------------------------------------------------------------- #
# Main file walker
# --------------------------------------------------------------------------- #
def parse_pqo_file(path: str | Path) -> list[SolutionRecord]:
    """Parse one ``.pqo`` file into a list of :class:`SolutionRecord`."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    records: list[SolutionRecord] = []
    current: Optional[SolutionRecord] = None
    sim: Optional[int] = None
    state: str = "initial"
    mode: Optional[str] = None  # active block parser

    def start_record(state_: str, number: Optional[int], label: str) -> SolutionRecord:
        nonlocal current
        rec = SolutionRecord(
            source_file=path.name,
            simulation=sim,
            state=state_,
            solution_number=number,
            solution_label=label.strip() if label else "",
        )
        records.append(rec)
        current = rec
        return rec

    for raw in lines:
        # 1) Banner lines switch block mode and sometimes the calc state.
        banner = _BANNER_RE.match(raw.strip())
        if banner is not None and (raw.strip().startswith("-")):
            title = banner.group("title").strip().lower()
            if "initial solution calculations" in title:
                state = "initial"
                mode = None
            elif "batch-reaction calculations" in title:
                state = "batch"
                mode = None
            else:
                mode = _BLOCK_TITLES.get(title)  # None for unrelated banners
            continue

        # 2) Context markers (may appear while a previous block mode is still set,
        #    so they are checked *before* feeding the line to a block parser).
        m = _SIM_RE.search(raw)
        if m:
            sim = int(m.group(1))
            mode = None
            continue

        m = _INITIAL_RE.match(raw)
        if m:
            start_record("initial", int(m.group(1)), m.group(2) or "")
            mode = None
            continue

        m = _REACTION_RE.match(raw)
        if m:
            # A reaction step begins a new batch record; the solution number is
            # confirmed by the following "Using solution" line.
            start_record("batch", None, "")
            mode = None
            continue

        m = _USING_SOL_RE.match(raw)
        if m:
            if current is not None and current.state == "batch" and current.solution_number is None:
                current.solution_number = int(m.group(1))
                if m.group(2):
                    current.solution_label = m.group(2).strip()
            else:
                start_record("batch", int(m.group(1)), m.group(2) or "")
            mode = None
            continue

        # 3) Feed data lines to the active block parser.
        if current is None or mode is None:
            continue
        stripped = raw.strip()
        if not stripped:
            continue
        tokens = stripped.split()

        if mode == "composition":
            _parse_composition_line(tokens, current)
        elif mode == "description":
            _parse_description_line(stripped, current)
        elif mode == "assemblage":
            _parse_assemblage_line(tokens, current)
        elif mode == "saturation":
            _parse_saturation_line(tokens, current)

    return records


# --------------------------------------------------------------------------- #
# Record -> DataFrame conversion
# --------------------------------------------------------------------------- #
def records_to_frames(
    records: list[SolutionRecord],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Convert records into (results_wide, saturation_long, assemblage_long)."""
    result_rows: list[dict] = []
    sat_rows: list[dict] = []
    asm_rows: list[dict] = []

    for rec in records:
        base = {
            "record_key": rec.key,
            "source_file": rec.source_file,
            "simulation": rec.simulation,
            "state": rec.state,
            "solution_number": rec.solution_number,
            "solution_label": rec.solution_label,
        }

        # --- wide results row ---
        wide = dict(base)
        wide.update(rec.scalars)
        for element, molality in rec.elements.items():
            wide[f"mol_{element}"] = molality
        # Compact, consistent set of key saturation indices.
        for phase in KEY_PHASES:
            wide[f"SI_{phase}"] = rec.saturation.get(phase)
        # Key phase-assemblage deltas (moles precipitated/dissolved).
        for phase, vals in rec.assemblage.items():
            wide[f"delta_{phase}"] = vals["delta"]
        result_rows.append(wide)

        # --- long saturation rows ---
        for phase, si in rec.saturation.items():
            sat_rows.append({**base, "phase": phase, "SI": si})

        # --- long assemblage rows ---
        for phase, vals in rec.assemblage.items():
            asm_rows.append({**base, "phase": phase, **vals})

    results = pd.DataFrame(result_rows)
    saturation = pd.DataFrame(sat_rows)
    assemblage = pd.DataFrame(asm_rows)
    return results, saturation, assemblage


def parse_all_pqo(
    paths: Iterable[str | Path],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Parse every ``.pqo`` file in *paths* and return the three concatenated frames."""
    all_records: list[SolutionRecord] = []
    for p in paths:
        all_records.extend(parse_pqo_file(p))
    return records_to_frames(all_records)
