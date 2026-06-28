"""ICP station — reduce MEASURED or PREDICTED solution data. No plasma simulation, no fabrication.

This is not an ICP-OES / ICP-MS plasma simulator. It is a data-reduction workflow over concentration
rows the user already has (measured in the lab, or predicted by PHREEQC). It mirrors the app's
``icp_processor`` in miniature:

* convert mg/L (or ppm) to **mM** via standard atomic weights,
* apply an optional **blank** subtraction, then a **dilution** factor,
* flag **below-detection-limit** readings,
* and, when a *measured* and a *predicted* value exist for the same (sample, element), emit a
  **validation residual** (measured − predicted).

The two hard rules it enforces:

* **It never invents rows.** It processes exactly the rows you provide; the corrected table is the
  same length as the input. No data is generated to fill gaps.
* **It never fabricates measured values from a solid composition.**
  :func:`can_synthesize_measured_from_composition` is permanently ``False`` and
  :func:`refuse_measured_from_composition` returns the standing refusal. Validation residuals come
  only from real measured+predicted pairs — nothing here labels a model estimate "measured".
"""
from __future__ import annotations

import chem

MEASURED = "measured"
PREDICTED = "predicted"
_ROLE_ALIASES = {
    "measured": MEASURED, "measurement": MEASURED, "lab": MEASURED, "experimental": MEASURED,
    "predicted": PREDICTED, "prediction": PREDICTED, "model": PREDICTED, "phreeqc": PREDICTED,
    "sim": PREDICTED, "simulated": PREDICTED,
}

# Input → mg/L scale factor (then mg/L → mM via molar mass). ppm≈mg/L for dilute aqueous solutions.
_UNIT_TO_MGL = {"mg/l": 1.0, "mgl": 1.0, "ppm": 1.0, "mg/kg": 1.0,
                "ppb": 1e-3, "ug/l": 1e-3, "µg/l": 1e-3}
_UNIT_IS_MM = {"mm", "mmol/l", "mmol/litre"}

PLASMA_EXPLANATION = ("The ICP station reduces measured/predicted concentration data; it does not "
                      "simulate the plasma and never fabricates measured values.")
SOLID_TO_MEASURED_REFUSAL = (
    "ICP concentrations describe a measured (or model-predicted) liquid. This station will not "
    "generate measured ICP values from a solid composition alone — that would be fabricating measured "
    "data. Provide the measured (or PHREEQC-predicted) solution concentrations and it will reduce them.")


def can_synthesize_measured_from_composition() -> bool:
    """Permanently ``False`` — ICP never invents measured values from a solid assay."""
    return False


def refuse_measured_from_composition(*_args, **_kwargs) -> dict:
    """The standing refusal, as a structured response (so the gate is visible in the API too)."""
    return {"station": "icp", "accepted": False, "fabricated": False,
            "reason": SOLID_TO_MEASURED_REFUSAL, "explanation": PLASMA_EXPLANATION}


