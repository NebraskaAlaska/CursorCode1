"""Step 2 — parse the experimental ICP / mix-design workbook(s).

Writes to ``data/processed``:
    icp_raw_<sheet>.csv          (faithful grid dump of every sheet)
    icp_oxide_compositions.csv   (best-effort tidy raw-material oxide table)

Run:  python scripts/02_parse_icp.py
"""
from __future__ import annotations

import _path_setup  # noqa: F401  (adds project root to sys.path; must precede package import)

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.parsers import parse_icp_workbook


def main() -> None:
    config.ensure_output_dirs()

    workbooks = sorted(config.ICP_DIR.glob("*.xlsx")) + sorted(config.ICP_DIR.glob("*.xls"))
    csv_inputs = sorted(config.ICP_DIR.glob("*.csv"))
    print(f"Found {len(workbooks)} Excel and {len(csv_inputs)} CSV ICP file(s).")

    for wb in workbooks:
        print(f"  parsing {wb.name}")
        frames = parse_icp_workbook(wb, config.PROCESSED_DIR)
        oxides = frames["oxide_compositions"]
        oxides.to_csv(
            config.PROCESSED_DIR / "icp_oxide_compositions.csv", index=False
        )
        print(
            f"    -> {len(oxides)} oxide-composition rows "
            f"for materials: {sorted(oxides['material'].unique()) if not oxides.empty else '[]'}"
        )

    # Plain CSV ICP files (none yet) are copied through as-is for the pipeline.
    for csv in csv_inputs:
        out = config.PROCESSED_DIR / f"icp_{csv.stem}.csv"
        out.write_bytes(csv.read_bytes())
        print(f"  copied {csv.name} -> {out.name}")

    print(f"  wrote CSVs to {config.PROCESSED_DIR}")


if __name__ == "__main__":
    main()
