"""Step 9 — generate, run, and ingest PHREEQC simulations for a run's needs-new conditions.

Reads the conditions a run's suggestion table flags as *needs new simulation*, and for
each one templates a `.pqi` (via :mod:`phreeqc_runner`), runs PHREEQC, and ingests the
parsed output into ``data/processed/phreeqc_results.csv`` (tagged ``generated``) so the
new scenario becomes mappable. Prints a per-variant summary table. Requires a configured
PHREEQC binary + database (``PHREEQC_EXE`` / ``PHREEQC_DATABASE``).

Run:  python scripts/09_generate_simulations.py --run "<run name>"
"""
from __future__ import annotations

import argparse

import _path_setup  # noqa: F401  (adds project root to sys.path; must precede package import)
import pandas as pd

from flyash_phreeqc_ml import (config, mapping_table, phreeqc_runner, profiles,
                               run_manager, scenarios)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True, help="run name (as in the sidebar)")
    ap.add_argument("--timeout", type=float, default=None, help="per-run timeout (seconds)")
    args = ap.parse_args()

    if not phreeqc_runner.is_configured():
        print(phreeqc_runner._SETUP_HELP)
        return

    data = run_manager.read_data_file(args.run)
    if data.empty:
        print(f"Run '{args.run}' has no data rows. Nothing to generate.")
        return

    results_path = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    manifest = (scenarios.build_scenario_manifest(pd.read_csv(results_path))
                if results_path.exists()
                else pd.DataFrame(columns=scenarios.MANIFEST_COLUMNS))
    cond_map = run_manager.read_condition_mapping(args.run)
    table = mapping_table.build_suggestion_table(data, manifest, cond_map)
    needs = mapping_table.needs_new_simulation(table)
    if needs.empty:
        print("No conditions need a new simulation for this run.")
        return

    workdir = run_manager.generated_simulations_dir(args.run)
    profile = profiles.default_dataset_profile()
    rows: list[dict] = []
    for ck in needs["condition_key"].astype(str):
        sample = mapping_table.condition_representative_sample(data, ck, profile)
        reason = phreeqc_runner.generation_blocked_reason(sample, profile)
        if reason:
            rows.append({"condition_key": ck, "variant": "-", "status": "blocked", "detail": reason})
            continue
        inputs = phreeqc_runner.build_input(sample, profile)
        if not inputs:
            rows.append({"condition_key": ck, "variant": "-", "status": "blocked",
                         "detail": "no template produced"})
            continue
        for gi in inputs:
            try:
                out = phreeqc_runner.run(gi.pqi_text, workdir, basename=gi.basename,
                                         timeout=args.timeout)
                keys = phreeqc_runner.ingest(out, args.run, condition_key=gi.source_condition_key,
                                             metadata=gi.metadata)
                rows.append({"condition_key": ck, "variant": gi.model_label,
                             "status": "ok", "detail": f"{len(keys)} record(s) ingested"})
            except phreeqc_runner.PhreeqcRunnerError as exc:
                rows.append({"condition_key": ck, "variant": gi.model_label,
                             "status": "failed", "detail": str(exc).splitlines()[0]})

    summary = pd.DataFrame(rows)
    print(summary.to_string(index=False))
    n_ok = int((summary["status"] == "ok").sum())
    print(f"\n{n_ok}/{len(summary)} variant(s) ingested. "
          f"Generated files under {workdir.relative_to(config.PROJECT_ROOT)}.")


if __name__ == "__main__":
    main()
