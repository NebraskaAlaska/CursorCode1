"""Authoritative ICP concentration reduction and quality-control contract.

This module reduces concentration rows supplied by a user or another deterministic
model.  It does not simulate an ICP plasma and never fabricates a measurement.  One
machine-readable QC contract is used by the Digital Lab, the Virtual LAB runner, and
the older wide measured-release comparison path.

For raw readings the scientific order is:

``supplied reading -> optional blank subtraction -> dilution multiplication -> mM``.

Missing dilution is deliberately *not* interpreted as 1.0.  An undiluted sample must
carry an explicit factor of ``1.0``.  Below-detection and below-blank observations
remain visible as censored evidence; no zero, DL/2, or other substitution is made.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from .. import units

# Roles describe what a value is.  They are never silently relabelled.
MEASURED = "measured"
PREDICTED = "predicted"
_ROLE_ALIASES = {
    "measured": MEASURED, "measurement": MEASURED, "lab": MEASURED,
    "experimental": MEASURED, "m": MEASURED, "meas": MEASURED,
    "predicted": PREDICTED, "prediction": PREDICTED, "model": PREDICTED,
    "phreeqc": PREDICTED, "sim": PREDICTED, "simulated": PREDICTED,
    "p": PREDICTED, "pred": PREDICTED,
}

SUPPORTED_ELEMENTS = tuple(units.MOLAR_MASSES)
_ELEMENT_CANON = {e.lower(): e for e in units.MOLAR_MASSES}

_UNIT_CANON = {
    "mm": units.UNIT_MM, "mmol/l": units.UNIT_MM, "mmol/litre": units.UNIT_MM,
    "mg/l": units.UNIT_MGL, "mgl": units.UNIT_MGL, "mgl-1": units.UNIT_MGL,
    "mg/litre": units.UNIT_MGL, "ppm": units.UNIT_PPM, "mg/kg": units.UNIT_PPM,
    "ppb": units.UNIT_PPB, "ug/l": units.UNIT_PPB, "µg/l": units.UNIT_PPB,
}

# Authoritative row states.  Status priority is excluded > censored > review > usable.
QC_CONTRACT_VERSION = "phase1b.icp_qc.v1"
QC_USABLE = "usable"
QC_CENSORED = "censored"
QC_REVIEW_REQUIRED = "review_required"
QC_EXCLUDED = "excluded"
QC_STATUSES = (QC_USABLE, QC_CENSORED, QC_REVIEW_REQUIRED, QC_EXCLUDED)

# Machine-readable issue codes.  Consumers use status/eligibility; codes explain why.
QC_MISSING_CONCENTRATION = "missing_concentration"
QC_NONNUMERIC_CONCENTRATION = "nonnumeric_concentration"
QC_NONFINITE_CONCENTRATION = "nonfinite_concentration"
QC_NEGATIVE_CONCENTRATION = "negative_concentration"
QC_MISSING_UNIT = "missing_unit"
QC_UNKNOWN_UNIT = "unknown_unit"
QC_MISSING_ELEMENT = "missing_element"
QC_UNKNOWN_ELEMENT = "unknown_element"
QC_MISSING_SAMPLE_ID = "missing_sample_id"
QC_MISSING_DILUTION = "missing_dilution_factor"
QC_NONNUMERIC_DILUTION = "nonnumeric_dilution_factor"
QC_NONFINITE_DILUTION = "nonfinite_dilution_factor"
QC_NONPOSITIVE_DILUTION = "nonpositive_dilution_factor"
QC_INVALID_BLANK = "invalid_blank_value"
QC_NONFINITE_BLANK = "nonfinite_blank_value"
QC_NEGATIVE_BLANK = "negative_blank_value"
QC_INVALID_DETECTION_LIMIT = "invalid_detection_limit"
QC_NONFINITE_DETECTION_LIMIT = "nonfinite_detection_limit"
QC_NEGATIVE_DETECTION_LIMIT = "negative_detection_limit"
QC_BELOW_DETECTION = "below_detection_limit"
QC_BELOW_BLANK = "below_blank"
QC_AT_BLANK = "at_blank"
QC_MISSING_ROLE = "missing_role"
QC_UNKNOWN_ROLE = "unknown_role"
QC_DUPLICATE_MEASURED = "duplicate_measured"
QC_DUPLICATE_PREDICTED = "duplicate_predicted"
QC_DUPLICATE_NOT_SELECTED = "duplicate_not_selected"
QC_INVALID_RESOLUTION = "invalid_resolution"
QC_UPSTREAM_BLOCKED = "upstream_qc_blocked"
QC_STAGE_UNCONFIRMED = "final_correction_stage_unconfirmed"
QC_RESOLVED = "resolved_reviewable_issue"

# Serialized residual evidence states.  These are deliberately separate from the
# concentration-row QC states above: a historical residual can be scientifically
# unknown because its Phase 1B eligibility evidence was never serialized, without
# being labelled as a hard-invalid concentration.
SERIALIZED_RESIDUAL_ELIGIBLE = "explicitly_eligible"
SERIALIZED_RESIDUAL_INELIGIBLE = "explicitly_ineligible"
SERIALIZED_RESIDUAL_LEGACY_UNKNOWN = "legacy_unknown"
QC_SERIALIZED_ELIGIBILITY_FALSE = "serialized_residual_explicitly_ineligible"
QC_SERIALIZED_ELIGIBILITY_MISSING = "serialized_residual_qc_missing"
QC_SERIALIZED_ELIGIBILITY_MALFORMED = "serialized_residual_qc_malformed"
SERIALIZED_ICP_QC_REGENERATION_MESSAGE = (
    "This ICP comparison predates the current QC contract. Re-run the comparison "
    "to establish ICP validation eligibility."
)

_HARD_CODES = {
    QC_MISSING_CONCENTRATION, QC_NONNUMERIC_CONCENTRATION, QC_NONFINITE_CONCENTRATION,
    QC_NEGATIVE_CONCENTRATION, QC_INVALID_BLANK, QC_NONFINITE_BLANK, QC_NEGATIVE_BLANK,
    QC_INVALID_DETECTION_LIMIT, QC_NONFINITE_DETECTION_LIMIT, QC_NEGATIVE_DETECTION_LIMIT,
}
_CENSORED_CODES = {QC_BELOW_DETECTION, QC_BELOW_BLANK, QC_AT_BLANK}
_REVIEW_CODES = {
    QC_MISSING_UNIT, QC_UNKNOWN_UNIT, QC_MISSING_ELEMENT, QC_UNKNOWN_ELEMENT,
    QC_MISSING_SAMPLE_ID, QC_MISSING_DILUTION, QC_NONNUMERIC_DILUTION,
    QC_NONFINITE_DILUTION, QC_NONPOSITIVE_DILUTION, QC_MISSING_ROLE, QC_UNKNOWN_ROLE,
    QC_DUPLICATE_MEASURED, QC_DUPLICATE_PREDICTED, QC_DUPLICATE_NOT_SELECTED,
    QC_INVALID_RESOLUTION, QC_UPSTREAM_BLOCKED, QC_STAGE_UNCONFIRMED,
}

FINAL_CORRECTED_STAGE = "final_corrected_concentration"
UNKNOWN_STAGE = "unknown"
CONVERSION_AUTHORITY = "flyash_phreeqc_ml.units"

PLASMA_EXPLANATION = (
    "ICP module processes measured/predicted concentration data; it does not simulate the plasma."
)
SOLID_TO_MEASURED_REFUSAL = (
    "ICP concentrations describe a measured (or model-predicted) liquid. I will not generate "
    "measured ICP values from a solid oxide composition alone — that would be fabricating measured "
    "data. Provide the measured (or PHREEQC-predicted) liquid concentrations and I'll process them."
)


class QcResolutionError(ValueError):
    """An attempted QC resolution is not an allowed, provenance-carrying correction."""


class HardInvalidResolutionError(QcResolutionError):
    """A caller attempted to approve/replace a hard scientific exclusion."""


@dataclass
class QcDecision:
    """Small reusable eligibility decision for an already-final concentration."""

    qc_status: str
    qc_codes: list[str] = field(default_factory=list)
    qc_reasons: list[str] = field(default_factory=list)
    validation_eligible: bool = False
    numeric_value: float | None = None

    def to_dict(self) -> dict:
        return {
            "qc_status": self.qc_status,
            "qc_codes": list(self.qc_codes),
            "qc_reasons": list(self.qc_reasons),
            "validation_eligible": bool(self.validation_eligible),
            "numeric_value": self.numeric_value,
        }


@dataclass(frozen=True)
class SerializedResidualEligibility:
    """Trust decision for one already-serialized ICP residual.

    Only a recognizable, explicit true value is eligible.  Missing, blank, NaN,
    malformed, and otherwise unknown evidence is *legacy unknown*, not inferred
    from measured/predicted/residual numbers.
    """

    evidence_state: str
    validation_eligible: bool
    qc_code: str
    qc_reason: str
    eligibility_column: str
    supplied_value: Any = None

    @property
    def is_legacy_unknown(self) -> bool:
        return self.evidence_state == SERIALIZED_RESIDUAL_LEGACY_UNKNOWN

    def to_dict(self) -> dict:
        return {
            "evidence_state": self.evidence_state,
            "validation_eligible": bool(self.validation_eligible),
            "qc_code": self.qc_code,
            "qc_reason": self.qc_reason,
            "eligibility_column": self.eligibility_column,
            "supplied_value": self.supplied_value,
        }


@dataclass
class CorrectedRow:
    """One row with supplied evidence, each correction step, and authoritative QC."""

    row_id: str
    sample_id: str
    element: str
    role: str
    supplied_concentration: Any
    input_value: float | None
    supplied_unit: Any
    input_unit: str | None
    supplied_dilution_factor: Any
    dilution_factor: float | None
    supplied_blank_value: Any
    blank_value: float | None
    blank_correction_applied: bool
    blank_corrected_value: float | None
    corrected_value: float | None
    value_mM: float | None
    supplied_detection_limit: Any
    detection_limit: float | None
    below_detection_limit: bool
    below_blank: bool
    conversion_id: str | None
    conversion_authority: str | None
    qc_status: str
    qc_codes: list[str] = field(default_factory=list)
    qc_reasons: list[str] = field(default_factory=list)
    validation_eligible: bool = False
    resolutions: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "row_id": self.row_id,
            "sample_id": self.sample_id,
            "element": self.element,
            "role": self.role,
            "supplied_concentration": self.supplied_concentration,
            "input_value": self.input_value,
            "supplied_unit": self.supplied_unit,
            "input_unit": self.input_unit,
            "supplied_dilution_factor": self.supplied_dilution_factor,
            "dilution_factor": self.dilution_factor,
            "supplied_blank_value": self.supplied_blank_value,
            "blank_value": self.blank_value,
            "blank_correction_applied": self.blank_correction_applied,
            "blank_corrected_value": self.blank_corrected_value,
            "corrected_value": self.corrected_value,
            "value_mM": self.value_mM,
            "supplied_detection_limit": self.supplied_detection_limit,
            "detection_limit": self.detection_limit,
            "below_detection_limit": self.below_detection_limit,
            "below_blank": self.below_blank,
            "conversion_id": self.conversion_id,
            "conversion_authority": self.conversion_authority,
            "qc_status": self.qc_status,
            "qc_codes": list(self.qc_codes),
            "qc_reasons": list(self.qc_reasons),
            "validation_eligible": bool(self.validation_eligible),
            "resolutions": deepcopy(self.resolutions),
            "warnings": list(self.warnings),
        }


@dataclass
class ResidualRow:
    """An eligible measured-minus-predicted comparison for one sample/element."""

    sample_id: str
    element: str
    measured_mM: float
    predicted_mM: float
    residual_mM: float
    percent_difference: float | None
    measured_row_id: str
    predicted_row_id: str
    validation_eligible: bool = True
    resolution_provenance: list[dict] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "element": self.element,
            "measured_mM": self.measured_mM,
            "predicted_mM": self.predicted_mM,
            "residual_mM": self.residual_mM,
            "percent_difference": self.percent_difference,
            "measured_row_id": self.measured_row_id,
            "predicted_row_id": self.predicted_row_id,
            "validation_eligible": self.validation_eligible,
            "resolution_provenance": deepcopy(self.resolution_provenance),
            "note": self.note,
        }


@dataclass
class IcpResult:
    corrected: list[CorrectedRow] = field(default_factory=list)
    residuals: list[ResidualRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    resolution_provenance: list[dict] = field(default_factory=list)
    explanation: str = PLASMA_EXPLANATION

    def corrected_table(self) -> list[dict]:
        return [r.to_dict() for r in self.corrected]

    def corrected_export_table(self) -> list[dict]:
        """Flat CSV-safe records with list/dict provenance encoded as JSON."""
        rows = self.corrected_table()
        for row in rows:
            row["qc_codes"] = json.dumps(row["qc_codes"], ensure_ascii=False)
            row["qc_reasons"] = json.dumps(row["qc_reasons"], ensure_ascii=False)
            row["resolutions"] = json.dumps(
                row["resolutions"], ensure_ascii=False, sort_keys=True)
            row["warnings"] = json.dumps(row["warnings"], ensure_ascii=False)
        return rows

    def residual_table(self) -> list[dict]:
        return [r.to_dict() for r in self.residuals]

    def qc_summary(self) -> dict[str, int]:
        return {status: sum(r.qc_status == status for r in self.corrected) for status in QC_STATUSES}


def can_synthesize_measured_from_composition() -> bool:
    return False


def canonical_element(element) -> str | None:
    return _ELEMENT_CANON.get(str(element or "").strip().lower())


def canonical_unit(unit) -> str | None:
    if unit is None:
        return None
    raw = str(unit).strip().lower()
    return _UNIT_CANON.get(raw.replace(" ", "")) or _UNIT_CANON.get(raw)


def canonical_role(role) -> str:
    return _ROLE_ALIASES.get(str(role or "").strip().lower(), "")


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().lower() in {"nan", "none", "<na>"}
    try:
        unequal = value != value
        if bool(unequal):
            return True
    except (TypeError, ValueError, RuntimeError):
        pass
    try:
        if str(value).strip().lower() in {"nan", "none", "<na>"}:
            return True
    except Exception:  # pragma: no cover - exotic scalar guard
        pass
    return False


def _numeric(value: Any) -> tuple[float | None, str | None]:
    """Return ``(finite_float, error_kind)`` where kind is missing/non-numeric/non-finite."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "missing"
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None, "nonnumeric"
    if not math.isfinite(result):
        return None, "nonfinite"
    return result, None


