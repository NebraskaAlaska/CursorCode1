"""Step 6 — generate Monday's experiment plan (a run sheet).

Writes ``data/raw/experimental_icp/monday_experiment_plan.csv`` with one row per
planned condition (de-duplicated by sample_id) and prints a per-set summary.

Run:  python scripts/06_generate_experiment_plan.py
"""
from __future__ import annotations

import _path_setup  # noqa: F401  (adds project root to sys.path; must precede package import)

from flyash_phreeqc_ml.experiments.plan_generator import summarize_plan, write_experiment_plan


def main() -> None:
    path, df = write_experiment_plan()
    print(f"  wrote {path}  ({len(df)} unique sample(s))")
    print("  samples per experiment set:")
    for set_name, count in summarize_plan(df).items():
        print(f"    {set_name:<16} {count}")


if __name__ == "__main__":
    main()
