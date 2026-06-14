"""Deterministic batch-reaction element closure (arithmetic only — no model/AI/ML).

For a batch reaction (solid material + reagent → leached liquid + residual solid) this
computes, per element, the **moles in** the charged solid, the **moles in the liquid**
(measured), and the **moles in the residual solid**, and reports the **gap**:

    gap = n_in − n_liquid − n_solid

The gap is a *measured fact* — element that is **not yet attributed** to liquid or
solid. No mechanism is attached; this module adds no chemistry interpretation.

Rules that keep it honest:

* All molar (mass → amount) conversions go through :func:`units.convert`, so every
  derived term carries a ``conversion_id`` and the molar mass used.
* A **missing required term** makes the whole closure ``incomplete`` and lists the
  fields — a partial number is **never** shown as if it were real.
* A missing recovered ``solid_mass_g`` is **assumed = material mass** and the
  assumption is recorded in ``assumptions`` — never silently fabricated.
* Uncertainty is propagated (standard error propagation) **only** when per-input
  sigmas are supplied; otherwise ``gap_sigma=None`` and the result is labelled
  ``uncertainty="unknown"`` — never implied to be zero.

The unit throughout is **mmol**. Pure functions operating on a row dict + a
:class:`profiles.DatasetProfile` that declares which columns/units apply.
"""
from __future__ import annotations

import math

import pandas as pd

from . import profiles, units

STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"
WORKING_UNIT = units.UNIT_MMOL

# Conversion id stamped on the liquid term when the measured value is already molar
# (mM) and carries no import-conversion companion — the molar step happened upstream.
LIQUID_MOLAR_ID = "molar_input(mM)"

# Accepted solid-assay units (declared by the profile, never guessed).
_WT_PCT = {"wt%", "%", "weight%", "wtpercent"}
_MG_PER_KG = {"mg/kg", "mgkg", "ppm"}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _to_float(value):
    """Best-effort float, or None for blank/NaN/non-numeric (never a guess)."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    s = str(value).strip()
    if s == "" or s.lower() in ("nan", "none", "na"):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _present(value):
    if value in (None, ""):
        return False
    try:
        return not pd.isna(value)
    except (TypeError, ValueError):  # pragma: no cover
        return True


def _assay_to_mg(content: float, unit: str, mass_g: float, content_col: str,
                 mass_col: str) -> tuple[float, str]:
    """Element mass (mg) from a solid assay × material mass; returns (mg, formula)."""
    u = str(unit).strip().lower().replace(" ", "")
    if u in _WT_PCT:
        # element_g = mass_g × content/100 ; mg = ×1000  →  mass_g × content × 10
        return mass_g * content * 10.0, f"({content_col} wt% × {mass_col} g × 10) mg"
    if u in _MG_PER_KG:
        # element_mg = (mass_g/1000 kg) × content(mg/kg)
        return mass_g / 1000.0 * content, f"({content_col} mg/kg × {mass_col} g / 1000) mg"
    raise units.UnknownUnitError(
        f"unsupported solid-assay unit {unit!r} for {content_col}; use 'wt%' or 'mg/kg'.")


def _term(value, *, conversion_id=None, molar_mass=None, formula="", inputs=None,
          missing=None, assumptions=None) -> dict:
    """One closure term (n_in / n_liquid / n_solid) with its provenance."""
    return {
        "value": value, "unit": WORKING_UNIT, "conversion_id": conversion_id,
        "molar_mass": molar_mass, "formula": formula, "inputs": inputs or {},
        "missing_fields": list(missing or []), "assumptions": list(assumptions or []),
    }


# --------------------------------------------------------------------------- #
# The three terms
# --------------------------------------------------------------------------- #
def moles_in(row: dict, element: str, profile=None) -> dict:
    """Moles of ``element`` charged in the solid = starting assay × material mass."""
    profile = profile or profiles.FLY_ASH_PROFILE
    mass_col = profile.material_mass_column
    content_col = f"{element}_starting_content"
    mass_g = _to_float(row.get(mass_col))
    content = _to_float(row.get(content_col))
    missing = [c for c, v in ((mass_col, mass_g), (content_col, content)) if v is None]
    if missing:
        return _term(None, missing=missing,
                     formula=f"n_in = (starting assay × material mass) / M_{element}")
    mg, mass_formula = _assay_to_mg(content, profile.starting_content_unit, mass_g,
                                    content_col, mass_col)
    res = units.convert(mg, units.UNIT_MG, units.UNIT_MMOL, element)
    return _term(res.value, conversion_id=res.conversion_id, molar_mass=res.molar_mass,
                 formula=f"n_in = {mass_formula} → {res.formula}",
                 inputs={mass_col: mass_g, content_col: content})


def moles_liquid(row: dict, element: str, profile=None) -> dict:
    """Moles of ``element`` in the liquid = measured conc (mM) × liquid volume."""
    profile = profile or profiles.FLY_ASH_PROFILE
    conc_col = f"{element}_mM"
    vol_col = profile.liquid_volume_column
    conc = _to_float(row.get(conc_col))
    vol_mL = _to_float(row.get(vol_col))
    missing = [c for c, v in ((conc_col, conc), (vol_col, vol_mL)) if v is None]
    if missing:
        return _term(None, missing=missing,
                     formula=f"n_liquid = {conc_col}(mM) × {vol_col}(mL)/1000")
    n = conc * (vol_mL / 1000.0)
    # The molar step (mg/L → mM) happened at import; carry its conversion id if present.
    cid = row.get(f"{conc_col}{units.CONVERSION_ID_SUFFIX}")
    cid = str(cid) if _present(cid) else LIQUID_MOLAR_ID
    return _term(n, conversion_id=cid, molar_mass=None,
                 formula=f"n_liquid = {conc_col}(mM) × {vol_col}(mL)/1000 = mmol",
                 inputs={conc_col: conc, vol_col: vol_mL})


def moles_solid(row: dict, element: str, profile=None) -> dict:
    """Moles of ``element`` in the residual solid = residue assay × recovered solid mass.

    If ``solid_mass_g`` is absent it is **assumed = material mass** (no mass-change
    correction) and the assumption is recorded — never silently fabricated.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    residue_col = f"{element}_solid_residue"
    solid_col = profile.solid_mass_column
    mat_col = profile.material_mass_column
    residue = _to_float(row.get(residue_col))
    solid_mass = _to_float(row.get(solid_col))
    assumptions: list[str] = []
    missing: list[str] = []
    used_mass_col = solid_col

    if residue is None:
        missing.append(residue_col)
    if solid_mass is None:
        mat_mass = _to_float(row.get(mat_col))
        if mat_mass is None:
            missing.append(solid_col)
        else:
            solid_mass = mat_mass
            used_mass_col = mat_col
            assumptions.append(
                f"recovered solid mass ({solid_col}) not recorded; assumed = "
                f"{mat_col} = {mat_mass:g} g (no mass-change correction).")
    if missing:
        return _term(None, missing=missing, assumptions=assumptions,
                     formula=f"n_solid = (residue assay × solid mass) / M_{element}")
    mg, mass_formula = _assay_to_mg(residue, profile.solid_residue_unit, solid_mass,
                                    residue_col, used_mass_col)
    res = units.convert(mg, units.UNIT_MG, units.UNIT_MMOL, element)
    return _term(res.value, conversion_id=res.conversion_id, molar_mass=res.molar_mass,
                 assumptions=assumptions,
                 formula=f"n_solid = {mass_formula} → {res.formula}",
                 inputs={residue_col: residue, used_mass_col: solid_mass})