def _evidence_value(value: Any) -> Any:
    """Serialization-safe preservation of supplied non-finite tokens."""
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    return value


def _to_float(value):
    """Back-compatible finite numeric helper."""
    return _numeric(value)[0]


def _status(codes: Iterable[str]) -> str:
    codes = set(codes)
    if codes & _HARD_CODES:
        return QC_EXCLUDED
    if codes & _CENSORED_CODES:
        return QC_CENSORED
    if codes & _REVIEW_CODES:
        return QC_REVIEW_REQUIRED
    return QC_USABLE


def _append_issue(codes: list[str], reasons: list[str], code: str, reason: str) -> None:
    if code not in codes:
        codes.append(code)
    if reason not in reasons:
        reasons.append(reason)


def to_mM(value: float, unit: str, element: str) -> tuple[float | None, str | None, list[str]]:
    canon_unit = canonical_unit(unit)
    if canon_unit is None:
        return None, None, [f"unrecognised unit {unit!r} — cannot convert to mM."]
    if not math.isfinite(float(value)):
        return None, None, ["non-finite concentration cannot be converted to mM."]
    try:
        result = units.convert(value, canon_unit, units.UNIT_MM, element=element)
    except units.UnknownElementError:
        return None, None, [f"no known molar mass for element {element!r} — cannot convert to mM."]
    except units.UnitConversionError as exc:
        return None, None, [f"could not convert {canon_unit} to mM: {exc}"]
    if not math.isfinite(float(result.value)):
        return None, None, ["unit conversion produced a non-finite concentration."]
    return float(result.value), result.conversion_id, []


