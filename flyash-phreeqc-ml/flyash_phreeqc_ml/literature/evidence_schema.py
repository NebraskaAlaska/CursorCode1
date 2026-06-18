"""Structured **evidence** schemas for values extracted from scholarly papers (pure data).

An evidence row is *what a paper reports* for one experiment, captured as structured fields with
**provenance and confidence** — never as bare "truth". Two schemas are supported:

* :class:`LeachingEvidence` — leaching / geochemistry papers (material, leachant, conditions,
  measured pH + element releases, analytical method).
* :class:`CompositeEvidence` — plastic / composite / mechanical papers (binder, plastic type/form,
  dosage, curing, compressive / flexural strength, density, water absorption, durability).

Hard rules baked into the schema:

* **Every row must carry provenance** (a DOI, or a title + source API) — enforced by
  :meth:`Evidence.has_provenance` and by ``evidence_store`` on save. A row with no source is not
  evidence.
* **Missing values stay ``None``** — never 0, never a guess.
* **Confidence is explicit** — a per-row ``extraction_confidence`` (0–1, banded high/medium/low),
  an ``extraction_scope`` (``abstract`` / ``full_text`` / ``manual``), optional ``field_confidence``
  per field, and a ``conflicts`` list for flagged conflicting values.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Evidence schema kinds.
SCHEMA_LEACHING = "leaching"
SCHEMA_COMPOSITE = "composite"
SCHEMA_KINDS = (SCHEMA_LEACHING, SCHEMA_COMPOSITE)

# Extraction scope — how much of the paper the values came from (abstract-only is weaker).
SCOPE_ABSTRACT = "abstract"
SCOPE_FULL_TEXT = "full_text"
SCOPE_MANUAL = "manual"

# Extraction status (how the extraction went — honest about why a row may be empty).
STATUS_OK = "ok"
STATUS_NO_TEXT = "no_text"            # no abstract/full text to extract from
STATUS_AI_OFF = "ai_unavailable"     # AI disabled — values must be entered manually
STATUS_AI_FAILED = "ai_failed"       # AI call failed / returned unusable output
STATUS_MANUAL = "manual"             # entered by the user

# Confidence bands.
CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"
HIGH_THRESHOLD = 0.75
MEDIUM_THRESHOLD = 0.45
# Abstract-only extraction can never claim more than MEDIUM confidence (not full experimental data).
ABSTRACT_CONFIDENCE_CAP = MEDIUM_THRESHOLD

# The leaching element set this project tracks (others are recorded in extra notes, not invented).
LEACHING_ELEMENTS = ("Ca", "Si", "Al", "Fe", "Na", "K", "Sc", "REE")


def confidence_band(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return CONF_LOW
    if f >= HIGH_THRESHOLD:
        return CONF_HIGH
    if f >= MEDIUM_THRESHOLD:
        return CONF_MEDIUM
    return CONF_LOW


@dataclass
class Provenance:
    """Where an evidence row came from (a citation). At least a DOI or a title is required."""

    source: str = ""                 # which API / "manual"
    doi: str | None = None
    title: str | None = None
    url: str | None = None
    authors: list = field(default_factory=list)
    year: int | None = None
    query: str | None = None         # the search query that surfaced the paper

    @property
    def is_present(self) -> bool:
        return bool(self.doi) or bool((self.title or "").strip())

    def citation(self) -> str:
        who = (self.authors[0] + " et al." if len(self.authors) > 1
               else (self.authors[0] if self.authors else "Unknown"))
        bits = [who]
        if self.year:
            bits.append(f"({self.year})")
        if self.title:
            bits.append(self.title)
        if self.doi:
            bits.append(f"https://doi.org/{self.doi}")
        return " ".join(bits)

    def to_dict(self) -> dict:
        return {"source": self.source, "doi": self.doi, "title": self.title, "url": self.url,
                "authors": list(self.authors), "year": self.year, "query": self.query,
                "citation": self.citation()}


@dataclass
class Evidence:
    """Common provenance + confidence carried by every evidence row."""

    provenance: Provenance = field(default_factory=Provenance)
    extraction_confidence: float = 0.0
    extraction_scope: str = SCOPE_ABSTRACT
    extraction_status: str = STATUS_OK
    field_confidence: dict = field(default_factory=dict)     # field -> 0..1
    conflicts: list = field(default_factory=list)            # flagged conflicting fields
    notes: str | None = None

    @property
    def has_provenance(self) -> bool:
        return self.provenance is not None and self.provenance.is_present

    @property
    def confidence_label(self) -> str:
        # Reflect the abstract-capped confidence (an abstract-only row can't be "high").
        return confidence_band(self._capped_confidence())

    def _capped_confidence(self) -> float:
        """Abstract-only extraction is capped (it is not full experimental data)."""
        c = max(0.0, min(1.0, float(self.extraction_confidence or 0.0)))
        if self.extraction_scope == SCOPE_ABSTRACT:
            return min(c, ABSTRACT_CONFIDENCE_CAP)
        return c


@dataclass
class LeachingEvidence(Evidence):
    """A leaching / geochemistry paper's reported experiment (values: ``None`` when not stated)."""

    schema_kind: str = SCHEMA_LEACHING
    material: str | None = None
    material_class: str | None = None         # e.g. "Class C fly ash" / source / class
    composition: str | None = None            # reported bulk composition (free text)
    leachant: str | None = None
    concentration_M: float | None = None
    solid_mass_g: float | None = None
    liquid_volume_mL: float | None = None
    ls_ratio: float | None = None
    time_min: float | None = None
    temperature_C: float | None = None
    pH: float | None = None
    elements_measured: list = field(default_factory=list)
    element_values_mM: dict = field(default_factory=dict)    # {"Ca": 5.2, "Si": None, ...} mM
    analytical_method: str | None = None       # ICP-OES / ICP-MS / ...
    filtration: str | None = None

    def to_row(self) -> dict:
        d = asdict(self)
        d["provenance"] = self.provenance.to_dict()
        d["confidence_label"] = self.confidence_label
        d["extraction_confidence"] = round(self._capped_confidence(), 3)
        d["citation"] = self.provenance.citation()
        return d


