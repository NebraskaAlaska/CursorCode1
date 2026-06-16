"""User-facing **material composition profiles** for the Simulate workflow.

This module is the **composition manager** a researcher uses to *provide* a material's
bulk composition (Class C fly ash, red mud, a generic material) so the deterministic
PHREEQC **input preview** can become scientifically meaningful instead of stopping at
``needs_material_composition``.

It is deliberately **distinct** from :class:`flyash_phreeqc_ml.profiles.MaterialProfile`
— that one is the *frozen, code-defined* material on the **scientific result path**
(mass balance / attribution / recovery). This one is a *mutable, user-created* description
used only by the **planning layer** (the Simulate tab + the input-preview builder). It is
off the scientific result path and writes nothing to disk.

Safety properties (mirroring the assay-quarantine rule used elsewhere in the project):

* A profile is **usable** only when its ``verification_status`` is ``user_confirmed`` or
  ``verified``. A ``draft`` or ``literature_unverified`` profile is **never** usable, so a
  preview built from it stays ``needs_material_composition`` until a human confirms it.
* Composition values are **never invented** — every entry comes from the user, an uploaded
  file, or a reviewed-and-confirmed literature value (whose citation is retained).
* AI / literature output enters only as ``literature_unverified`` and **cannot** be used for
  input generation until explicitly confirmed.

Duck typing: a :class:`MaterialProfile` exposes ``relevant_elements`` / ``usable_assay`` /
``display_name`` / ``candidate_phases`` so the existing
:mod:`flyash_phreeqc_ml.simulation.phreeqc_input_builder` consumes it through the **same**
``getattr`` interface it already uses for the frozen profile — the builder never imports
this package.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import units

# --------------------------------------------------------------------------- #
# Vocabulary (verification / source / basis) — single source of truth
# --------------------------------------------------------------------------- #
# Trust gate. Only the two "usable" statuses may feed an input preview.
STATUS_DRAFT = "draft"
STATUS_USER_CONFIRMED = "user_confirmed"
STATUS_LITERATURE_UNVERIFIED = "literature_unverified"
STATUS_VERIFIED = "verified"
VERIFICATION_STATUSES = (STATUS_DRAFT, STATUS_USER_CONFIRMED,
                         STATUS_LITERATURE_UNVERIFIED, STATUS_VERIFIED)
# A profile must be one of these to be used for PHREEQC input generation.
USABLE_STATUSES = (STATUS_USER_CONFIRMED, STATUS_VERIFIED)

STATUS_LABELS = {
    STATUS_DRAFT: "draft (not usable)",
    STATUS_USER_CONFIRMED: "user-confirmed",
    STATUS_LITERATURE_UNVERIFIED: "literature — unverified (not usable)",
    STATUS_VERIFIED: "verified",
}

# Where a composition came from (provenance origin; the trust gate is the status above).
SOURCE_USER_ENTERED = "user_entered"
SOURCE_UPLOADED_FILE = "uploaded_file"
SOURCE_LITERATURE = "literature_verified"     # a literature source (reviewed when confirmed)
SOURCE_FIXTURE = "fixture"
SOURCE_TYPES = (SOURCE_USER_ENTERED, SOURCE_UPLOADED_FILE, SOURCE_LITERATURE, SOURCE_FIXTURE)

# Composition basis (how the numbers are expressed).
BASIS_OXIDE_WT = "oxide_wt_percent"
BASIS_ELEMENT_WT = "element_wt_percent"
BASIS_MG_PER_KG = "mg_per_kg"
BASIS_MOL_PER_KG = "mol_per_kg"
KNOWN_BASES = (BASIS_OXIDE_WT, BASIS_ELEMENT_WT, BASIS_MG_PER_KG, BASIS_MOL_PER_KG)
BASIS_LABELS = {
    BASIS_OXIDE_WT: "oxide wt %",
    BASIS_ELEMENT_WT: "element wt %",
    BASIS_MG_PER_KG: "mg/kg (element)",
    BASIS_MOL_PER_KG: "mol/kg (element)",
}

# Status → the provenance string stamped on a resolved assay (display only).
_STATUS_TO_PROVENANCE = {
    STATUS_USER_CONFIRMED: "user-confirmed",
    STATUS_VERIFIED: "verified",
}

# Plausible total for an oxide assay (sum of oxides + LOI + moisture), as wt %.
OXIDE_SUM_MIN = 90.0
OXIDE_SUM_MAX = 102.0


# --------------------------------------------------------------------------- #
# Element / oxide stoichiometry (composition-domain; reuses units.MOLAR_MASSES)
# --------------------------------------------------------------------------- #
# Element molar masses come from the single registry (units.MOLAR_MASSES). The few extra
# weights needed only to weigh common oxides live here — they are NOT added to the units
# registry because the lab-import / scientific result path does not need them, so the
# change stays inside this planning-layer package. IUPAC 2021 abridged, g/mol (CIAAW).
_SUPPLEMENTAL_ATOMIC_WEIGHTS = {
    "O": 15.999, "Mg": 24.305, "S": 32.06, "P": 30.974,
    "Mn": 54.938, "Cr": 51.996, "Sr": 87.62, "Ba": 137.327, "C": 12.011,
}


def atomic_weight(element: str) -> float | None:
    """Atomic weight (g/mol) for ``element`` — units registry first, then the supplement."""
    if element in units.MOLAR_MASSES:
        return units.MOLAR_MASSES[element]
    return _SUPPLEMENTAL_ATOMIC_WEIGHTS.get(element)


# Oxide formula -> (target element, {element: atom count}). Covers the major oxides
# reported in fly-ash / bauxite-residue XRF assays.
OXIDE_FORMULAS: dict[str, tuple[str, dict[str, int]]] = {
    "SiO2": ("Si", {"Si": 1, "O": 2}),
    "Al2O3": ("Al", {"Al": 2, "O": 3}),
    "CaO": ("Ca", {"Ca": 1, "O": 1}),
    "Fe2O3": ("Fe", {"Fe": 2, "O": 3}),
    "FeO": ("Fe", {"Fe": 1, "O": 1}),
    "MgO": ("Mg", {"Mg": 1, "O": 1}),
    "Na2O": ("Na", {"Na": 2, "O": 1}),
    "K2O": ("K", {"K": 2, "O": 1}),
    "TiO2": ("Ti", {"Ti": 1, "O": 2}),
    "SO3": ("S", {"S": 1, "O": 3}),
    "P2O5": ("P", {"P": 2, "O": 5}),
    "MnO": ("Mn", {"Mn": 1, "O": 1}),
    "Mn2O3": ("Mn", {"Mn": 2, "O": 3}),
    "V2O5": ("V", {"V": 2, "O": 5}),
    "Cr2O3": ("Cr", {"Cr": 2, "O": 3}),
    "SrO": ("Sr", {"Sr": 1, "O": 1}),
    "BaO": ("Ba", {"Ba": 1, "O": 1}),
}
_OXIDE_LOOKUP = {k.lower(): k for k in OXIDE_FORMULAS}
_ELEMENT_LOOKUP = {e.lower(): e for e in
                   list(units.MOLAR_MASSES) + list(_SUPPLEMENTAL_ATOMIC_WEIGHTS)}

# Labels that are not composition (handled as LOI / moisture, never an element).
NON_ELEMENT_SPECIES = {"loi", "l.o.i", "l.o.i.", "loss on ignition", "moisture", "h2o",
                       "h2o-", "h2o+", "water", "total", "sum", "balance"}


def oxide_molar_mass(oxide: str) -> float:
    """Molar mass (g/mol) of a registered oxide, summed from its stoichiometry."""
    _, stoich = OXIDE_FORMULAS[oxide]
    return sum(atomic_weight(e) * n for e, n in stoich.items())


def oxide_gravimetric_factor(oxide: str) -> float:
    """Mass fraction of the *target element* in the oxide (e.g. CaO → 0.7147 for Ca)."""
    el, stoich = OXIDE_FORMULAS[oxide]
    return (atomic_weight(el) * stoich[el]) / oxide_molar_mass(oxide)


def canonical_oxide(species) -> str | None:
    """Canonical oxide formula for a possibly mis-cased label (``sio2`` → ``SiO2``)."""
    return _OXIDE_LOOKUP.get(str(species or "").strip().lower())


def canonical_element(species) -> str | None:
    """Canonical element symbol for a possibly mis-cased label (``ca`` → ``Ca``)."""
    s = str(species or "").strip()
    if not s:
        return None
    if atomic_weight(s) is not None:
        return s
    return _ELEMENT_LOOKUP.get(s.lower())


def is_non_element_label(species) -> bool:
    """True for LOI / moisture / total rows (kept for the sum, not an element)."""
    return str(species or "").strip().lower() in NON_ELEMENT_SPECIES


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ResolvedAssay:
    """An element assay resolved to **element wt %** for the input-preview comments.

    Duck-compatible with what ``phreeqc_input_builder`` reads from a frozen assay
    (``.value`` / ``.unit`` / ``.provenance``). Its ``provenance`` is a *display* string
    (``user-confirmed`` / ``verified``) — deliberately **not** one of the science
    ``USABLE_ASSAY_PROVENANCE`` values, so a Simulate composition can never be mistaken
    for a measured assay if it ever reached the result path.
    """

    element: str
    value: float
    unit: str = "wt%"
    provenance: str = "user-confirmed"
    basis: str = BASIS_ELEMENT_WT
    source_species: str = ""


@dataclass
class CompositionSource:
    """Where a composition came from (origin only — trust is the verification status)."""

    source_type: str = SOURCE_USER_ENTERED
    source_reference: str | None = None     # file name / data set name
    citation: str | None = None             # DOI / URL when literature-sourced
    title: str | None = None                # source title (literature)
    year: int | None = None
    retrieved_by: str | None = None         # "manual" / "upload" / "ai_literature"
    notes: str | None = None

    def display(self) -> str:
        bits = [self.source_type]
        if self.source_reference:
            bits.append(self.source_reference)
        if self.citation:
            bits.append(self.citation)
        return " — ".join(b for b in bits if b)


@dataclass
class CompositionEntry:
    """One reported component (an oxide like ``SiO2`` or an element like ``Ca``)."""

    species: str
    value: float | None = None              # in the profile's basis unit
    uncertainty: float | None = None        # ± in the basis unit (optional)
    note: str | None = None

    def numeric_value(self) -> float | None:
        try:
            v = float(self.value)
        except (TypeError, ValueError):
            return None
        return v if v == v else None         # drop NaN


def resolve_entry(entry: CompositionEntry, basis: str) -> tuple[str | None, float | None, str]:
    """Resolve one entry to ``(element, element_wt_percent, status)``.

    ``status`` is one of ``resolved`` / ``non_element`` (LOI/moisture/total) /
    ``unrecognized`` / ``no_value`` / ``negative``. Never raises.
    """
    v = entry.numeric_value()
    if is_non_element_label(entry.species):
        return None, None, "non_element"
    if v is None:
        return None, None, "no_value"
    if v < 0:
        return None, None, "negative"
    if basis == BASIS_ELEMENT_WT:
        el = canonical_element(entry.species)
        return (el, v, "resolved") if el else (None, None, "unrecognized")
    if basis == BASIS_OXIDE_WT:
        ox = canonical_oxide(entry.species)
        if ox:
            el, _ = OXIDE_FORMULAS[ox]
            return el, v * oxide_gravimetric_factor(ox), "resolved"
        return None, None, "unrecognized"
    if basis == BASIS_MG_PER_KG:
        el = canonical_element(entry.species)
        # 1 wt% = 10 g/kg = 10000 mg/kg  → wt% = (mg/kg) / 1e4
        return (el, v / 1e4, "resolved") if el else (None, None, "unrecognized")
    if basis == BASIS_MOL_PER_KG:
        el = canonical_element(entry.species)
        if not el:
            return None, None, "unrecognized"
        m = atomic_weight(el)
        # g/kg = mol/kg × M ; wt% = (g/kg) / 10
        return el, v * m / 10.0, "resolved"
    return None, None, "unrecognized"


# --------------------------------------------------------------------------- #
# The profile
# --------------------------------------------------------------------------- #
@dataclass
class MaterialProfile:
    """A user-managed material composition (mutable; session-only; off the result path)."""

    profile_id: str
    material_name: str
    material_type: str | None = None
    composition_basis: str = BASIS_OXIDE_WT
    entries: list = field(default_factory=list)        # list[CompositionEntry]
    moisture_pct: float | None = None
    loi_pct: float | None = None
    source: CompositionSource = field(default_factory=CompositionSource)
    verification_status: str = STATUS_DRAFT
    notes: str | None = None

    # -- trust gate -------------------------------------------------------- #
    @property
    def is_usable(self) -> bool:
        """May this profile feed a PHREEQC input preview? (status gate only.)"""
        return self.verification_status in USABLE_STATUSES

    def provenance_label(self) -> str:
        return _STATUS_TO_PROVENANCE.get(self.verification_status, self.verification_status)

    # -- composition resolution ------------------------------------------- #
    def element_assays(self) -> dict:
        """``{element: ResolvedAssay}`` in element wt %, aggregated across species.

        Always computes (so a *draft* can be reviewed); usability is gated separately by
        :meth:`usable_assay`. Multiple species mapping to one element (e.g. FeO + Fe2O3)
        are summed; non-positive / unrecognized / LOI rows contribute nothing.
        """
        agg: dict[str, dict] = {}
        for entry in self.entries:
            el, wt, status = resolve_entry(entry, self.composition_basis)
            if status != "resolved" or el is None or wt is None:
                continue
            slot = agg.setdefault(el, {"value": 0.0, "species": []})
            slot["value"] += wt
            slot["species"].append(str(entry.species))
        prov = self.provenance_label()
        return {
            el: ResolvedAssay(element=el, value=round(d["value"], 6), unit="wt%",
                              provenance=prov, basis=self.composition_basis,
                              source_species="+".join(d["species"]))
            for el, d in agg.items()
        }

    # -- duck-typed interface the input-preview builder reads -------------- #
    @property
    def display_name(self) -> str:
        return self.material_name

    @property
    def relevant_elements(self) -> tuple:
        return tuple(self.element_assays().keys())

    @property
    def candidate_phases(self) -> dict:
        # A user composition profile declares no precipitate phases; the preview prints
        # an explicit "define the phases" note rather than guessing.
        return {}

    def usable_assay(self, element: str):
        """The resolved assay for ``element`` **only when the profile is usable** (else None).

        This is the single gate the input-preview builder relies on: a draft /
        literature-unverified profile returns ``None`` for every element, so the preview
        stays ``needs_material_composition``.
        """
        if not self.is_usable:
            return None
        return self.element_assays().get(element)

    # -- bookkeeping ------------------------------------------------------- #
    def oxide_total(self) -> float | None:
        """Sum of oxide-basis entry values + LOI + moisture (wt %), or None off-basis."""
        if self.composition_basis != BASIS_OXIDE_WT:
            return None
        total = 0.0
        for entry in self.entries:
            v = entry.numeric_value()
            if v is not None and v >= 0:
                total += v
        for extra in (self.loi_pct, self.moisture_pct):
            try:
                if extra is not None and float(extra) >= 0:
                    total += float(extra)
            except (TypeError, ValueError):
                pass
        return round(total, 4)

    def basis_label(self) -> str:
        return BASIS_LABELS.get(self.composition_basis, self.composition_basis)

    def summary(self) -> dict:
        """A compact, display-safe summary (no file paths, no secrets)."""
        assays = self.element_assays()
        return {
            "profile_id": self.profile_id,
            "material_name": self.material_name,
            "material_type": self.material_type,
            "basis": self.composition_basis,
            "basis_label": self.basis_label(),
            "source_type": self.source.source_type,
            "source": self.source.display(),
            "verification_status": self.verification_status,
            "status_label": STATUS_LABELS.get(self.verification_status,
                                              self.verification_status),
            "is_usable": self.is_usable,
            "n_entries": len(self.entries),
            "n_elements_resolved": len(assays),
            "elements": sorted(assays),
            "oxide_total": self.oxide_total(),
        }


# --------------------------------------------------------------------------- #
# Validation result (structure; the logic lives in profile_validation.py)
# --------------------------------------------------------------------------- #
@dataclass
class ProfileValidationResult:
    """Outcome of validating a :class:`MaterialProfile` (errors / warnings / infos).

    ``ok`` is True only when there are **no errors**. ``can_confirm`` means the user is
    allowed to mark the profile confirmed (validation passed). ``usable_for_preview`` means
    it both validates *and* already carries a usable verification status — the single flag
    the UI uses to decide whether the profile may feed the PHREEQC input preview.
    """

    ok: bool = True
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    infos: list = field(default_factory=list)
    oxide_total: float | None = None
    n_elements_resolved: int = 0
    can_confirm: bool = False
    usable_for_preview: bool = False
    requires_confirmation: bool = False     # literature_unverified awaiting confirmation

    def all_messages(self) -> list:
        return list(self.errors) + list(self.warnings) + list(self.infos)


# --------------------------------------------------------------------------- #
# Parsing helpers (pure — no AI, no I/O)
# --------------------------------------------------------------------------- #
_PASTE_SPLIT = re.compile(r"[,;\t]|\s{2,}|\s*=\s*|\s*:\s*")
_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
# Fallback for a single-space line ("SiO2 38"): the value is the number at the END of the
# line (after an optional %), so a digit *inside* a formula (the 2 in SiO2) is never the value.
_ENTRY_RE = re.compile(
    r"^(?P<species>.*?\S)[\s,;:=\t]+"
    r"(?P<value>[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
    r"(?:\s*%)?(?:\s+\S+)*\s*$")              # tolerate a trailing unit/note (e.g. 'wt%')


def _first_number(token: str):
    m = _NUM_RE.search(str(token).replace("%", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_composition_text(text: str) -> list:
    """Parse pasted ``species value [± unc]`` lines into :class:`CompositionEntry`.

    Tolerant of commas / tabs / ``=`` / ``:`` / multiple spaces as separators, ``%`` signs,
    trailing note columns, and ``±`` uncertainties. The **species token is never scanned for
    a number** (so the ``2`` in ``SiO2`` is not mistaken for the value). Header-like or
    value-less lines are skipped. Never raises.
    """
    entries: list[CompositionEntry] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # uncertainty after ± / +/- (optional) — removed before species/value parsing
        unc = None
        m_unc = re.search(r"(?:±|\+/-|\+-)\s*([-+]?\d*\.?\d+)", line)
        if m_unc:
            try:
                unc = float(m_unc.group(1))
            except ValueError:
                unc = None
            line = (line[:m_unc.start()] + line[m_unc.end():]).strip()

        species = None
        value = None
        # primary: explicit separators (comma / semicolon / tab / 2+ spaces / = / :).
        parts = [p.strip() for p in _PASTE_SPLIT.split(line) if p and p.strip()]
        if len(parts) >= 2:
            species = parts[0]
            for p in parts[1:]:                # first numeric column after the species
                value = _first_number(p)
                if value is not None:
                    break
        if value is None:                      # fallback: "SiO2 38" with a single space
            m = _ENTRY_RE.match(line)
            if m:
                species = m.group("species").strip(" ,;:=\t")
                value = _first_number(m.group("value"))

        if not species or value is None:
            continue
        entries.append(CompositionEntry(species=species, value=value, uncertainty=unc))
    return entries


def entries_from_records(records, *, species_key: str, value_key: str,
                         uncertainty_key: str | None = None) -> list:
    """Build entries from row dicts (e.g. ``df.to_dict('records')``). Never raises.

    Rows with a blank species **or** a blank / non-numeric / NaN value are skipped, so an
    editor's empty starter rows don't become value-less entries.
    """
    out: list[CompositionEntry] = []
    for rec in records or []:
        species = str(rec.get(species_key, "") or "").strip()
        if not species:
            continue
        raw = rec.get(value_key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value != value:                       # NaN → skip (blank editor cell)
            continue
        unc = None
        if uncertainty_key is not None:
            try:
                u = float(rec.get(uncertainty_key))
                unc = u if u == u else None
            except (TypeError, ValueError):
                unc = None
        out.append(CompositionEntry(species=species, value=value, uncertainty=unc))
    return out


def profile_from_literature_candidates(profile_id: str, material_name: str,
                                       material_type: str | None,
                                       candidates: list,
                                       *, basis: str = BASIS_ELEMENT_WT) -> MaterialProfile:
    """Build a **quarantined** (``literature_unverified``) profile from literature dicts.

    ``candidates`` are plain dicts (so this stays AI-free and testable) with keys
    ``element`` / ``value`` / ``unit`` / ``citation`` / ``title`` / ``year`` /
    ``confidence``. The result is **never usable** until a human confirms it — that is the
    point of returning ``literature_unverified``. The first candidate's citation labels the
    profile source; per-entry citations are kept in each entry's ``note``.
    """
    entries: list[CompositionEntry] = []
    first_cite = first_title = None
    first_year = None
    for c in candidates or []:
        el = c.get("element")
        if not el:
            continue
        try:
            value = float(c.get("value"))
        except (TypeError, ValueError):
            continue
        cite = c.get("citation")
        if first_cite is None and cite:
            first_cite, first_title, first_year = cite, c.get("title"), c.get("year")
        note_bits = []
        if cite:
            note_bits.append(f"source: {cite}")
        if c.get("title"):
            note_bits.append(str(c.get("title")))
        entries.append(CompositionEntry(species=str(el), value=value,
                                        note="; ".join(note_bits) or None))
    src = CompositionSource(source_type=SOURCE_LITERATURE, citation=first_cite,
                            title=first_title, year=first_year, retrieved_by="ai_literature",
                            source_reference="AI literature search")
    return MaterialProfile(
        profile_id=profile_id, material_name=material_name, material_type=material_type,
        composition_basis=basis, entries=entries, source=src,
        verification_status=STATUS_LITERATURE_UNVERIFIED,
        notes="Literature-proposed composition — review every value + citation before use.")