_RESOLUTION_FIELDS = {"unit", "dilution_factor", "role", "measured_or_predicted", "element", "sample_id"}
_FIELD_ALIASES = {"measured_or_predicted": "role"}


def _resolution_source_value(row: Mapping[str, Any], field: str) -> Any:
    """Return the authoritative, uncorrected source value for one metadata field."""
    if field == "role":
        key = "measured_or_predicted" if "measured_or_predicted" in row else "role"
        return row.get(key)
    if field == "sample_id":
        # Match the processor's long-standing sample-id alias semantics: a non-blank
        # ``sample`` is authoritative when ``sample_id`` is absent or blank.
        return row.get("sample_id") or row.get("sample")
    return row.get(field)


def _resolution_issue(row: Mapping[str, Any], field: str) -> str | None:
    """Identify a genuinely review-required issue in authoritative source metadata."""
    value = _resolution_source_value(row, field)
    if field == "unit":
        if _blank(value):
            return QC_MISSING_UNIT
        if canonical_unit(value) is None:
            return QC_UNKNOWN_UNIT
    elif field == "dilution_factor":
        number, error = _numeric(value)
        if error == "missing":
            return QC_MISSING_DILUTION
        if error == "nonnumeric":
            return QC_NONNUMERIC_DILUTION
        if error == "nonfinite":
            return QC_NONFINITE_DILUTION
        if number is not None and number <= 0:
            return QC_NONPOSITIVE_DILUTION
    elif field == "role":
        if _blank(value):
            return QC_MISSING_ROLE
        if not canonical_role(value):
            return QC_UNKNOWN_ROLE
    elif field == "element":
        if _blank(value):
            return QC_MISSING_ELEMENT
        if canonical_element(value) is None:
            return QC_UNKNOWN_ELEMENT
    elif field == "sample_id" and _blank(value):
        return QC_MISSING_SAMPLE_ID
    return None


def _validated_resolution_replacement(field: str, value: Any) -> Any:
    """Return a canonical replacement that actually clears the field's QC issue."""
    if _blank(value):
        raise QcResolutionError("replacement_value must be explicit and non-blank")
    if field == "unit":
        replacement = canonical_unit(value)
        if replacement is None:
            raise QcResolutionError(f"replacement unit {value!r} is unknown")
        return replacement
    if field == "dilution_factor":
        replacement, error = _numeric(value)
        if error is not None or replacement is None or replacement <= 0:
            raise QcResolutionError(
                "replacement dilution_factor must be a finite number greater than zero")
        return replacement
    if field == "role":
        replacement = canonical_role(value)
        if not replacement:
            raise QcResolutionError(f"replacement role {value!r} is unknown")
        return replacement
    if field == "element":
        replacement = canonical_element(value)
        if replacement is None:
            raise QcResolutionError(f"replacement element {value!r} is unknown")
        return replacement
    replacement = str(value).strip()
    if not replacement:
        raise QcResolutionError("replacement sample_id must be explicit and non-blank")
    return replacement


def _require_identity_continuity(row: Mapping[str, Any], field: str, replacement: Any) -> None:
    """Do not let a correction contradict a recognized identity in a source alias."""
    if field != "role":
        return
    recognized = {
        canonical_role(row.get(key))
        for key in ("measured_or_predicted", "role")
        if key in row and canonical_role(row.get(key))
    }
    if len(recognized) > 1:
        raise QcResolutionError(
            "authoritative source row contains conflicting recognized role aliases")
    if recognized and replacement not in recognized:
        raise QcResolutionError(
            "role correction cannot relabel a recognized predicted/measured source identity")


