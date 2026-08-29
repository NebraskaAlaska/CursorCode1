"""Structured **evidence** schemas for values extracted from scholarly papers (pure data).

An evidence row is *what a paper reports* for one experiment, captured as structured fields with
**provenance and confidence** — never as bare "truth". Two schemas are supported:

* :class:`LeachingEvidence` — leaching / geochemistry papers (material, leachant, conditions,
  measured pH + element releases, analytical method).
* :class:`CompositeEvidence` — plastic / composite / mechanical papers (binder, plastic type/form,
  dosage, curing, compressive / flexural strength, density, water absorption, durability).

Hard rules baked into the schema:

* **Every row must carry provenance** (a DOI, or a title + source/provider) — enforced by
  :meth:`Evidence.has_provenance` and by ``evidence_store`` on save. A row with no source is not
  evidence.
* **Missing values stay ``None``** — never 0, never a guess.
* **Confidence is explicit** — a per-row ``extraction_confidence`` (0–1, banded high/medium/low),
  an ``extraction_scope`` (``abstract`` / ``full_text`` / ``manual``), optional ``field_confidence``
  per field, and a ``conflicts`` list for flagged conflicting values.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

# Evidence schema kinds.
SCHEMA_LEACHING = "leaching"
SCHEMA_COMPOSITE = "composite"
SCHEMA_KINDS = (SCHEMA_LEACHING, SCHEMA_COMPOSITE)

# Extraction scope — how much of the paper the values came from (abstract-only is weaker).
SCOPE_ABSTRACT = "abstract"
SCOPE_FULL_TEXT = "full_text"
SCOPE_MANUAL = "manual"
EXTRACTION_SCOPES = (SCOPE_ABSTRACT, SCOPE_FULL_TEXT, SCOPE_MANUAL)

# Extraction status (how the extraction went — honest about why a row may be empty).
STATUS_OK = "ok"
STATUS_NO_TEXT = "no_text"            # no abstract/full text to extract from
STATUS_AI_OFF = "ai_unavailable"     # AI disabled — values must be entered manually
STATUS_AI_FAILED = "ai_failed"       # AI call failed / returned unusable output
STATUS_MANUAL = "manual"             # entered by the user
EXTRACTION_STATUSES = (STATUS_OK, STATUS_NO_TEXT, STATUS_AI_OFF, STATUS_AI_FAILED, STATUS_MANUAL)

# Human-review lifecycle. AI extraction always starts ``needs_review``; manual
# entry starts ``draft`` and only an explicit review action may produce
# ``reviewed`` or ``rejected``.
REVIEW_DRAFT = "draft"
REVIEW_NEEDS_REVIEW = "needs_review"
REVIEW_REVIEWED = "reviewed"
REVIEW_REJECTED = "rejected"
REVIEW_STATUSES = (REVIEW_DRAFT, REVIEW_NEEDS_REVIEW, REVIEW_REVIEWED, REVIEW_REJECTED)

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
    if not math.isfinite(f):
        return CONF_LOW
    if f >= HIGH_THRESHOLD:
        return CONF_HIGH
    if f >= MEDIUM_THRESHOLD:
        return CONF_MEDIUM
    return CONF_LOW


@dataclass
class DiscoveryRoute:
    """How a citation entered the library, distinct from the citation itself.

    ``executed_queries`` contains only queries actually sent to a source. Search
    suggestions that were never run must not appear here.
    """

    method: str = ""                 # scholarly_api_search / doi / manual / upload
    source: str = ""
    query: str | None = None
    executed_queries: list = field(default_factory=list)
    record_identifier: str | None = None

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "source": self.source,
            "query": self.query,
            "executed_queries": list(self.executed_queries),
            "record_identifier": self.record_identifier,
        }


@dataclass
class SourceLocation:
    """Where the structured evidence appears within the cited source."""

    page: str | None = None
    page_range: str | None = None
    table: str | None = None
    figure: str | None = None
    section: str | None = None
    supplementary_item: str | None = None
    dataset_record_identifier: str | None = None
    note: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def summary(self) -> str:
        labelled = (
            ("page", self.page), ("pages", self.page_range), ("table", self.table),
            ("figure", self.figure), ("section", self.section),
            ("supplement", self.supplementary_item),
            ("dataset/record", self.dataset_record_identifier),
        )
        bits = [f"{label} {value}" for label, value in labelled if value not in (None, "")]
        if self.note:
            bits.append(str(self.note))
        return "; ".join(bits)


def _discovery_dict(value: DiscoveryRoute | dict | None) -> dict:
    if isinstance(value, DiscoveryRoute):
        return value.to_dict()
    if isinstance(value, dict):
        return DiscoveryRoute(
            method=str(value.get("method") or ""),
            source=str(value.get("source") or ""),
            query=value.get("query"),
            executed_queries=list(value.get("executed_queries") or []),
            record_identifier=value.get("record_identifier"),
        ).to_dict()
    return DiscoveryRoute().to_dict()


def _source_location_dict(value: SourceLocation | dict | None) -> dict:
    if isinstance(value, SourceLocation):
        return value.to_dict()
    if isinstance(value, dict):
        known = {key: value.get(key) for key in SourceLocation.__dataclass_fields__}
        return SourceLocation(**known).to_dict()
    return SourceLocation().to_dict()


@dataclass
class Provenance:
    """Structured citation provenance.

    A DOI is sufficient. Without a DOI, both a title and a source/provider are
    required; a bare title is not defensible provenance.
    """

    source: str = ""                 # which API / "manual"
    doi: str | None = None
    title: str | None = None
    url: str | None = None
    authors: list = field(default_factory=list)
    year: int | None = None
    query: str | None = None         # the search query that surfaced the paper
    discovery_route: DiscoveryRoute | dict | None = None

    @property
    def is_present(self) -> bool:
        return bool((self.doi or "").strip()) or bool(
            (self.title or "").strip() and (self.source or "").strip())

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
        route = _discovery_dict(self.discovery_route)
        if not route["source"]:
            route["source"] = self.source
        if route["query"] is None:
            route["query"] = self.query
        return {"source": self.source, "provider": self.source, "doi": self.doi,
                "title": self.title, "url": self.url, "authors": list(self.authors),
                "year": self.year, "query": self.query, "discovery_route": route,
                "citation": self.citation()}


@dataclass
class Evidence:
    """Common provenance + confidence carried by every evidence row."""

    provenance: Provenance = field(default_factory=Provenance)
    source_location: SourceLocation | dict = field(default_factory=SourceLocation)
    project_id: str | None = None
    material_id: str | None = None
    topic: str | None = None
    claim: str | None = None
    reported_values: dict = field(default_factory=dict)
    units: dict = field(default_factory=dict)
    conditions: dict = field(default_factory=dict)
    extraction_confidence: float = 0.0
    extraction_scope: str = SCOPE_ABSTRACT
    extraction_status: str = STATUS_OK
    field_confidence: dict = field(default_factory=dict)     # field -> 0..1
    conflicts: list = field(default_factory=list)            # flagged conflicting fields
    notes: str | None = None
    review_status: str = REVIEW_NEEDS_REVIEW
    reviewer: str | None = None
    review_time: str | None = None
    review_reason: str | None = None
    creation_origin: str | None = None
    revision: int = 1
    previous_revision_id: str | None = None
    previous_revision_hash: str | None = None

    @property
    def has_provenance(self) -> bool:
        return self.provenance is not None and self.provenance.is_present

    @property
    def confidence_label(self) -> str:
        # Reflect the abstract-capped confidence (an abstract-only row can't be "high").
        return confidence_band(self._capped_confidence())

    @property
    def is_reviewed(self) -> bool:
        return self.review_status == REVIEW_REVIEWED

    def _capped_confidence(self) -> float:
        """Abstract-only extraction is capped (it is not full experimental data)."""
        try:
            c = float(self.extraction_confidence or 0.0)
        except (TypeError, ValueError):
            c = 0.0
        if not math.isfinite(c):
            c = 0.0
        c = max(0.0, min(1.0, c))
        if self.extraction_scope == SCOPE_ABSTRACT:
            return min(c, ABSTRACT_CONFIDENCE_CAP)
        return c

    def _common_row(self) -> dict:
        d = asdict(self)
        provenance = self.provenance.to_dict() if isinstance(self.provenance, Provenance) \
            else dict(self.provenance or {})
        location = _source_location_dict(self.source_location)
        d["provenance"] = provenance
        d["source_location"] = location
        d["source_location_summary"] = SourceLocation(**location).summary
        d["confidence_label"] = self.confidence_label
        d["extraction_confidence"] = round(self._capped_confidence(), 3)
        if provenance.get("citation"):
            d["citation"] = provenance["citation"]
        elif isinstance(self.provenance, Provenance):
            d["citation"] = self.provenance.citation()
        else:
            d["citation"] = Provenance(
                source=str(provenance.get("source") or provenance.get("provider") or ""),
                doi=provenance.get("doi"), title=provenance.get("title"),
                url=provenance.get("url"), authors=list(provenance.get("authors") or []),
                year=provenance.get("year"), query=provenance.get("query"),
            ).citation()
        # Useful flat citation/location columns for deterministic CSV export;
        # nested JSON remains authoritative.
        for key in ("source", "doi", "title", "url", "authors", "year", "query"):
            d[f"citation_{key}"] = provenance.get(key)
        for key, value in location.items():
            d[f"source_{key}"] = value
        return d


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
        return self._common_row()


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
        return self._common_row()


# Ordered columns for the CSV export / evidence table (per schema). Nested
# provenance and source-location objects are retained alongside flat fields.
COMMON_COLUMNS = (
    "artifact_id", "logical_id", "artifact_status", "artifact_revision",
    "artifact_created_at", "artifact_updated_at", "related_run_id",
    "project_id", "material_id", "topic", "claim", "schema_kind",
    "citation_title", "citation_authors", "citation_year", "citation_doi", "citation_url",
    "citation_source", "citation_query", "provenance", "source_location",
    "source_page", "source_page_range", "source_table", "source_figure", "source_section",
    "source_supplementary_item", "source_dataset_record_identifier", "source_note",
    "source_location_summary", "reported_values", "units", "conditions",
)
LEACHING_VALUE_COLUMNS = (
    "material", "material_class", "leachant", "concentration_M", "solid_mass_g", "liquid_volume_mL",
    "ls_ratio", "time_min", "temperature_C", "pH", "elements_measured", "element_values_mM",
    "analytical_method", "filtration", "composition")
COMPOSITE_VALUE_COLUMNS = (
    "material_binder", "plastic_type", "plastic_form", "plastic_particle_size", "plastic_dosage",
    "water_binder_ratio", "activator_cement_content", "curing_time", "specimen_geometry",
    "compressive_strength_MPa", "flexural_strength_MPa", "density_kg_m3", "water_absorption_pct",
    "durability_observations")
REVIEW_COLUMNS = (
    "extraction_scope", "extraction_status", "extraction_confidence", "confidence_label",
    "field_confidence", "conflicts", "notes", "creation_origin", "review_status", "reviewer",
    "review_time", "review_reason", "revision", "previous_revision_id",
    "previous_revision_hash", "citation",
)
LEACHING_COLUMNS = COMMON_COLUMNS + LEACHING_VALUE_COLUMNS + REVIEW_COLUMNS
COMPOSITE_COLUMNS = COMMON_COLUMNS + COMPOSITE_VALUE_COLUMNS + REVIEW_COLUMNS


def columns_for(schema_kind: str) -> tuple:
    if schema_kind == SCHEMA_LEACHING:
        return LEACHING_COLUMNS
    if schema_kind == SCHEMA_COMPOSITE:
        return COMPOSITE_COLUMNS
    raise ValueError(f"unsupported evidence schema_kind: {schema_kind!r}")
