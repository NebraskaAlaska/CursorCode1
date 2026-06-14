"""Synthetic workbook generators — mirror the real ICP workbooks without their data.

The real research workbooks (the CFA+MK mix-design file in ``data/raw/icp_mix_design/``
and the lab dissolution workbook) are confidential and must not be relied on in tests.
These generators write **synthetic** ``.xlsx`` files that reproduce the real files'
*sheet / block structure* — so the parsers are exercised against the true layout — with
made-up numbers. Tests call them with a ``tmp_path`` destination; nothing is committed.

* :func:`write_mix_design_workbook` — mirrors ``CFA + MK design mix_UMass.xlsx``: a single
  sheet with "Chemical Composition of <material>" blocks (oxide header row + weight-%
  row), the layout :func:`parsers.icp_parser.extract_oxide_tables` reads.
* :func:`write_dissolution_workbook` — mirrors the Class C fly-ash dissolution workbook:
  a horizontal ICP OES sheet (mg/L + mmol/l unit groups, OA/PF/GS condition columns,
  per-element blocks with reaction-time rows) and a pH sheet, the layout
  :mod:`flyash_phreeqc_ml.dissolution_workbook` reads.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

N = None  # a blank cell


# --------------------------------------------------------------------------- #
# CFA + MK mix-design workbook (parsed by icp_parser.extract_oxide_tables)
# --------------------------------------------------------------------------- #
# Synthetic oxide weight-% values (NOT the real composition). Asserted in tests.
MIX_DESIGN_VALUES = {
    "CFA": {"SiO2": 38.56, "Al2O3": 19.8, "Fe2O3": 6.26, "MgO": 5.15,
            "CaO": 22.28, "SO3": 1.72, "K2O": 0.61, "Na2O": 1.58},
    "MK": {"SiO2": 56.61, "Al2O3": 39.16, "Fe2O3": 1.87, "MgO": 0.09,
           "CaO": 0.05, "SO3": 0.05, "K2O": 0.3, "Na2O": 0.01},
}
_OXIDES = ["SiO2", "Al2O3", "Fe2O3", "MgO", "CaO", "SO3", "K2O", "Na2O"]


def _composition_block(material: str, label: str | None) -> list[list]:
    vals = MIX_DESIGN_VALUES[material]
    return [
        [N, N, f"Chemical Composition of {material}"],          # title (col 2)
        [N, N, *_OXIDES],                                       # oxide header (cols 2-9)
        [N, label, *[vals[o] for o in _OXIDES]],               # weight-% (cols 2-9)
    ]


def mix_design_grid() -> list[list]:
    """The header-less cell grid the mix-design sheet holds (CFA then MK blocks)."""
    grid: list[list] = []
    grid += _composition_block("CFA", "Weight (%) ")
    grid.append([N] * 10)
    grid += _composition_block("MK", N)
    grid.append([N] * 10)
    # A filler "assumption" row, mirroring the real file's extra mass-balance cells.
    grid.append([N, N, N, N, N, N, "MK Mass Assumption", N, N, 260])
    return grid


def write_mix_design_workbook(path: str | Path, *, sheet_name: str = "CFA-MK-A") -> Path:
    """Write a synthetic CFA+MK mix-design workbook to ``path``; return it."""
    path = Path(path)
    pd.DataFrame(mix_design_grid()).to_excel(
        path, sheet_name=sheet_name, index=False, header=False)
    return path


# --------------------------------------------------------------------------- #
# Class C fly-ash dissolution workbook (parsed by dissolution_workbook)
# --------------------------------------------------------------------------- #
def dissolution_grids() -> tuple[list[list], list[list]]:
    """The (ICP OES grid, pH grid) for the synthetic dissolution workbook."""
    hdr = [N, "Time", N, "NaOH-OA", "NaOH-PF", "NaOH-GS", N, "NaOH-OA", "NaOH-PF", "NaOH-GS"]
    icp = [
        [N, N, N, "mg/L", N, N, N, "mmol/l", N, N],            # global unit row
        ["Calcium", N, N, N, N, N, N, N, N, N],
        hdr,
        [N, 10, N, 93.43, 5.74, 6.87, N, 2.5, 2.1, 1.8],       # mmol preferred over mg
        [N, 60, N, 7.82, 11.66, 4.185, N, 3.0, 2.6, 2.2],
        [N, 720, N, 20.25, "-", "-", N, 0.5, "-", "-"],        # PF/GS missing at 720
        [N] * 10,
        ["Silicon", N, N, N, N, N, N, N, N, N],
        hdr,
        [N, 10, N, 8.64, 20.31, 18.7, N, 1.2, 1.0, 0.8],
        [N, 60, N, 51.76, 43.36, 32.81, N, 1.5, 1.3, 1.1],
        [N] * 10,
        ["Aluminum", N, N, N, N, N, N, N, N, N],
        hdr,
        [N, 10, N, 54.0, 79.5, 71.0, N, "-", 2.9, 2.6],        # OA mmol missing -> mg fallback
        [N, 60, N, 60.0, 91.9, 69.9, N, 4.8, 3.4, 2.5],
    ]
    ph = [
        ["Sample", "Time (min)", "pH", N, N, N, N, "pH", N, N],
        ["0.5M NaOH-OA-10", 10, 13.1, N, N, "Time", N, "NaOH-OA", "NaOH-PF", "NaOH-GS"],
        ["0.5M NaOH-OA-60", 60, 13.05, N, N, 10, N, 13.1, 14, 14],
        ["0.5M NaOH-OA-720", 720, 12.7, N, N, 20, N, 13.15, 14, 13.99],   # 20-min only in matrix
        ["0.5M NaOH-PF-10", 10, 14, N, N, 60, N, 13.05, 13.88, 13.89],
        ["0.5M NaOH-PF-60", 60, 13.88, N, N, N, N, N, N, N],
        ["0.5M NaOH-PF-720", 720, "-", N, N, N, N, N, N, N],             # pH "-" -> blank
        ["0.5M NaOH-GS-10", 10, 14, N, N, N, N, N, N, N],
        ["0.5M NaOH-GS-60", 60, 13.89, N, N, N, N, N, N, N],
        ["0.5M NaOH-GS-720", 720, "-", N, N, N, N, N, N, N],
        ["0.5M HCL-OA-10", 10, 3.21, N, N, N, N, N, N, N],
    ]
    return icp, ph


def write_dissolution_workbook(path: str | Path) -> Path:
    """Write a synthetic Class C fly-ash dissolution workbook to ``path``; return it."""
    path = Path(path)
    icp, ph = dissolution_grids()
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame(icp).to_excel(writer, sheet_name="ICP OES", index=False, header=False)
        pd.DataFrame(ph).to_excel(writer, sheet_name="pH", index=False, header=False)
    return path