def _f(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x else None  # drop NaN


def _canon_role(role):
    return _ROLE_ALIASES.get(str(role or "").strip().lower(), "")


def _to_mM(value, unit, element):
    """mg/L (or ppm/ppb, or pass-through mM) → mM using a standard atomic weight. None if not possible."""
    u = str(unit or "").strip().lower().replace(" ", "")
    if u in _UNIT_IS_MM:
        return value, None
    scale = _UNIT_TO_MGL.get(u)
    if scale is None:
        return None, f"unrecognised unit {unit!r} — cannot convert to mM."
    try:
        mass = chem.molar_mass(element)
    except chem.FormulaParseError:
        return None, f"no atomic weight for {element!r} — cannot convert to mM."
    return (value * scale) / mass, None  # mg/L ÷ g/mol = mmol/L = mM


def process(rows, apply_blank: bool = True) -> dict:
    """Reduce a list of ICP concentration rows. Returns corrected rows, residuals, and QC warnings.

    Each row may carry: ``sample_id``, ``element``, ``concentration`` (or ``value``), ``unit``,
    ``dilution_factor``, ``blank_value``, ``detection_limit``, ``role`` (measured/predicted). Never
    raises on a bad row — it warns and continues. The corrected table has exactly one entry per input
    row; nothing is invented.
    """
    rows = list(rows or [])
    if not rows:
        return {"station": "icp", "corrected": [], "residuals": [], "fabricated": False,
                "warnings": ["No rows provided. ICP reduces data you supply; it does not generate "
                             "measured values."],
                "explanation": PLASMA_EXPLANATION}

    corrected, warnings = [], []
    for raw in rows:
        r = dict(raw or {})
        sample = str(r.get("sample_id") or r.get("sample") or "").strip() or "(unnamed)"
        element = str(r.get("element") or "").strip()
        role = _canon_role(r.get("role") or r.get("measured_or_predicted"))
        unit = r.get("unit")
        value = _f(r.get("concentration") if "concentration" in r else r.get("value"))
        dil = _f(r.get("dilution_factor"))
        blank = _f(r.get("blank_value"))
        dl = _f(r.get("detection_limit"))
        tag = f"{sample}/{element or '?'}"
        row_warnings = []

        if not element:
            row_warnings.append(f"{tag}: missing element — cannot convert to mM.")
        if value is None:
            row_warnings.append(f"{tag}: missing/non-numeric concentration — mM skipped.")
        if unit in (None, ""):
            row_warnings.append(f"{tag}: missing unit — cannot convert to mM.")
        if value is not None and value < 0:
            row_warnings.append(f"{tag}: negative concentration {value:g} is impossible — flagged.")
        if dil is None:
            dil = 1.0
        elif dil <= 0:
            row_warnings.append(f"{tag}: dilution_factor {dil:g} ≤ 0 invalid — using 1.0.")
            dil = 1.0

        below_dl = bool(dl is not None and value is not None and value < dl)
        if below_dl:
            row_warnings.append(f"{tag}: {value:g} below detection limit {dl:g} — non-detect, not zero.")

        corrected_value = value
        if value is not None:
            if apply_blank and blank is not None:
                corrected_value = value - blank
                if corrected_value < 0:
                    row_warnings.append(f"{tag}: blank {blank:g} ≥ reading {value:g} — clamped to 0.")
                    corrected_value = 0.0
            corrected_value = corrected_value * dil

        value_mM = None
        if corrected_value is not None and element and unit not in (None, ""):
            value_mM, warn = _to_mM(corrected_value, unit, element)
            if warn:
                row_warnings.append(f"{tag}: {warn}")

        corrected.append({
            "sample_id": sample, "element": element, "role": role,
            "input_value": value, "input_unit": unit, "dilution_factor": dil,
            "blank_value": blank, "corrected_value": corrected_value,
            "value_mM": value_mM, "below_detection_limit": below_dl,
            "warnings": row_warnings,
        })
        warnings.extend(row_warnings)

    residuals = _residuals(corrected, warnings)
    # Dedupe warnings, preserve order.
    warnings = list(dict.fromkeys(warnings))
    return {"station": "icp", "corrected": corrected, "residuals": residuals,
            "fabricated": False, "warnings": warnings, "explanation": PLASMA_EXPLANATION}


def _residuals(corrected, warnings) -> list:
    """Build measured−predicted residuals from matching (sample, element) pairs. Validation, not sim."""
    measured, predicted = {}, {}
    for r in corrected:
        if r["value_mM"] is None or r["below_detection_limit"]:
            continue
        key = (r["sample_id"], r["element"])
        if r["role"] == MEASURED:
            measured[key] = r["value_mM"]
        elif r["role"] == PREDICTED:
            predicted[key] = r["value_mM"]

    out = []
    for key in sorted(set(measured) & set(predicted)):
        m, p = measured[key], predicted[key]
        pct = None if p == 0 else round(100.0 * (m - p) / p, 4)
        out.append({"sample_id": key[0], "element": key[1], "measured_mM": m, "predicted_mM": p,
                    "residual_mM": round(m - p, 8), "percent_difference": pct,
                    "note": "" if p != 0 else "predicted is 0 — percent difference undefined."})
    if (measured and predicted) and not out:
        warnings.append("Measured and predicted rows were given but none share a (sample, element) "
                        "pair — no residuals computed.")
    return out
