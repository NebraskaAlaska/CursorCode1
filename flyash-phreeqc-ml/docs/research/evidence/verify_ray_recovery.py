"""Reproduce the six-element combined recovery in Ray et al. (2025), Table 4."""

import argparse
import csv
import hashlib
from decimal import Decimal, InvalidOperation
from pathlib import Path


ELEMENTS = ("Sc", "La", "Ce", "Pr", "Nd", "Sm")
REQUIRED = {"run", *ELEMENTS, "total_mg_L", "published_total_pct"}
ALL_RUNS = set(range(1, 21))
# Sum of the six Table 1 concentrations for the original red-mud digest (mg/L).
DIGEST_MG_L = Decimal("24.960")
ROUNDING_LIMIT_PP = Decimal("0.005")
TABLE_SHA256 = "c39db9ec634f23678595fa9d0476292082701170af7ad2a82b0be3565bed1d32"


def number(row, field, run):
    try:
        value = Decimal(row[field] or "")
    except InvalidOperation as error:
        raise ValueError(f"run {run}: missing or invalid {field}") from error
    if not value.is_finite() or value < 0:
        raise ValueError(f"run {run}: {field} must be finite and non-negative")
    return value


def verify(path, subset):
    results = {}
    source_rows = {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, strict=True)
        fields = reader.fieldnames or []
        missing = sorted(REQUIRED.difference(fields))
        if missing:
            raise ValueError("missing CSV column(s): " + ", ".join(missing))
        if len(set(fields)) != len(fields):
            raise ValueError("duplicate CSV column names")
        for row in reader:
            if None in row:
                raise ValueError(f"line {reader.line_num}: more values than column names")
            try:
                run = int(row["run"] or "")
            except ValueError as error:
                raise ValueError(f"line {reader.line_num}: missing or invalid run number") from error
            if run not in ALL_RUNS or run in results:
                raise ValueError(f"line {reader.line_num}: unknown or repeated run {run}")
            total = number(row, "total_mg_L", run)
            concentrations = [number(row, symbol, run) for symbol in ELEMENTS]
            if total > DIGEST_MG_L or any(value > DIGEST_MG_L for value in concentrations):
                raise ValueError(f"run {run}: concentration exceeds this study's combined digest")
            if sum(concentrations) != total:
                raise ValueError(f"run {run}: element sum differs from supplied total_mg_L")
            published = number(row, "published_total_pct", run)
            if published > 100:
                raise ValueError(f"run {run}: published_total_pct exceeds 100")
            calculated = 100 * total / DIGEST_MG_L
            error_pp = abs(calculated - published)
            if error_pp >= ROUNDING_LIMIT_PP:
                raise ValueError(f"run {run}: difference of {error_pp:.6f} percentage points "
                                 f"exceeds or equals {ROUNDING_LIMIT_PP}")
            results[run] = (calculated, published, error_pp)
            if not subset:
                values = [str(value.normalize()) for value in (*concentrations, total, published)]
                source_rows[run] = ",".join([str(run), *values]) + "\n"
    if not results:
        raise ValueError("no data rows")
    if not subset and set(results) != ALL_RUNS:
        missing_runs = ", ".join(str(run) for run in sorted(ALL_RUNS.difference(results)))
        raise ValueError(f"full check is missing run(s): {missing_runs}")
    if not subset:
        numeric_table = "".join(source_rows[run] for run in sorted(source_rows))
        if hashlib.sha256(numeric_table.encode("ascii")).hexdigest() != TABLE_SHA256:
            raise ValueError("numbers differ from the archived transcription of Table 4")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path, help="transcription of the published Table 4")
    parser.add_argument("--subset", action="store_true", help="allow fewer than 20 runs for a spot check")
    args = parser.parse_args()
    try:
        results = verify(args.csv_file, args.subset)
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"Check failed: {error}\n")

    for run, (calculated, published, _) in sorted(results.items()):
        print(f"Run {run:2d}: calculated {calculated:.4f}% | published {published:.2f}%")
    maximum = max(error_pp for _, _, error_pp in results.values())
    print(f"Checked {len(results)} of 20 runs. Maximum difference: {maximum:.6f} percentage points.")


if __name__ == "__main__":
    main()