def reviewable_metadata_fields(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Fields with correctable missing/invalid metadata in the authoritative source row."""
    return tuple(field for field in ("unit", "dilution_factor", "role", "element", "sample_id")
                 if _resolution_issue(row, field) is not None)


def resolve_reviewable_issue(
    row: Mapping[str, Any], *, field: str, replacement_value: Any,
    resolved_by: str, reason: str, resolved_at: str | None = None,
) -> dict:
    """Return a copy carrying one explicit, provenance-preserving metadata correction.

    Concentration, blank, and detection-limit replacements are deliberately forbidden:
    a hard-invalid observation cannot be turned valid by an "approve anyway" action.
    """
    field = _FIELD_ALIASES.get(str(field), str(field))
    if field not in {_FIELD_ALIASES.get(f, f) for f in _RESOLUTION_FIELDS}:
        raise HardInvalidResolutionError(
            f"{field!r} is not reviewable; hard scientific values cannot be approved/replaced"
        )
    if not str(resolved_by or "").strip() or not str(reason or "").strip():
        raise QcResolutionError("resolved_by and reason are required")
    out = deepcopy(dict(row or {}))
    issue = _resolution_issue(out, field)
    if issue is None:
        raise QcResolutionError(
            f"{field!r} is already valid in the authoritative source row and cannot be changed")
    replacement = _validated_resolution_replacement(field, replacement_value)
    _require_identity_continuity(out, field, replacement)
    existing = out.get("qc_resolutions") or []
    if isinstance(existing, Mapping):
        existing = [dict(existing)]
    if not isinstance(existing, (list, tuple)):
        raise QcResolutionError("existing qc_resolutions must be a list of mappings")
    for raw in existing:
        prior_field = _FIELD_ALIASES.get(
            str((raw or {}).get("field") or ""), str((raw or {}).get("field") or ""),
        ) if isinstance(raw, Mapping) else ""
        if prior_field == field:
            raise QcResolutionError(
                f"{field!r} already has a correction; repeated replacement is not allowed")
    entry = {
        "field": field,
        "original_value": _evidence_value(_resolution_source_value(out, field)),
        "replacement_value": _evidence_value(replacement),
        "resolved_by": str(resolved_by).strip(),
        "reason": str(reason).strip(),
        "resolved_at": resolved_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    out["qc_resolutions"] = [*list(existing), entry]
    return out


def _resolved_row(row: dict) -> tuple[dict, list[dict], list[str]]:
    """Apply valid reviewable corrections while retaining their original provenance."""
    effective = dict(row)
    provenance: list[dict] = []
    errors: list[str] = []
    entries = row.get("qc_resolutions") or []
    if isinstance(entries, Mapping):
        entries = [entries]
    seen_fields: set[str] = set()
    for raw in entries if isinstance(entries, (list, tuple)) else []:
        entry = dict(raw or {})
        field = _FIELD_ALIASES.get(str(entry.get("field") or ""), str(entry.get("field") or ""))
        if field not in {_FIELD_ALIASES.get(f, f) for f in _RESOLUTION_FIELDS}:
            errors.append(f"invalid resolution field {field!r}; hard values cannot be approved")
            continue
        if field in seen_fields:
            errors.append(f"multiple resolutions target {field!r}; repeated replacement is not allowed")
            continue
        seen_fields.add(field)
        if _blank(entry.get("replacement_value")) or not str(entry.get("resolved_by") or "").strip() \
                or not str(entry.get("reason") or "").strip():
            errors.append(f"resolution for {field!r} lacks replacement/resolved_by/reason")
            continue
        issue = _resolution_issue(row, field)
        if issue is None:
            errors.append(
                f"resolution for {field!r} attempts to change already-valid authoritative metadata")
            continue
        expected_original = _evidence_value(_resolution_source_value(row, field))
        if entry.get("original_value") != expected_original:
            errors.append(f"resolution for {field!r} does not preserve its authoritative original value")
            continue
        try:
            replacement = _validated_resolution_replacement(field, entry.get("replacement_value"))
            _require_identity_continuity(row, field, replacement)
        except QcResolutionError as exc:
            errors.append(str(exc))
            continue
        entry["field"] = field
        entry["original_value"] = expected_original
        entry["replacement_value"] = _evidence_value(replacement)
        target = "measured_or_predicted" if field == "role" else field
        effective[target] = replacement
        provenance.append(entry)
    return effective, provenance, errors


def _row_identifier(row: Mapping[str, Any], occurrence: int) -> str:
    supplied = str(row.get("row_id") or "").strip()
    if supplied:
        return supplied
    safe = {k: v for k, v in row.items() if k != "qc_resolutions"}
    blob = json.dumps(safe, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    return f"row-{digest}-{occurrence}"


def process(rows, *, apply_blank: bool = True, duplicate_resolutions=None) -> IcpResult:
    """Reduce raw long-form ICP rows and build only QC-eligible residuals."""
    corrected: list[CorrectedRow] = []
    warnings: list[str] = []
    occurrences: dict[str, int] = {}

    for supplied in (rows or []):
        original = dict(supplied or {})
        fingerprint = json.dumps({k: v for k, v in original.items() if k != "qc_resolutions"},
                                 sort_keys=True, default=str)
        occurrences[fingerprint] = occurrences.get(fingerprint, 0) + 1
        row_id = _row_identifier(original, occurrences[fingerprint])
        row, resolutions, resolution_errors = _resolved_row(original)

        supplied_sample = original.get("sample_id", original.get("sample"))
        sample_id = str(row.get("sample_id") or row.get("sample") or "").strip()
        supplied_element = original.get("element")
        effective_element = row.get("element")
        element = canonical_element(effective_element) or str(effective_element or "").strip()
        supplied_role = original.get("measured_or_predicted", original.get("role"))
        effective_role = row.get("measured_or_predicted", row.get("role"))
        role = canonical_role(effective_role)
        supplied_unit = original.get("unit")
        effective_unit = row.get("unit")
        canon_unit = canonical_unit(effective_unit)
        supplied_value = original.get("concentration") if "concentration" in original else original.get("value")
        effective_value = row.get("concentration") if "concentration" in row else row.get("value")
        supplied_dilution = original.get("dilution_factor")
        effective_dilution = row.get("dilution_factor")
        supplied_blank = original.get("blank_value")
        supplied_dl = original.get("detection_limit")

        value, value_error = _numeric(effective_value)
        dilution, dilution_error = _numeric(effective_dilution)
        blank, blank_error = _numeric(row.get("blank_value"))
        dl, dl_error = _numeric(row.get("detection_limit"))
        codes: list[str] = []
        reasons: list[str] = []
        tag = f"{sample_id or '(unnamed)'}/{element or '?'}"

        if not sample_id:
            _append_issue(codes, reasons, QC_MISSING_SAMPLE_ID,
                          f"{tag}: missing sample_id; comparison mapping requires an explicit sample.")
        if not str(effective_element or "").strip():
            _append_issue(codes, reasons, QC_MISSING_ELEMENT,
                          f"{tag}: missing element; unit conversion is unavailable.")
        elif canonical_element(effective_element) is None:
            _append_issue(codes, reasons, QC_UNKNOWN_ELEMENT,
                          f"{tag}: unknown element {effective_element!r}; molar mass is unavailable.")

        if value_error == "missing":
            _append_issue(codes, reasons, QC_MISSING_CONCENTRATION,
                          f"{tag}: concentration is missing; row is scientifically excluded.")
        elif value_error == "nonnumeric":
            _append_issue(codes, reasons, QC_NONNUMERIC_CONCENTRATION,
                          f"{tag}: concentration {effective_value!r} is non-numeric; row is excluded.")
        elif value_error == "nonfinite":
            _append_issue(codes, reasons, QC_NONFINITE_CONCENTRATION,
                          f"{tag}: concentration {effective_value!r} is NaN/infinite; row is excluded.")
        elif value is not None and value < 0:
            _append_issue(codes, reasons, QC_NEGATIVE_CONCENTRATION,
                          f"{tag}: negative concentration {value:g} is physically impossible; row is excluded.")

        if _blank(effective_unit):
            _append_issue(codes, reasons, QC_MISSING_UNIT,
                          f"{tag}: concentration unit is missing and must be explicitly resolved.")
        elif canon_unit is None:
            _append_issue(codes, reasons, QC_UNKNOWN_UNIT,
                          f"{tag}: unit {effective_unit!r} is unknown and must be explicitly resolved.")

        if dilution_error == "missing":
            _append_issue(codes, reasons, QC_MISSING_DILUTION,
                          f"{tag}: dilution_factor is missing; explicit 1.0 is required for undiluted data.")
        elif dilution_error == "nonnumeric":
            _append_issue(codes, reasons, QC_NONNUMERIC_DILUTION,
                          f"{tag}: dilution_factor {effective_dilution!r} is non-numeric; no 1.0 fallback applied.")
        elif dilution_error == "nonfinite":
            _append_issue(codes, reasons, QC_NONFINITE_DILUTION,
                          f"{tag}: dilution_factor {effective_dilution!r} is NaN/infinite; row requires correction.")
        elif dilution is not None and dilution <= 0:
            _append_issue(codes, reasons, QC_NONPOSITIVE_DILUTION,
                          f"{tag}: dilution_factor {dilution:g} must be greater than zero; no fallback applied.")

        if row.get("blank_value") is not None and not (
                isinstance(row.get("blank_value"), str) and not row.get("blank_value").strip()):
            if blank_error == "nonnumeric":
                _append_issue(codes, reasons, QC_INVALID_BLANK,
                              f"{tag}: blank_value {row.get('blank_value')!r} is invalid; row is excluded.")
            elif blank_error == "nonfinite":
                _append_issue(codes, reasons, QC_NONFINITE_BLANK,
                              f"{tag}: blank_value is NaN/infinite; row is excluded.")
            elif blank is not None and blank < 0:
                _append_issue(codes, reasons, QC_NEGATIVE_BLANK,
                              f"{tag}: negative blank_value {blank:g} is physically invalid; row is excluded.")

        if row.get("detection_limit") is not None and not (
                isinstance(row.get("detection_limit"), str)
                and not row.get("detection_limit").strip()):
            if dl_error == "nonnumeric":
                _append_issue(codes, reasons, QC_INVALID_DETECTION_LIMIT,
                              f"{tag}: detection_limit {row.get('detection_limit')!r} is invalid; row is excluded.")
            elif dl_error == "nonfinite":
                _append_issue(codes, reasons, QC_NONFINITE_DETECTION_LIMIT,
                              f"{tag}: detection_limit is NaN/infinite; row is excluded.")
            elif dl is not None and dl < 0:
                _append_issue(codes, reasons, QC_NEGATIVE_DETECTION_LIMIT,
                              f"{tag}: negative detection_limit {dl:g} is physically invalid; row is excluded.")

        if _blank(effective_role):
            _append_issue(codes, reasons, QC_MISSING_ROLE,
                          f"{tag}: measured/predicted role is missing; validation pairing is blocked.")
        elif not role:
            _append_issue(codes, reasons, QC_UNKNOWN_ROLE,
                          f"{tag}: role {effective_role!r} is unknown; validation pairing is blocked.")

        for err in resolution_errors:
            _append_issue(codes, reasons, QC_INVALID_RESOLUTION, f"{tag}: {err}.")
        if resolutions:
            codes.append(QC_RESOLVED)
            reasons.extend(
                f"{tag}: resolved {r['field']} from {r.get('original_value')!r} to "
                f"{r.get('replacement_value')!r} by {r.get('resolved_by')}: {r.get('reason')}"
                for r in resolutions
            )

        below_dl = bool(value is not None and dl is not None and dl >= 0 and value < dl)
        if below_dl:
            _append_issue(codes, reasons, QC_BELOW_DETECTION,
                          f"{tag}: supplied reading {value:g} is below detection limit {dl:g}; "
                          "retained as censored/non-detect without substitution.")

        blank_applied = bool(apply_blank and blank is not None and blank_error is None)
        blank_corrected: float | None = None
        corrected_value: float | None = None
        below_blank = False
        arithmetic_ready = (
            value is not None and value >= 0 and dilution is not None and dilution > 0
            and not (set(codes) & _HARD_CODES)
        )
        if arithmetic_ready:
            blank_corrected = value - blank if blank_applied else value
            if blank_applied and blank_corrected < 0:
                below_blank = True
                _append_issue(codes, reasons, QC_BELOW_BLANK,
                              f"{tag}: reading - blank = {blank_corrected:g}; retained below blank, not clamped to zero.")
            elif blank_applied and blank_corrected == 0:
                _append_issue(codes, reasons, QC_AT_BLANK,
                              f"{tag}: reading equals blank; zero net signal is censored/non-quantifiable.")
            corrected_value = blank_corrected * dilution
            if not math.isfinite(corrected_value):
                corrected_value = None
                _append_issue(codes, reasons, QC_NONFINITE_CONCENTRATION,
                              f"{tag}: correction arithmetic produced a non-finite value; row is excluded.")

        row_status = _status(codes)
        value_mM: float | None = None
        conversion_id: str | None = None
        if (row_status in (QC_USABLE, QC_REVIEW_REQUIRED) and corrected_value is not None
                and corrected_value >= 0 and canon_unit is not None
                and canonical_element(effective_element) is not None):
            value_mM, conversion_id, conversion_warnings = to_mM(corrected_value, canon_unit, element)
            for warning in conversion_warnings:
                _append_issue(codes, reasons, QC_UNKNOWN_UNIT, f"{tag}: {warning}")
            row_status = _status(codes)
        if row_status in (QC_EXCLUDED, QC_CENSORED):
            value_mM = None
            conversion_id = None

        eligible = bool(
            row_status == QC_USABLE and value_mM is not None and sample_id
            and role in (MEASURED, PREDICTED)
        )
        result_row = CorrectedRow(
            row_id=row_id, sample_id=sample_id, element=element,
            role=role, supplied_concentration=_evidence_value(supplied_value), input_value=value,
            supplied_unit=_evidence_value(supplied_unit), input_unit=canon_unit,
            supplied_dilution_factor=_evidence_value(supplied_dilution), dilution_factor=dilution,
            supplied_blank_value=_evidence_value(supplied_blank), blank_value=blank,
            blank_correction_applied=blank_applied, blank_corrected_value=blank_corrected,
            corrected_value=corrected_value, value_mM=value_mM,
            supplied_detection_limit=_evidence_value(supplied_dl), detection_limit=dl,
            below_detection_limit=below_dl, below_blank=below_blank,
            conversion_id=conversion_id,
            conversion_authority=CONVERSION_AUTHORITY if conversion_id else None,
            qc_status=row_status, qc_codes=codes, qc_reasons=reasons,
            validation_eligible=eligible, resolutions=resolutions, warnings=list(reasons),
        )
        corrected.append(result_row)
        warnings.extend(reasons)

    residuals, duplicate_provenance = _build_residuals(
        corrected, warnings, duplicate_resolutions=duplicate_resolutions or [])
    resolution_provenance = [r for row in corrected for r in row.resolutions] + duplicate_provenance
    return IcpResult(
        corrected=corrected, residuals=residuals, warnings=_dedupe(warnings),
        resolution_provenance=resolution_provenance,
    )


def _duplicate_resolution_map(resolutions) -> tuple[dict[tuple[str, str, str], dict], list[str]]:
    selected: dict[tuple[str, str, str], dict] = {}
    # Once more than one resolution targets a key it is permanently ambiguous for
    # this processing pass.  Without this separate set, a third resolution could
    # reinsert a key removed by the second one and silently restore last-entry
    # selection semantics.
    conflicted: set[tuple[str, str, str]] = set()
    errors: list[str] = []
    for raw in resolutions or []:
        r = dict(raw or {})
        role = canonical_role(r.get("role"))
        key = (str(r.get("sample_id") or "").strip(),
               canonical_element(r.get("element")) or str(r.get("element") or "").strip(), role)
        if not all(key) or not str(r.get("selected_row_id") or "").strip() \
                or not str(r.get("resolved_by") or "").strip() or not str(r.get("reason") or "").strip():
            errors.append(f"invalid duplicate resolution {r!r}")
            continue
        if key in conflicted:
            errors.append(
                f"multiple duplicate resolutions supplied for {key!r}; selection remains ambiguous")
            continue
        if key in selected:
            selected.pop(key, None)
            conflicted.add(key)
            errors.append(
                f"multiple duplicate resolutions supplied for {key!r}; selection remains ambiguous")
            continue
        selected[key] = r
    return selected, errors


def _mark_duplicate(row: CorrectedRow, code: str, reason: str, *, selected: bool = False) -> None:
    _append_issue(row.qc_codes, row.qc_reasons, code, reason)
    if selected:
        return
    if row.qc_status == QC_USABLE:
        row.qc_status = QC_REVIEW_REQUIRED
    row.validation_eligible = False
    row.warnings = list(row.qc_reasons)


def _build_residuals(corrected, warnings, *, duplicate_resolutions) -> tuple[list[ResidualRow], list[dict]]:
    """Pair rows without dictionary overwrite; ambiguous duplicates fail closed."""
    grouped: dict[tuple[str, str, str], list[CorrectedRow]] = {}
    for row in corrected:
        if row.role in (MEASURED, PREDICTED):
            grouped.setdefault((row.sample_id, row.element, row.role), []).append(row)

    selected_map, resolution_errors = _duplicate_resolution_map(duplicate_resolutions)
    warnings.extend(resolution_errors)
    chosen: dict[tuple[str, str, str], CorrectedRow] = {}
    resolution_provenance: list[dict] = []

    for key, candidates in grouped.items():
        if len(candidates) == 1:
            if candidates[0].validation_eligible:
                chosen[key] = candidates[0]
            continue
        code = QC_DUPLICATE_MEASURED if key[2] == MEASURED else QC_DUPLICATE_PREDICTED
        ids = [r.row_id for r in candidates]
        reason = (f"{key[0] or '(unnamed)'}/{key[1]}: {len(candidates)} {key[2]} rows "
                  f"({', '.join(ids)}); duplicate ambiguity blocks residual pairing.")
        resolution = selected_map.get(key)
        selected_id = str((resolution or {}).get("selected_row_id") or "")
        selected_rows = [r for r in candidates if r.row_id == selected_id]
        if resolution and len(selected_rows) == 1 and selected_rows[0].validation_eligible:
            selected_row = selected_rows[0]
            for row in candidates:
                if row is selected_row:
                    _mark_duplicate(row, QC_RESOLVED,
                                    f"{reason} Selected {selected_id} by explicit resolution.", selected=True)
                else:
                    _mark_duplicate(row, QC_DUPLICATE_NOT_SELECTED,
                                    f"{reason} Row not selected by explicit resolution.")
            chosen[key] = selected_row
            resolution_provenance.append(dict(resolution))
        else:
            if resolution:
                reason += " Supplied selection is missing, non-unique, or QC-ineligible."
            for row in candidates:
                _mark_duplicate(row, code, reason)
            warnings.append(reason)

    residuals: list[ResidualRow] = []
    measured_keys = {(s, e) for s, e, role in chosen if role == MEASURED}
    predicted_keys = {(s, e) for s, e, role in chosen if role == PREDICTED}
    for sample_id, element in sorted(measured_keys & predicted_keys):
        measured = chosen[(sample_id, element, MEASURED)]
        predicted = chosen[(sample_id, element, PREDICTED)]
        if not (measured.validation_eligible and predicted.validation_eligible):
            continue
        m, p = float(measured.value_mM), float(predicted.value_mM)
        if p == 0:
            pct = None
            note = "predicted is 0 — percent difference undefined."
        else:
            pct = round(100.0 * (m - p) / p, 4)
            note = ""
        pair_resolutions = [
            r for r in resolution_provenance
            if str(r.get("sample_id")) == sample_id and canonical_element(r.get("element")) == element
        ]
        residuals.append(ResidualRow(
            sample_id=sample_id, element=element, measured_mM=m, predicted_mM=p,
            residual_mM=round(m - p, 8), percent_difference=pct,
            measured_row_id=measured.row_id, predicted_row_id=predicted.row_id,
            resolution_provenance=pair_resolutions, note=note,
        ))
    if measured_keys and predicted_keys and not residuals:
        warnings.append(
            "Measured and predicted rows were supplied, but no unambiguous QC-eligible "
            "(sample_id, element) pair exists; no residual was computed."
        )
    return residuals, resolution_provenance


def _split_codes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v)]
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return []
    return [v for v in text.split("|") if v]


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n", "", "nan"}:
        return False
    return None


def parse_qc_bool(value: Any) -> bool | None:
    """Public parser for serialized QC booleans (CSV-safe true/false strings)."""
    return _as_bool(value)


def serialized_residual_eligibility(
    row: Mapping[str, Any], element: str,
) -> SerializedResidualEligibility:
    """Classify the QC evidence stored beside an existing ICP residual.

    This helper is for downstream consumers of an *already serialized* comparison.
    It intentionally does not reconstruct eligibility from numeric concentrations or
    a numeric residual.  A comparison builder processing source data from scratch is
    responsible for calculating and serializing the Phase 1B eligibility field.
    """
    canonical_el = canonical_element(element) or str(element or "").strip()
    column = f"residual_{canonical_el}_validation_eligible"
    if column not in row:
        return SerializedResidualEligibility(
            SERIALIZED_RESIDUAL_LEGACY_UNKNOWN, False,
            QC_SERIALIZED_ELIGIBILITY_MISSING,
            SERIALIZED_ICP_QC_REGENERATION_MESSAGE, column,
        )

    supplied = row.get(column)
    if _blank(supplied):
        return SerializedResidualEligibility(
            SERIALIZED_RESIDUAL_LEGACY_UNKNOWN, False,
            QC_SERIALIZED_ELIGIBILITY_MISSING,
            SERIALIZED_ICP_QC_REGENERATION_MESSAGE, column, supplied,
        )

    parsed = _as_bool(supplied)
    if parsed is True:
        return SerializedResidualEligibility(
            SERIALIZED_RESIDUAL_ELIGIBLE, True, "", "", column, supplied,
        )
    if parsed is False:
        reason_column = f"residual_{canonical_el}_qc_reasons"
        reason = row.get(reason_column) if reason_column in row else None
        if _blank(reason):
            reason = "Serialized Phase 1B ICP residual eligibility is explicitly false."
        return SerializedResidualEligibility(
            SERIALIZED_RESIDUAL_INELIGIBLE, False,
            QC_SERIALIZED_ELIGIBILITY_FALSE, str(reason), column, supplied,
        )
    return SerializedResidualEligibility(
        SERIALIZED_RESIDUAL_LEGACY_UNKNOWN, False,
        QC_SERIALIZED_ELIGIBILITY_MALFORMED,
        "ICP residual eligibility evidence is malformed or unrecognized. "
        "Re-run the comparison to establish ICP validation eligibility.",
        column, supplied,
    )


def serialized_residual_eligibility_decisions(frame, element: str):
    """Return one :class:`SerializedResidualEligibility` per comparison row."""
    import pandas as pd  # local: keep the long-row processor lightweight

    if frame is None:
        return pd.Series(dtype=object)
    if frame.empty:
        return pd.Series(index=frame.index, dtype=object)
    return frame.apply(
        lambda row: serialized_residual_eligibility(row, element), axis=1,
    )


def serialized_residual_eligibility_mask(frame, element: str):
    """Fail-closed boolean mask for consumers of serialized ICP residuals."""
    decisions = serialized_residual_eligibility_decisions(frame, element)
    return decisions.map(lambda decision: decision.validation_eligible).astype(bool)


def serialized_icp_residual_evidence_table(
    frame, *, elements: Iterable[str] = ("Ca", "Si", "Al", "Fe"),
):
    """Describe serialized QC evidence for visible numeric ICP residuals.

    Historical values remain in the returned comparison/export; this companion table
    labels whether each value carries explicit Phase 1B evidence.  It never converts a
    legacy numeric residual into trusted evidence.
    """
    import pandas as pd  # local: keep the long-row processor lightweight

    columns = [
        "row_index", "sample_id", "element", "residual_column", "residual_value",
        "evidence_state", "validation_eligible", "qc_code", "qc_reason",
        "eligibility_column", "supplied_eligibility",
    ]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    records: list[dict] = []
    for element in elements:
        canonical_el = canonical_element(element) or str(element)
        residual_column = f"residual_{canonical_el}"
        if residual_column not in frame.columns:
            continue
        residuals = pd.to_numeric(frame[residual_column], errors="coerce")
        finite = residuals.map(lambda value: pd.notna(value) and math.isfinite(float(value)))
        for idx, row in frame.loc[finite].iterrows():
            decision = serialized_residual_eligibility(row, canonical_el)
            records.append({
                "row_index": idx,
                "sample_id": row.get("sample_id", ""),
                "element": canonical_el,
                "residual_column": residual_column,
                "residual_value": float(_numeric(row.get(residual_column))[0]),
                "evidence_state": decision.evidence_state,
                "validation_eligible": decision.validation_eligible,
                "qc_code": decision.qc_code,
                "qc_reason": decision.qc_reason,
                "eligibility_column": decision.eligibility_column,
                "supplied_eligibility": decision.supplied_value,
            })
    return pd.DataFrame(records, columns=columns)


def assess_final_concentration(
    value: Any, *, role: Any, element: Any, unit: Any = units.UNIT_MM,
    upstream_status: Any = None, upstream_codes: Any = None,
    upstream_reasons: Any = None, upstream_eligible: Any = None,
    stage: Any = FINAL_CORRECTED_STAGE, stage_confirmed: Any = True,
    require_explicit_stage: bool = False,
) -> QcDecision:
    """Assess an already-corrected concentration through the same QC vocabulary.

    This adapter exists for the older wide `Ca_mM`/`Si_mM` schema.  It never claims
    a raw dilution of 1.0: the caller is declaring that the supplied value is already
    the final corrected concentration.  Active file workflows can require that stage
    declaration to be explicitly confirmed.
    """
    codes = _split_codes(upstream_codes)
    reasons = _split_codes(upstream_reasons)
    numeric, error = _numeric(value)
    canonical_el = canonical_element(element)
    canonical_u = canonical_unit(unit)
    canonical_r = canonical_role(role)

    if error == "missing":
        _append_issue(codes, reasons, QC_MISSING_CONCENTRATION, "final concentration is missing")
    elif error == "nonnumeric":
        _append_issue(codes, reasons, QC_NONNUMERIC_CONCENTRATION,
                      f"final concentration {value!r} is non-numeric")
    elif error == "nonfinite":
        _append_issue(codes, reasons, QC_NONFINITE_CONCENTRATION,
                      f"final concentration {value!r} is NaN/infinite")
    elif numeric is not None and numeric < 0:
        _append_issue(codes, reasons, QC_NEGATIVE_CONCENTRATION,
                      f"negative final concentration {numeric:g} is physically impossible")
    if not canonical_el:
        _append_issue(codes, reasons, QC_UNKNOWN_ELEMENT,
                      f"element {element!r} has no conversion authority")
    if not canonical_u:
        _append_issue(codes, reasons, QC_UNKNOWN_UNIT, f"unit {unit!r} is unavailable/unknown")
    if not canonical_r:
        code = QC_MISSING_ROLE if _blank(role) else QC_UNKNOWN_ROLE
        _append_issue(codes, reasons, code, f"role {role!r} is not explicit measured/predicted")
    if require_explicit_stage:
        confirmed = _as_bool(stage_confirmed)
        if str(stage or "").strip() != FINAL_CORRECTED_STAGE or confirmed is not True:
            _append_issue(
                codes, reasons, QC_STAGE_UNCONFIRMED,
                "wide ICP value is not explicitly confirmed as a final blank/dilution-corrected concentration",
            )
    upstream_bool = _as_bool(upstream_eligible)
    if upstream_bool is False or str(upstream_status or "").strip() in {
        QC_CENSORED, QC_REVIEW_REQUIRED, QC_EXCLUDED,
    }:
        _append_issue(codes, reasons, QC_UPSTREAM_BLOCKED,
                      "stored upstream ICP QC state is not validation-eligible")

    status = _status(codes)
    eligible = bool(status == QC_USABLE and numeric is not None)
    return QcDecision(status, _dedupe(codes), _dedupe(reasons), eligible, numeric)


def qc_columns_for(value_column: str) -> dict[str, str]:
    """Stable wide-schema provenance/QC column names for one concentration column."""
    return {
        "role": f"{value_column}_role",
        "input_stage": f"{value_column}_input_stage",
        "stage_confirmed": f"{value_column}_stage_confirmed",
        "supplied_dilution_factor": f"{value_column}_supplied_dilution_factor",
        "supplied_blank_value": f"{value_column}_supplied_blank_value",
        "blank_correction_applied": f"{value_column}_blank_correction_applied",
        "blank_corrected_value": f"{value_column}_blank_corrected_value",
        "corrected_value": f"{value_column}_corrected_value",
        "supplied_detection_limit": f"{value_column}_supplied_detection_limit",
        "conversion_authority": f"{value_column}_conversion_authority",
        "qc_status": f"{value_column}_qc_status",
        "qc_codes": f"{value_column}_qc_codes",
        "qc_reasons": f"{value_column}_qc_reasons",
        "validation_eligible": f"{value_column}_validation_eligible",
        "resolution_provenance": f"{value_column}_resolution_provenance",
    }


def decision_from_wide_row(
    row: Mapping[str, Any], value_column: str, *, role: str,
    element: str, require_explicit_stage: bool = False,
) -> QcDecision:
    cols = qc_columns_for(value_column)
    stored_role = row.get(cols["role"])
    global_role = row.get("icp_role")
    if not _blank(stored_role):
        effective_role = stored_role
    elif not _blank(global_role):
        effective_role = global_role
    else:
        # Compatibility callers can still identify the measured/predicted side by
        # context. Active file workflows require the serialized role explicitly.
        effective_role = None if require_explicit_stage else role
    stored_stage = row.get(cols["input_stage"])
    effective_stage = row.get("icp_input_stage", FINAL_CORRECTED_STAGE) \
        if _blank(stored_stage) else stored_stage
    stored_confirmed = row.get(cols["stage_confirmed"])
    effective_confirmed = row.get("icp_stage_confirmed", True) \
        if _blank(stored_confirmed) else stored_confirmed
    return assess_final_concentration(
        row.get(value_column), role=effective_role, element=element,
        unit=units.UNIT_MM, upstream_status=row.get(cols["qc_status"]),
        upstream_codes=row.get(cols["qc_codes"]), upstream_reasons=row.get(cols["qc_reasons"]),
        upstream_eligible=row.get(cols["validation_eligible"]),
        stage=effective_stage, stage_confirmed=effective_confirmed,
        require_explicit_stage=require_explicit_stage,
    )


def annotate_wide_final_concentrations(
    frame, *, role: str = MEASURED,
    elements: Iterable[str] = ("Ca", "Si", "Al", "Fe", "Na", "K"),
    require_explicit_stage: bool = True,
):
    """Return a DataFrame copy with serialized authoritative QC/provenance columns."""
    import pandas as pd  # local: keep the long-row processor lightweight

    out = frame.copy()
    value_columns = [f"{element}_mM" for element in elements
                     if f"{element}_mM" in out.columns]
    qc_names = list(dict.fromkeys(
        name for value_column in value_columns
        for name in qc_columns_for(value_column).values()))
    missing = [name for name in qc_names if name not in out.columns]
    if missing:
        out = pd.concat(
            [out, pd.DataFrame({name: [None] * len(out) for name in missing}, index=out.index)],
            axis=1,
        )
    for element in elements:
        value_column = f"{element}_mM"
        if value_column not in out.columns:
            continue
        cols = qc_columns_for(value_column)
        for name in cols.values():
            # CSV reload turns an all-empty provenance column into float NaN.  QC
            # annotation subsequently stores strings/bools there, so normalize the
            # storage dtype before scalar assignment (no value is changed).
            out[name] = out[name].astype(object)
        for idx, raw in out.iterrows():
            row = raw.to_dict()
            # Preserve any explicit upstream state while rechecking hard numeric validity.
            decision = decision_from_wide_row(
                row, value_column, role=role, element=element,
                require_explicit_stage=require_explicit_stage,
            )
            stored_stage = row.get(cols["input_stage"])
            stage = row.get("icp_input_stage", UNKNOWN_STAGE) if _blank(stored_stage) else stored_stage
            stored_confirmed = row.get(cols["stage_confirmed"])
            confirmed = row.get("icp_stage_confirmed", False) \
                if _blank(stored_confirmed) else stored_confirmed
            stored_role = row.get(cols["role"])
            global_role = row.get("icp_role")
            effective_role = (
                stored_role if not _blank(stored_role)
                else global_role if not _blank(global_role)
                else None if require_explicit_stage else role
            )
            out.at[idx, cols["role"]] = canonical_role(effective_role) or str(effective_role or "")
            stored_resolution = row.get(cols["resolution_provenance"])
            out.at[idx, cols["resolution_provenance"]] = (
                row.get("icp_resolution_provenance", "")
                if _blank(stored_resolution) else stored_resolution)
            out.at[idx, cols["input_stage"]] = stage
            out.at[idx, cols["stage_confirmed"]] = bool(_as_bool(confirmed))
            out.at[idx, cols["blank_correction_applied"]] = False
            out.at[idx, cols["corrected_value"]] = decision.numeric_value
            out.at[idx, cols["conversion_authority"]] = CONVERSION_AUTHORITY
            out.at[idx, cols["qc_status"]] = decision.qc_status
            out.at[idx, cols["qc_codes"]] = "|".join(decision.qc_codes)
            out.at[idx, cols["qc_reasons"]] = "|".join(decision.qc_reasons)
            out.at[idx, cols["validation_eligible"]] = bool(decision.validation_eligible)
    return out


def wide_qc_table(
    frame, *, elements: Iterable[str] = ("Ca", "Si", "Al", "Fe", "Na", "K"),
):
    """Long display/export table retaining invalid/censored rows instead of hiding them."""
    import pandas as pd

    rows: list[dict] = []
    if frame is None:
        return pd.DataFrame()
    annotated = annotate_wide_final_concentrations(
        frame, elements=elements, require_explicit_stage=True)
    for _, source in annotated.iterrows():
        rd = source.to_dict()
        for element in elements:
            value_column = f"{element}_mM"
            if value_column not in annotated.columns:
                continue
            cols = qc_columns_for(value_column)
            supplied = rd.get(f"{value_column}{units.ORIG_VALUE_SUFFIX}", rd.get(value_column))
            supplied_unit = rd.get(f"{value_column}{units.ORIG_UNIT_SUFFIX}", units.UNIT_MM)
            if _blank(supplied) and _blank(rd.get(value_column)):
                continue
            rows.append({
                "sample_id": rd.get("sample_id"), "element": element, "role": MEASURED,
                "supplied_value": supplied, "supplied_unit": supplied_unit,
                "supplied_dilution_factor": rd.get(cols["supplied_dilution_factor"]),
                "supplied_blank_value": rd.get(cols["supplied_blank_value"]),
                "blank_correction_applied": rd.get(cols["blank_correction_applied"]),
                "blank_corrected_value": rd.get(cols["blank_corrected_value"]),
                "corrected_value_mM": rd.get(value_column),
                "supplied_detection_limit": rd.get(cols["supplied_detection_limit"]),
                "input_stage": rd.get(cols["input_stage"]),
                "conversion_id": rd.get(f"{value_column}{units.CONVERSION_ID_SUFFIX}"),
                "conversion_authority": rd.get(cols["conversion_authority"]),
                "qc_status": rd.get(cols["qc_status"]),
                "qc_codes": rd.get(cols["qc_codes"]),
                "qc_reasons": rd.get(cols["qc_reasons"]),
                "validation_eligible": rd.get(cols["validation_eligible"]),
                "resolution_provenance": rd.get(cols["resolution_provenance"]),
            })
    return pd.DataFrame(rows)


def qc_display_label(status: str) -> str:
    return {
        QC_USABLE: "✓ usable",
        QC_REVIEW_REQUIRED: "⚠ review/correction required",
        QC_EXCLUDED: "⊘ excluded",
        QC_CENSORED: "<DL / censored",
    }.get(status, "⚠ unknown QC state")


def _dedupe(items) -> list:
    return list(dict.fromkeys(items))
