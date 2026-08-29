"""XRD Advisory / Pattern Planning — a **safe** XRD helper (planning, not identification).

This module does **not** identify phases from a measured pattern, and it does not fabricate a
high-confidence diffractogram. It plans a measurement:

* take a list of **expected** phases and return their **approximate** principal 2θ positions
  (Cu Kα) from a small internal demo/reference dictionary — labelled approximate/advisory,
* for a phase not in the dictionary, say plainly that **reference data is needed**,
* turn **PHREEQC-predicted precipitates** into a "phases to check by XRD" checklist, and
* suggest a context checklist (e.g. after NaOH leaching of Class C fly ash) — again expected/
  checklist, never a measured result.

**XRD Advisory v2** organises this into four user-facing modes (all advisory, all cautious):

1. :func:`expected_peaks` — approximate Cu Kα peaks for known/suspected phase *names*. A bare
   *formula* (e.g. ``CaCO3``) is flagged as polymorph-ambiguous — a formula cannot fix a pattern.
2. :func:`match_measured_peaks` — compare measured 2θ positions against the internal references and
   return **tentative** possible phases with a capped confidence (never an identification).
3. :func:`phases_to_check_from_predicted` — turn PHREEQC-predicted/saturated phases into a
   "phases PHREEQC suggests you check by XRD" list (saturation is not XRD validation).
4. :func:`reference_data_notes` — what the internal approximate table covers and what needs external
   reference data (CIF / ICDD PDF / a library such as pymatgen) later.

:func:`classify_request` maps a free-text prompt to one of these modes (used by the router).

Safety properties (mirroring the project rules):

* Every output is labelled **expected / checklist**, never "identified" or "measured".
* Peak positions are **approximate demo/reference values** (Cu Kα) — the disclaimer says to confirm
  against measured XRD and a reference database (ICDD PDF). Overlap and amorphous-content caveats
  travel with every result.
* It never claims a phase is present; it only lists what *to check*.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CU_KALPHA_WAVELENGTH_A = 1.5406
PEAK_BASIS = ("approximate demo / reference 2θ values for Cu Kα (λ≈1.5406 Å) — a planning aid, "
              "NOT a measured pattern")
DISCLAIMER = (
    "These are EXPECTED phases and APPROXIMATE reference peak positions to plan a measurement — "
    "not a measured phase identification. Confirm every phase against your measured XRD and a "
    "reference database (e.g. ICDD PDF). Peaks overlap, and amorphous (glassy) content — common in "
    "fly ash — can hide or mimic crystalline phases.")
EXPLANATION = ("XRD Advisory plans the measurement: it lists expected phases and approximate "
               "reference peaks (advisory). It does not identify phases from data — compare with "
               "measured XRD and a reference database to confirm.")

# Checklist entry statuses.
STATUS_REFERENCE_AVAILABLE = "reference_available"
STATUS_REFERENCE_NEEDED = "reference_data_needed"

# --------------------------------------------------------------------------- #
# v2 modes — the four user-facing XRD Advisory tasks (all advisory, never identification).
# --------------------------------------------------------------------------- #
MODE_EXPECTED_PEAKS = "expected_peaks"
MODE_MATCH_MEASURED = "match_measured_peaks"
MODE_PHREEQC_CHECKLIST = "phreeqc_phase_checklist"
MODE_CONTEXT_CHECKLIST = "context_checklist"
MODE_REFERENCE_NOTES = "reference_data_notes"

# Default 2θ match tolerance (degrees). ±0.2° suits typical lab Cu Kα data; widen toward ±0.3° for
# lower-resolution scans or shifted peaks (solid solution / strain). Documented + caller-overridable.
DEFAULT_MATCH_TOLERANCE_DEG = 0.2

# Measured/reference import contracts. 2theta is physically bounded to (0, 180] degrees. The
# importer deliberately does not impose a narrower instrument-specific scan range.
XRD_RECORD_SCHEMA_VERSION = 1
MAX_XRD_SOURCE_BYTES = 50 * 1024 * 1024
MIN_PHYSICAL_2THETA_DEG = 0.0
MAX_PHYSICAL_2THETA_DEG = 180.0
DUPLICATE_KEEP_ALL = "keep_all"
DUPLICATE_REJECT_LATER = "reject_later"
DUPLICATE_POLICIES = (DUPLICATE_KEEP_ALL, DUPLICATE_REJECT_LATER)
LICENSE_UNKNOWN = "unknown"
REDISTRIBUTION_UNKNOWN = "unknown"
REDISTRIBUTION_NOT_PERMITTED = "not_permitted"
REDISTRIBUTION_PERMITTED_BY_CITED_SOURCE = "permitted_by_cited_source"
REDISTRIBUTION_STATUSES = (
    REDISTRIBUTION_UNKNOWN,
    REDISTRIBUTION_NOT_PERMITTED,
    REDISTRIBUTION_PERMITTED_BY_CITED_SOURCE,
)
RADIATION_COMPATIBLE = "compatible"
RADIATION_INCOMPATIBLE = "incompatible"
RADIATION_UNKNOWN = "unknown"
WAVELENGTH_COMPATIBILITY_TOLERANCE_A = 0.01
_SENSITIVE_METADATA_KEY = re.compile(
    r"(^|_)(api_?key|token|password|passwd|secret|cookie|authorization|credential|license_?key)s?($|_)",
    re.IGNORECASE,
)
_METADATA_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SENSITIVE_METADATA_PARTS = {
    "token", "password", "passwd", "secret", "cookie", "authorization", "credential",
}


class XrdDataError(ValueError):
    """A controlled import/comparison error with an actionable user-facing message."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_safe_copy(value: Any) -> Any:
    """Return a detached JSON-safe value and fail closed on NaN/Infinity/custom objects."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise XrdDataError(f"XRD record content must be finite JSON-safe data: {exc}") from exc


def _is_sensitive_metadata_key(value: Any) -> bool:
    text = _METADATA_CAMEL_BOUNDARY_RE.sub("_", str(value))
    parts = [part.lower() for part in re.split(r"[^A-Za-z0-9]+", text) if part]
    compact = "".join(parts)
    adjacent = set(zip(parts, parts[1:]))
    return bool(
        _SENSITIVE_METADATA_KEY.search(str(value))
        or _SENSITIVE_METADATA_PARTS.intersection(parts)
        or adjacent.intersection({("api", "key"), ("license", "key")})
        or compact in {"apikey", "licensekey", "accesstoken", "refreshtoken",
                       "clientsecret", "bearertoken"}
    )


def _assert_no_sensitive_metadata(value: Any, path: str = "metadata") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _is_sensitive_metadata_key(key):
                raise XrdDataError(f"secret-like metadata field is not permitted: {path}.{key}")
            _assert_no_sensitive_metadata(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_no_sensitive_metadata(child, f"{path}[{index}]")


def _stable_record_id(prefix: str, value: Any) -> str:
    encoded = json.dumps(_json_safe_copy(value), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return f"{prefix}{hashlib.sha256(encoded).hexdigest()[:32]}"


def _source_bytes(source: Any) -> tuple[bytes, str | None]:
    """Read uploaded bytes/text/file-like input once; filesystem paths are not accepted."""
    inferred_name = None
    if isinstance(source, bytes):
        return source, inferred_name
    if isinstance(source, bytearray):
        return bytes(source), inferred_name
    if isinstance(source, Path):
        raise XrdDataError(
            "filesystem path inputs are not accepted; supply explicit bytes or an upload"
        )
    if hasattr(source, "read"):
        inferred_name = Path(str(getattr(source, "name", ""))).name or None
        payload = source.read()
        if isinstance(payload, str):
            return payload.encode("utf-8"), inferred_name
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload), inferred_name
        raise XrdDataError("uploaded XRD source must yield text or bytes")
    if isinstance(source, str):
        return source.encode("utf-8"), inferred_name
    raise XrdDataError("XRD source must be CSV/JSON text, bytes, or a readable upload")


def _safe_source_filename(
    value: Any, *, kind: str, expected_extension: str,
) -> str:
    filename = str(value or "").strip()
    if not filename:
        raise XrdDataError(f"source_filename is required for {kind} provenance")
    if filename in {".", ".."} or "/" in filename or "\\" in filename or "\x00" in filename:
        raise XrdDataError("source_filename must be a file name, not a path")
    extension = str(expected_extension or "").strip().lower()
    if not extension.startswith("."):
        extension = f".{extension}"
    if not filename.lower().endswith(extension):
        raise XrdDataError(
            f"{kind} source_filename must end with {extension}; deceptive extensions are refused")
    return filename


def _assert_source_size(raw: bytes) -> None:
    if len(raw) > MAX_XRD_SOURCE_BYTES:
        raise XrdDataError(
            "XRD source exceeds the "
            f"{MAX_XRD_SOURCE_BYTES // (1024 * 1024)} MiB safety limit")


def _decode_source(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise XrdDataError("XRD CSV/JSON source must be UTF-8 text") from exc


def _normalise_heading(value: Any) -> str:
    text = str(value or "").strip().lower().replace("θ", "theta").replace("°", "deg")
    return re.sub(r"[^a-z0-9]+", "", text)


_TWO_THETA_HEADINGS = (
    "2theta", "twotheta", "2thetadeg", "twothetadeg", "2th", "angle", "angledeg",
    "position", "positiondeg", "two_theta", "2-theta",
)
_INTENSITY_HEADINGS = (
    "intensity", "counts", "count", "cps", "countspersecond", "relativeintensity",
    "relintensity", "intensitycounts", "i",
)


def _canonical_mapping_key(value: Any) -> str | None:
    key = _normalise_heading(value)
    if key in {_normalise_heading(item) for item in _TWO_THETA_HEADINGS} | {"twotheta"}:
        return "two_theta"
    if key in {_normalise_heading(item) for item in _INTENSITY_HEADINGS}:
        return "intensity"
    return None


def _find_heading(headings: list[str], requested: str) -> str | None:
    if requested in headings:
        return requested
    normalised = _normalise_heading(requested)
    matches = [heading for heading in headings if _normalise_heading(heading) == normalised]
    if len(matches) > 1:
        raise XrdDataError(f"column mapping {requested!r} is ambiguous across original headings")
    return matches[0] if matches else None


def _resolve_column_mapping(headings: list[str], supplied: dict | None = None) -> tuple[dict, str]:
    """Resolve common headings while retaining the exact originals and mapping method."""
    if not headings:
        raise XrdDataError("XRD table has no header row")
    mapping: dict[str, str | None] = {"two_theta": None, "intensity": None}
    method = "common_heading_autodetect"
    if supplied:
        method = "user_supplied"
        for left, right in supplied.items():
            canonical_left = _canonical_mapping_key(left)
            canonical_right = _canonical_mapping_key(right)
            if canonical_left:
                source_heading = _find_heading(headings, str(right))
                if source_heading is None:
                    raise XrdDataError(f"mapped source column {right!r} is not present")
                mapping[canonical_left] = source_heading
            elif canonical_right:
                source_heading = _find_heading(headings, str(left))
                if source_heading is None:
                    raise XrdDataError(f"mapped source column {left!r} is not present")
                mapping[canonical_right] = source_heading
            else:
                raise XrdDataError(
                    f"column mapping {left!r}: {right!r} must name two_theta or intensity")

    if mapping["two_theta"] is None:
        candidates = [heading for heading in headings
                      if _canonical_mapping_key(heading) == "two_theta"]
        if len(candidates) > 1:
            raise XrdDataError(
                "multiple common 2theta headings are present; supply an explicit column_mapping")
        mapping["two_theta"] = candidates[0] if candidates else None
    if mapping["intensity"] is None:
        candidates = [heading for heading in headings
                      if _canonical_mapping_key(heading) == "intensity"]
        if len(candidates) > 1:
            raise XrdDataError(
                "multiple common intensity headings are present; supply an explicit column_mapping")
        mapping["intensity"] = candidates[0] if candidates else None
    if mapping["two_theta"] is None:
        raise XrdDataError(
            "XRD table needs a 2theta column; supply column_mapping for an uncommon heading")
    return mapping, method


def _finite_number(value: Any, *, field_name: str, allow_missing: bool = False) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if allow_missing:
            return None
        raise XrdDataError(f"{field_name} is missing")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise XrdDataError(f"{field_name} is not numeric") from exc
    if not math.isfinite(number):
        raise XrdDataError(f"{field_name} is not finite")
    return number


def _physical_two_theta(value: Any) -> float:
    number = _finite_number(value, field_name="2theta")
    assert number is not None
    if not (MIN_PHYSICAL_2THETA_DEG < number <= MAX_PHYSICAL_2THETA_DEG):
        raise XrdDataError("2theta is outside the physical range (0, 180] degrees")
    return number


def _optional_wavelength(value: Any) -> float | None:
    number = _finite_number(value, field_name="wavelength", allow_missing=True)
    if number is not None and number <= 0:
        raise XrdDataError("wavelength must be positive when supplied")
    return number


def _radiation_label(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else RADIATION_UNKNOWN


def _validated_reference_licensing(
    *, license_status: Any, redistribution_status: Any,
    doi: Any = None, url: Any = None, redistribution_basis: Any = None,
) -> tuple[str, str]:
    """Normalize the closed redistribution state and enforce licensing invariants."""
    license_value = str(license_status or "").strip() or LICENSE_UNKNOWN
    if license_value.lower() == LICENSE_UNKNOWN:
        license_value = LICENSE_UNKNOWN
    redistribution_value = str(redistribution_status or "").strip().lower() \
        or REDISTRIBUTION_UNKNOWN
    if redistribution_value not in REDISTRIBUTION_STATUSES:
        raise XrdDataError(
            "redistribution_permission_status must be unknown, not_permitted, or "
            "permitted_by_cited_source; natural-language permission claims are refused")
    permission_claimed = (
        redistribution_value == REDISTRIBUTION_PERMITTED_BY_CITED_SOURCE)
    restricted_license = bool(re.search(
        r"(?<![a-z0-9])(?:proprietary|restricted)(?![a-z0-9])",
        license_value.lower(),
    ))
    if restricted_license and permission_claimed:
        raise XrdDataError(
            "a proprietary/restricted license cannot be recorded as redistributable")
    if permission_claimed:
        if license_value == LICENSE_UNKNOWN:
            raise XrdDataError(
                "redistribution cannot be marked permitted while license status is unknown")
        if not (str(doi or "").strip() or str(url or "").strip()):
            raise XrdDataError(
                "redistribution permission requires a cited DOI or URL")
        if not str(redistribution_basis or "").strip():
            raise XrdDataError(
                "redistribution permission requires an explicit license citation or basis")
    return license_value, redistribution_value


def _normalise_radiation(value: Any) -> str | None:
    key = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    if not key or key in {"unknown", "unspecified", "na", "none"}:
        return None
    aliases = {
        "cuka": "cu_kalpha", "cukalpha": "cu_kalpha", "cukalpha1": "cu_kalpha1",
        "copperkalpha": "cu_kalpha", "moka": "mo_kalpha", "mokalpha": "mo_kalpha",
        "coka": "co_kalpha", "cokalpha": "co_kalpha", "feka": "fe_kalpha",
        "fekalpha": "fe_kalpha", "crka": "cr_kalpha", "crkalpha": "cr_kalpha",
    }
    return aliases.get(key, key)


@dataclass
class MeasuredXrdPattern:
    """Measured signal import with exact source identity; it carries no phase conclusion."""

    pattern_id: str = ""
    project_id: str = ""
    material_id: str = ""
    sample_id: str = ""
    source_filename: str = ""
    source_sha256: str = ""
    data_format: str = "csv"
    two_theta_unit: str = "degrees 2theta"
    intensity_unit: str | None = None
    intensity_type: str | None = None
    radiation_source: str = RADIATION_UNKNOWN
    wavelength_angstrom: float | None = None
    instrument: str = ""
    method: str = ""
    scan_start_deg: float | None = None
    scan_end_deg: float | None = None
    step_size_deg: float | None = None
    measured_at: str | None = None
    operator: str = ""
    lab: str = ""
    raw_imported_row_count: int = 0
    accepted_row_count: int = 0
    rejected_rows: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    original_headings: list = field(default_factory=list)
    column_mapping: dict = field(default_factory=dict)
    duplicate_policy: str = "not_applicable"
    duplicate_two_theta: list = field(default_factory=list)
    source_order_preserved: bool = True
    source_was_monotonic: bool = True
    user_peak_list: list = field(default_factory=list)
    peak_selection_provenance: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)
    schema_version: int = XRD_RECORD_SCHEMA_VERSION

    def plot_ready_rows(self) -> list[dict]:
        """Source-order rows for plotting measured signal; missing intensity remains ``None``."""
        return [{"x_two_theta_deg": row["two_theta_deg"], "y_intensity": row["intensity"],
                 "source_row_number": row["source_row_number"]} for row in self.rows]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["plot_ready_rows"] = self.plot_ready_rows()
        return _json_safe_copy(payload)


@dataclass
class ExternalXrdReference:
    """User-supplied external peak table with explicit provenance and licensing state."""

    reference_id: str = ""
    phase_name: str = ""
    formula: str = ""
    polymorph: str = ""
    radiation_source: str = RADIATION_UNKNOWN
    wavelength_angstrom: float | None = None
    peaks: list = field(default_factory=list)
    source_name: str = ""
    source_record_id: str = ""
    title: str = ""
    authors: list = field(default_factory=list)
    year: int | str | None = None
    doi: str = ""
    url: str = ""
    license_status: str = LICENSE_UNKNOWN
    redistribution_permission_status: str = REDISTRIBUTION_UNKNOWN
    source_filename: str = ""
    source_sha256: str = ""
    data_format: str = "csv"
    notes: str = ""
    review_status: str = "needs_review"
    original_headings: list = field(default_factory=list)
    column_mapping: dict = field(default_factory=dict)
    rejected_rows: list = field(default_factory=list)
    source_metadata: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)
    schema_version: int = XRD_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _assert_no_sensitive_metadata(self.source_metadata, "source_metadata")
        basis = (self.source_metadata.get("redistribution_basis")
                 or self.source_metadata.get("license_citation") or "")
        self.license_status, self.redistribution_permission_status = (
            _validated_reference_licensing(
                license_status=self.license_status,
                redistribution_status=self.redistribution_permission_status,
                doi=self.doi,
                url=self.url,
                redistribution_basis=basis,
            )
        )

    @property
    def reference_2theta(self) -> list[float]:
        return [row["two_theta_deg"] for row in self.peaks]

    def identity(self) -> dict:
        return {
            "reference_id": self.reference_id,
            "source_filename": self.source_filename,
            "source_sha256": self.source_sha256,
        }

    def provenance(self) -> dict:
        return {
            "phase_name": self.phase_name,
            "formula": self.formula,
            "polymorph": self.polymorph,
            "radiation_source": self.radiation_source,
            "wavelength_angstrom": self.wavelength_angstrom,
            "source_name": self.source_name,
            "source_record_id": self.source_record_id,
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "doi": self.doi,
            "url": self.url,
            "license_status": self.license_status,
            "redistribution_permission_status": self.redistribution_permission_status,
            "source_filename": self.source_filename,
            "source_sha256": self.source_sha256,
            "data_format": self.data_format,
            "review_status": self.review_status,
            "notes": self.notes,
            "original_headings": list(self.original_headings),
            "column_mapping": _json_safe_copy(self.column_mapping),
            "rejected_rows": _json_safe_copy(self.rejected_rows),
            "source_metadata": _json_safe_copy(self.source_metadata),
        }

    def to_dict(self) -> dict:
        return _json_safe_copy(asdict(self))


def _issue_code(message: str) -> str:
    low = message.lower()
    if "missing" in low:
        return "missing_2theta"
    if "not numeric" in low:
        return "non_numeric_2theta"
    if "not finite" in low:
        return "non_finite_2theta"
    if "physical range" in low:
        return "physically_impossible_2theta"
    return "invalid_2theta"


def _optional_metadata_number(metadata: dict, key: str, *, positive: bool = False) -> float | None:
    value = metadata.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    number = _finite_number(value, field_name=key)
    if positive and number is not None and number <= 0:
        raise XrdDataError(f"{key} must be positive when supplied")
    return number


def _csv_rows(raw: bytes) -> tuple[list[str], list[tuple[int, dict]]]:
    reader = csv.DictReader(io.StringIO(_decode_source(raw)), restval=None)
    headings = [str(item) for item in (reader.fieldnames or []) if item is not None]
    if len(headings) != len(set(headings)):
        raise XrdDataError("XRD CSV contains duplicate original headings; map them uniquely first")
    rows = []
    for row_number, row in enumerate(reader, start=2):
        rows.append((row_number, {heading: row.get(heading) for heading in headings}))
    return headings, rows


def _parse_position_rows(
    raw_rows: list[tuple[int, dict]],
    mapping: dict,
    *,
    position_key: str = "two_theta",
) -> tuple[list[dict], list[dict], list[str]]:
    """Parse peak/pattern rows without fabricating missing or invalid intensity values."""
    accepted: list[dict] = []
    rejected: list[dict] = []
    warnings: list[str] = []
    theta_heading = mapping[position_key]
    intensity_heading = mapping.get("intensity")
    for row_number, original_row in raw_rows:
        try:
            two_theta = _physical_two_theta(original_row.get(theta_heading))
        except XrdDataError as exc:
            rejected.append({
                "source_row_number": row_number,
                "reason_code": _issue_code(str(exc)),
                "reason": str(exc),
                "original_row": original_row,
            })
            continue

        intensity = None
        intensity_issue = None
        if intensity_heading is not None:
            raw_intensity = original_row.get(intensity_heading)
            try:
                intensity = _finite_number(raw_intensity, field_name="intensity", allow_missing=True)
            except XrdDataError as exc:
                # The position remains usable. Preserve the exact supplied value and retain missing
                # intensity as None instead of turning it into zero or dropping the position.
                intensity_issue = str(exc)
                warnings.append(
                    f"Row {row_number}: {exc}; position retained with missing intensity.")
        if intensity is not None and intensity < 0:
            intensity_issue = (
                "negative intensity retained; background correction can produce negative values")
            warnings.append(
                f"Row {row_number}: negative intensity {intensity:g} retained without clamping; "
                "background-corrected signal can be negative.")
        accepted.append({
            "source_row_number": row_number,
            "two_theta_deg": two_theta,
            "intensity": intensity,
            "intensity_issue": intensity_issue,
            "original_values": original_row,
        })
    return accepted, rejected, warnings


def _handle_measured_duplicates(
    rows: list[dict], rejected: list[dict], duplicate_policy: str | None,
) -> tuple[list[dict], list[dict], str, list[dict], list[str]]:
    if duplicate_policy is not None and duplicate_policy not in DUPLICATE_POLICIES:
        raise XrdDataError(
            f"duplicate_policy must be one of {', '.join(DUPLICATE_POLICIES)}")
    by_position: dict[float, list[dict]] = {}
    for row in rows:
        by_position.setdefault(row["two_theta_deg"], []).append(row)
    duplicates = [
        {"two_theta_deg": position,
         "source_row_numbers": [row["source_row_number"] for row in group]}
        for position, group in by_position.items() if len(group) > 1
    ]
    if not duplicates:
        return rows, rejected, duplicate_policy or "not_applicable", [], []
    if duplicate_policy is None:
        raise XrdDataError(
            "duplicate 2theta values require an explicit duplicate_policy: keep_all or reject_later")
    warnings = []
    if duplicate_policy == DUPLICATE_KEEP_ALL:
        warnings.append(
            "Duplicate 2theta rows were retained in source order under the explicit keep_all policy.")
        return rows, rejected, duplicate_policy, duplicates, warnings

    seen: set[float] = set()
    kept: list[dict] = []
    for row in rows:
        position = row["two_theta_deg"]
        if position not in seen:
            seen.add(position)
            kept.append(row)
            continue
        rejected.append({
            "source_row_number": row["source_row_number"],
            "reason_code": "duplicate_2theta_rejected_later",
            "reason": (
                "later duplicate 2theta row rejected under the explicit reject_later policy"),
            "original_row": row["original_values"],
        })
    warnings.append(
        "Later duplicate 2theta rows were rejected in source order under the explicit "
        "reject_later policy; no averaging was performed.")
    return kept, rejected, duplicate_policy, duplicates, warnings


def import_measured_pattern_csv(
    source: Any,
    *,
    source_filename: str | None = None,
    metadata: dict | None = None,
    column_mapping: dict | None = None,
    duplicate_policy: str | None = None,
    user_peak_list: list | tuple | None = None,
    peak_selection_provenance: dict | None = None,
) -> MeasuredXrdPattern:
    """Import a measured CSV signal with exact hash, row decisions, and source-order provenance.

    Common headings (``2theta``, ``2θ``, ``Angle``, ``Intensity``, ``Counts``, ``cps``) are mapped
    automatically. A user mapping may be given in either canonical-to-original or
    original-to-canonical form. Duplicate positions require an explicit policy only when present;
    rows are never sorted or averaged.
    """
    raw, inferred_filename = _source_bytes(source)
    _assert_source_size(raw)
    filename = _safe_source_filename(
        source_filename or inferred_filename, kind="measured XRD", expected_extension=".csv")
    safe_metadata = _json_safe_copy(metadata or {})
    _assert_no_sensitive_metadata(safe_metadata)
    two_theta_unit = str(safe_metadata.get("two_theta_unit") or "degrees 2theta")
    if _normalise_heading(two_theta_unit) not in {
            "degree2theta", "degrees2theta", "deg2theta", "2thetadeg", "2thetadegrees"}:
        raise XrdDataError(
            "measured CSV 2theta_unit must be degrees 2theta; no angle-unit conversion is performed")
    headings, raw_rows = _csv_rows(raw)
    resolved_mapping, mapping_method = _resolve_column_mapping(headings, column_mapping)
    rows, rejected, warnings = _parse_position_rows(raw_rows, resolved_mapping)
    rows, rejected, policy, duplicates, duplicate_warnings = _handle_measured_duplicates(
        rows, rejected, duplicate_policy)
    warnings.extend(duplicate_warnings)
    if not rows:
        warnings.append("No physically usable measured 2theta rows were imported.")

    source_was_monotonic = all(
        left["two_theta_deg"] <= right["two_theta_deg"] for left, right in zip(rows, rows[1:]))
    if not source_was_monotonic:
        warnings.append(
            "Measured rows are not monotonic in 2theta; source order was preserved and no silent "
            "reordering was performed.")
    source_sha256 = hashlib.sha256(raw).hexdigest()
    positions = [row["two_theta_deg"] for row in rows]
    supplied_scan_start = _optional_metadata_number(safe_metadata, "scan_start_deg")
    supplied_scan_end = _optional_metadata_number(safe_metadata, "scan_end_deg")
    step_size = _optional_metadata_number(safe_metadata, "step_size_deg", positive=True)
    wavelength = _optional_wavelength(
        safe_metadata.get("wavelength_angstrom", safe_metadata.get("wavelength")))
    identity_payload = {
        "project_id": safe_metadata.get("project_id", ""),
        "material_id": safe_metadata.get("material_id", ""),
        "sample_id": safe_metadata.get("sample_id", ""),
        "source_filename": filename,
        "source_sha256": source_sha256,
        "two_theta_unit": two_theta_unit,
        "radiation_source": _radiation_label(
            safe_metadata.get("radiation_source", safe_metadata.get("radiation"))),
        "wavelength_angstrom": wavelength,
        "instrument": str(safe_metadata.get("instrument") or ""),
        "method": str(safe_metadata.get("method") or ""),
    }
    pattern = MeasuredXrdPattern(
        pattern_id=_stable_record_id("xrdpat_", identity_payload),
        project_id=str(safe_metadata.get("project_id") or ""),
        material_id=str(safe_metadata.get("material_id") or ""),
        sample_id=str(safe_metadata.get("sample_id") or ""),
        source_filename=filename,
        source_sha256=source_sha256,
        two_theta_unit=two_theta_unit,
        intensity_unit=(None if safe_metadata.get("intensity_unit") in (None, "")
                        else str(safe_metadata["intensity_unit"])),
        intensity_type=(None if safe_metadata.get("intensity_type") in (None, "")
                        else str(safe_metadata["intensity_type"])),
        radiation_source=_radiation_label(
            safe_metadata.get("radiation_source", safe_metadata.get("radiation"))),
        wavelength_angstrom=wavelength,
        instrument=str(safe_metadata.get("instrument") or ""),
        method=str(safe_metadata.get("method") or ""),
        scan_start_deg=(supplied_scan_start if supplied_scan_start is not None
                        else (min(positions) if positions else None)),
        scan_end_deg=(supplied_scan_end if supplied_scan_end is not None
                      else (max(positions) if positions else None)),
        step_size_deg=step_size,
        measured_at=(None if safe_metadata.get("measured_at") in (None, "")
                     else str(safe_metadata["measured_at"])),
        operator=str(safe_metadata.get("operator") or ""),
        lab=str(safe_metadata.get("lab") or ""),
        raw_imported_row_count=len(raw_rows),
        accepted_row_count=len(rows),
        rejected_rows=rejected,
        rows=rows,
        original_headings=headings,
        column_mapping={
            "method": mapping_method,
            "canonical_to_original": resolved_mapping,
            "original_headings": headings,
        },
        duplicate_policy=policy,
        duplicate_two_theta=duplicates,
        source_order_preserved=True,
        source_was_monotonic=source_was_monotonic,
        metadata=safe_metadata,
        warnings=warnings,
    )
    if user_peak_list is not None:
        pattern = attach_user_peak_list(
            pattern, user_peak_list, provenance=peak_selection_provenance)
    return pattern


def attach_user_peak_list(
    pattern: MeasuredXrdPattern,
    peaks: list | tuple,
    *,
    provenance: dict | None = None,
) -> MeasuredXrdPattern:
    """Return a pattern copy with a validated, explicitly user-supplied measured peak list."""
    if not isinstance(pattern, MeasuredXrdPattern):
        raise XrdDataError("attach_user_peak_list requires a MeasuredXrdPattern")
    parsed = []
    for index, item in enumerate(peaks or [], start=1):
        if isinstance(item, dict):
            raw_position = (item.get("two_theta_deg") if "two_theta_deg" in item
                            else item.get("two_theta", item.get("position")))
            raw_intensity = item.get("relative_intensity", item.get("intensity"))
            user_note = str(item.get("note") or "")
        else:
            raw_position, raw_intensity, user_note = item, None, ""
        try:
            position = _physical_two_theta(raw_position)
            intensity = _finite_number(
                raw_intensity, field_name="peak relative intensity", allow_missing=True)
        except XrdDataError as exc:
            raise XrdDataError(f"user peak {index}: {exc}") from exc
        parsed.append({
            "peak_index": index,
            "two_theta_deg": position,
            "relative_intensity": intensity,
            "note": user_note,
            "selection_method": "user_supplied",
        })
    supplied = _json_safe_copy(provenance or {})
    selection = {
        "selection_method": "user_supplied_peak_list",
        "source_pattern_id": pattern.pattern_id,
        "source_sha256": pattern.source_sha256,
        "provided_by": str(supplied.get("provided_by") or supplied.get("operator") or ""),
        "selected_at": str(supplied.get("selected_at") or _utc_now()),
        "notes": str(supplied.get("notes") or ""),
        "user_edits": _json_safe_copy(supplied.get("user_edits") or []),
        "parameters": _json_safe_copy(supplied.get("parameters") or {}),
        "supplied_provenance": supplied,
    }
    return replace(pattern, user_peak_list=parsed, peak_selection_provenance=selection)


def _reference_provenance_fields(source_metadata: dict) -> dict:
    source_name = str(
        source_metadata.get("source_name") or source_metadata.get("database_name")
        or source_metadata.get("provider") or "").strip()
    if not source_name:
        raise XrdDataError(
            "external XRD reference requires source metadata with source_name/database_name/provider")
    phase_name = str(source_metadata.get("phase_name") or source_metadata.get("phase") or "").strip()
    if not phase_name:
        raise XrdDataError("external XRD reference requires a phase_name in source metadata")
    authors = source_metadata.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    if not isinstance(authors, (list, tuple)):
        raise XrdDataError("reference authors must be a list or string when supplied")
    license_status = str(source_metadata.get("license_status") or "").strip() or LICENSE_UNKNOWN
    redistribution_status = str(
        source_metadata.get("redistribution_permission_status")
        or source_metadata.get("redistribution_status") or "").strip() or REDISTRIBUTION_UNKNOWN
    redistribution_basis = str(
        source_metadata.get("redistribution_basis")
        or source_metadata.get("license_citation") or ""
    ).strip()
    license_status, redistribution_status = _validated_reference_licensing(
        license_status=license_status,
        redistribution_status=redistribution_status,
        doi=source_metadata.get("doi"),
        url=source_metadata.get("url"),
        redistribution_basis=redistribution_basis,
    )
    return {
        "source_name": source_name,
        "phase_name": phase_name,
        "formula": str(source_metadata.get("formula") or ""),
        "polymorph": str(
            source_metadata.get("polymorph") or source_metadata.get("crystal_form") or ""),
        "radiation_source": _radiation_label(
            source_metadata.get("radiation_source", source_metadata.get("radiation"))),
        "wavelength_angstrom": _optional_wavelength(
            source_metadata.get("wavelength_angstrom", source_metadata.get("wavelength"))),
        "source_record_id": str(
            source_metadata.get("source_record_id") or source_metadata.get("record_id")
            or source_metadata.get("card_id") or ""),
        "title": str(source_metadata.get("title") or ""),
        "authors": [str(author) for author in authors],
        "year": source_metadata.get("year"),
        "doi": str(source_metadata.get("doi") or ""),
        "url": str(source_metadata.get("url") or ""),
        "license_status": license_status,
        "redistribution_permission_status": redistribution_status,
        "redistribution_basis": redistribution_basis,
        "notes": str(source_metadata.get("notes") or ""),
        "review_status": str(source_metadata.get("review_status") or "needs_review"),
    }


def _build_external_reference(
    *,
    raw: bytes,
    filename: str,
    data_format: str,
    source_metadata: dict,
    peaks: list[dict],
    rejected: list[dict],
    headings: list[str],
    mapping: dict,
    mapping_method: str,
    warnings: list[str],
) -> ExternalXrdReference:
    safe_metadata = _json_safe_copy(source_metadata)
    _assert_no_sensitive_metadata(safe_metadata, "source_metadata")
    fields = _reference_provenance_fields(safe_metadata)
    if not peaks:
        raise XrdDataError("external XRD reference contains no finite physical 2theta positions")
    source_sha256 = hashlib.sha256(raw).hexdigest()
    duplicate_positions = {
        peak["two_theta_deg"] for peak in peaks
        if sum(row["two_theta_deg"] == peak["two_theta_deg"] for row in peaks) > 1
    }
    if duplicate_positions:
        warnings.append(
            "Duplicate reference positions were retained in source order; no averaging or "
            "reordering was performed.")
    identity_payload = {
        "source_filename": filename,
        "source_sha256": source_sha256,
        "source_name": fields["source_name"],
        "source_record_id": fields["source_record_id"],
        "phase_name": fields["phase_name"],
        "formula": fields["formula"],
        "polymorph": fields["polymorph"],
        "radiation_source": fields["radiation_source"],
        "wavelength_angstrom": fields["wavelength_angstrom"],
        "license_status": fields["license_status"],
        "redistribution_permission_status": fields["redistribution_permission_status"],
    }
    return ExternalXrdReference(
        reference_id=_stable_record_id("xrdref_", identity_payload),
        phase_name=fields["phase_name"],
        formula=fields["formula"],
        polymorph=fields["polymorph"],
        radiation_source=fields["radiation_source"],
        wavelength_angstrom=fields["wavelength_angstrom"],
        peaks=peaks,
        source_name=fields["source_name"],
        source_record_id=fields["source_record_id"],
        title=fields["title"],
        authors=fields["authors"],
        year=fields["year"],
        doi=fields["doi"],
        url=fields["url"],
        license_status=fields["license_status"],
        redistribution_permission_status=fields["redistribution_permission_status"],
        source_filename=filename,
        source_sha256=source_sha256,
        data_format=data_format,
        notes=fields["notes"],
        review_status=fields["review_status"],
        original_headings=headings,
        column_mapping={
            "method": mapping_method,
            "canonical_to_original": mapping,
            "original_headings": headings,
        },
        rejected_rows=rejected,
        source_metadata=safe_metadata,
        warnings=warnings,
    )


def import_reference_csv(
    source: Any,
    *,
    source_filename: str | None = None,
    source_metadata: dict | None = None,
    column_mapping: dict | None = None,
) -> ExternalXrdReference:
    """Adapt a user-supplied reference CSV; no database content is fetched or bundled."""
    raw, inferred_filename = _source_bytes(source)
    _assert_source_size(raw)
    filename = _safe_source_filename(
        source_filename or inferred_filename, kind="external XRD reference",
        expected_extension=".csv")
    if not source_metadata:
        raise XrdDataError("external XRD reference requires source_metadata")
    headings, raw_rows = _csv_rows(raw)
    mapping, mapping_method = _resolve_column_mapping(headings, column_mapping)
    peaks, rejected, warnings = _parse_position_rows(raw_rows, mapping)
    reference_peaks = [{
        "source_row_number": row["source_row_number"],
        "two_theta_deg": row["two_theta_deg"],
        "relative_intensity": row["intensity"],
        "intensity_issue": row["intensity_issue"],
        "original_values": row["original_values"],
    } for row in peaks]
    return _build_external_reference(
        raw=raw, filename=filename, data_format="csv", source_metadata=source_metadata,
        peaks=reference_peaks, rejected=rejected, headings=headings, mapping=mapping,
        mapping_method=mapping_method, warnings=warnings)


def _json_reference_rows(payload: Any) -> tuple[list[dict], dict]:
    embedded_metadata: dict = {}
    if isinstance(payload, dict):
        embedded_metadata = {
            key: value for key, value in payload.items()
            if key not in {"peaks", "reference_peaks", "rows", "reference_positions",
                           "relative_intensities"}
        }
        rows = payload.get("peaks", payload.get("reference_peaks", payload.get("rows")))
        if rows is None and "reference_positions" in payload:
            positions = payload.get("reference_positions") or []
            intensities = payload.get("relative_intensities") or []
            rows = [
                {"two_theta": position,
                 "relative_intensity": intensities[index] if index < len(intensities) else None}
                for index, position in enumerate(positions)
            ]
    else:
        rows = payload
    if not isinstance(rows, list):
        raise XrdDataError("reference JSON needs a peaks/reference_peaks/rows array")
    normalised_rows = []
    for item in rows:
        if isinstance(item, dict):
            normalised_rows.append(item)
        else:
            normalised_rows.append({"two_theta": item})
    return normalised_rows, embedded_metadata


def import_reference_json(
    source: Any,
    *,
    source_filename: str | None = None,
    source_metadata: dict | None = None,
    column_mapping: dict | None = None,
) -> ExternalXrdReference:
    """Adapt a user-supplied reference JSON object/array with exact file and license provenance."""
    raw, inferred_filename = _source_bytes(source)
    _assert_source_size(raw)
    filename = _safe_source_filename(
        source_filename or inferred_filename, kind="external XRD reference",
        expected_extension=".json")
    try:
        payload = json.loads(_decode_source(raw))
    except json.JSONDecodeError as exc:
        raise XrdDataError(f"invalid reference JSON: {exc.msg}") from exc
    rows, embedded_metadata = _json_reference_rows(payload)
    merged_metadata = _json_safe_copy(embedded_metadata)
    if source_metadata:
        merged_metadata.update(_json_safe_copy(source_metadata))
    if not merged_metadata:
        raise XrdDataError("external XRD reference requires source metadata")
    headings = []
    for row in rows:
        for key in row:
            if key not in headings:
                headings.append(str(key))
    mapping, mapping_method = _resolve_column_mapping(headings, column_mapping)
    numbered_rows = [(index, {heading: row.get(heading) for heading in headings})
                     for index, row in enumerate(rows, start=1)]
    peaks, rejected, warnings = _parse_position_rows(numbered_rows, mapping)
    reference_peaks = [{
        "source_row_number": row["source_row_number"],
        "two_theta_deg": row["two_theta_deg"],
        "relative_intensity": row["intensity"],
        "intensity_issue": row["intensity_issue"],
        "original_values": row["original_values"],
    } for row in peaks]
    return _build_external_reference(
        raw=raw, filename=filename, data_format="json", source_metadata=merged_metadata,
        peaks=reference_peaks, rejected=rejected, headings=headings, mapping=mapping,
        mapping_method=mapping_method, warnings=warnings)

# Confidence levels for measured-peak matching. Even the HIGHEST level stays tentative — the wording
# is always "tentatively consistent with", never "identified as".
CONFIDENCE_LOW = "low"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_HIGH = "high"
CONFIDENCE_WORDING = {
    CONFIDENCE_LOW: ("tentatively consistent with (weak / partial — a single peak, or high "
                     "ambiguity)"),
    CONFIDENCE_MEDIUM: ("tentatively consistent with (several peaks match, but ambiguity or a "
                        "missing major peak remains)"),
    CONFIDENCE_HIGH: ("tentatively consistent with (a strong candidate — multiple characteristic "
                      "peaks, low ambiguity) — still NOT an identification"),
}

MATCH_EXPLANATION = ("Match Measured Peaks compares your measured 2θ positions against the internal "
                     "approximate reference peaks and returns TENTATIVE possible phases — never an "
                     "identification. Confidence is capped by how many characteristic peaks match.")
REFERENCE_NOTES_EXPLANATION = ("Reference Data Notes lists which phases the internal approximate "
                               "table covers and which need external reference data — the internal "
                               "peaks are teaching/advisory references, not certified standards.")

# Dominant (strongest) reference reflection per phase — used only to caution when a candidate's
# dominant peak is absent from a measured list. Approximate Cu Kα; a planning aid, not a standard.
_DOMINANT_2THETA = {
    "quartz": 26.6, "calcite": 29.4, "portlandite": 18.0, "gypsum": 11.6, "hematite": 33.2,
    "magnetite": 35.5, "mullite": 26.0, "corundum": 35.1, "ettringite": 9.1,
}

# Formulas that are NOT a single phase: the same formula crystallises as several polymorphs with
# DIFFERENT patterns, so a formula alone cannot fix an XRD pattern — the phase must be named.
_POLYMORPHIC_FORMULAS = {
    "sio2": ("quartz", "cristobalite", "tridymite", "amorphous silica"),
    "caco3": ("calcite", "aragonite", "vaterite"),
    "al2o3": ("corundum (α-Al2O3)", "γ-Al2O3", "other transition aluminas"),
    "fe2o3": ("hematite (α-Fe2O3)", "maghemite (γ-Fe2O3)"),
    "tio2": ("anatase", "rutile", "brookite"),
}

# Common fly-ash / cementitious phases NOT in the internal table — they need external reference data
# (CIF / ICDD PDF) before they can be planned. NAMES ONLY; no peaks are fabricated for them.
_NEEDS_EXTERNAL_REFERENCE = (
    "C-S-H / C-A-S-H gel (poorly crystalline)", "hydrotalcite", "katoite / hydrogarnet",
    "anatase / rutile (TiO2)", "periclase (MgO)", "lime (CaO)",
    "feldspars (albite / anorthite)", "maghemite", "brucite (Mg(OH)2)",
    "thenardite / mirabilite (Na2SO4 phases)",
)

_FORMULA_HINT_RE = re.compile(r"\d|\(")


def _input_looks_like_formula(raw) -> bool:
    """A cheap hint that a token is a chemical formula (has a digit or a parenthesis), not a name."""
    return bool(_FORMULA_HINT_RE.search(str(raw or "")))


def _to_float(value):
    """Best-effort float (drops None / non-numeric / NaN) — used to clean measured peak input."""
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return f if f == f else None


@dataclass(frozen=True)
class PhaseRef:
    """A reference phase: display name, formula, a few approximate principal 2θ peaks, a note."""

    name: str
    formula: str
    main_2theta: tuple = ()        # approximate principal peaks, degrees (Cu Kα)
    note: str = ""


# --------------------------------------------------------------------------- #
# Internal demo / reference dictionary — APPROXIMATE principal peaks (Cu Kα).
# These are well-known textbook positions for the strongest reflections, rounded and kept to a few
# major peaks each. They are labelled approximate everywhere and are for planning only.
# --------------------------------------------------------------------------- #
_REFERENCE: dict[str, PhaseRef] = {
    "quartz": PhaseRef("Quartz", "SiO2", (20.9, 26.6, 50.1),
                       "26.6° is the dominant reflection; very common in fly ash."),
    "calcite": PhaseRef("Calcite", "CaCO3", (29.4, 39.4, 43.1),
                        "29.4° (104) is dominant; a common carbonation product."),
    "portlandite": PhaseRef("Portlandite", "Ca(OH)2", (18.0, 34.1, 47.1),
                            "18.0° (001) is diagnostic; forms in high-Ca alkaline systems."),
    "gypsum": PhaseRef("Gypsum", "CaSO4·2H2O", (11.6, 20.7, 29.1),
                       "11.6° (020) at low angle is diagnostic."),
    "hematite": PhaseRef("Hematite", "Fe2O3", (24.1, 33.2, 35.6),
                         "33.2° (104) and 35.6° (110) are the main pair."),
    "magnetite": PhaseRef("Magnetite", "Fe3O4", (30.1, 35.5, 62.6),
                          "35.5° (311) is dominant; overlaps maghemite/spinels."),
    "mullite": PhaseRef("Mullite", "3Al2O3·2SiO2", (16.4, 26.0, 40.9),
                        "the 26°/40.9° group is characteristic; common in Class F fly ash."),
    "corundum": PhaseRef("Corundum", "Al2O3", (25.6, 35.1, 43.4),
                         "35.1° (104) is dominant; an internal-standard phase."),
    "ettringite": PhaseRef("Ettringite", "Ca6Al2(SO4)3(OH)12·26H2O", (9.1, 15.8, 22.9),
                           "9.1° at low angle is diagnostic; forms in sulfate-rich alkaline cure."),
}

# Synonyms → canonical reference key (so "Ca(OH)2" / "calcium hydroxide" → portlandite).
_SYNONYMS: dict[str, str] = {
    "quartz": "quartz", "sio2": "quartz", "silica": "quartz",
    "calcite": "calcite", "caco3": "calcite", "calcium carbonate": "calcite",
    "portlandite": "portlandite", "ca(oh)2": "portlandite", "caoh2": "portlandite",
    "calcium hydroxide": "portlandite", "ch": "portlandite",
    "gypsum": "gypsum", "caso4·2h2o": "gypsum", "caso4.2h2o": "gypsum", "caso42h2o": "gypsum",
    "hematite": "hematite", "fe2o3": "hematite", "haematite": "hematite",
    "magnetite": "magnetite", "fe3o4": "magnetite",
    "mullite": "mullite",
    "corundum": "corundum", "al2o3": "corundum", "alumina": "corundum",
    "ettringite": "ettringite", "aft": "ettringite",
}

# Phases a researcher commonly checks for after alkaline (NaOH) leaching of Class C fly ash, plus a
# note that the bulk of fly ash is amorphous glass. Advisory only.
_CLASS_C_NAOH_CHECKLIST = ("quartz", "calcite", "portlandite", "ettringite", "mullite",
                           "hematite", "magnetite")


@dataclass
class XrdAdvisory:
    """The advisory output: an expected-phase checklist + warnings + the standing disclaimer."""

    checklist: list = field(default_factory=list)      # list[dict]: phase/formula/status/peaks/note
    unknown_phases: list = field(default_factory=list)  # names with no reference data
    warnings: list = field(default_factory=list)
    disclaimer: str = DISCLAIMER
    explanation: str = EXPLANATION
    peak_basis: str = PEAK_BASIS

    def checklist_table(self) -> list[dict]:
        return list(self.checklist)

    def peak_table(self) -> list[dict]:
        """Flat (phase, approximate 2θ) rows for the phases that have reference peaks."""
        rows: list[dict] = []
        for entry in self.checklist:
            for two_theta in entry.get("approx_2theta", []) or []:
                rows.append({"phase": entry["phase"], "formula": entry["formula"],
                             "approx_2theta_deg": two_theta, "basis": PEAK_BASIS})
        return rows


# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #
def reference_phase_names() -> list[str]:
    """Display names of the phases in the internal reference dictionary."""
    return [ref.name for ref in _REFERENCE.values()]


def canonical_phase(name) -> str | None:
    """Canonical reference key for a phase name/formula (``Ca(OH)2`` → ``portlandite``), else None."""
    if not name:
        return None
    key = str(name).strip().lower()
    if key in _SYNONYMS:
        return _SYNONYMS[key]
    # tolerate trailing punctuation / extra spaces
    key2 = key.replace(" ", "")
    return _SYNONYMS.get(key2)


def _entry_for(name: str) -> dict:
    """Build a checklist entry for one requested phase name (found → peaks; else reference-needed).

    A bare *formula* is detected (``from_formula``) and, for a polymorphic system, the alternative
    polymorphs are listed (``polymorph_alternatives``) with a caution — a formula does not fix a
    pattern, so the peaks shown are only for the assumed polymorph.
    """
    raw = str(name).strip()
    from_formula = _input_looks_like_formula(raw)
    canon = canonical_phase(name)
    if canon is None:
        return {"phase": raw, "formula": "", "status": STATUS_REFERENCE_NEEDED,
                "approx_2theta": [], "label": "expected (reference data needed)",
                "from_formula": from_formula, "polymorph_alternatives": [],
                "note": "No internal reference for this phase — supply a reference pattern to check it."}
    ref = _REFERENCE[canon]
    polymorphs = list(_POLYMORPHIC_FORMULAS.get(raw.lower().replace(" ", ""), ()))
    note = ref.note
    if from_formula and polymorphs:
        note = (f"'{raw}' is a chemical FORMULA, not a phase — it can crystallise as "
                f"{', '.join(polymorphs)}. The peaks shown assume the {ref.name} polymorph; name the "
                f"actual phase to plan an exact pattern.")
    return {"phase": ref.name, "formula": ref.formula, "status": STATUS_REFERENCE_AVAILABLE,
            "approx_2theta": list(ref.main_2theta), "label": "expected / checklist (approximate peaks)",
            "from_formula": from_formula, "polymorph_alternatives": polymorphs, "note": note}


def expected_peaks(phases) -> XrdAdvisory:
    """Build an advisory checklist of **expected** phases with **approximate** reference peaks.

    ``phases`` is a list of phase names/formulas. Each is matched against the internal reference
    dictionary; matches get approximate principal 2θ (Cu Kα), the rest are flagged
    *reference data needed*. Every entry is labelled expected/advisory — never identified.
    """
    checklist: list[dict] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for name in (phases or []):
        if name is None or not str(name).strip():
            continue
        entry = _entry_for(name)
        dedupe_key = (entry["phase"] or str(name)).lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        checklist.append(entry)
        if entry["status"] == STATUS_REFERENCE_NEEDED:
            unknown.append(entry["phase"])

    warnings = _standard_warnings(checklist)
    return XrdAdvisory(checklist=checklist, unknown_phases=unknown, warnings=warnings)


def phases_to_check_from_predicted(predicted_phases) -> XrdAdvisory:
    """Turn PHREEQC-predicted precipitates into a 'phases to check by XRD' checklist.

    Accepts a list of phase **names**, or a list of ``{"phase": ..., "SI": ...}`` dicts as produced
    by the PHREEQC executor's saturation indices. These are *candidate* phases to look for — the
    advisory says so. It never asserts the phase is present; only measured XRD can confirm it.
    """
    names: list[str] = []
    for item in (predicted_phases or []):
        if isinstance(item, dict):
            name = item.get("phase") or item.get("name")
        else:
            name = item
        if name is None or not str(name).strip():
            continue
        names.append(_strip_phreeqc_suffix(str(name)))

    advisory = expected_peaks(names)
    advisory.warnings.insert(
        0, "PHREEQC suggests these phases MAY be worth checking by XRD — they are model-PREDICTED "
           "candidates and a saturation index is NOT XRD validation. Confirm each by measured XRD "
           "before reporting it as present.")
    return advisory


def suggest_phases_for_context(text: str) -> XrdAdvisory:
    """Suggest an **advisory** phase checklist from a free-text context (keyword-based, deterministic).

    Currently recognises alkaline (NaOH/KOH) leaching of Class C / high-calcium fly ash and returns
    a common checklist plus the amorphous-glass caveat. Otherwise returns a minimal, clearly-advisory
    list. Always labelled expected/checklist; it identifies nothing.
    """
    low = str(text or "").lower()
    is_alkaline = any(w in low for w in ("naoh", "koh", "alkal", "caustic", "hydroxide"))
    is_flyash = any(w in low for w in ("fly ash", "flyash", "fli ash", "coal ash",
                                       "class c", "class f"))
    advisory = expected_peaks(list(_CLASS_C_NAOH_CHECKLIST)) if (is_alkaline or is_flyash) \
        else expected_peaks(["quartz", "calcite"])
    advisory.warnings.insert(
        0, "Suggested phases are a planning checklist, not a prediction of what is present. Fly ash "
           "is largely AMORPHOUS glass — a flat hump near 20–35° 2θ is expected and is not a "
           "crystalline phase.")
    return advisory


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _strip_phreeqc_suffix(name: str) -> str:
    """Best-effort strip of PHREEQC phase decorations (e.g. ``Calcite(d)`` → ``Calcite``)."""
    return name.split("(")[0].strip() or name.strip()


def _standard_warnings(checklist) -> list[str]:
    """The overlap + amorphous + preferred-orientation + confirm-with-reference warnings every result
    should carry (plus a formula/polymorph caution when a formula was given)."""
    warns = [
        "Peaks can overlap (e.g. quartz, mullite, and feldspars cluster near 26–28° 2θ) — a single "
        "2θ match is not proof of a phase.",
        "Amorphous content (fly-ash glass) is invisible to phase peaks but raises the background — "
        "do not infer absence of glass from sharp peaks.",
        "Preferred orientation and variable crystallinity change relative peak INTENSITIES — rely on "
        "peak POSITIONS, not heights, for planning; treat intensities qualitatively.",
        "Confirm every phase against measured XRD and a reference pattern database (ICDD PDF).",
    ]
    if any(e["status"] == STATUS_REFERENCE_NEEDED for e in checklist):
        warns.append("Some requested phases have no internal reference here — supply a reference "
                     "pattern to plan their peaks.")
    if any(e.get("polymorph_alternatives") for e in checklist):
        warns.append("A chemical FORMULA was given for a polymorphic system — a formula does not fix "
                     "an XRD pattern (e.g. CaCO3 = calcite / aragonite / vaterite). Name the phase.")
    return warns


# --------------------------------------------------------------------------- #
# Mode 2 — Match Measured Peaks (TENTATIVE possible phases; never an identification).
# --------------------------------------------------------------------------- #
@dataclass
class XrdMatchResult:
    """Tentative measured-peak matching: candidate phases (with capped confidence) + diagnostics.

    Every candidate is phrased "tentatively consistent with", never "identified". Confidence is
    capped by how many characteristic peaks match and how unique those matches are.
    """

    tolerance_deg: float = DEFAULT_MATCH_TOLERANCE_DEG
    measured_2theta: list = field(default_factory=list)
    candidates: list = field(default_factory=list)         # list[dict] (sorted strongest-first)
    unmatched_measured: list = field(default_factory=list)  # measured peaks with no internal candidate
    measured_peak_identity: dict = field(default_factory=dict)
    reference_identities: list = field(default_factory=list)
    radiation_compatibility: list = field(default_factory=list)
    overlap_ambiguity_count: int = 0
    comparison_status: str = "tentative_advisory"
    limitations: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    disclaimer: str = DISCLAIMER
    explanation: str = MATCH_EXPLANATION
    wording_note: str = (
        "Matches are TENTATIVELY CONSISTENT WITH an advisory possible match only. Check against "
        "an appropriate reference source and use full-pattern expert review.")

    def candidate_table(self) -> list[dict]:
        """Flat rows for the UI. The confidence column is labelled ``confidence (tentative)`` so a
        bare 'high' can never read as a confirmed identification — the underlying data key on each
        candidate (``candidates[i]["confidence"]``) is unchanged for callers/tests."""
        rows: list[dict] = []
        for c in self.candidates:
            rows.append({
                "phase": c["phase"], "formula": c["formula"],
                "confidence (tentative)": c["confidence"],
                "matched": f'{c["n_matched"]}/{c["n_reference"]}',
                "matched_2theta_deg": ", ".join(f'{m["measured"]:g}' for m in c["matched_peaks"]) or "—",
                "missing_major_2theta_deg": ", ".join(f"{x:g}" for x in c["missing_major_peaks"]) or "—",
                "reference_id": c.get("reference_id", "internal_approximate_reference"),
                "radiation compatibility": c.get("radiation_compatibility", "not_assessed"),
                "overlap ambiguity count": c.get("ambiguity_overlap_count", 0),
                "assessment": c["wording"],
            })
        return rows

    def to_dict(self) -> dict:
        return _json_safe_copy(asdict(self))


def _coerce_measured_peaks(
    measured_input: Any,
    measured_identity: dict | None,
    measured_radiation: Any,
    measured_wavelength: Any,
) -> tuple[list[dict], dict, str, float | None, list[str]]:
    warnings: list[str] = []
    if isinstance(measured_input, MeasuredXrdPattern):
        pattern = measured_input
        raw_peaks = pattern.user_peak_list
        if not raw_peaks:
            warnings.append(
                "The measured pattern has no explicit user-supplied peak list; raw scan points "
                "were not treated as peaks.")
        identity = {
            "pattern_id": pattern.pattern_id,
            "source_filename": pattern.source_filename,
            "source_sha256": pattern.source_sha256,
            "peak_list_id": _stable_record_id("xrdpeaks_", {
                "pattern_id": pattern.pattern_id, "peaks": raw_peaks,
                "selection": pattern.peak_selection_provenance,
            }),
            "peak_selection_provenance": pattern.peak_selection_provenance,
        }
        radiation = pattern.radiation_source
        wavelength = pattern.wavelength_angstrom
    else:
        raw_peaks = measured_input or []
        identity = _json_safe_copy(measured_identity or {})
        radiation = _radiation_label(measured_radiation)
        wavelength = _optional_wavelength(measured_wavelength)

    parsed: list[dict] = []
    for index, item in enumerate(raw_peaks, start=1):
        if isinstance(item, dict):
            raw_position = (item.get("two_theta_deg") if "two_theta_deg" in item
                            else item.get("two_theta", item.get("measured")))
            peak_id = item.get("peak_id") or item.get("peak_index") or index
        else:
            raw_position, peak_id = item, index
        try:
            position = _physical_two_theta(raw_position)
        except XrdDataError:
            warnings.append(f"Measured peak {index} was ignored because its 2theta is not finite/physical.")
            continue
        parsed.append({"peak_index": index, "peak_id": peak_id, "two_theta_deg": round(position, 6)})
    if "peak_list_id" not in identity:
        identity["peak_list_id"] = _stable_record_id("xrdpeaks_", {
            "provided_identity": identity, "peaks": parsed,
        })
    return parsed, identity, _radiation_label(radiation), wavelength, warnings


def _coerce_external_reference(value: Any) -> ExternalXrdReference:
    if isinstance(value, ExternalXrdReference):
        reference = value
    elif isinstance(value, dict):
        safe = _json_safe_copy(value)
        try:
            reference = ExternalXrdReference(
                reference_id=str(safe.get("reference_id") or ""),
                phase_name=str(safe.get("phase_name") or safe.get("phase") or ""),
                formula=str(safe.get("formula") or ""),
                polymorph=str(safe.get("polymorph") or safe.get("crystal_form") or ""),
                radiation_source=_radiation_label(
                    safe.get("radiation_source", safe.get("radiation"))),
                wavelength_angstrom=_optional_wavelength(
                    safe.get("wavelength_angstrom", safe.get("wavelength"))),
                peaks=list(safe.get("peaks") or []),
                source_name=str(safe.get("source_name") or ""),
                source_record_id=str(safe.get("source_record_id") or ""),
                title=str(safe.get("title") or ""),
                authors=list(safe.get("authors") or []),
                year=safe.get("year"), doi=str(safe.get("doi") or ""),
                url=str(safe.get("url") or ""),
                license_status=str(safe.get("license_status") or LICENSE_UNKNOWN),
                redistribution_permission_status=str(
                    safe.get("redistribution_permission_status") or REDISTRIBUTION_UNKNOWN),
                source_filename=str(safe.get("source_filename") or ""),
                source_sha256=str(safe.get("source_sha256") or ""),
                data_format=str(safe.get("data_format") or ""),
                notes=str(safe.get("notes") or ""),
                review_status=str(safe.get("review_status") or "needs_review"),
                original_headings=list(safe.get("original_headings") or []),
                column_mapping=dict(safe.get("column_mapping") or {}),
                rejected_rows=list(safe.get("rejected_rows") or []),
                source_metadata=dict(safe.get("source_metadata") or {}),
                warnings=list(safe.get("warnings") or []),
                created_at=str(safe.get("created_at") or _utc_now()),
                schema_version=int(safe.get("schema_version") or XRD_RECORD_SCHEMA_VERSION),
            )
        except (TypeError, ValueError) as exc:
            raise XrdDataError(f"malformed external XRD reference record: {exc}") from exc
    else:
        raise XrdDataError("references must contain ExternalXrdReference records or their dictionaries")

    if not reference.reference_id or not reference.phase_name or not reference.source_name:
        raise XrdDataError(
            "external reference identity, phase_name, and source_name provenance are required")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", reference.source_sha256):
        raise XrdDataError("external reference requires an exact 64-character source_sha256")
    if not reference.source_filename:
        raise XrdDataError("external reference requires source_filename provenance")
    parsed_peaks = []
    for index, peak in enumerate(reference.peaks, start=1):
        if not isinstance(peak, dict):
            peak = {"two_theta_deg": peak}
        try:
            position = _physical_two_theta(
                peak.get("two_theta_deg", peak.get("two_theta", peak.get("reference"))))
            intensity = _finite_number(
                peak.get("relative_intensity", peak.get("intensity")),
                field_name="reference relative intensity", allow_missing=True)
        except XrdDataError as exc:
            raise XrdDataError(f"reference peak {index}: {exc}") from exc
        parsed_peaks.append({
            **_json_safe_copy(peak),
            "two_theta_deg": position,
            "relative_intensity": intensity,
        })
    if not parsed_peaks:
        raise XrdDataError("external reference needs at least one finite physical peak position")
    return replace(
        reference,
        peaks=parsed_peaks,
        license_status=reference.license_status or LICENSE_UNKNOWN,
        redistribution_permission_status=(
            reference.redistribution_permission_status or REDISTRIBUTION_UNKNOWN),
    )


def radiation_compatibility(
    measured_radiation: Any,
    reference_radiation: Any,
    *,
    measured_wavelength_angstrom: Any = None,
    reference_wavelength_angstrom: Any = None,
) -> dict:
    """Compare radiation identities without wavelength conversion."""
    measured_label = _radiation_label(measured_radiation)
    reference_label = _radiation_label(reference_radiation)
    measured_key = _normalise_radiation(measured_label)
    reference_key = _normalise_radiation(reference_label)
    measured_wave = _optional_wavelength(measured_wavelength_angstrom)
    reference_wave = _optional_wavelength(reference_wavelength_angstrom)
    reasons: list[str] = []

    incompatible = False
    if measured_key is not None and reference_key is not None and measured_key != reference_key:
        incompatible = True
        reasons.append("radiation source labels differ")
    if measured_wave is not None and reference_wave is not None:
        delta = abs(measured_wave - reference_wave)
        if delta > WAVELENGTH_COMPATIBILITY_TOLERANCE_A:
            incompatible = True
            reasons.append(
                f"wavelengths differ by {delta:.6g} A; no wavelength conversion was performed")
    if incompatible:
        status = RADIATION_INCOMPATIBLE
    elif measured_key is not None and reference_key is not None:
        status = RADIATION_COMPATIBLE
        reasons.append("supplied radiation identities are compatible for a position comparison")
    else:
        status = RADIATION_UNKNOWN
        reasons.append(
            "radiation compatibility is unknown because one or both identities are incomplete")
    return {
        "status": status,
        "measured_radiation": measured_label,
        "reference_radiation": reference_label,
        "measured_wavelength_angstrom": measured_wave,
        "reference_wavelength_angstrom": reference_wave,
        "wavelength_conversion_performed": False,
        "reasons": reasons,
    }


def _one_to_one_peak_pairs(measured: list[dict], reference_positions: list[float], tol: float) -> list[dict]:
    possibilities = []
    for measured_index, measured_peak in enumerate(measured):
        for reference_index, reference_peak in enumerate(reference_positions):
            delta = abs(measured_peak["two_theta_deg"] - reference_peak)
            if delta <= tol:
                possibilities.append((delta, measured_index, reference_index))
    used_measured: set[int] = set()
    used_reference: set[int] = set()
    matched = []
    for delta, measured_index, reference_index in sorted(possibilities):
        if measured_index in used_measured or reference_index in used_reference:
            continue
        used_measured.add(measured_index)
        used_reference.add(reference_index)
        measured_value = measured[measured_index]["two_theta_deg"]
        reference_value = reference_positions[reference_index]
        matched.append({
            "measured_peak_index": measured_index,
            "reference_peak_index": reference_index,
            "measured": measured_value,
            "reference": reference_value,
            "delta": round(delta, 6),
            "measured_two_theta_deg": measured_value,
            "reference_two_theta_deg": reference_value,
            "delta_deg": round(delta, 6),
            "signed_delta_deg": round(measured_value - reference_value, 6),
        })
    return sorted(matched, key=lambda pair: pair["reference_peak_index"])


def _generic_references(references: Any) -> tuple[list[dict], bool]:
    if references is None:
        return ([{
            "key": key,
            "phase": ref.name,
            "formula": ref.formula,
            "polymorph": "",
            "positions": list(ref.main_2theta),
            "intensities": [None] * len(ref.main_2theta),
            "dominant": _DOMINANT_2THETA.get(key),
            "external": False,
            "reference_id": f"internal_approximate_{key}",
            "identity": {"reference_id": f"internal_approximate_{key}"},
            "provenance": {"basis": PEAK_BASIS},
            "radiation_source": "Cu Kalpha",
            "wavelength_angstrom": CU_KALPHA_WAVELENGTH_A,
        } for key, ref in _REFERENCE.items()], False)

    values = references if isinstance(references, (list, tuple)) else [references]
    generic = []
    seen_reference_ids: set[str] = set()
    for value in values:
        ref = _coerce_external_reference(value)
        if ref.reference_id in seen_reference_ids:
            raise XrdDataError(f"duplicate external reference identity: {ref.reference_id}")
        seen_reference_ids.add(ref.reference_id)
        intensities = [peak.get("relative_intensity") for peak in ref.peaks]
        finite_intensities = [
            (index, intensity) for index, intensity in enumerate(intensities)
            if isinstance(intensity, (int, float)) and math.isfinite(float(intensity))
        ]
        dominant = None
        if finite_intensities:
            dominant_index = max(finite_intensities, key=lambda item: item[1])[0]
            dominant = ref.peaks[dominant_index]["two_theta_deg"]
        generic.append({
            "key": ref.reference_id,
            "phase": ref.phase_name,
            "formula": ref.formula,
            "polymorph": ref.polymorph,
            "positions": ref.reference_2theta,
            "intensities": intensities,
            "dominant": dominant,
            "external": True,
            "reference_id": ref.reference_id,
            "identity": ref.identity(),
            "provenance": ref.provenance(),
            "radiation_source": ref.radiation_source,
            "wavelength_angstrom": ref.wavelength_angstrom,
        })
    return generic, True


def match_measured_peaks(
    measured_2theta,
    tolerance=DEFAULT_MATCH_TOLERANCE_DEG,
    *,
    references=None,
    measured_identity: dict | None = None,
    measured_radiation: Any = None,
    measured_wavelength_angstrom: Any = None,
) -> XrdMatchResult:
    """Compare measured 2θ positions to the internal references → TENTATIVE candidate phases.

    ``measured_2theta`` is a list of degrees-2θ (numbers or numeric strings); ``tolerance`` is the
    half-window in degrees (default :data:`DEFAULT_MATCH_TOLERANCE_DEG`). Confidence is deliberately
    cautious and **cannot** reach ``high`` from a single matched peak:

    * ``low`` — 0–1 matched peaks (a single peak is always low), or all matches ambiguous;
    * ``medium`` — 2 matched peaks, or 3+ with ambiguity / a missing dominant peak;
    * ``high`` — 3+ matched peaks, at least 2 of them unique (not overlapping another candidate), a
      high matched fraction, and the dominant reflection present — and still only *tentative*.
    """
    measured_peaks, peak_identity, measured_radiation_label, measured_wavelength, input_warnings = (
        _coerce_measured_peaks(
            measured_2theta, measured_identity, measured_radiation,
            measured_wavelength_angstrom))
    measured = [peak["two_theta_deg"] for peak in measured_peaks]
    tol = _to_float(tolerance)
    if tol is None or not math.isfinite(tol) or tol <= 0:
        tol = DEFAULT_MATCH_TOLERANCE_DEG

    if not measured:
        return XrdMatchResult(
            tolerance_deg=tol,
            measured_peak_identity=peak_identity,
            warnings=input_warnings + [
                "No measured 2theta peaks are available for advisory matching."])

    generic_references, external = _generic_references(references)
    raw_candidates = []
    radiation_checks = []
    for ref in generic_references:
        matched = _one_to_one_peak_pairs(measured_peaks, ref["positions"], tol)
        if not matched and not external:
            continue
        if external:
            compatibility = radiation_compatibility(
                measured_radiation_label, ref["radiation_source"],
                measured_wavelength_angstrom=measured_wavelength,
                reference_wavelength_angstrom=ref["wavelength_angstrom"])
        else:
            compatibility = {
                "status": RADIATION_COMPATIBLE,
                "measured_radiation": "legacy internal Cu Kalpha basis",
                "reference_radiation": "Cu Kalpha",
                "measured_wavelength_angstrom": None,
                "reference_wavelength_angstrom": CU_KALPHA_WAVELENGTH_A,
                "wavelength_conversion_performed": False,
                "reasons": ["legacy internal-reference matcher uses its documented Cu Kalpha basis"],
            }
        radiation_checks.append({
            "reference_id": ref["reference_id"],
            **compatibility,
        })
        raw_candidates.append((ref, matched, compatibility))

    # Which measured peaks are claimed by more than one candidate phase (overlap / ambiguity)?
    claims: dict = {}
    for ref, matched, compatibility in raw_candidates:
        for m in matched:
            claims.setdefault(m["measured_peak_index"], set()).add(ref["reference_id"])
    overlap_count = sum(1 for claimants in claims.values() if len(claimants) > 1)

    candidates = []
    limitations = [
        "Position-only comparison does not account for background, instrument broadening, "
        "preferred orientation, solid-solution shifts, or full-pattern fit quality.",
        "Amorphous content may raise the background without producing reference peaks.",
        "No wavelength conversion is performed.",
    ]
    for ref, matched, compatibility in raw_candidates:
        n_matched = len(matched)
        n_ref = len(ref["positions"])
        unique = sum(
            1 for m in matched if len(claims.get(m["measured_peak_index"], ())) == 1)
        frac = n_matched / n_ref if n_ref else 0.0
        matched_reference_indices = {m["reference_peak_index"] for m in matched}
        missing = [
            position for index, position in enumerate(ref["positions"])
            if index not in matched_reference_indices]
        dominant = ref["dominant"]
        dominant_missing = dominant is not None and all(
            pair["reference"] != dominant for pair in matched)

        if n_matched <= 1:
            conf = CONFIDENCE_LOW
        elif n_matched == 2:
            conf = CONFIDENCE_MEDIUM
        else:
            conf = CONFIDENCE_HIGH if (unique >= 2 and frac >= 0.66) else CONFIDENCE_MEDIUM
        if conf == CONFIDENCE_HIGH and dominant_missing:
            conf = CONFIDENCE_MEDIUM        # never 'high' without the dominant reflection present
        stronger_comparison_blocked = compatibility["status"] != RADIATION_COMPATIBLE
        if stronger_comparison_blocked:
            conf = CONFIDENCE_LOW

        ambiguity_count = sum(
            1 for pair in matched if len(claims.get(pair["measured_peak_index"], ())) > 1)
        formula_polymorph_caution = bool(ref["formula"] and not ref["polymorph"] and ref["external"])
        if formula_polymorph_caution and conf == CONFIDENCE_HIGH:
            conf = CONFIDENCE_MEDIUM

        note_bits = []
        if dominant_missing:
            note_bits.append(f"dominant {dominant:g}° peak not in your list")
        elif dominant is None:
            note_bits.append(
                "reference intensities were not supplied, so a dominant-peak check is unavailable")
        if matched and unique == 0:
            note_bits.append("all matched peaks overlap other candidates (ambiguous)")
        if stronger_comparison_blocked:
            note_bits.append(
                f"radiation compatibility is {compatibility['status']}; stronger comparison is blocked")
        if formula_polymorph_caution:
            note_bits.append(
                "a formula alone cannot establish a polymorph; check the crystal form explicitly")
        matched_measured_indices = {pair["measured_peak_index"] for pair in matched}
        candidate_unmatched_measured = [
            peak["two_theta_deg"] for index, peak in enumerate(measured_peaks)
            if index not in matched_measured_indices]
        candidates.append({
            "phase": ref["phase"], "formula": ref["formula"], "polymorph": ref["polymorph"],
            "key": ref["key"], "confidence": conf, "tentative_confidence": conf,
            "wording": f"{ref['phase']}: {CONFIDENCE_WORDING[conf]}", "n_matched": n_matched,
            "n_reference": n_ref, "unique_matches": unique, "matched_peaks": matched,
            "missing_major_peaks": missing, "dominant_missing": dominant_missing,
            "dominant_reference_2theta_deg": dominant,
            "dominant_peak_warning": (
                f"Dominant reference peak {dominant:g}° was not matched."
                if dominant_missing else (
                    "Dominant reference peak was matched."
                    if dominant is not None else
                    "Dominant-peak check unavailable because relative intensities were not supplied.")),
            "unmatched_reference_peaks": missing,
            "unmatched_measured_peaks": candidate_unmatched_measured,
            "ambiguity_overlap_count": ambiguity_count,
            "radiation_compatibility": compatibility["status"],
            "radiation_compatibility_detail": compatibility,
            "stronger_comparison_blocked": stronger_comparison_blocked,
            "wavelength_conversion_performed": False,
            "reference_id": ref["reference_id"],
            "reference_identity": ref["identity"],
            "reference_provenance": ref["provenance"],
            "formula_polymorph_caution": formula_polymorph_caution,
            "limitations": limitations + ([
                "Radiation identity is unknown or incompatible; positional similarity cannot "
                "support a stronger comparison."
            ] if stronger_comparison_blocked else []) + ([
                "Formula does not uniquely establish the polymorph/crystal form."
            ] if formula_polymorph_caution else []),
            "note": "; ".join(note_bits) or (
                "position-only tentative advisory possible match; check against an appropriate "
                "reference source."),
        })

    rank = {CONFIDENCE_HIGH: 3, CONFIDENCE_MEDIUM: 2, CONFIDENCE_LOW: 1}
    candidates.sort(key=lambda c: (rank[c["confidence"]], c["n_matched"]), reverse=True)

    unmatched = [
        peak["two_theta_deg"] for index, peak in enumerate(measured_peaks) if index not in claims]
    reference_identities = [ref["identity"] for ref in generic_references]
    result_warnings = input_warnings + _match_warnings(
        candidates, unmatched, tol, external=external, radiation_checks=radiation_checks)
    return XrdMatchResult(
        tolerance_deg=tol,
        measured_2theta=measured,
        candidates=candidates,
        unmatched_measured=unmatched,
        measured_peak_identity=peak_identity,
        reference_identities=reference_identities,
        radiation_compatibility=radiation_checks,
        overlap_ambiguity_count=overlap_count,
        comparison_status="tentative_advisory_possible_match",
        limitations=limitations,
        warnings=result_warnings,
        disclaimer=(
            "Tentative advisory possible matches only; check against an appropriate reference "
            "source. This position comparison is not a phase conclusion."
            if external else DISCLAIMER),
        explanation=(
            "Measured peaks were compared with user-supplied external references using one "
            "position-only advisory matcher. Exact source and radiation provenance are retained."
            if external else MATCH_EXPLANATION),
    )


def _match_warnings(candidates, unmatched, tol, *, external=False, radiation_checks=None) -> list[str]:
    """The standing caution set for tentative measured-peak matching."""
    if external:
        warns = [
            f"Tentative advisory position comparison at ±{tol:g}° 2theta; check every possible "
            "match against an appropriate reference source and the full measured pattern.",
            "A one-peak possible match is weak because peaks can overlap; several characteristic "
            "peaks and expert full-pattern review are needed for a stronger interpretation.",
            "Amorphous content, background, preferred orientation, broadening, and solid-solution "
            "shifts remain limitations.",
            "No wavelength conversion was performed.",
        ]
        checks = radiation_checks or []
        if any(check["status"] == RADIATION_INCOMPATIBLE for check in checks):
            warns.append(
                "At least one reference has incompatible radiation; stronger comparison is blocked.")
        if any(check["status"] == RADIATION_UNKNOWN for check in checks):
            warns.append(
                "At least one radiation identity is unknown; stronger comparison is blocked.")
        if any(candidate.get("formula_polymorph_caution") for candidate in candidates):
            warns.append(
                "A formula alone cannot establish a polymorph; check the supplied crystal form "
                "against an appropriate reference source.")
        if any(candidate.get("ambiguity_overlap_count", 0) for candidate in candidates):
            warns.append(
                "Overlapping reference positions remain ambiguous and are counted explicitly.")
        if unmatched:
            warns.append(
                "Unmatched measured peaks remain uninterpreted: "
                + ", ".join(f"{value:g}" for value in unmatched) + ".")
        return warns

    warns = [
        f"Matching is TENTATIVE and position-only at ±{tol:g}° 2θ — it is NOT a phase identification. "
        "Confirm with measured reference patterns (ICDD PDF) and full-pattern (Rietveld) fitting.",
        "A single matched peak is weak evidence — peaks overlap (especially 26–35° 2θ), so several "
        "characteristic peaks are needed before a phase is even a strong candidate.",
        "Only the small internal reference set is searched — a real sample may contain phases not "
        "listed here, and amorphous content shows no peaks at all.",
    ]
    if unmatched:
        warns.append("Measured peaks with no candidate in the internal table: "
                     + ", ".join(f"{x:g}" for x in unmatched)
                     + " — these need external reference data to interpret.")
    if any(c["confidence"] == CONFIDENCE_HIGH for c in candidates):
        warns.append("Even a 'high' tentative match stays 'tentatively consistent with', never "
                     "'identified' — quantitative confirmation needs reference standards.")
    return warns


# --------------------------------------------------------------------------- #
# Mode 4 — Reference Data Notes (coverage of the internal approximate table).
# --------------------------------------------------------------------------- #
def reference_data_notes() -> dict:
    """What the internal approximate table covers, and what needs external reference data later.

    Returns a plain dict. The internal peaks are explicitly framed as teaching/advisory references,
    not certified standards; phases outside the small set are listed by NAME only (no fabricated
    peaks) as needing external reference data (CIF / ICDD PDF / a library such as pymatgen).
    """
    covered = [{"phase": ref.name, "formula": ref.formula,
                "n_reference_peaks": len(ref.main_2theta),
                "dominant_2theta_deg": _DOMINANT_2THETA.get(key),
                "approx_2theta_deg": list(ref.main_2theta), "note": ref.note}
               for key, ref in _REFERENCE.items()]
    return {
        "covered_phases": covered,
        "covered_count": len(covered),
        "needs_external_reference": list(_NEEDS_EXTERNAL_REFERENCE),
        "peak_basis": PEAK_BASIS,
        "explanation": REFERENCE_NOTES_EXPLANATION,
        "note": ("The internal peaks are TEACHING / ADVISORY approximate references (a few principal "
                 "Cu Kα reflections each), NOT certified phase-identification standards. They plan a "
                 "measurement; they do not identify phases."),
        "future_work": ("External reference data (ICDD PDF cards or CIF files, optionally via a "
                        "library such as pymatgen) would be needed for full-pattern / quantitative "
                        "work and for phases outside this small set. Not used yet."),
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- #
# Prompt → mode classification (deterministic; used by the instrument router).
# --------------------------------------------------------------------------- #
_TWO_THETA_TOKEN_RE = re.compile(r"2\s*-?\s*(?:theta|θ)", re.I)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_MEASURED_HINT_RE = re.compile(
    r"(measured|observed|might\s+match|possible\s+phase|what\s+phase|which\s+phase|\bmatch\w*\b|"
    r"\bpeaks?\b)", re.I)
_PHREEQC_HINT_RE = re.compile(r"\b(phreeqc|saturat\w*|predicted|prediction\w*|precipitat\w*)\b", re.I)
_CONTEXT_HINT_RE = re.compile(
    r"\b(after|leach\w*|naoh|koh|fly\s*ash|flyash|fli\s*ash|class\s*[cf])\b", re.I)
_REFNOTES_HINT_RE = re.compile(
    r"(reference\s+data|coverage|covered\s+by|external\s+reference|cif\b|icdd\b|pymatgen|"
    r"(?:which|what)\s+phases?[^?.]{0,40}(?:cover|reference\s+table|in\s+the\s+table))", re.I)


def _extract_two_theta(text) -> list:
    """Pull plausible 2θ values (2–90°) from free text, ignoring the literal '2theta'/'2θ' token."""
    cleaned = _TWO_THETA_TOKEN_RE.sub(" ", str(text or ""))
    vals = []
    for m in _NUMBER_RE.finditer(cleaned):
        try:
            v = float(m.group())
        except ValueError:
            continue
        if 2.0 <= v <= 90.0:
            vals.append(round(v, 3))
    return vals


def _extract_phase_names(text) -> list:
    """Find known reference phase NAMES/synonyms mentioned in free text (deterministic, de-duped)."""
    low = str(text or "").lower()
    found, seen = [], set()
    for token in sorted(_SYNONYMS, key=len, reverse=True):       # longest first (multi-word names)
        if re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", low):
            name = _REFERENCE[_SYNONYMS[token]].name
            if name not in seen:
                seen.add(name)
                found.append(name)
    return found


def classify_request(text) -> dict:
    """Map a free-text XRD prompt to one of the v2 modes + extract its inputs (deterministic).

    Returns ``{"mode", "measured_2theta", "phases", "rationale"}``. Priority: explicit measured 2θ
    peaks → matching; a PHREEQC/saturation reference → the PHREEQC checklist; a reference-coverage
    question → reference notes; a leaching/fly-ash context (with no named phases) → a context
    checklist; otherwise expected peaks for the named phases.
    """
    raw = str(text or "")
    low = raw.lower()
    measured = _extract_two_theta(raw)
    phases = _extract_phase_names(raw)

    if measured and (_TWO_THETA_TOKEN_RE.search(raw) or _MEASURED_HINT_RE.search(low)):
        return {"mode": MODE_MATCH_MEASURED, "measured_2theta": measured, "phases": phases,
                "rationale": "Measured 2θ peaks given with a 'what phases might match' framing."}
    if _PHREEQC_HINT_RE.search(low):
        return {"mode": MODE_PHREEQC_CHECKLIST, "measured_2theta": [], "phases": phases,
                "rationale": "Prompt references PHREEQC-predicted / saturated phases to check by XRD."}
    if _REFNOTES_HINT_RE.search(low):
        return {"mode": MODE_REFERENCE_NOTES, "measured_2theta": [], "phases": phases,
                "rationale": "Prompt asks which phases the internal reference table covers."}
    if _CONTEXT_HINT_RE.search(low) and not phases:
        return {"mode": MODE_CONTEXT_CHECKLIST, "measured_2theta": [], "phases": phases,
                "rationale": "Leaching / fly-ash context — an advisory phase checklist to plan XRD."}
    return {"mode": MODE_EXPECTED_PEAKS, "measured_2theta": [], "phases": phases,
            "rationale": "Named / expected phases — list approximate reference peaks (advisory)."}
