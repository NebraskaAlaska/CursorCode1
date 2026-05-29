"""Step 1 — parse all PHREEQC inputs (.pqi) and outputs (.pqo).

Writes to ``data/processed``:
    phreeqc_input_solutions.csv
    phreeqc_input_equilibrium_phases.csv
    phreeqc_results.csv
    phreeqc_saturation_indices.csv
    phreeqc_phase_assemblage.csv

Run:  python scripts/01_parse_phreeqc.py
"""
from __future__ import annotations

import _path_setup  # noqa: F401  (adds project root to sys.path; must precede package import)

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.parsers import parse_all_pqi, parse_all_pqo


def main() -> None:
    config.ensure_output_dirs()

    pqi_files = sorted(config.PHREEQC_INPUT_DIR.glob("*.pqi"))
    pqo_files = sorted(config.PHREEQC_OUTPUT_DIR.glob("*.pqo"))
    print(f"Found {len(pqi_files)} .pqi and {len(pqo_files)} .pqo files.")

    # --- inputs ---
    input_solutions, input_phases = parse_all_pqi(pqi_files)
    input_solutions.to_csv(
        config.PROCESSED_DIR / config.PHREEQC_INPUT_SOLUTIONS_CSV, index=False
    )
    input_phases.to_csv(
        config.PROCESSED_DIR / config.PHREEQC_EQUILIBRIUM_PHASES_CSV, index=False
    )
    print(
        f"  inputs: {len(input_solutions)} solution-element rows, "
        f"{len(input_phases)} equilibrium-phase rows"
    )

    # --- outputs ---
    results, saturation, assemblage = parse_all_pqo(pqo_files)
    results.to_csv(config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV, index=False)
    saturation.to_csv(config.PROCESSED_DIR / config.PHREEQC_SI_CSV, index=False)
    assemblage.to_csv(config.PROCESSED_DIR / config.PHREEQC_ASSEMBLAGE_CSV, index=False)
    print(
        f"  outputs: {len(results)} solution-state rows, "
        f"{len(saturation)} saturation-index rows, "
        f"{len(assemblage)} phase-assemblage rows"
    )
    print(f"  wrote CSVs to {config.PROCESSED_DIR}")


if __name__ == "__main__":
    main()