# --------------------------------------------------------------------------- #
# Uncertainty propagation
# --------------------------------------------------------------------------- #
def _product_sigma(term: dict, sigmas: dict) -> float | None:
    """σ of a product term via relative-error quadrature over its numeric inputs."""
    v = term["value"]
    if v is None:
        return None
    rel_sq = []
    for name, val in term["inputs"].items():
        sg = sigmas.get(name)
        if sg is not None and isinstance(val, (int, float)) and val not in (0, 0.0):
            rel_sq.append((float(sg) / float(val)) ** 2)
    if not rel_sq:
        return None
    return abs(v) * math.sqrt(sum(rel_sq))


def _gap_sigma(t_in: dict, t_liq: dict, t_sol: dict, sigmas: dict | None):
    """Propagate per-input sigmas to gap_sigma (sum in quadrature). None → unknown."""
    if not sigmas:
        return None, "unknown"
    parts = [s for s in (_product_sigma(t_in, sigmas), _product_sigma(t_liq, sigmas),
                         _product_sigma(t_sol, sigmas)) if s is not None]
    if not parts:
        return None, "unknown"
    return math.sqrt(sum(p ** 2 for p in parts)), "propagated"


# --------------------------------------------------------------------------- #
# Closure
# --------------------------------------------------------------------------- #
def closure(row: dict, element: str, *, profile=None, sigmas: dict | None = None) -> dict:
    """Deterministic element closure for one batch row.

    Returns ``{element, n_in, n_liquid, n_solid, gap, gap_fraction, gap_sigma,
    uncertainty, status, missing_fields, assumptions, provenance, unit}``. When any
    required term is missing the status is ``incomplete``, the missing fields are
    listed, and ``gap`` is ``None`` (never a partial number shown as real).
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    t_in = moles_in(row, element, profile)
    t_liq = moles_liquid(row, element, profile)
    t_sol = moles_solid(row, element, profile)

    missing = sorted(set(t_in["missing_fields"] + t_liq["missing_fields"]
                         + t_sol["missing_fields"]))
    assumptions = t_in["assumptions"] + t_liq["assumptions"] + t_sol["assumptions"]
    provenance = {"n_in": t_in, "n_liquid": t_liq, "n_solid": t_sol}
    n_in, n_liq, n_sol = t_in["value"], t_liq["value"], t_sol["value"]

    base = {
        "element": element, "unit": WORKING_UNIT,
        "n_in": n_in, "n_liquid": n_liq, "n_solid": n_sol,
        "missing_fields": missing, "assumptions": assumptions, "provenance": provenance,
    }
    if n_in is None or n_liq is None or n_sol is None:
        base.update(gap=None, gap_fraction=None, gap_sigma=None,
                    uncertainty="unknown", status=STATUS_INCOMPLETE)
        return base

    gap = n_in - n_liq - n_sol
    gap_fraction = (gap / n_in) if n_in not in (None, 0) else None
    gap_sigma, uncertainty = _gap_sigma(t_in, t_liq, t_sol, sigmas)
    base.update(gap=gap, gap_fraction=gap_fraction, gap_sigma=gap_sigma,
                uncertainty=uncertainty, status=STATUS_COMPLETE)
    return base


# --------------------------------------------------------------------------- #
# Sanity warnings (validation-surface style; never silent fixes)
# --------------------------------------------------------------------------- #
# Thresholds: a negative gap is "significant" if it exceeds 2σ (when known) or 5% of
# the input (when σ unknown). Over-recovery beyond 5% and gap > 100% of input are
# flagged. These NAME a likely culprit; they never alter the numbers.
NEG_GAP_FRACTION_TOL = 0.05
OVER_RECOVERY_TOL = 1.05


def closure_warnings(result: dict) -> list[dict]:
    """Validation-style issues for one closure (``severity/check/column/message``)."""
    el = result["element"]
    if result["status"] != STATUS_COMPLETE:
        return [{
            "severity": "info", "check": "mass_balance_incomplete", "column": el,
            "message": (f"{el} closure incomplete — missing "
                        f"{', '.join(result['missing_fields']) or 'inputs'}. No gap shown."),
        }]

    gap, gf, sig = result["gap"], result["gap_fraction"], result["gap_sigma"]
    n_in, n_liq, n_sol = result["n_in"], result["n_liquid"], result["n_solid"]
    issues: list[dict] = []

    neg_significant = (gap < -2.0 * sig) if sig is not None else \
        (gf is not None and gf < -NEG_GAP_FRACTION_TOL)
    if neg_significant:
        issues.append({
            "severity": "warning", "check": "mass_balance_negative_gap", "column": el,
            "message": (f"{el} gap is negative ({gap:.3g} mmol): more {el} recovered than "
                        "charged. Likely a low/uncertain starting assay, contamination, or a "
                        "unit error — check the starting content and units."),
        })
    if gf is not None and gf > 1.0:
        issues.append({
            "severity": "error", "check": "mass_balance_gap_over_input", "column": el,
            "message": (f"{el} unaccounted gap is {gf:.0%} of the charged amount — larger than "
                        "what went in. Check the starting assay / material mass / units."),
        })
    if n_in not in (None, 0):
        recovery = (n_liq + n_sol) / n_in
        if recovery > OVER_RECOVERY_TOL:
            issues.append({
                "severity": "warning", "check": "mass_balance_over_recovery", "column": el,
                "message": (f"{el} total recovery is {recovery:.0%} (liquid + solid exceed the "
                            "charged amount) — likely an assay or unit error."),
            })
    return issues


# --------------------------------------------------------------------------- #
# Frame helpers (for the UI / batch reporting)
# --------------------------------------------------------------------------- #
def is_enabled(profile=None) -> bool:
    """True if the profile opts into the batch-reaction mass balance."""
    profile = profile or profiles.FLY_ASH_PROFILE
    return bool(getattr(profile, "mass_balance_elements", ()) or ())


def closure_records(data: pd.DataFrame, profile=None,
                    *, sigmas_by_row=None) -> list[dict]:
    """One closure dict per (row, element) for an opted-in profile (else ``[]``).

    ``sigmas_by_row`` (optional) maps a row's ``sample_id`` to a per-input sigma dict.
    """
    profile = profile or profiles.FLY_ASH_PROFILE
    elements = list(getattr(profile, "mass_balance_elements", ()) or ())
    if not elements or data is None or data.empty:
        return []
    out: list[dict] = []
    sigmas_by_row = sigmas_by_row or {}
    for _, r in data.iterrows():
        row = r.to_dict()
        sid = str(row.get("sample_id", "")).strip()
        for el in elements:
            res = closure(row, el, profile=profile, sigmas=sigmas_by_row.get(sid))
            res = {**res, "sample_id": sid}
            out.append(res)
    return out


CLOSURE_TABLE_COLUMNS = [
    "sample_id", "element", "n_in", "n_liquid", "n_solid", "gap", "gap_fraction",
    "gap_sigma", "uncertainty", "status", "missing_fields",
]


def closure_table(records: list[dict]) -> pd.DataFrame:
    """A flat table view of :func:`closure_records` output (numbers + status)."""
    rows = []
    for r in records or []:
        rows.append({
            "sample_id": r.get("sample_id", ""), "element": r["element"],
            "n_in": r["n_in"], "n_liquid": r["n_liquid"], "n_solid": r["n_solid"],
            "gap": r["gap"], "gap_fraction": r["gap_fraction"],
            "gap_sigma": r["gap_sigma"], "uncertainty": r["uncertainty"],
            "status": r["status"], "missing_fields": ", ".join(r["missing_fields"]),
        })
    return pd.DataFrame(rows, columns=CLOSURE_TABLE_COLUMNS)
