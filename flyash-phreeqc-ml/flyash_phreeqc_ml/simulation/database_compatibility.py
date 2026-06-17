"""PHREEQC **database compatibility** checks for the Simulate workflow.

A PHREEQC prediction is only as good as the thermodynamic database. The same input run with
``phreeqc.dat`` and with a cementitious database (CEMDATA18) gives different precipitation /
saturation-index results — and many cement/fly-ash phases simply **do not exist** in the
general databases. This module inspects the *configured* database (the one the executor will
use) and reports, transparently, which phases it actually defines.

It is conservative and honest:

* It **reads** the configured database (never ships one — CEMDATA18 is not redistributable).
* It **never pretends a missing phase exists**: a phase is "available" only if its exact name
  appears at the start of a line in the database text (``^Calcite\\b`` matches ``Calcite`` but
  not ``Calcite_xyz``).
* It is **pure** — no PHREEQC execution, no AI, no comparison / result-path module. It just
  reads text and reports.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import config

# --------------------------------------------------------------------------- #
# Database families + compatibility levels
# --------------------------------------------------------------------------- #
FAMILY_PHREEQC = "phreeqc"
FAMILY_LLNL = "llnl"
FAMILY_WATEQ = "wateq"
FAMILY_MINTEQ = "minteq"
FAMILY_PITZER = "pitzer"
FAMILY_SIT = "sit"
FAMILY_CEMDATA = "cemdata"
FAMILY_UNKNOWN = "unknown"

# Compatibility relative to a requested phase template.
LEVEL_UNKNOWN = "unknown"                    # no readable database
LEVEL_BASIC_AQUEOUS = "basic_aqueous_only"   # DB present but none of the template's phases
LEVEL_PARTIAL = "partial"                    # some of the template's phases present
LEVEL_SUITABLE = "suitable_for_template"     # all of the template's phases present

# Name fragments (lower-case) → family. Checked against the file name first, then the header.
_FAMILY_MARKERS = (
    ("cemdata", FAMILY_CEMDATA), ("llnl", FAMILY_LLNL), ("lawrence livermore", FAMILY_LLNL),
    ("wateq", FAMILY_WATEQ), ("minteq", FAMILY_MINTEQ), ("pitzer", FAMILY_PITZER),
    ("sit.dat", FAMILY_SIT), ("phreeqc", FAMILY_PHREEQC),
)


# --------------------------------------------------------------------------- #
# Structures
# --------------------------------------------------------------------------- #
@dataclass
class DatabaseInfo:
    database_path: str | None
    database_label: str
    database_exists: bool
    detected_family: str


@dataclass
class PhaseAvailability:
    phase: str
    available: bool


@dataclass
class DatabaseCompatibilityReport:
    database_path: str | None
    database_label: str
    database_exists: bool
    detected_family: str
    available_phases: list = field(default_factory=list)      # list[str]
    missing_phases: list = field(default_factory=list)        # list[str]
    phase_availability: list = field(default_factory=list)    # list[PhaseAvailability]
    warnings: list = field(default_factory=list)              # list[str]
    compatibility_level: str = LEVEL_UNKNOWN

    @property
    def precipitation_meaningful(self) -> bool:
        """True only when the database exists AND defines at least one requested phase."""
        return self.database_exists and bool(self.available_phases)

    def info(self) -> DatabaseInfo:
        return DatabaseInfo(self.database_path, self.database_label, self.database_exists,
                            self.detected_family)


# --------------------------------------------------------------------------- #
# Reading + detection
# --------------------------------------------------------------------------- #
def _resolve_path(database: str | None) -> str | None:
    db = database if database is not None else config.PHREEQC_DATABASE_PATH
    return str(db) if db else None


def read_database_text(database: str | None = None) -> str | None:
    """The configured (or given) database file text, or ``None`` if absent/unreadable."""
    path = _resolve_path(database)
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def detect_family(database: str | None = None, *, text: str | None = None) -> str:
    """Best-effort database family from the file name, then the file header."""
    path = _resolve_path(database)
    name = Path(path).name.lower() if path else ""
    for frag, fam in _FAMILY_MARKERS:
        if frag in name:
            return fam
    if text is None:
        text = read_database_text(database)
    if text:
        head = text[:4000].lower()
        for frag, fam in _FAMILY_MARKERS:
            if frag in head:
                return fam
    return FAMILY_UNKNOWN


def phase_present(phase: str, text: str | None) -> bool:
    """True if ``phase`` is defined at column 0 in the database text (exact name match).

    Anchored on whitespace / end-of-line (not ``\\b``) so phase names ending in a non-word
    character (``SiO2(a)``, ``Fe(OH)3(a)``) match correctly, while ``Cal`` still does not match
    inside ``Calcite``.
    """
    if not text:
        return False
    return bool(re.search(rf"(?m)^{re.escape(str(phase))}(?=\s|$)", text))


def check_phases(phases, database: str | None = None, *,
                 text: str | None = None) -> list[PhaseAvailability]:
    """One :class:`PhaseAvailability` per requested phase (never raises)."""
    if text is None:
        text = read_database_text(database)
    return [PhaseAvailability(str(p), phase_present(p, text or "")) for p in (phases or [])]


def database_defines_phases(phases, database: str | None = None) -> bool:
    """True only if the configured/given database defines **every** name in ``phases``."""
    text = read_database_text(database)
    if text is None:
        return False
    return all(phase_present(p, text) for p in phases)


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def build_report(database: str | None = None, *,
                 expected_phases=None) -> DatabaseCompatibilityReport:
    """Inspect the configured (or given) database and report its compatibility.

    ``expected_phases`` is the phase template the user wants. When the database is missing the
    report is honest (everything ``missing``, level ``unknown``); when it is present the report
    lists exactly which requested phases it defines and which it does not — never claiming a
    phase exists that does not.
    """
    path = _resolve_path(database)
    text = read_database_text(database)
    exists = text is not None
    family = detect_family(database, text=text)
    label = Path(path).name if path else "(none configured)"
    expected = [str(p) for p in (expected_phases or [])]

    report = DatabaseCompatibilityReport(
        database_path=path, database_label=label, database_exists=exists,
        detected_family=family)

    if not exists:
        report.compatibility_level = LEVEL_UNKNOWN
        report.missing_phases = list(expected)          # unverified → treated as not-confirmed
        report.phase_availability = [PhaseAvailability(p, False) for p in expected]
        report.warnings.append(
            "No PHREEQC database is configured or readable — phase availability cannot be "
            "verified, so precipitation / saturation-index prediction is unverified. Set "
            "PHREEQC_DATABASE (and prefer a cementitious database such as CEMDATA18 for high-pH "
            "fly-ash / cement systems).")
        return report

    if expected:
        avail = check_phases(expected, text=text)
        report.phase_availability = avail
        report.available_phases = [a.phase for a in avail if a.available]
        report.missing_phases = [a.phase for a in avail if not a.available]
        if report.missing_phases:
            report.warnings.append(
                f"`{label}` does not define: {', '.join(report.missing_phases)} — these phases "
                "are skipped (no precipitation / SI is predicted for them).")
        if not report.available_phases:
            report.warnings.append(
                f"`{label}` defines none of the requested phases — precipitation / SI prediction "
                "is effectively aqueous-only. For high-pH cement/fly-ash phases use CEMDATA18.")

    report.compatibility_level = _level(report, bool(expected))
    if family in (FAMILY_PHREEQC, FAMILY_LLNL, FAMILY_WATEQ, FAMILY_MINTEQ) and expected:
        report.warnings.append(
            f"`{label}` is a general-purpose database (family: {family}); cement/fly-ash phases "
            "(Portlandite, Ettringite, C-S-H, …) are typically absent. It is fine for a smoke "
            "test, weak for cementitious high-pH predictions.")
    return report


def _level(report: DatabaseCompatibilityReport, had_expected: bool) -> str:
    if not report.database_exists:
        return LEVEL_UNKNOWN
    if not had_expected:
        return LEVEL_BASIC_AQUEOUS                       # aqueous-only mode: no phases requested
    if report.available_phases and not report.missing_phases:
        return LEVEL_SUITABLE
    if report.available_phases:
        return LEVEL_PARTIAL
    return LEVEL_BASIC_AQUEOUS                            # DB has none of the requested phases
