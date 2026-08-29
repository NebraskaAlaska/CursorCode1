"""Export an explicitly selected experimental-design advisory plan.

This script is intentionally a thin command-line adapter around
``experiments.plan_generator``. It contains no material, treatment, factor,
control, or replicate defaults of its own. The historical Class-C-fly-ash
matrix remains available only through the explicit ``cfa-preset`` command.

Examples::

    python scripts/06_generate_experiment_plan.py cfa-preset --output plan.csv
    python scripts/06_generate_experiment_plan.py generic --spec design.json --output plan.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import _path_setup  # noqa: F401  (adds project root to sys.path; precedes package import)

from flyash_phreeqc_ml import config
from flyash_phreeqc_ml.experiments import plan_generator


_GENERIC_REQUIRED_FIELDS = {
    "material_id",
    "goal",
    "factors",
    "fixed_conditions",
    "replicates",
    "max_run_count",
}
_GENERIC_OPTIONAL_FIELDS = {"controls", "sample_prefix", "experiment_date"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an advisory plan using one explicitly selected mode.")
    modes = parser.add_subparsers(dest="mode", required=True)

    preset = modes.add_parser(
        "cfa-preset",
        help="Explicitly select the labelled historical CFA/NaOH leaching preset.",
    )
    preset.add_argument(
        "--output", type=Path,
        default=config.EXPERIMENTAL_ICP_DIR / config.EXPERIMENT_PLAN_CSV,
        help="CSV destination (legacy run-sheet path only after this preset is selected).",
    )
    preset.add_argument("--json-output", type=Path, help="Optional complete JSON destination.")
    preset.add_argument("--experiment-date", help="Optional date copied into the preset run sheet.")
    preset.add_argument("--max-run-count", type=int, help="Optional explicit refusal cap.")

    generic = modes.add_parser(
        "generic",
        help="Build a generic plan solely from an explicit JSON specification.",
    )
    generic.add_argument("--spec", type=Path, required=True, help="User-defined plan JSON object.")
    generic.add_argument("--output", type=Path, required=True, help="CSV destination.")
    generic.add_argument("--json-output", type=Path, help="Optional complete JSON destination.")
    return parser


def _read_generic_spec(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("generic plan specification must be a JSON object")
    missing = sorted(_GENERIC_REQUIRED_FIELDS - set(payload))
    if missing:
        raise ValueError(f"generic plan specification is missing: {', '.join(missing)}")
    unsupported = sorted(set(payload) - _GENERIC_REQUIRED_FIELDS - _GENERIC_OPTIONAL_FIELDS)
    if unsupported:
        raise ValueError(f"generic plan specification has unsupported fields: {', '.join(unsupported)}")
    return payload


def _write_plan(result: dict, csv_path: Path, json_path: Path | None) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(plan_generator.plan_to_csv(result["plan"]), encoding="utf-8")
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(plan_generator.plan_to_json(result), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.mode == "cfa-preset":
            result = plan_generator.build_cfa_preset_advisory(
                experiment_date=args.experiment_date,
                max_run_count=args.max_run_count,
            )
        else:
            result = plan_generator.build_generic_factor_plan(**_read_generic_spec(args.spec))
        _write_plan(result, args.output, args.json_output)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Experiment plan not written: {exc}", file=sys.stderr)
        return 2

    print(f"wrote {args.output} ({result['run_count']} advisory plan rows)")
    print(f"mode: {result['mode']}")
    print(f"duplicate conditions removed: {result['duplicate_conditions_removed']}")
    for assumption in result["assumptions"]:
        print(f"assumption: {assumption}")
    if args.json_output is not None:
        print(f"wrote {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
