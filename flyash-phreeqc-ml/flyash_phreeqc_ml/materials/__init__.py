"""Material **composition manager** for the Simulate workflow (planning layer only).

A user provides, reviews, and confirms a material's bulk composition here so the
deterministic PHREEQC *input preview* can include the dissolved-material chemistry instead
of stopping at ``needs_material_composition``. It is **off the scientific result path**
(no mapping / residual / validation / comparison), runs no PHREEQC, and writes nothing to
disk — profiles live only in the caller's session.

Distinct from :class:`flyash_phreeqc_ml.profiles.MaterialProfile` (the frozen, code-defined
material used by the *scientific* batch-chemistry result path); see
:mod:`flyash_phreeqc_ml.materials.profile_schema`.
"""
from __future__ import annotations

from .profile_schema import (  # noqa: F401
    BASIS_ELEMENT_WT,
    BASIS_LABELS,
    BASIS_MG_PER_KG,
    BASIS_MOL_PER_KG,
    BASIS_OXIDE_WT,
    KNOWN_BASES,
    OXIDE_FORMULAS,
    SOURCE_FIXTURE,
    SOURCE_LITERATURE,
    SOURCE_TYPES,
    SOURCE_UPLOADED_FILE,
    SOURCE_USER_ENTERED,
    STATUS_DRAFT,
    STATUS_LABELS,
    STATUS_LITERATURE_UNVERIFIED,
    STATUS_USER_CONFIRMED,
    STATUS_VERIFIED,
    USABLE_STATUSES,
    VERIFICATION_STATUSES,
    CompositionEntry,
    CompositionSource,
    MaterialProfile,
    ProfileValidationResult,
    ResolvedAssay,
    canonical_element,
    canonical_oxide,
    entries_from_records,
    oxide_gravimetric_factor,
    parse_composition_text,
    profile_from_literature_candidates,
    resolve_entry,
)
from .profile_validation import validate_profile  # noqa: F401

__all__ = [
    "MaterialProfile", "CompositionEntry", "CompositionSource", "ResolvedAssay",
    "ProfileValidationResult", "validate_profile", "parse_composition_text",
    "entries_from_records", "profile_from_literature_candidates", "resolve_entry",
    "canonical_oxide", "canonical_element", "oxide_gravimetric_factor",
    "BASIS_OXIDE_WT", "BASIS_ELEMENT_WT", "BASIS_MG_PER_KG", "BASIS_MOL_PER_KG",
    "KNOWN_BASES", "BASIS_LABELS", "OXIDE_FORMULAS",
    "STATUS_DRAFT", "STATUS_USER_CONFIRMED", "STATUS_LITERATURE_UNVERIFIED",
    "STATUS_VERIFIED", "USABLE_STATUSES", "VERIFICATION_STATUSES", "STATUS_LABELS",
    "SOURCE_USER_ENTERED", "SOURCE_UPLOADED_FILE", "SOURCE_LITERATURE", "SOURCE_FIXTURE",
    "SOURCE_TYPES",
]
