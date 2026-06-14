"""Calculation registry + verification helpers (transparency, not chemistry).

This module documents and **re-derives the downstream arithmetic** the app
applies on top of PHREEQC output and measured data — unit conversions, dilution
correction, liquid/solid ratio, mass released, recovery, and the
``measured − PHREEQC`` residuals. It then audits whether the values stored in
``comparison_measured_vs_phreeqc.csv`` match a fresh recomputation.

It deliberately does **not** reimplement PHREEQC. Saturation index and pH are
*explained* (they come straight from the PHREEQC solver), never recomputed here.
Everything in this file is plain arithmetic so it can be unit-tested in isolation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from . import units

# Atomic masses (g/mol) used for the ICP mg/L -> mM conversion. The single registry
# lives in :mod:`flyash_phreeqc_ml.units`; this is a back-compat alias so existing
# imports of ``calculations.ATOMIC_MASSES`` keep working but resolve to one source.
ATOMIC_MASSES: dict[str, float] = units.MOLAR_MASSES

# Audit status vocabulary.
STATUS_PASS = "pass"
STATUS_WARNING = "warning"
STATUS_FAIL = "fail"
STATUS_NA = "not available"

# Default tolerances for the audit. pass <= PASS_TOL; warning band up to WARN_TOL;
# anything larger is a fail. Missing inputs -> not available.
PASS_TOL = 1e-6
WARN_TOL = 1e-4


# --------------------------------------------------------------------------- #
# Pure calculation helpers (these are the formulas the app actually uses)
# --------------------------------------------------------------------------- #
def mgl_to_mM(mg_per_l: float, element: str) -> float:
    """Convert an ICP concentration in mg/L to mM using the element atomic mass.

    ``mM = mg/L / atomic_mass_g_mol``. Routes through the single conversion authority
    (:func:`units.convert`), so the formula + molar mass are defined in one place.
    Raises :class:`units.UnknownElementError` for an unknown element.
    """
    return units.convert(mg_per_l, units.UNIT_MGL, units.UNIT_MM, element).value


def apply_dilution(reported_concentration: float, dilution_factor: float) -> float:
    """Correct a reported concentration for dilution: reported * dilution_factor."""
    return reported_concentration * dilution_factor


def liquid_solid_ratio(solution_volume_mL: float, fly_ash_mass_g: float) -> float:
    """Liquid/solid ratio = solution_volume_mL / fly_ash_mass_g."""
    return solution_volume_mL / fly_ash_mass_g


def mass_released_mg(concentration_mg_L: float, solution_volume_L: float) -> float:
    """Mass released (mg) = concentration_mg_L * solution_volume_L."""
    return concentration_mg_L * solution_volume_L


def recovery_percent(extracted_mass: float, original_mass_in_solid: float) -> float:
    """Recovery (%) = extracted_mass / original_mass_in_solid * 100."""
    return extracted_mass / original_mass_in_solid * 100.0


def residual(measured: float, predicted: float) -> float:
    """Residual = measured − PHREEQC prediction (same units as the inputs)."""
    return measured - predicted


# --------------------------------------------------------------------------- #
# Formula registry (for the Calculation Verification tab display)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Formula:
    """One documented calculation: equation, I/O columns, units, provenance."""

    name: str
    equation: str          # plain-text equation
    latex: str             # LaTeX body for st.latex (no surrounding $$)
    inputs: tuple[str, ...]
    output: str
    units: str
    explanation: str
    source: str            # "app-calculated" or "parsed from PHREEQC"
    detail: str = ""       # extra detail, surfaced only in developer mode


FORMULAS: tuple[Formula, ...] = (
    Formula(
        name="pH residual",
        equation="residual_pH = measured_final_pH - phreeqc_pH",
        latex=r"\mathrm{residual\_pH} = \mathrm{measured\_final\_pH} - \mathrm{phreeqc\_pH}",
        inputs=("final_pH", "phreeqc_pH"),
        output="residual_pH",
        units="pH units",
        explanation="Positive residual means the measured pH is higher than PHREEQC predicts.",
        source="app-calculated",
        detail="pH is a logarithm of activity, so residuals are differences of logs — "
               "a residual of +1 is a 10x difference in H+ activity, not a 'small' error.",
    ),
    Formula(
        name="Element residual (Ca/Si/Al/Fe)",
        equation="residual_X = measured_X_mM - phreeqc_X_mM",
        latex=r"\mathrm{residual\_X} = \mathrm{measured\_X\_mM} - \mathrm{phreeqc\_X\_mM}",
        inputs=("X_mM", "phreeqc_X_mM"),
        output="residual_X",
        units="mM",
        explanation="measured − PHREEQC for each element. Positive means the experiment "
                    "released more of X than PHREEQC predicted.",
        source="app-calculated",
        detail="Fe is often unpredicted by the CEMDATA18 runs, so residual_Fe can be entirely "
               "NaN — that is 'unavailable', not 'PHREEQC predicts zero Fe'.",
    ),
    Formula(
        name="ICP mg/L → mM conversion",
        equation="mM = mg_per_L / atomic_mass_g_mol",
        latex=r"\mathrm{mM} = \dfrac{\mathrm{mg/L}}{\mathrm{atomic\_mass\ (g/mol)}}",
        inputs=("concentration_mg_L", "atomic_mass_g_mol"),
        output="X_mM",
        units="mM (from mg/L)",
        explanation="Converts an ICP mass concentration to a molar concentration using the "
                    f"element atomic mass. Atomic masses: {ATOMIC_MASSES}.",
        source="app-calculated",
        detail="Always apply the dilution factor BEFORE converting — ICP reports the diluted "
               "aliquot, not the original solution.",
    ),
    Formula(
        name="Dilution correction",
        equation="corrected_concentration = reported_concentration * dilution_factor",
        latex=r"\mathrm{corrected} = \mathrm{reported} \times \mathrm{dilution\_factor}",
        inputs=("reported_concentration", "dilution_factor"),
        output="corrected_concentration",
        units="same as reported (e.g. mg/L)",
        explanation="Scales the measured aliquot back up to the original solution concentration.",
        source="app-calculated",
        detail="Omitting the dilution factor is the most common ICP unit error — the validator "
               "warns when no dilution factor is recorded.",
    ),
    Formula(
        name="Liquid/solid ratio",
        equation="liquid_solid_ratio = solution_volume_mL / fly_ash_mass_g",
        latex=r"\mathrm{L/S} = \dfrac{\mathrm{solution\_volume\_mL}}{\mathrm{fly\_ash\_mass\_g}}",
        inputs=("solution_volume_mL", "fly_ash_mass_g"),
        output="liquid_solid_ratio",
        units="mL/g (≈ dimensionless)",
        explanation="Ratio of leachant volume to solid mass; a key leaching variable.",
        source="app-calculated",
    ),
    Formula(
        name="Mass released",
        equation="mass_released_mg = concentration_mg_L * solution_volume_L",
        latex=r"\mathrm{mass\_released\_mg} = \mathrm{concentration\_mg/L} \times \mathrm{volume\_L}",
        inputs=("concentration_mg_L", "solution_volume_L"),
        output="mass_released_mg",
        units="mg",
        explanation="Total mass of an element leached into solution.",
        source="app-calculated",
    ),
    Formula(
        name="Recovery percent",
        equation="recovery_percent = extracted_mass / original_mass_in_solid * 100",
        latex=r"\mathrm{recovery\%} = \dfrac{\mathrm{extracted\_mass}}{\mathrm{original\_mass\_in\_solid}} \times 100",
        inputs=("extracted_mass", "original_mass_in_solid"),
        output="recovery_percent",
        units="%",
        explanation="Fraction of an element extracted from the solid, as a percentage.",
        source="app-calculated",
    ),
    Formula(
        name="Saturation index (PHREEQC)",
        equation="SI = log10(IAP / Ksp)",
        latex=r"\mathrm{SI} = \log_{10}\!\left(\dfrac{\mathrm{IAP}}{K_{sp}}\right)",
        inputs=("ion activity product (IAP)", "solubility product (Ksp)"),
        output="SI (per phase)",
        units="log10 (dimensionless)",
        explanation="SI > 0 supersaturated (tends to precipitate); SI = 0 at equilibrium; "
                    "SI < 0 undersaturated (tends to dissolve). PHREEQC computes this from "
                    "speciated activities — the app parses it, it does not recompute it.",
        source="parsed from PHREEQC",
        detail="Recomputing SI would require every species' activity and the thermodynamic "
               "database, so the app treats PHREEQC's SI as authoritative output.",
    ),
    Formula(
        name="pH (PHREEQC / measured)",
        equation="pH = -log10(a_H+)",
        latex=r"\mathrm{pH} = -\log_{10}\!\left(a_{\mathrm{H}^+}\right)",
        inputs=("hydrogen ion activity a_H+",),
        output="pH",
        units="pH units",
        explanation="pH is defined on hydrogen ion ACTIVITY, not concentration. In high-ionic-"
                    "strength alkali systems activity ≠ concentration, so PHREEQC's activity "
                    "model matters. The app parses pH; it does not recompute it.",
        source="parsed from PHREEQC",
        detail="At ~pH 13 and high Na+, activity coefficients deviate strongly from 1, which is "
               "exactly why an equilibrium speciation model (PHREEQC) is used.",
    ),
)


# --------------------------------------------------------------------------- #
# Residual audit (recompute residuals from the stored comparison CSV)
# --------------------------------------------------------------------------- #
# key -> (measured_col, phreeqc_col, stored_residual_col, units)
RESIDUAL_AUDITS: tuple[tuple[str, str, str, str, str], ...] = (
    ("pH", "final_pH", "phreeqc_pH", "residual_pH", "pH units"),
    ("Ca", "Ca_mM", "phreeqc_Ca_mM", "residual_Ca", "mM"),
    ("Si", "Si_mM", "phreeqc_Si_mM", "residual_Si", "mM"),
    ("Al", "Al_mM", "phreeqc_Al_mM", "residual_Al", "mM"),
    ("Fe", "Fe_mM", "phreeqc_Fe_mM", "residual_Fe", "mM"),
)

AUDIT_COLUMNS = [
    "sample_id", "formula", "input_1", "input_2",
    "calculated_value", "stored_value", "difference", "status",
]


def _to_float(value) -> float | None:
    """Best-effort float; None for blanks / NaN / non-numeric."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def classify(calculated: float | None, stored: float | None,
             pass_tol: float = PASS_TOL, warn_tol: float = WARN_TOL) -> str:
    """Status from a calculated vs stored comparison.

    not available -> either side missing; pass -> within ``pass_tol``;
    warning -> within ``warn_tol`` (rounding); fail -> beyond ``warn_tol``.
    """
    if calculated is None or stored is None:
        return STATUS_NA
    diff = abs(calculated - stored)
    if diff <= pass_tol:
        return STATUS_PASS
    if diff <= warn_tol:
        return STATUS_WARNING
    return STATUS_FAIL


