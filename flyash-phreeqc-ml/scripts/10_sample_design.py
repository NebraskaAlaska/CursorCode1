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

import _path_setup  # noqa: F401  (adds project root to sys.path; must precede package import)
import pandas as pd

from flyash_phreeqc_ml import config, phreeqc_runner, run_manager
from flyash_phreeqc_ml.ml import sampling
from flyash_phreeqc_ml.parsers.pqo_parser import parse_pqo_file, records_to_frames

_MOL_OUTPUTS = [("Ca", "mol_Ca"), ("Si", "mol_Si"), ("Al", "mol_Al"),
                ("Fe", "mol_Fe"), ("Na", "mol_Na"), ("K", "mol_K")]
_SI_OUTPUTS = ["Cal", "Portlandite"]


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True, help="run name (as in the sidebar)")
    ap.add_argument("--n-samples", type=int, default=64, help="design size (LHS points)")
    ap.add_argument("--seed", type=int, default=0, help="LHS seed (reproducible)")
    ap.add_argument("--timeout", type=float, default=None, help="per-run timeout (seconds)")
    args = ap.parse_args()

    sdir = run_manager.surrogate_dir(args.run)
    design = sampling.latin_hypercube_design(args.n_samples, seed=args.seed)
    design_path = sdir / "surrogate_design.csv"
    design.to_csv(design_path, index=False)
    print(f"wrote design  {design_path.relative_to(config.PROJECT_ROOT)}  ({len(design)} rows)")

    if not phreeqc_runner.is_configured():
        print(phreeqc_runner._SETUP_HELP)
        print("Design written; surrogate_dataset.csv NOT built (PHREEQC not configured).")
        return

    workdir = sdir / "runs"
    rows: list[dict] = []
    for _, r in design.iterrows():
        rec = {"sample_id": r["sample_id"], "NaOH_M": r["NaOH_M"],
               "liquid_solid_ratio": r["liquid_solid_ratio"],
               "temperature_C": r["temperature_C"], "co2_scenario": r["co2_scenario"]}
        try:
            text, _ = phreeqc_runner.build_single_input(
                r["NaOH_M"], r["liquid_solid_ratio"], r["temperature_C"],
                r["co2_scenario"], label=str(r["sample_id"]))
            out = phreeqc_runner.run(text, workdir, basename=str(r["sample_id"]),
                                     timeout=args.timeout)
            rec.update(_batch_outputs(out))
            rec["status"], rec["error"] = "ok", ""
        except phreeqc_runner.PhreeqcRunnerError as exc:
            rec["status"], rec["error"] = "failed", str(exc).splitlines()[0]
        rows.append(rec)

    dataset = pd.DataFrame(rows)
    ds_path = sdir / "surrogate_dataset.csv"
    dataset.to_csv(ds_path, index=False)
    n_ok = int((dataset["status"] == "ok").sum())
    print(f"wrote dataset {ds_path.relative_to(config.PROJECT_ROOT)}  "
          f"({len(dataset)} rows, {n_ok} ok, {len(dataset) - n_ok} failed)")


if __name__ == "__main__":
    main()
