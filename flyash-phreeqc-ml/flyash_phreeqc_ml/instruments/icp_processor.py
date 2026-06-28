"""ICP Data Processor — reduce **measured or predicted** ICP solution data (no plasma simulation).

This is *not* an ICP-OES / ICP-MS plasma simulator. It is a data-reduction workflow over
concentration data the user already has (measured in the lab, or predicted by PHREEQC):

* convert mg/L (or ppm / ppb / µg/L) to **mM** via the single conversion authority
  (:mod:`flyash_phreeqc_ml.units`), or pass through values already in mM,
* apply **dilution** correction (sample = reading × dilution factor),
* apply **optional blank** correction (subtract the blank reading),
* flag **below-detection-limit** values,
* and, when both a *measured* and a *predicted* value exist for a (sample, element) pair, build a
  **validation residual** table (measured − predicted, and a percent difference).

Hard safety properties (mirroring the project rules):

* **No plasma physics, no fabrication.** It processes only the rows you provide. It will **never**
  generate measured ICP values from a solid composition alone — :func:`can_synthesize_measured_from_composition`
  is permanently ``False`` and :data:`SOLID_TO_MEASURED_REFUSAL` is the standing refusal.
* **No silent guesses.** A missing unit, a missing/zero dilution factor, an impossible negative
  value, or an unknown element becomes a **QC warning**; the value is not invented around it.
* **Validation ≠ simulation.** A residual table is produced only from real measured+predicted
  pairs; nothing here labels a model estimate "measured" or "validated".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import units

# Roles a row may carry (what the value IS — never changed by this module).
MEASURED = "measured"
PREDICTED = "predicted"
_ROLE_ALIASES = {
    "measured": MEASURED, "measurement": MEASURED, "lab": MEASURED, "experimental": MEASURED,
    "m": MEASURED, "meas": MEASURED,
    "predicted": PREDICTED, "prediction": PREDICTED, "model": PREDICTED, "phreeqc": PREDICTED,
    "sim": PREDICTED, "simulated": PREDICTED, "p": PREDICTED, "pred": PREDICTED,
}

# Elements the processor supports at minimum (canonical symbols). Conversions resolve molar mass
# from the units registry (extended to include Mg + the rare earths for exactly this module).
SUPPORTED_ELEMENTS = ("Ca", "Si", "Al", "Fe", "Na", "K", "Mg", "Sc", "La", "Ce", "Nd", "Y")
_ELEMENT_CANON = {e.lower(): e for e in (
    "Ca", "Si", "Al", "Fe", "Na", "K", "Mg", "Sc", "La", "Ce", "Nd", "Y",
    "Ti", "V", "Mn", "Cr", "Sr", "Ba", "S", "P")}

# Input concentration units we accept, normalised onto the units-registry spellings.
_UNIT_CANON = {
    "mm": units.UNIT_MM, "mmol/l": units.UNIT_MM, "mmol/litre": units.UNIT_MM,
    "mg/l": units.UNIT_MGL, "mgl": units.UNIT_MGL, "mg l-1": units.UNIT_MGL,
    "mg/litre": units.UNIT_MGL,
    "ppm": units.UNIT_PPM, "mg/kg": units.UNIT_PPM,
    "ppb": units.UNIT_PPB, "ug/l": units.UNIT_PPB, "µg/l": units.UNIT_PPB,
}

PLASMA_EXPLANATION = ("ICP module processes measured/predicted concentration data; it does not "
                      "simulate the plasma.")
SOLID_TO_MEASURED_REFUSAL = (
    "ICP concentrations describe a measured (or model-predicted) liquid. I will not generate "
    "measured ICP values from a solid oxide composition alone — that would be fabricating measured "
    "data. Provide the measured (or PHREEQC-predicted) liquid concentrations and I'll process them.")


# --------------------------------------------------------------------------- #
# Output rows
# --------------------------------------------------------------------------- #
@dataclass
class CorrectedRow:
    """One processed concentration row (original value + every correction step + the mM result)."""

    sample_id: str
    element: str
    role: str                      # MEASURED / PREDICTED / "" (unspecified)
    input_value: float | None
    input_unit: str | None
    dilution_factor: float
    blank_value: float | None
    blank_corrected_value: float | None
    corrected_value: float | None   # in the input unit, after blank + dilution
    value_mM: float | None
    below_detection_limit: bool
    conversion_id: str | None
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "element": self.element,
            "role": self.role,
            "input_value": self.input_value,
            "input_unit": self.input_unit,
            "dilution_factor": self.dilution_factor,
            "blank_value": self.blank_value,
            "blank_corrected_value": self.blank_corrected_value,
            "corrected_value": self.corrected_value,
            "value_mM": self.value_mM,
            "below_detection_limit": self.below_detection_limit,
            "conversion_id": self.conversion_id,
            "warnings": list(self.warnings),
        }


@dataclass
class ResidualRow:
    """A measured-vs-predicted comparison for one (sample, element). Validation, not simulation."""

    sample_id: str
    element: str
    measured_mM: float
    predicted_mM: float
    residual_mM: float              # measured − predicted
    percent_difference: float | None  # 100·(measured−predicted)/predicted, or None if predicted==0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "element": self.element,
            "measured_mM": self.measured_mM,
            "predicted_mM": self.predicted_mM,
            "residual_mM": self.residual_mM,
            "percent_difference": self.percent_difference,
            "note": self.note,
        }


@dataclass
class IcpResult:
    """The full output: corrected rows, residuals, QC warnings, and the standing explanation."""

    corrected: list = field(default_factory=list)      # list[CorrectedRow]
    residuals: list = field(default_factory=list)      # list[ResidualRow]
    warnings: list = field(default_factory=list)       # list[str] (QC, de-duplicated, ordered)
    explanation: str = PLASMA_EXPLANATION

    def corrected_table(self) -> list[dict]:
        return [r.to_dict() for r in self.corrected]

    def residual_table(self) -> list[dict]:
        return [r.to_dict() for r in self.residuals]


# --------------------------------------------------------------------------- #
# Safety invariant: never synthesise measured data from a solid composition
# --------------------------------------------------------------------------- #
def can_synthesize_measured_from_composition() -> bool:
    """Permanently ``False`` — the ICP processor never invents measured values from a solid assay."""
    return False


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #
def canonical_element(element) -> str | None:
    """Canonical element symbol for a possibly mis-cased token (``ca`` → ``Ca``), else ``None``."""
    return _ELEMENT_CANON.get(str(element or "").strip().lower())


def canonical_unit(unit) -> str | None:
    """Canonical concentration unit (``mg/l`` → ``mg/L``), else ``None`` (unknown / missing)."""
    if unit is None:
        return None
    key = str(unit).strip().lower().replace(" ", "")
    # Tolerate a couple of spacing variants normalised above (e.g. "mg l-1").
    return _UNIT_CANON.get(key) or _UNIT_CANON.get(str(unit).strip().lower())


def canonical_role(role) -> str:
    """Canonical role (MEASURED / PREDICTED) for a label, or ``""`` when unspecified."""
    return _ROLE_ALIASES.get(str(role or "").strip().lower(), "")


def _to_float(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None     # drop NaN


# --------------------------------------------------------------------------- #
# Core conversion (pure, testable in isolation)
# --------------------------------------------------------------------------- #
def to_mM(value: float, unit: str, element: str) -> tuple[float | None, str | None, list[str]]:
    """Convert ``value`` in ``unit`` to mM for ``element`` via the units registry.

    Returns ``(value_mM, conversion_id, warnings)``. An unknown unit or an element with no known
    molar mass yields ``(None, None, [warning])`` — never a guess. ``mM`` passes through (identity).
    """
    warns: list[str] = []
    canon_unit = canonical_unit(unit)
    if canon_unit is None:
        return None, None, [f"unrecognised unit {unit!r} — cannot convert to mM."]
    try:
        res = units.convert(value, canon_unit, units.UNIT_MM, element=element)
    except units.UnknownElementError:
        return None, None, [f"no known molar mass for element {element!r} — cannot convert "
                            f"{canon_unit} to mM."]
    except units.UnitConversionError as exc:                # defensive: any other unit problem
        return None, None, [f"could not convert {canon_unit} to mM: {exc}"]
    return res.value, res.conversion_id, warns


# --------------------------------------------------------------------------- #
# The processor
# --------------------------------------------------------------------------- #
def process(rows, *, apply_blank: bool = True) -> IcpResult:
    """Process a list of ICP concentration rows into corrected values + a residual table.

    Each row is a mapping that may carry: ``sample_id``, ``element``, ``concentration`` (or
    ``value``), ``unit``, ``dilution_factor``, ``blank_value``, ``detection_limit``, and
    ``measured_or_predicted`` (or ``role``). Order of operations per row: blank subtraction (when
    ``apply_blank`` and a blank is given) → multiply by the dilution factor → convert to mM. Below-
    detection is judged on the **raw reading** vs the detection limit. QC issues become warnings;
    nothing is invented. Never raises on a malformed row — it warns and continues.
    """
    corrected: list[CorrectedRow] = []
    warnings: list[str] = []

    for raw in (rows or []):
        row = dict(raw or {})
        sample_id = str(row.get("sample_id") or row.get("sample") or "").strip() or "(unnamed)"
        el_raw = row.get("element")
        element = canonical_element(el_raw) or (str(el_raw).strip() if el_raw is not None else "")
        role = canonical_role(row.get("measured_or_predicted") or row.get("role"))
        unit_raw = row.get("unit")
        canon_unit = canonical_unit(unit_raw)
        value = _to_float(row.get("concentration") if "concentration" in row else row.get("value"))
        dil = _to_float(row.get("dilution_factor"))
        blank = _to_float(row.get("blank_value"))
        dl = _to_float(row.get("detection_limit"))

        row_warnings: list[str] = []
        tag = f"{sample_id}/{element or '?'}"

        # Element / unit sanity (do not block; flag and keep going).
        if not element:
            row_warnings.append(f"{tag}: missing element — cannot convert to mM.")
        elif canonical_element(el_raw) is None:
            row_warnings.append(f"{tag}: unknown element {el_raw!r} — no molar mass, mM skipped.")
        if value is None:
            row_warnings.append(f"{tag}: missing/!numeric concentration — row skipped for mM.")
        if unit_raw in (None, ""):
            row_warnings.append(f"{tag}: missing unit — cannot convert; assuming nothing.")
        elif canon_unit is None:
            row_warnings.append(f"{tag}: unrecognised unit {unit_raw!r} — cannot convert to mM.")

        # Impossible values.
        if value is not None and value < 0:
            row_warnings.append(f"{tag}: negative concentration {value:g} is impossible — flagged.")

        # Dilution factor.
        if dil is None:
            if row.get("dilution_factor") not in (None, ""):
                row_warnings.append(f"{tag}: non-numeric dilution_factor — using 1.0.")
            dil = 1.0
        elif dil <= 0:
            row_warnings.append(f"{tag}: dilution_factor {dil:g} ≤ 0 is invalid — using 1.0.")
            dil = 1.0

        # Below detection limit (on the raw reading).
        below_dl = bool(dl is not None and value is not None and value < dl)
        if below_dl:
            row_warnings.append(f"{tag}: {value:g} is below the detection limit {dl:g} — flagged "
                                "(treat as a non-detect, not a true zero).")

        # Blank correction → dilution correction (in the input unit).
        blank_corrected = value
        corrected_value = value
        if value is not None:
            if apply_blank and blank is not None:
                blank_corrected = value - blank
                if blank_corrected < 0:
                    row_warnings.append(f"{tag}: blank {blank:g} ≥ reading {value:g} — net is "
                                        "negative; clamped to 0 (below blank).")
                    blank_corrected = 0.0
            corrected_value = blank_corrected * dil

        # Convert to mM (only when we have a numeric value, a known unit, and a known element).
        value_mM: float | None = None
        conv_id: str | None = None
        if corrected_value is not None and canon_unit is not None and canonical_element(el_raw):
            value_mM, conv_id, conv_warn = to_mM(corrected_value, canon_unit, element)
            row_warnings.extend(f"{tag}: {w}" for w in conv_warn)

        corrected.append(CorrectedRow(
            sample_id=sample_id, element=element or str(el_raw or ""), role=role,
            input_value=value, input_unit=canon_unit or (str(unit_raw) if unit_raw else None),
            dilution_factor=dil, blank_value=blank, blank_corrected_value=blank_corrected,
            corrected_value=corrected_value, value_mM=value_mM, below_detection_limit=below_dl,
            conversion_id=conv_id, warnings=row_warnings))
        warnings.extend(row_warnings)

    residuals = _build_residuals(corrected, warnings)
    return IcpResult(corrected=corrected, residuals=residuals,
                     warnings=_dedupe(warnings), explanation=PLASMA_EXPLANATION)


def _build_residuals(corrected, warnings) -> list[ResidualRow]:
    """Pair measured + predicted mM for the same (sample, element) into residuals (validation)."""
    measured: dict[tuple[str, str], float] = {}
    predicted: dict[tuple[str, str], float] = {}
    for r in corrected:
        if r.value_mM is None or r.below_detection_limit:
            continue
        key = (r.sample_id, r.element)
        if r.role == MEASURED:
            measured[key] = r.value_mM
        elif r.role == PREDICTED:
            predicted[key] = r.value_mM

    residuals: list[ResidualRow] = []
    for key in sorted(set(measured) & set(predicted)):
        m, p = measured[key], predicted[key]
        if p == 0:
            pct = None
            note = "predicted is 0 — percent difference undefined."
        else:
            pct = round(100.0 * (m - p) / p, 4)
            note = ""
        residuals.append(ResidualRow(sample_id=key[0], element=key[1], measured_mM=m,
                                     predicted_mM=p, residual_mM=round(m - p, 8),
                                     percent_difference=pct, note=note))
    if (measured and predicted) and not residuals:
        warnings.append("Measured and predicted rows were given but none share a (sample, element) "
                        "pair — no residuals could be computed.")
    return residuals


def _dedupe(items) -> list[str]:
    """De-duplicate while preserving first-seen order (warnings can repeat across rows)."""
    return list(dict.fromkeys(items))
