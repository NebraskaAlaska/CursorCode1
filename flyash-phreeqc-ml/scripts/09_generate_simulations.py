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
from flyash_phreeqc_ml.simulation import phreeqc_executor, phreeqc_review_manifest


def _candidate_set(run_name: str):
    """Regenerate the complete ordered trusted input set and its source-data identity."""
    data = run_manager.read_data_file(run_name)
    if data.empty:
        return [], {"workflow": "generate_needed_simulations", "version": 1,
                    "run": run_name,
                    "source_data_sha256": phreeqc_review_manifest.dataframe_hash(data)}, []

    results_path = config.PROCESSED_DIR / config.PHREEQC_RESULTS_CSV
    results = (pd.read_csv(results_path) if results_path.exists()
               else pd.DataFrame(columns=scenarios.MANIFEST_COLUMNS))
    manifest = (scenarios.build_scenario_manifest(results) if not results.empty
                else pd.DataFrame(columns=scenarios.MANIFEST_COLUMNS))
    cond_map = run_manager.read_condition_mapping(run_name)
    table = mapping_table.build_suggestion_table(data, manifest, cond_map)
    needs = mapping_table.needs_new_simulation(table)
    profile = profiles.default_dataset_profile()
    inputs = []
    blocked = []
    for ck in needs.get("condition_key", pd.Series(dtype=str)).astype(str):
        sample = mapping_table.condition_representative_sample(data, ck, profile)
        reason = phreeqc_runner.generation_blocked_reason(sample, profile)
        if reason:
            blocked.append({"condition_key": ck, "reason": reason})
            continue
        generated = phreeqc_runner.build_input(sample, profile)
        if not generated:
            blocked.append({"condition_key": ck, "reason": "no template produced"})
            continue
        inputs.extend(generated)
    source_identity = {
        "workflow": "generate_needed_simulations",
        "version": 1,
        "generator_contract": "typed-legacy-condition-builder-v1",
        "run": run_name,
        "source_data_sha256": phreeqc_review_manifest.dataframe_hash(data),
        "condition_mapping_sha256": phreeqc_review_manifest.dataframe_hash(cond_map),
        "existing_results_sha256": phreeqc_review_manifest.dataframe_hash(results),
        "ordered_needed_conditions": needs.get(
            "condition_key", pd.Series(dtype=str)).astype(str).tolist(),
    }
    return inputs, source_identity, blocked


def _available_environment():
    availability = phreeqc_executor.check_availability()
    if not availability.can_run or availability.environment_identity is None:
        raise phreeqc_review_manifest.ReviewManifestError(availability.message)
    if not phreeqc_runner.is_cemdata_compatible(availability.database_path):
        raise phreeqc_review_manifest.ReviewManifestError(
            "The generated-condition workflow requires a CEMDATA-compatible database defining "
            "Cal and Portlandite.")
    return availability


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True, help="run name (as in the sidebar)")
    ap.add_argument("--timeout", type=float, default=None, help="per-run timeout (seconds)")
    ap.add_argument(
        "--confirm-reviewed-inputs", action="store_true",
        help="deprecated and non-executable; use --prepare-review-manifest/--review-manifest")
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--prepare-review-manifest", metavar="PATH",
                       help="write exact .pqi files and versioned JSON review evidence; do not run")
    modes.add_argument("--review-manifest", metavar="PATH",
                       help="execute only after exact manifest verification")
    args = ap.parse_args(argv)

    if args.confirm_reviewed_inputs:
        print("Deprecated option blocked: --confirm-reviewed-inputs cannot authorize execution. "
              "First use --prepare-review-manifest PATH, review every .pqi and the JSON, then "
              "run with --review-manifest PATH. Nothing was executed.")
        return 2
    if not (args.prepare_review_manifest or args.review_manifest):
        print("Execution blocked: prepare exact review evidence with --prepare-review-manifest "
              "PATH, then execute with --review-manifest PATH. Nothing was executed.")
        return 2

    try:
        availability = _available_environment()
        inputs, source_identity, blocked = _candidate_set(args.run)
        if not inputs:
            print(f"Run '{args.run}' produced no runnable deterministic inputs. Nothing executed.")
            return 1
        if args.prepare_review_manifest:
            path = phreeqc_review_manifest.prepare_review_manifest(
                inputs, args.prepare_review_manifest, source_identity=source_identity,
                execution_environment=availability.environment_identity)
            print(f"Prepared {len(inputs)} exact input(s) for review at {path.parent}")
            print(f"Review manifest: {path}")
            print("No PHREEQC execution occurred. This manifest is local review evidence, not a "
                  "scientific result.")
            if blocked:
                print(f"{len(blocked)} unsupported condition(s) were recorded in source identity "
                      "but produced no runnable input.")
            return 0
        verified = phreeqc_review_manifest.verify_review_manifest(
            args.review_manifest, inputs, source_identity=source_identity,
            execution_environment=availability.environment_identity)
        execution_environment = verified.execution_environment
        # Finish all review/confirmation checks before creating the run workspace.
        confirmations = [
            phreeqc_runner.confirm_reviewed_input(
                phreeqc_runner.review_input(item),
                expected_environment=execution_environment)
            for item in verified.inputs
        ]
    except (phreeqc_review_manifest.ReviewManifestError,
            phreeqc_runner.PhreeqcRunnerError) as exc:
        print(f"Execution blocked: {exc}")
        print("Nothing was executed.")
        return 2

    workdir = run_manager.generated_simulations_dir(args.run)
    rows: list[dict] = []
    for gi, confirmation in zip(verified.inputs, confirmations):
        try:
            out = phreeqc_runner.run(
                gi, workdir, basename=gi.basename, timeout=args.timeout,
                exe=execution_environment.executable.resolved_path,
                database=execution_environment.database.resolved_path,
                confirmation=confirmation)
            keys = phreeqc_runner.ingest(out, args.run,
                                         condition_key=gi.source_condition_key,
                                         metadata=gi.metadata)
            rows.append({"condition_key": gi.source_condition_key,
                         "variant": gi.model_label, "status": "ok",
                         "detail": f"{len(keys)} record(s) ingested"})
        except phreeqc_runner.PhreeqcRunnerError as exc:
            rows.append({"condition_key": gi.source_condition_key,
                         "variant": gi.model_label, "status": "failed",
                         "detail": str(exc).splitlines()[0]})

    summary = pd.DataFrame(rows)
    print(summary.to_string(index=False))
    n_ok = int((summary["status"] == "ok").sum())
    print(f"\n{n_ok}/{len(summary)} variant(s) ingested. "
          f"Generated files under {workdir.relative_to(config.PROJECT_ROOT)}.")
    return 0 if n_ok == len(summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