@dataclass
class CompositeEvidence(Evidence):
    """A plastic / composite / mechanical paper's reported experiment (values ``None`` when absent)."""

    schema_kind: str = SCHEMA_COMPOSITE
    material_binder: str | None = None
    plastic_type: str | None = None            # PET / HDPE / PP / ...
    plastic_form: str | None = None            # fibre / flake / pellet / powder
    plastic_particle_size: str | None = None
    plastic_dosage: str | None = None          # % or ratio (free text — units vary)
    water_binder_ratio: float | None = None
    activator_cement_content: str | None = None
    curing_time: str | None = None             # e.g. "28 days"
    specimen_geometry: str | None = None
    compressive_strength_MPa: float | None = None
    flexural_strength_MPa: float | None = None
    density_kg_m3: float | None = None
    water_absorption_pct: float | None = None
    durability_observations: str | None = None

    def to_row(self) -> dict:
        d = asdict(self)
        d["provenance"] = self.provenance.to_dict()
        d["confidence_label"] = self.confidence_label
        d["extraction_confidence"] = round(self._capped_confidence(), 3)
        d["citation"] = self.provenance.citation()
        return d


# Ordered columns for the CSV export / evidence table (per schema).
LEACHING_COLUMNS = (
    "material", "material_class", "leachant", "concentration_M", "solid_mass_g", "liquid_volume_mL",
    "ls_ratio", "time_min", "temperature_C", "pH", "elements_measured", "element_values_mM",
    "analytical_method", "filtration", "composition", "extraction_scope", "extraction_status",
    "extraction_confidence", "confidence_label", "conflicts", "notes", "citation")
COMPOSITE_COLUMNS = (
    "material_binder", "plastic_type", "plastic_form", "plastic_particle_size", "plastic_dosage",
    "water_binder_ratio", "activator_cement_content", "curing_time", "specimen_geometry",
    "compressive_strength_MPa", "flexural_strength_MPa", "density_kg_m3", "water_absorption_pct",
    "durability_observations", "extraction_scope", "extraction_status", "extraction_confidence",
    "confidence_label", "conflicts", "notes", "citation")


def columns_for(schema_kind: str) -> tuple:
    return LEACHING_COLUMNS if schema_kind == SCHEMA_LEACHING else COMPOSITE_COLUMNS
