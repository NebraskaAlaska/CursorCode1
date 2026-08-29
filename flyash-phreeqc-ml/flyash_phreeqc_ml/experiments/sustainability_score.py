"""Feature 3 — sustainability / cost *proxy* indicators (a scaffold).

These are deliberately simple, dimensionless-ish indicators computed per measured
row. They are **not** real dollar costs or life-cycle numbers — they are proxies to
help rank conditions (e.g. "which condition gets the most REE per unit of bulk
dissolution and reagent intensity"). Real costing comes later, once measured data
and process assumptions exist.

Every indicator degrades gracefully: a missing input yields ``NaN`` for that
indicator rather than raising, so a partially-filled sheet still scores.
"""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
import io
import json
import math
import re

import numpy as np
import pandas as pd

# Bulk-matrix elements summed into the "how much did we dissolve" indicator.
_BULK_COLUMNS = ["Ca_mM", "Si_mM", "Al_mM", "Fe_mM"]

# Fields counted as "required measured fields" for the missing-data penalty.
_REQUIRED_MEASURED_FIELDS = [
    "final_pH",
    "conductivity_mS_cm",
    "Ca_mM",
    "Si_mM",
    "Al_mM",
    "Fe_mM",
    "Na_mM",
    "K_mM",
    "Sc_ppb",
    "total_REE_ppb",
]

SUSTAINABILITY_COLUMNS = [
    "sample_id",
    "NaOH_mol_per_L",
    "treatment_time_min",
    "total_bulk_dissolved_mM",
    "REE_selectivity_proxy",
    "Sc_selectivity_proxy",
    "NaOH_time_intensity",
    "penalty_bulk_dissolution",
    "penalty_missing_data",
]


class SustainabilityScreenError(ValueError):
    """A supplied inventory row is malformed or scientifically incomparable."""


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """Numeric view of *name*, or an all-NaN series if the column is absent."""
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype="float64")


