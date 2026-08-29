"""Deprecated Digital Lab instrument-registry compatibility adapter.

Canonical authority: :mod:`flyash_phreeqc_ml.instruments.virtual_lab_machines`.

This module retains the historical lookup API and constant names but owns no scientific metadata.
Every :class:`InstrumentSpec` is a read-only view over one of the twelve canonical machine objects.
Canonical and legacy IDs are accepted for lookup; returned specs always carry canonical IDs.
"""
from __future__ import annotations

from . import virtual_lab_machines as machine_contract
from .instrument_schema import InstrumentSpec

# Historical constant names now resolve to canonical IDs so new router/UI outputs cannot leak aliases.
PHREEQC_LEACHING = machine_contract.PHREEQC_LEACHING
ICP_DATA_PROCESSOR = machine_contract.ICP_PROCESSOR
ICP_PROCESSOR = machine_contract.ICP_PROCESSOR
XRD_ADVISORY = machine_contract.XRD_ADVISORY
MECHANICAL_TEST_PROCESSOR = machine_contract.MECHANICAL
MECHANICAL = machine_contract.MECHANICAL
ML_SURROGATE_PREDICTOR = machine_contract.ML_SURROGATE
LITERATURE_EVIDENCE_ENGINE = machine_contract.LITERATURE_ENGINE
SUSTAINABILITY_SCREENING = machine_contract.SUSTAINABILITY
SUSTAINABILITY = machine_contract.SUSTAINABILITY
FTIR_RAMAN_INTERPRETER = machine_contract.FTIR_RAMAN
SEM_EDS_PROCESSOR = machine_contract.SEM_EDS
TGA_DSC_PROCESSOR = machine_contract.TGA_DSC
EXPERIMENTAL_DESIGN_ASSISTANT = machine_contract.EXPERIMENTAL_DESIGN
VALIDATION_UNCERTAINTY_ASSISTANT = machine_contract.VALIDATION_UNCERTAINTY

# Explicit legacy strings for compatibility tests/callers that need to identify old saved values.
LEGACY_XRD_ADVISORY = "xrd_advisory_module"
LEGACY_MECHANICAL_TEST_PROCESSOR = "mechanical_test_processor"
LEGACY_SUSTAINABILITY_SCREENING = "sustainability_screening"

_INSTRUMENTS: tuple[InstrumentSpec, ...] = tuple(
    InstrumentSpec(machine) for machine in machine_contract.list_virtual_lab_machines())
_BY_ID: dict[str, InstrumentSpec] = {spec.instrument_id: spec for spec in _INSTRUMENTS}


def all_instruments() -> tuple[InstrumentSpec, ...]:
    """All twelve canonical machines as compatibility views, in canonical order."""
    return _INSTRUMENTS


def instrument_ids() -> tuple[str, ...]:
    """All twelve canonical IDs in canonical order."""
    return machine_contract.machine_ids()


def get(instrument_id: str) -> InstrumentSpec | None:
    """Compatibility lookup accepting canonical or known legacy IDs; never guesses."""
    canonical = machine_contract.canonical_machine_id(instrument_id)
    return _BY_ID.get(canonical) if canonical is not None else None


def require(instrument_id: str) -> InstrumentSpec:
    """Compatibility lookup that raises a controlled ``KeyError`` for unknown IDs."""
    spec = get(instrument_id)
    if spec is None:
        known = ", ".join(machine_contract.machine_ids())
        aliases = ", ".join(machine_contract.LEGACY_MACHINE_ID_ALIASES)
        raise KeyError(f"unknown instrument {instrument_id!r}; canonical: {known}; aliases: {aliases}")
    return spec


def active_instruments() -> tuple[InstrumentSpec, ...]:
    """Deprecated static-code view; readiness/runtime requirements remain separate."""
    return tuple(spec for spec in _INSTRUMENTS if spec.active)


def by_mode(mode: str) -> tuple[InstrumentSpec, ...]:
    """Compatibility mode query backed by the canonical contract's deterministic interpretation."""
    selected = {machine.machine_id for machine in machine_contract.list_machines_by_mode(mode)}
    return tuple(spec for spec in _INSTRUMENTS if spec.instrument_id in selected)


def display_name(instrument_id: str) -> str:
    spec = get(instrument_id)
    return spec.display_name if spec is not None else str(instrument_id)
