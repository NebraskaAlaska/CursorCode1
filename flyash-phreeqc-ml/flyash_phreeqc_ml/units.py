"""Single conversion authority — every unit conversion in the app goes through here.

The problem this solves: conversions used to happen in three places (the importer's
mg/L·ppm·ppb logic, ``config.PHREEQC_MOLALITY_TO_MM``, and ``calculations.mgl_to_mM``),
each storing only the *converted* number. The original value, its unit, the formula,
and the molar mass used were lost — so a wrong conversion was undetectable afterward
and a residual could be silently wrong.

This module is the one place that:

* owns the molar-mass registry (:data:`MOLAR_MASSES`, IUPAC standard atomic weights —
  the same numbers are surfaced in the UI, not only here),
* owns the conversion registry (:data:`CONVERSIONS`) — each entry has an ``id``, a
  ``from_unit`` / ``to_unit``, a human-readable ``formula`` string, and the function,
* and exposes :func:`convert`, which returns a :class:`ConversionResult` carrying the
  converted value **plus** the registry id and the parameters used (molar mass), so a
  caller can store full provenance.

**No silent fallbacks.** An unknown unit or an unknown element raises a typed error
(:class:`UnknownUnitError` / :class:`UnknownElementError`) — never a guess, never a
default. The only "soft" id is :data:`LEGACY_ID`, used to *describe* old data that was
imported before provenance existed; it is never produced by :func:`convert`.

Imports nothing from the rest of the package (so ``config`` can re-export the molality
factor from here without a cycle).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


# --------------------------------------------------------------------------- #
# Typed errors (no silent fallbacks)
# --------------------------------------------------------------------------- #
class UnitConversionError(Exception):
    """Base error for any unit-conversion problem."""


class UnknownUnitError(UnitConversionError):
    """Raised when a from/to unit pair has no registered conversion."""


class UnknownElementError(UnitConversionError):
    """Raised when a mass→molar conversion is asked for with an unknown element."""


# --------------------------------------------------------------------------- #
# Molar masses — IUPAC standard atomic weights (the single registry)
# --------------------------------------------------------------------------- #
# Values are IUPAC 2021 standard atomic weights (abridged, g/mol), as published by
# the IUPAC Commission on Isotopic Abundances and Atomic Weights (CIAAW):
#   https://iupac.qmul.ac.uk/AtWt/  (Ca 40.078, Si 28.085, Al 26.9815385,
#   Fe 55.845, Na 22.98976928, K 39.0983). Rounded here to the precision the ICP
#   conversion needs; the source string is surfaced in the UI alongside the table.
MOLAR_MASSES: dict[str, float] = {
    "Ca": 40.078,
    "Si": 28.085,
    "Al": 26.982,
    "Fe": 55.845,
    "Na": 22.990,
    "K": 39.098,
}
MOLAR_MASS_SOURCE = "IUPAC standard atomic weights (2021 abridged), g/mol — CIAAW"

# Canonical unit spellings.
UNIT_MM = "mM"
UNIT_MGL = "mg/L"
UNIT_PPM = "ppm"
UNIT_PPB = "ppb"
UNIT_MOLALITY = "mol/kgw"
# Mass → amount-of-substance units (used by the batch-reaction mass balance).
UNIT_MG = "mg"
UNIT_MMOL = "mmol"

# PHREEQC reports element totals as molality (mol/kgw). For dilute solutions
# mol/kgw ≈ mol/L, so ×1000 gives mM. Defined once here; config re-exports it.
MOLALITY_TO_MM_FACTOR = 1000.0

# The units a lab concentration column may be imported in (the importer's contract).
# Molality is a PHREEQC/model-side unit, so it is deliberately NOT a lab import unit.
LAB_CONCENTRATION_SOURCE_UNITS = (UNIT_MGL, UNIT_PPM, UNIT_PPB, UNIT_MM)

# Conversion ids that are not "a real conversion".
IDENTITY_ID = "identity"            # value already in the target unit
LEGACY_ID = "unknown(legacy)"       # describes pre-provenance data; never produced here

# Per-converted-column provenance companions. For a converted column ``X`` the
# importer also stores ``X_orig_value`` / ``X_orig_unit`` / ``X_conversion_id`` so the
# conversion can be re-derived and audited later (flat-CSV wide companions).
ORIG_VALUE_SUFFIX = "_orig_value"
ORIG_UNIT_SUFFIX = "_orig_unit"
CONVERSION_ID_SUFFIX = "_conversion_id"
CONVERSION_PROVENANCE_SUFFIXES = (ORIG_VALUE_SUFFIX, ORIG_UNIT_SUFFIX, CONVERSION_ID_SUFFIX)


def provenance_columns_for(column: str) -> list[str]:
    """The three companion column names for a converted column (stable order)."""
    return [f"{column}{s}" for s in CONVERSION_PROVENANCE_SUFFIXES]


def is_conversion_provenance_column(name: str) -> bool:
    """True if ``name`` is a conversion-provenance companion (so it is *not* unknown)."""
    return any(str(name).endswith(s) for s in CONVERSION_PROVENANCE_SUFFIXES)


# --------------------------------------------------------------------------- #
# Conversion registry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Conversion:
    """One registered conversion: id, units, a human-readable formula, the function.

    ``formula`` is a template; ``{element}`` is substituted with the element symbol
    for display (e.g. ``mM = (mg/L) / M_Ca``). ``func(value, molar_mass)`` does the
    arithmetic; ``molar_mass`` is ``None`` when ``requires_molar_mass`` is False.
    """

    id: str
    from_unit: str
    to_unit: str
    formula: str
    requires_molar_mass: bool
    func: object  # callable(value, molar_mass|None) -> value


CONVERSIONS: tuple[Conversion, ...] = (
    Conversion(
        id="mgL_to_mM", from_unit=UNIT_MGL, to_unit=UNIT_MM,
        formula="mM = (mg/L) / M_{element}",
        requires_molar_mass=True, func=lambda v, m: v / m),
    Conversion(
        id="ppm_to_mM", from_unit=UNIT_PPM, to_unit=UNIT_MM,
        formula="mM = (ppm) / M_{element}   [ppm ≈ mg/L for dilute aqueous]",
        requires_molar_mass=True, func=lambda v, m: v / m),
    Conversion(
        id="ppb_to_mM", from_unit=UNIT_PPB, to_unit=UNIT_MM,
        formula="mM = (ppb / 1000) / M_{element}   [ppb = µg/L]",
        requires_molar_mass=True, func=lambda v, m: (v / 1000.0) / m),
    Conversion(
        id="molality_to_mM", from_unit=UNIT_MOLALITY, to_unit=UNIT_MM,
        formula="mM = molality × 1000   [mol/kgw ≈ mol/L for dilute solutions]",
        requires_molar_mass=False, func=lambda v, m: v * MOLALITY_TO_MM_FACTOR),
    Conversion(
        id="mg_to_mmol", from_unit=UNIT_MG, to_unit=UNIT_MMOL,
        formula="mmol = mg / M_{element}",
        requires_molar_mass=True, func=lambda v, m: v / m),
)

_BY_PAIR: dict[tuple[str, str], Conversion] = {
    (c.from_unit, c.to_unit): c for c in CONVERSIONS
}


@dataclass(frozen=True)
class ConversionResult:
    """A converted value plus everything needed to re-derive or audit it."""

    value: float
    conversion_id: str
    from_unit: str
    to_unit: str
    element: str | None
    molar_mass: float | None
    formula: str


# --------------------------------------------------------------------------- #
# Lookups + display
# --------------------------------------------------------------------------- #
def known_elements() -> list[str]:
    return list(MOLAR_MASSES)


def molar_mass(element: str) -> float:
    """Molar mass for an element, or :class:`UnknownElementError` (never a guess)."""
    if element not in MOLAR_MASSES:
        raise UnknownElementError(
            f"unknown element {element!r}; known: {', '.join(MOLAR_MASSES)}")
    return MOLAR_MASSES[element]


def supported_source_units(to_unit: str = UNIT_MM) -> list[str]:
    """Source units that can be converted *to* ``to_unit`` (identity included first)."""
    return [to_unit] + [c.from_unit for c in CONVERSIONS if c.to_unit == to_unit]


def _formula_for(formula: str, element: str | None) -> str:
    return formula.replace("{element}", str(element)) if element else \
        formula.replace("{element}", "?")


def _unknown_unit_message(from_unit: str, to_unit: str, element: str | None) -> str:
    who = element or "this variable"
    if to_unit == UNIT_MM:
        supported = ", ".join(LAB_CONCENTRATION_SOURCE_UNITS)
    else:
        supported = ", ".join(supported_source_units(to_unit))
    return f"unit {from_unit!r} not recognized for {who}; supported: {supported}"


def molar_mass_rows() -> list[dict]:
    """The molar-mass registry as display rows (for the Calculation Verification UI)."""
    return [{"element": e, "molar_mass_g_mol": m, "source": MOLAR_MASS_SOURCE}
            for e, m in MOLAR_MASSES.items()]


def conversion_registry_rows() -> list[dict]:
    """The conversion registry as display rows (identity + every registered conversion)."""
    rows = [{"id": IDENTITY_ID, "from_unit": "<target>", "to_unit": "<target>",
             "formula": "value unchanged (already in the target unit)",
             "requires_molar_mass": False}]
    for c in CONVERSIONS:
        rows.append({"id": c.id, "from_unit": c.from_unit, "to_unit": c.to_unit,
                     "formula": c.formula, "requires_molar_mass": c.requires_molar_mass})
    return rows


# --------------------------------------------------------------------------- #
# The one conversion entry point
# --------------------------------------------------------------------------- #
def convert(value, from_unit: str, to_unit: str, element: str | None = None) -> ConversionResult:
    """Convert ``value`` from ``from_unit`` to ``to_unit`` for ``element``.

    Returns a :class:`ConversionResult` with the converted value, the registry id,
    the units, the element, the molar mass used (or ``None``), and the human-readable
    formula. Identity (``from_unit == to_unit``) returns the value unchanged with
    ``conversion_id == "identity"``. An unregistered unit pair raises
    :class:`UnknownUnitError`; a mass→molar conversion with an unknown element raises
    :class:`UnknownElementError`. **There is no silent fallback.**
    """
    fu, tu = str(from_unit), str(to_unit)
    if fu == tu:
        return ConversionResult(value=value, conversion_id=IDENTITY_ID, from_unit=fu,
                                to_unit=tu, element=element, molar_mass=None,
                                formula=f"value unchanged (already {tu})")
    conv = _BY_PAIR.get((fu, tu))
    if conv is None:
        raise UnknownUnitError(_unknown_unit_message(fu, tu, element))
    m = None
    if conv.requires_molar_mass:
        m = molar_mass(element) if element is not None else None
        if m is None:
            raise UnknownElementError(
                f"conversion {conv.id!r} needs an element molar mass; got element={element!r}. "
                f"Known: {', '.join(MOLAR_MASSES)}")
    return ConversionResult(value=conv.func(value, m), conversion_id=conv.id, from_unit=fu,
                            to_unit=tu, element=element, molar_mass=m,
                            formula=_formula_for(conv.formula, element))


def convert_series(series: pd.Series, from_unit: str, to_unit: str,
                   element: str | None = None) -> tuple[pd.Series, ConversionResult]:
    """Vectorised :func:`convert` over a column (non-numeric → NaN).

    Returns ``(converted_series, meta)`` where ``meta`` is a :class:`ConversionResult`
    describing the column-level conversion (its ``value`` field is NaN — the per-cell
    values are in the returned series). The unit/element validation is identical to
    :func:`convert`, so an unknown unit/element raises the same typed errors.
    """
    numeric = pd.to_numeric(series, errors="coerce")
    fu, tu = str(from_unit), str(to_unit)
    if fu == tu:
        meta = ConversionResult(float("nan"), IDENTITY_ID, fu, tu, element, None,
                                f"value unchanged (already {tu})")
        return numeric, meta
    conv = _BY_PAIR.get((fu, tu))
    if conv is None:
        raise UnknownUnitError(_unknown_unit_message(fu, tu, element))
    m = None
    if conv.requires_molar_mass:
        m = molar_mass(element) if element is not None else None
        if m is None:
            raise UnknownElementError(
                f"conversion {conv.id!r} needs an element molar mass; got element={element!r}.")
    converted = numeric.apply(lambda v: conv.func(v, m))
    meta = ConversionResult(float("nan"), conv.id, fu, tu, element, m,
                            _formula_for(conv.formula, element))
    return converted, meta


def molality_to_mM(value: float) -> float:
    """PHREEQC molality (mol/kgw) → mM, via the single registry factor."""
    return convert(value, UNIT_MOLALITY, UNIT_MM).value