def compute_sustainability_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Compute proxy indicators for each row of an experimental-release frame.

    Returns a new DataFrame with one row per input row and the columns listed in
    :data:`SUSTAINABILITY_COLUMNS`. Division-by-zero (no bulk dissolution measured)
    yields ``NaN`` for the selectivity proxies rather than ``inf``.
    """
    out = pd.DataFrame(index=df.index)
    out["sample_id"] = df["sample_id"] if "sample_id" in df.columns else ""

    naoh = _col(df, "NaOH_M")
    time_min = _col(df, "time_min")

    out["NaOH_mol_per_L"] = naoh
    out["treatment_time_min"] = time_min

    # Bulk dissolution = sum of matrix elements; all-NaN row -> NaN (min_count=1).
    bulk = pd.concat([_col(df, c) for c in _BULK_COLUMNS], axis=1).sum(axis=1, min_count=1)
    out["total_bulk_dissolved_mM"] = bulk

    # Selectivity proxies: trace (ppb) per unit bulk (mM). Guard zero denominators.
    safe_bulk = bulk.where(bulk != 0, other=np.nan)
    out["REE_selectivity_proxy"] = _col(df, "total_REE_ppb") / safe_bulk
    out["Sc_selectivity_proxy"] = _col(df, "Sc_ppb") / safe_bulk

    out["NaOH_time_intensity"] = naoh * time_min
    out["penalty_bulk_dissolution"] = bulk

    # Count of missing required measured fields per row (absent column counts as
    # missing for every row).
    present = pd.concat([_col(df, c) for c in _REQUIRED_MEASURED_FIELDS], axis=1)
    out["penalty_missing_data"] = present.isna().sum(axis=1).astype(int)

    return out[SUSTAINABILITY_COLUMNS].reset_index(drop=True)


def _finite_number(value, label: str, *, allow_missing: bool = False):
    if value is None or (isinstance(value, str) and not value.strip()):
        if allow_missing:
            return None
        raise SustainabilityScreenError(f"{label} is required")
    if isinstance(value, bool):
        raise SustainabilityScreenError(f"{label} must not be a boolean")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SustainabilityScreenError(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise SustainabilityScreenError(f"{label} must be finite")
    return number


def _unit(value, label: str) -> str:
    unit = re.sub(r"\s+", " ", str(value or "").strip())
    if not unit:
        raise SustainabilityScreenError(f"{label} is required")
    return unit


def _normalized_unit(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _factor_units(factor_unit: str) -> tuple[str, str] | None:
    raw = str(factor_unit or "").strip()
    if "/" in raw:
        numerator, denominator = raw.rsplit("/", 1)
    else:
        match = re.match(r"^(.+?)\s+per\s+(.+)$", raw, flags=re.IGNORECASE)
        if not match:
            return None
        numerator, denominator = match.groups()
    if not numerator.strip() or not denominator.strip():
        return None
    return numerator.strip(), denominator.strip()


def screen_inventory(rows: list[dict]) -> dict:
    """Calculate a transparent amount x supplied-factor inventory screen.

    No factor, unit conversion, boundary, or source is inferred. Rows with missing factors
    remain missing; incompatible rows are retained with a blocking reason.
    """
    if not isinstance(rows, list) or not rows:
        raise SustainabilityScreenError("at least one user-supplied inventory row is required")

    contributions = []
    missing_factors = []
    blocked = []
    totals: dict[tuple[str, str], float] = {}
    total_ranges: dict[tuple[str, str], list[float]] = {}
    evidence_ids = set()
    for index, supplied in enumerate(rows, start=1):
        if not isinstance(supplied, dict):
            raise SustainabilityScreenError(f"inventory row {index} must be an object")
        item = str(supplied.get("item") or supplied.get("process") or "").strip()
        if not item:
            raise SustainabilityScreenError(f"inventory row {index} requires item/process name")
        amount = _finite_number(supplied.get("amount"), f"{item}.amount")
        if amount < 0:
            raise SustainabilityScreenError(f"{item}.amount must be non-negative")
        amount_unit = _unit(supplied.get("amount_unit"), f"{item}.amount_unit")
        boundary = str(supplied.get("boundary") or "").strip()
        common = {
            "row_index": index,
            "item": item,
            "amount": amount,
            "amount_unit": amount_unit,
            "boundary": boundary or None,
            "geography": supplied.get("geography"),
            "year": supplied.get("year"),
            "notes": supplied.get("notes"),
        }
        factor = _finite_number(supplied.get("factor"), f"{item}.factor", allow_missing=True)
        if factor is None:
            row = {**common, "status": "missing_factor", "factor": None,
                   "contribution": None, "result_unit": None,
                   "reason": "No factor was supplied; the row remains missing."}
            contributions.append(row)
            missing_factors.append({"row_index": index, "item": item,
                                    "amount": amount, "amount_unit": amount_unit})
            continue
        if factor < 0:
            blocked.append({**common, "status": "blocked", "factor": factor,
                            "reason": "A supplied factor cannot be negative."})
            contributions.append(blocked[-1])
            continue

        factor_unit = str(supplied.get("factor_unit") or "").strip()
        units = _factor_units(factor_unit)
        factor_source = str(supplied.get("factor_source") or "").strip()
        source_type = str(supplied.get("source_type") or "").strip()
        evidence_id = str(supplied.get("evidence_id") or "").strip() or None
        reason = None
        if not boundary:
            reason = "Boundary is required before this contribution can enter a total."
        elif units is None:
            reason = "Factor unit must state output per amount unit (for example kg CO2e/kg)."
        elif _normalized_unit(units[1]) != _normalized_unit(amount_unit):
            reason = (f"Amount unit {amount_unit!r} is incompatible with factor denominator "
                      f"{units[1]!r}; no automatic conversion was applied.")
        elif not factor_source:
            reason = "Every supplied factor requires a source."
        elif source_type not in {"literature_evidence", "user_assumption"}:
            reason = "source_type must be literature_evidence or user_assumption."
        elif source_type == "literature_evidence" and not evidence_id:
            reason = "A literature factor requires its durable evidence ID."
        if reason:
            row = {**common, "status": "blocked", "factor": factor,
                   "factor_unit": factor_unit or None, "factor_source": factor_source or None,
                   "source_type": source_type or None, "evidence_id": evidence_id,
                   "contribution": None, "result_unit": units[0] if units else None,
                   "reason": reason}
            contributions.append(row)
            blocked.append(row)
            continue

        result_unit = units[0]
        contribution = amount * factor
        factor_min = _finite_number(supplied.get("factor_min"), f"{item}.factor_min",
                                    allow_missing=True)
        factor_max = _finite_number(supplied.get("factor_max"), f"{item}.factor_max",
                                    allow_missing=True)
        contribution_min = contribution_max = None
        if factor_min is not None or factor_max is not None:
            if factor_min is None or factor_max is None:
                raise SustainabilityScreenError(
                    f"{item} sensitivity requires both factor_min and factor_max")
            if factor_min < 0 or factor_max < factor_min or not (factor_min <= factor <= factor_max):
                raise SustainabilityScreenError(
                    f"{item} factor range must be non-negative and contain the supplied factor")
            contribution_min = amount * factor_min
            contribution_max = amount * factor_max
        row = {
            **common,
            "status": "included",
            "factor": factor,
            "factor_unit": factor_unit,
            "factor_source": factor_source,
            "source_type": source_type,
            "evidence_id": evidence_id,
            "contribution": contribution,
            "result_unit": result_unit,
            "factor_min": factor_min,
            "factor_max": factor_max,
            "contribution_min": contribution_min,
            "contribution_max": contribution_max,
            "reason": None,
        }
        contributions.append(row)
        if evidence_id:
            evidence_ids.add(evidence_id)
        key = (boundary, result_unit)
        totals[key] = totals.get(key, 0.0) + contribution
        if contribution_min is not None:
            bounds = total_ranges.setdefault(key, [0.0, 0.0])
            bounds[0] += contribution_min
            bounds[1] += contribution_max

    total_rows = []
    for (boundary, unit), total in sorted(totals.items()):
        bounds = total_ranges.get((boundary, unit))
        total_rows.append({
            "boundary": boundary,
            "result_unit": unit,
            "total": total,
            "sensitivity_min": bounds[0] if bounds else None,
            "sensitivity_max": bounds[1] if bounds else None,
        })
    return {
        "mode": "user_inventory_screen",
        "contributions": contributions,
        "totals": total_rows,
        "missing_factors": missing_factors,
        "blocked_rows": blocked,
        "included_count": sum(row.get("status") == "included" for row in contributions),
        "excluded_count": sum(row.get("status") != "included" for row in contributions),
        "evidence_ids": sorted(evidence_ids),
        "assumptions": [
            "Only explicitly supplied amounts and factors were calculated.",
            "Totals are separated by boundary and compatible output unit.",
            "Missing factors remain missing and are not treated as zero.",
        ],
        "limitations": [
            "This is a transparent screening calculation, not a certified assessment or feasibility result.",
            "Included and excluded items define the displayed boundary; review completeness before use.",
        ],
    }


def screen_condition_proxy(df: pd.DataFrame, *, eligibility_column: str) -> dict:
    """Run the historical condition proxy only for explicitly eligible supplied rows."""
    if eligibility_column not in df.columns:
        raise SustainabilityScreenError(
            "condition proxy requires an explicit per-row eligibility column")
    eligible = df[eligibility_column].map(lambda value: value is True or str(value).lower() == "true")
    selected = df.loc[eligible].copy()
    scores = compute_sustainability_scores(selected) if not selected.empty \
        else pd.DataFrame(columns=SUSTAINABILITY_COLUMNS)
    return {
        "mode": "experimental_condition_screening_proxy",
        "scores": scores,
        "included_rows": int(eligible.sum()),
        "excluded_rows": int((~eligible).sum()),
        "eligibility_column": eligibility_column,
        "limitations": [
            "This condition score is a screening proxy based only on supplied experimental rows.",
            "It is not a certified assessment, cost result, or feasibility result.",
        ],
    }


def _spreadsheet_safe(value):
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if not stripped:
        return value
    numeric_text = False
    try:
        numeric_text = Decimal(stripped).is_finite()
    except (InvalidOperation, ValueError):
        pass
    if stripped.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) \
            and not numeric_text:
        return "'" + value
    return value


def inventory_to_csv(result: dict) -> str:
    rows = list(result.get("contributions") or [])
    columns = sorted({key for row in rows for key in row})
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _spreadsheet_safe(value) for key, value in row.items()})
    return buffer.getvalue()


def inventory_to_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
