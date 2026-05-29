"""Step 3 — build the joined ``master_dataset.csv``.

Reads the processed PHREEQC CSVs from step 1 and joins each output solution state
to its input composition. Writes ``data/processed/master_dataset.csv``.

Run:  python scripts/03_build_master_dataset.py
"""
from __future__ import annotations

import _path_setup  # noqa: F401  (adds project root to sys.path; must precede package import)

import pandas as pd

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.datasets import build_master_dataset


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
    input_solutions = _read(config.PHREEQC_INPUT_SOLUTIONS_CSV)

    master = build_master_dataset(results, input_solutions)
    out = config.PROCESSED_DIR / config.MASTER_DATASET_CSV
    master.to_csv(out, index=False)
    print(f"  master_dataset: {master.shape[0]} rows x {master.shape[1]} columns")
    print(f"  wrote {out}")


if __name__ == "__main__":
    main()
