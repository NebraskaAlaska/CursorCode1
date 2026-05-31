"""Step 6 — generate Monday's experiment plan (a run sheet).

Writes ``data/raw/experimental_icp/monday_experiment_plan.csv`` with one row per
planned condition (de-duplicated by sample_id) and prints a per-set summary.

Run:  python scripts/06_generate_experiment_plan.py
"""
from __future__ import annotations

import _path_setup  # noqa: F401  (adds project root to sys.path; must precede package import)

from flyash_phreeqc_ml.experiments.plan_generator import summarize_plan, write_experiment_plan


def main() -> None:
    path, stats = write_experiment_plan()
    plan = stats["plan"]

    print(f"  wrote {path}")
    print(f"  planned rows (before de-duplication): {stats['n_raw']}")
    print(f"  unique rows  (after de-duplication):  {stats['n_unique']}")
    print(f"  duplicate rows removed:               {stats['n_removed']}")
    print(
        "  (duplicates are removed when the same condition appears in more than\n"
        "   one experiment set — e.g. NaOH 0.5 M / 60 min / open / R1 is requested\n"
        "   by the time, NaOH, CO2, and replicate sets but scheduled only once.)"
    )

    print("  rows requested per experiment set (before de-duplication):")
    for set_name, count in stats["raw_per_set"].items():
        print(f"    {set_name:<16} {count}")
    print("  unique rows attributed per experiment set (first set wins):")
    for set_name, count in summarize_plan(plan).items():
        print(f"    {set_name:<16} {count}")


if __name__ == "__main__":
    main()
