"""Step 4 — generate the Phase-1 exploratory plots.

Reads the processed PHREEQC CSVs and writes PNGs to ``reports/figures``.

Run:  python scripts/04_make_plots.py
"""
from __future__ import annotations

import _path_setup  # noqa: F401  (adds project root to sys.path; must precede package import)

import pandas as pd

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.viz import make_phase1_plots


def _read(name: str) -> pd.DataFrame:
    path = config.PROCESSED_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run scripts/01_parse_phreeqc.py first."
        )
    return pd.read_csv(path)


def main() -> None:
    config.ensure_output_dirs()

    results = _read(config.PHREEQC_RESULTS_CSV)
    saturation = _read(config.PHREEQC_SI_CSV)

    written = make_phase1_plots(results, saturation, config.FIGURES_DIR)
    for p in written:
        print(f"  wrote {p}")
    print(f"  {len(written)} figure(s) in {config.FIGURES_DIR}")


if __name__ == "__main__":
    main()
