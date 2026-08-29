"""Export an explicitly selected sustainability screening advisory.

This is a thin command-line adapter around ``experiments.sustainability_score``.
It supplies no factors, units, boundaries, eligibility decisions, or scientific
defaults. Both modes remain screening proxies and can never produce LCA, TEA,
certified carbon-footprint, cost, feasibility, or validated-result claims.

Examples::

    python scripts/08_sustainability_score.py inventory \
        --input inventory.json --csv-output screen.csv --json-output screen.json
    python scripts/08_sustainability_score.py condition-proxy \
        --input rows.csv --eligibility-column include_in_proxy --csv-output proxy.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

import _path_setup  # noqa: F401  (adds project root to sys.path; precedes package import)

from flyash_phreeqc_ml.experiments import sustainability_score


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one explicit sustainability screening-proxy mode.")
    modes = parser.add_subparsers(dest="mode", required=True)

    inventory = modes.add_parser(
        "inventory", help="Calculate only explicitly supplied amount × factor rows.")
    inventory.add_argument("--input", type=Path, required=True, help="Inventory JSON list/object.")
    inventory.add_argument("--csv-output", type=Path, required=True)
    inventory.add_argument("--json-output", type=Path, help="Optional complete JSON export.")

    condition = modes.add_parser(
        "condition-proxy", help="Score explicitly eligible rows in a supplied experimental CSV.")
    condition.add_argument("--input", type=Path, required=True, help="Supplied experimental CSV.")
    condition.add_argument("--eligibility-column", required=True)
    condition.add_argument("--csv-output", type=Path, required=True)
    condition.add_argument("--json-output", type=Path, help="Optional complete JSON export.")
    return parser


def _read_inventory(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        unsupported = set(payload) - {"rows"}
        if unsupported:
            raise ValueError("inventory JSON object may contain only the 'rows' field")
        payload = payload.get("rows")
    if not isinstance(payload, list):
        raise ValueError("inventory input must be a JSON list or an object with a 'rows' list")
    return payload


def _condition_json(result: dict) -> str:
    frame = result["scores"]
    payload = {key: value for key, value in result.items() if key != "scores"}
    payload["scores"] = frame.astype(object).where(pd.notna(frame), None).to_dict(
        orient="records")
    return sustainability_score.inventory_to_json(payload)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.mode == "inventory":
            result = sustainability_score.screen_inventory(_read_inventory(args.input))
            csv_text = sustainability_score.inventory_to_csv(result)
            json_text = sustainability_score.inventory_to_json(result)
            summary = (f"included={result['included_count']}, "
                       f"excluded={result['excluded_count']}")
        else:
            rows = pd.read_csv(args.input)
            result = sustainability_score.screen_condition_proxy(
                rows, eligibility_column=args.eligibility_column)
            csv_text = sustainability_score.inventory_to_csv(
                {"contributions": result["scores"].to_dict(orient="records")})
            json_text = _condition_json(result)
            summary = (f"included={result['included_rows']}, "
                       f"excluded={result['excluded_rows']}")

        _write(args.csv_output, csv_text)
        if args.json_output is not None:
            _write(args.json_output, json_text)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Sustainability screen not written: {exc}", file=sys.stderr)
        return 2

    print(f"wrote {args.csv_output} ({result['mode']}; {summary})")
    print("screening proxy only; not LCA, TEA, a certified result, or a feasibility verdict")
    if args.json_output is not None:
        print(f"wrote {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
