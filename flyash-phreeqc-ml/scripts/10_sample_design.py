"""Step 10 — Latin-hypercube sample the PHREEQC input space and build the surrogate dataset.

Writes a reproducible design matrix to ``experiments/<run>/outputs/surrogate/surrogate_design.csv``,
then runs each design point through :mod:`phreeqc_runner` and collects inputs + parsed
outputs (pH, Ca/Si/Al/Fe/Na/K mM, selected saturation indices) into ``surrogate_dataset.csv``.
**Non-converged / failed runs are recorded with a ``status`` column, not dropped.** If PHREEQC
is not configured, the design is still written (inspectable) but the dataset is not built.

Run:  python scripts/10_sample_design.py --run "<run>" --n-samples 200 --seed 0
"""
from __future__ import annotations

import argparse
from pathlib import Path

import _path_setup  # noqa: F401  (adds project root to sys.path; must precede package import)
import pandas as pd

from flyash_phreeqc_ml import config, phreeqc_runner, run_manager
from flyash_phreeqc_ml.ml import sampling
from flyash_phreeqc_ml.parsers.pqo_parser import parse_pqo_file, records_to_frames
from flyash_phreeqc_ml.simulation import phreeqc_executor, phreeqc_review_manifest

_MOL_OUTPUTS = [("Ca", "mol_Ca"), ("Si", "mol_Si"), ("Al", "mol_Al"),
                ("Fe", "mol_Fe"), ("Na", "mol_Na"), ("K", "mol_K")]
_SI_OUTPUTS = ["Cal", "Portlandite"]


def _display_path(path) -> str:
    path = Path(path)
    try:
        return str(path.relative_to(config.PROJECT_ROOT))
    except ValueError:
        return str(path)


def _batch_outputs(pqo_path) -> dict:
    """Parse a `.pqo` and extract the batch-state outputs the surrogate learns."""
    results, _sat, _asm = records_to_frames(parse_pqo_file(pqo_path))
    if results.empty:
        return {}
    batch = (results[results["state"].astype(str).str.lower() == "batch"]
             if "state" in results.columns else results)
    row = (batch.iloc[-1] if not batch.empty else results.iloc[-1])
    out: dict = {"pH": pd.to_numeric(pd.Series([row.get("pH")]), errors="coerce").iloc[0]}
    for el, mol_col in _MOL_OUTPUTS:
        v = pd.to_numeric(pd.Series([row.get(mol_col)]), errors="coerce").iloc[0]
        out[f"{el}_mM"] = (v * config.PHREEQC_MOLALITY_TO_MM) if pd.notna(v) else None
    for phase in _SI_OUTPUTS:
        out[f"SI_{phase}"] = pd.to_numeric(pd.Series([row.get(f"SI_{phase}")]),
                                           errors="coerce").iloc[0]
    return out


def _design_set(run_name: str, n_samples: int, seed: int):
    design = sampling.latin_hypercube_design(n_samples, seed=seed)
    inputs = [
        phreeqc_runner.build_design_input(
            row["NaOH_M"], row["liquid_solid_ratio"], row["temperature_C"],
            row["co2_scenario"], sample_id=str(row["sample_id"]))
        for _, row in design.iterrows()
    ]
    source_identity = {
        "workflow": "surrogate_sample_design",
        "version": 1,
        "generator_contract": "typed-legacy-design-builder-v1",
        "run": run_name,
        "n_samples": int(n_samples),
        "seed": int(seed),
        "design_sha256": phreeqc_review_manifest.dataframe_hash(design),
    }
    return design, inputs, source_identity


