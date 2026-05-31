"""Step 8 — compute sustainability / cost *proxy* indicators.

Loads measured experimental-release data from ``data/raw/experimental_icp/`` and
writes ``outputs/tables/sustainability_score.csv`` (one row per sample). These are
proxy indicators only — not real costs. If no measured data exists yet, the script
prints a notice and exits cleanly.

Run:  python scripts/08_sustainability_score.py
"""
from __future__ import annotations

import _path_setup  # noqa: F401  (adds project root to sys.path; must precede package import)

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.experiments import compute_sustainability_scores
from flyash_phreeqc_ml.parsers import has_measured_data, load_experimental_release


def main() -> None:
    config.ensure_tables_dir()

    measured = load_experimental_release(strict=False)
    if not has_measured_data(measured):
        print(
            "No measured experimental data found yet "
            f"(only template/plan in {config.EXPERIMENTAL_ICP_DIR}).\n"
            "  -> Fill the release template, then re-run. Skipping sustainability score."
        )
        return

    scores = compute_sustainability_scores(measured)
    out = config.TABLES_DIR / config.SUSTAINABILITY_SCORE_CSV
    scores.to_csv(out, index=False)
    print(f"  wrote {out}  ({scores.shape[0]} rows x {scores.shape[1]} cols)")


if __name__ == "__main__":
    main()