def audit_residual(measured, predicted, stored,
                   pass_tol: float = PASS_TOL, warn_tol: float = WARN_TOL) -> dict:
    """Recompute one residual and compare it to the stored value.

    Returns a dict with calculated_value, stored_value, difference, status.
    'not available' if either input (measured/predicted) or the stored value is
    missing — the residual cannot be re-derived or compared.
    """
    m = _to_float(measured)
    p = _to_float(predicted)
    s = _to_float(stored)
    calc = None if (m is None or p is None) else residual(m, p)
    if calc is None or s is None:
        status = STATUS_NA
    else:
        status = classify(calc, s, pass_tol, warn_tol)
    diff = None if (calc is None or s is None) else abs(calc - s)
    return {
        "calculated_value": calc,
        "stored_value": s,
        "difference": diff,
        "status": status,
    }


def audit_comparison(df: pd.DataFrame,
                     pass_tol: float = PASS_TOL, warn_tol: float = WARN_TOL) -> pd.DataFrame:
    """Audit every residual in a stored comparison frame.

    For each row and each residual whose stored column exists, recompute
    ``measured − PHREEQC`` and compare to the stored residual. Returns a tidy
    frame with :data:`AUDIT_COLUMNS`. Rows with missing inputs are reported as
    'not available' rather than skipped, so gaps stay visible.
    """
    records: list[dict] = []
    if df is None or df.empty:
        return pd.DataFrame(columns=AUDIT_COLUMNS)

    for _, row in df.iterrows():
        sample_id = row.get("sample_id", "")
        for key, meas_col, pheq_col, resid_col, _units in RESIDUAL_AUDITS:
            if resid_col not in df.columns:
                continue
            measured = row.get(meas_col)
            predicted = row.get(pheq_col)
            stored = row.get(resid_col)
            res = audit_residual(measured, predicted, stored, pass_tol, warn_tol)
            records.append({
                "sample_id": sample_id,
                "formula": f"residual_{key} = {meas_col} - {pheq_col}",
                "input_1": _to_float(measured),
                "input_2": _to_float(predicted),
                "calculated_value": res["calculated_value"],
                "stored_value": res["stored_value"],
                "difference": res["difference"],
                "status": res["status"],
            })
    return pd.DataFrame(records, columns=AUDIT_COLUMNS)