def _available_environment():
    availability = phreeqc_executor.check_availability()
    if not availability.can_run or availability.environment_identity is None:
        raise phreeqc_review_manifest.ReviewManifestError(availability.message)
    if not phreeqc_runner.is_cemdata_compatible(availability.database_path):
        raise phreeqc_review_manifest.ReviewManifestError(
            "The surrogate design runner requires a CEMDATA-compatible database defining Cal "
            "and Portlandite.")
    return availability


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True, help="run name (as in the sidebar)")
    ap.add_argument("--n-samples", type=int, default=64, help="design size (LHS points)")
    ap.add_argument("--seed", type=int, default=0, help="LHS seed (reproducible)")
    ap.add_argument("--timeout", type=float, default=None, help="per-run timeout (seconds)")
    ap.add_argument(
        "--confirm-reviewed-inputs", action="store_true",
        help="deprecated and non-executable; use --prepare-review-manifest/--review-manifest")
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--prepare-review-manifest", metavar="PATH",
                       help="write design, exact .pqi files, and JSON evidence; do not run")
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
        design, inputs, source_identity = _design_set(args.run, args.n_samples, args.seed)
        if args.prepare_review_manifest:
            path = phreeqc_review_manifest.prepare_review_manifest(
                inputs, args.prepare_review_manifest, source_identity=source_identity,
                execution_environment=availability.environment_identity)
            sdir = run_manager.surrogate_dir(args.run)
            sdir.mkdir(parents=True, exist_ok=True)
            design_path = sdir / "surrogate_design.csv"
            design.to_csv(design_path, index=False)
            print(f"wrote design  {_display_path(design_path)}  ({len(design)} rows)")
            print(f"Prepared {len(inputs)} exact input(s) for review at {path.parent}")
            print(f"Review manifest: {path}")
            print("No PHREEQC execution occurred. This manifest is local review evidence, not a "
                  "scientific result.")
            return 0
        verified = phreeqc_review_manifest.verify_review_manifest(
            args.review_manifest, inputs, source_identity=source_identity,
            execution_environment=availability.environment_identity)
        execution_environment = verified.execution_environment
        confirmations = [
            phreeqc_runner.confirm_reviewed_input(
                phreeqc_runner.review_input(item),
                expected_environment=execution_environment)
            for item in verified.inputs
        ]
    except (phreeqc_review_manifest.ReviewManifestError,
            phreeqc_runner.PhreeqcRunnerError) as exc:
        print(f"Execution blocked: {exc}")
        print("Nothing was executed; surrogate_dataset.csv was not built.")
        return 2

    sdir = run_manager.surrogate_dir(args.run)
    sdir.mkdir(parents=True, exist_ok=True)
    design_path = sdir / "surrogate_design.csv"
    design.to_csv(design_path, index=False)
    print(f"wrote design  {_display_path(design_path)}  ({len(design)} rows)")

    workdir = sdir / "runs"
    rows: list[dict] = []
    for ((_, r), generated_input, confirmation) in zip(
            design.iterrows(), verified.inputs, confirmations):
        rec = {"sample_id": r["sample_id"], "NaOH_M": r["NaOH_M"],
               "liquid_solid_ratio": r["liquid_solid_ratio"],
               "temperature_C": r["temperature_C"], "co2_scenario": r["co2_scenario"]}
        try:
            out = phreeqc_runner.run(
                generated_input, workdir, basename=generated_input.basename,
                exe=execution_environment.executable.resolved_path,
                database=execution_environment.database.resolved_path,
                timeout=args.timeout,
                confirmation=confirmation)
            rec.update(_batch_outputs(out))
            rec["status"], rec["error"] = "ok", ""
        except phreeqc_runner.PhreeqcRunnerError as exc:
            rec["status"], rec["error"] = "failed", str(exc).splitlines()[0]
        rows.append(rec)

    dataset = pd.DataFrame(rows)
    ds_path = sdir / "surrogate_dataset.csv"
    dataset.to_csv(ds_path, index=False)
    n_ok = int((dataset["status"] == "ok").sum())
    print(f"wrote dataset {_display_path(ds_path)}  "
          f"({len(dataset)} rows, {n_ok} ok, {len(dataset) - n_ok} failed)")
    return 0 if n_ok == len(dataset) else 1


if __name__ == "__main__":
    raise SystemExit(main())
