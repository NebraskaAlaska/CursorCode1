"""Run the full Phase-1 pipeline end to end.

Equivalent to running scripts 01 -> 02 -> 03 -> 04 in order.

Run:  python scripts/run_phase1.py
"""
from __future__ import annotations

import _path_setup  # noqa: F401  (adds project root to sys.path; must precede package import)

import importlib.util
from pathlib import Path

_STEPS = [
    "01_parse_phreeqc.py",
    "02_parse_icp.py",
    "03_build_master_dataset.py",
    "04_make_plots.py",
]


def _run_step(script_path: Path) -> None:
    """Import a numerically-named step module and call its ``main()``."""
    spec = importlib.util.spec_from_file_location(script_path.stem, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.main()


def main() -> None:
    here = Path(__file__).resolve().parent
    for step in _STEPS:
        print(f"\n=== {step} ===")
        _run_step(here / step)
    print("\nPhase 1 complete.")


if __name__ == "__main__":
    main()
