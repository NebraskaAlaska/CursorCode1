"""Step 5 (Phase 2) — compare measured experimental release vs PHREEQC.

Reads measured CSVs from ``data/raw/experimental_icp/`` and the Phase-1
``phreeqc_results.csv``. If measured data exists it writes:

    data/processed/experimental_release.csv             (parsed measured data)
    data/processed/comparison_measured_vs_phreeqc.csv   (joined + residuals)
    reports/figures/measured_vs_phreeqc.png             (if pairs are linked)
    reports/figures/residuals_by_sample.png

If only the blank template is present (the state until Monday's data lands), the
script prints a notice and exits cleanly without producing comparison artifacts —
so it is always safe to run as part of the pipeline.

Dual behaviour — global vs per-run
-----------------------------------
* **Standalone / CLI (no ``--run``):** writes the *global* artifacts above
  (``data/processed/`` + ``reports/figures/``). This keeps the script-only
  pipeline working exactly as before.
* **``--run RUN_NAME`` (how the app invokes it):** additionally copies the
  comparison CSV into ``experiments/<run>/outputs/`` and its figures into
  ``experiments/<run>/outputs/figures/``, then writes a provenance stamp
  (``comparison_meta.json``). Per-run outputs are what the app's Results tab reads,
  so one run's results never leak into another run's tab.

Optional sample -> PHREEQC mapping
----------------------------------
To actually compute residuals, each measured ``sample_id`` must be linked to a
PHREEQC ``record_key``. Drop a 2-column CSV at
``data/raw/experimental_icp/sample_phreeqc_map.csv`` with headers
``sample_id,phreeqc_record_key`` and it will be applied automatically. Without it
the comparison still runs but PHREEQC columns/residuals stay NaN.

Run:  python scripts/05_compare_experimental.py
      python scripts/05_compare_experimental.py --run my_lab_run
"""
from __future__ import annotations

import argparse

import _path_setup  # noqa: F401  (adds project root to sys.path; must precede package import)

import pandas as pd

from flyash_phreeqc_ml import config, run_manager
from flyash_phreeqc_ml.compare import compare_measured_vs_phreeqc
from flyash_phreeqc_ml.parsers import has_measured_data, load_experimental_release
from flyash_phreeqc_ml.viz import make_comparison_plots


def _load_mapping() -> pd.DataFrame | None:
    path = config.EXPERIMENTAL_ICP_DIR / config.SAMPLE_PHREEQC_MAP_CSV
    if path.exists():
        print(f"  using sample->PHREEQC mapping: {path.name}")
        return pd.read_csv(path)
    return None


def _warn_if_fe_unpredicted(comparison: pd.DataFrame) -> None:
    """Warn when Fe is measured but PHREEQC has no Fe prediction to compare against.

    The CEMDATA18 runs may not include Fe, so ``phreeqc_Fe_mM`` (and therefore
    ``residual_Fe``) can be entirely NaN. That is not a bug, but it means Fe
    residuals are unavailable — flag it loudly so it is not mistaken for "PHREEQC
    predicts zero Fe".
    """
    measured_fe = pd.to_numeric(comparison.get("Fe_mM"), errors="coerce")
    phreeqc_fe = pd.to_numeric(comparison.get("phreeqc_Fe_mM"), errors="coerce")
    n_measured = int(measured_fe.notna().sum()) if measured_fe is not None else 0
    has_pred = bool(phreeqc_fe.notna().any()) if phreeqc_fe is not None else False

    if n_measured > 0 and not has_pred:
        print(
            "Warning: Fe was measured experimentally, but current PHREEQC outputs "
            "do not include Fe predictions, so residual_Fe is unavailable."
        )


def _write_per_run_outputs(run_name: str, comparison: pd.DataFrame) -> None:
    """Copy the comparison CSV + figures into the run's outputs and stamp provenance.

    This is what the app's Results tab reads, so a run only ever shows its own
    comparison. Raises :class:`run_manager.RunTypeError` for non-lab runs.
    """
    out = run_manager.comparison_path(run_name)
    out.parent.mkdir(parents=True, exist_ok=True)
    comparison.to_csv(out, index=False)
    print(f"  wrote {out}  (per-run)")

    fig_dir = run_manager.comparison_figures_dir(run_name)
    for p in make_comparison_plots(comparison, fig_dir):
        print(f"  wrote {p}  (per-run)")

    meta = run_manager.write_comparison_meta(run_name)
    print(f"  wrote {meta}  (provenance stamp)")


def main(run_name: str | None = None) -> None:
    config.ensure_output_dirs()

    # Measured data (skips the blank template automatically).
    measured = load_experimental_release()
    if not has_measured_data(measured):
        print(
            "No measured experimental data found yet "
            f"(only the template in {config.EXPERIMENTAL_ICP_DIR}).\n"
            "  -> Fill experimental_release_template.csv and save it as a new CSV "
            "in that folder, then re-run. Skipping comparison."
        )
        return

    print(f"  loaded {len(measured)} measured sample row(s).")
    measured.to_csv(config.PROCESSED_DIR / config.EXPERIMENTAL_RELEASE_CSV, index=False)

    # PHREEQC predictions from Phase 1.
    results_path = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    if not results_path.exists():
        raise FileNotFoundError(
            f"{results_path} not found. Run scripts/01_parse_phreeqc.py first."
        )
    phreeqc_results = pd.read_csv(results_path)

    mapping = _load_mapping()
    comparison = compare_measured_vs_phreeqc(measured, phreeqc_results, mapping=mapping)

    # Global artifacts (the CLI-only pipeline path — always written).
    out = config.PROCESSED_DIR / config.COMPARISON_CSV
    comparison.to_csv(out, index=False)
    print(f"  wrote {out}  ({comparison.shape[0]} rows x {comparison.shape[1]} cols)")

    _warn_if_fe_unpredicted(comparison)

    figures = make_comparison_plots(comparison, config.FIGURES_DIR)
    for p in figures:
        print(f"  wrote {p}")
    if not figures:
        print("  (no comparison figures: link samples to PHREEQC via the mapping file)")

    # Per-run artifacts + provenance stamp (how the app invokes this script).
    if run_name:
        _write_per_run_outputs(run_name, comparison)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        dest="run_name",
        default=None,
        help="Experiment run name: also write per-run outputs + provenance stamp "
             "under experiments/<run>/outputs/ (in addition to the global path).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main(_parse_args().run_name)
