"""Step 7 — validate filled experimental-release CSV(s).

Scans ``data/raw/experimental_icp/`` for measured-release files (skipping the
template, the sample->PHREEQC map, and the generated plan), runs the QA/QC checks,
and writes ``outputs/tables/experimental_validation_report.csv``. Prints a short
error/warning summary so problems are visible without opening the report.

Run:  python scripts/07_validate_experimental_data.py
"""
from __future__ import annotations

import _path_setup  # noqa: F401  (adds project root to sys.path; must precede package import)

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.experiments import validate_experimental_dir


def main() -> None:
    config.ensure_tables_dir()

    report = validate_experimental_dir()
    out = config.TABLES_DIR / config.EXPERIMENTAL_VALIDATION_REPORT_CSV
    report.to_csv(out, index=False)

    n_err = int((report["severity"] == "error").sum())
    n_warn = int((report["severity"] == "warning").sum())
    print(f"  wrote {out}  ({len(report)} report row(s))")
    print(f"  {n_err} error(s), {n_warn} warning(s)")

    for _, row in report[report["severity"].isin(["error", "warning"])].iterrows():
        print(f"    [{row['severity']:<7}] {row['source']}: {row['message']}")


if __name__ == "__main__":
    main()