# --------------------------------------------------------------------------- #
# Conversion audit (re-derive each converted column from its provenance companions)
# --------------------------------------------------------------------------- #
# This is the mechanism that catches a wrong molar mass or a changed conversion
# formula *after the fact*: for every converted column X with provenance companions
# (X_orig_value / X_orig_unit / X_conversion_id), recompute the conversion through the
# single authority (units.convert) and compare to the stored converted value.
VERIFY_CONVERSION_COLUMNS = [
    "column", "element", "conversion_id", "from_unit", "molar_mass_used", "formula",
    "n_rows", "n_pass", "n_warning", "n_fail", "n_not_available", "status",
]

STATUS_LEGACY = "legacy (no provenance)"


def _unit_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    s = str(value).strip()
    return s == "" or s.lower() == "nan"


def _element_from_mm_column(col: str) -> str | None:
    """``Ca_mM`` -> ``Ca`` when it is a known element column, else ``None``."""
    if col.endswith("_mM"):
        el = col[:-3]
        if el in units.MOLAR_MASSES:
            return el
    return None


def verify_conversions(df: pd.DataFrame,
                       pass_tol: float = PASS_TOL, warn_tol: float = WARN_TOL) -> pd.DataFrame:
    """Re-derive every converted column from its provenance companions and grade it.

    For each element concentration column ``X_mM`` present in ``df``:

    * if its provenance companions (``X_mM_orig_value`` / ``_orig_unit`` /
      ``_conversion_id``) exist, recompute ``units.convert(orig_value, orig_unit, mM,
      element)`` per row and compare to the stored ``X_mM`` — counting pass / warning /
      fail / not-available, with the overall column status = the worst seen;
    * if the companions are absent but the column has data (an **existing run imported
      before provenance existed**), it is reported with ``conversion_id =
      "unknown(legacy)"`` and status ``legacy (no provenance)`` — flagged, never errored.

    Returns one row per audited column (:data:`VERIFY_CONVERSION_COLUMNS`). This is what
    catches a wrong molar mass or a changed formula after the data was saved.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=VERIFY_CONVERSION_COLUMNS)

    rows: list[dict] = []
    for col in df.columns:
        element = _element_from_mm_column(col)
        if element is None:
            continue
        val_col, unit_col, id_col = units.provenance_columns_for(col)
        has_prov = all(c in df.columns for c in (val_col, unit_col, id_col))
        has_data = pd.to_numeric(df[col], errors="coerce").notna().any()

        if not has_prov:
            if not has_data:
                continue  # empty column, nothing to verify
            rows.append({
                "column": col, "element": element, "conversion_id": units.LEGACY_ID,
                "from_unit": "", "molar_mass_used": None, "formula": "",
                "n_rows": int(len(df)), "n_pass": 0, "n_warning": 0, "n_fail": 0,
                "n_not_available": int(len(df)), "status": STATUS_LEGACY,
            })
            continue

        n_pass = n_warn = n_fail = n_na = 0
        seen_id = seen_from = seen_formula = ""
        seen_mass = None
        for _, r in df.iterrows():
            stored = _to_float(r.get(col))
            orig = _to_float(r.get(val_col))
            ounit = r.get(unit_col)
            if stored is None or orig is None or _unit_blank(ounit):
                n_na += 1
                continue
            try:
                res = units.convert(orig, str(ounit).strip(), units.UNIT_MM, element)
            except units.UnitConversionError:
                n_fail += 1
                continue
            seen_id, seen_from = res.conversion_id, res.from_unit
            seen_formula, seen_mass = res.formula, res.molar_mass
            status = classify(res.value, stored, pass_tol, warn_tol)
            if status == STATUS_PASS:
                n_pass += 1
            elif status == STATUS_WARNING:
                n_warn += 1
            else:
                n_fail += 1

        overall = (STATUS_FAIL if n_fail else STATUS_WARNING if n_warn
                   else STATUS_PASS if n_pass else STATUS_NA)
        rows.append({
            "column": col, "element": element,
            "conversion_id": seen_id or units.LEGACY_ID, "from_unit": seen_from,
            "molar_mass_used": seen_mass, "formula": seen_formula,
            "n_rows": int(len(df)), "n_pass": n_pass, "n_warning": n_warn,
            "n_fail": n_fail, "n_not_available": n_na, "status": overall,
        })
    return pd.DataFrame(rows, columns=VERIFY_CONVERSION_COLUMNS)
