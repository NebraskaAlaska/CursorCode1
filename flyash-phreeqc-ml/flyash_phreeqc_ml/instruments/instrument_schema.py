"""Compatibility schema for the historical Digital Lab ``InstrumentSpec`` API.

The authoritative vocabulary and scientific metadata live in
:mod:`flyash_phreeqc_ml.instruments.virtual_lab_machines`. This module re-exports that vocabulary
and exposes a read-only adapter so older UI/tests can keep using ``InstrumentSpec``-like fields
without maintaining a second catalogue.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import virtual_lab_machines as machine_contract

# One vocabulary for new code. Historical mode names remain distinct compatibility query values.
MODE_PHYSICAL_SIMULATION = machine_contract.MODE_PHYSICAL_SIMULATION
MODE_DATA_PROCESSING = machine_contract.MODE_DATA_PROCESSING
MODE_ADVISORY_PLANNING = machine_contract.MODE_ADVISORY_PLANNING
MODE_TRAINED_MODEL_PREDICTION = machine_contract.MODE_TRAINED_MODEL_PREDICTION
MODE_EVIDENCE_ENGINE = machine_contract.MODE_EVIDENCE_ENGINE
MODE_CROSS_CUTTING_VALIDATION = machine_contract.MODE_CROSS_CUTTING_VALIDATION
MODE_SIGNAL_SIMULATION = machine_contract.MODE_SIGNAL_SIMULATION  # deprecated compatibility value
MODE_TRAINED_MODEL = machine_contract.MODE_TRAINED_MODEL          # deprecated compatibility value
MODES = machine_contract.MODES

EXEC_ADVISORY_ONLY = machine_contract.EXEC_ADVISORY_ONLY
EXEC_DATA_PROCESSING = machine_contract.EXEC_DATA_PROCESSING
EXEC_PREVIEW_THEN_CONFIRM = machine_contract.EXEC_PREVIEW_THEN_CONFIRM
EXEC_TRAINED_MODEL_REQUIRED = machine_contract.EXEC_TRAINED_MODEL_REQUIRED
EXEC_EVIDENCE_REQUIRED = machine_contract.EXEC_EVIDENCE_REQUIRED
EXEC_MEASURED_DATA_REQUIRED = machine_contract.EXEC_MEASURED_DATA_REQUIRED
EXECUTION_MODES = machine_contract.EXECUTION_MODES

# Compatibility readiness chips are derived from canonical maturity + requirements. They are not
# environment checks and never replace MachineRuntimeAvailability.
READY = "implemented_backend"
DATA_PROCESSING = "data_processing"
ADVISORY = "advisory"
PLANNING = "limited_workflow"
TRAINED_MODEL_REQUIRED = "trained_model_required"
EVIDENCE_REQUIRED = "reference_or_evidence_required"
MEASURED_DATA_REQUIRED = "measured_data_required"
BLUEPRINT = "blueprint_only"

READINESS_LABELS = {
    READY: "Implemented backend",
    DATA_PROCESSING: "Data processing",
    ADVISORY: "Advisory",
    PLANNING: "Limited workflow",
    TRAINED_MODEL_REQUIRED: "Approved trained model required",
    EVIDENCE_REQUIRED: "Reference / evidence required",
    MEASURED_DATA_REQUIRED: "Measured data required",
    BLUEPRINT: "Blueprint / planned",
}

READINESS_BADGE = {
    READY: "success",
    DATA_PROCESSING: "success",
    ADVISORY: "info",
    PLANNING: "info",
    TRAINED_MODEL_REQUIRED: "warning",
    EVIDENCE_REQUIRED: "warning",
    MEASURED_DATA_REQUIRED: "warning",
    BLUEPRINT: "neutral",
}


@dataclass(frozen=True)
class InstrumentSpec:
    """Read-only compatibility view over one canonical :class:`VirtualLabMachine`."""

    canonical_machine: machine_contract.VirtualLabMachine

    @property
    def instrument_id(self) -> str:
        return self.canonical_machine.machine_id

    @property
    def display_name(self) -> str:
        return self.canonical_machine.display_name

    @property
    def short_description(self) -> str:
        return self.canonical_machine.short_description

    @property
    def category(self) -> str:
        return self.canonical_machine.category

    @property
    def mode(self) -> str:
        return self.canonical_machine.mode

    @property
    def what_it_can_do(self) -> str:
        return self.canonical_machine.what_it_can_do

    @property
    def required_inputs(self) -> tuple:
        return self.canonical_machine.required_inputs

    @property
    def optional_inputs(self) -> tuple:
        return self.canonical_machine.optional_inputs

    @property
    def output_types(self) -> tuple:
        return self.canonical_machine.honest_outputs

    @property
    def limitations(self) -> tuple:
        return self.canonical_machine.must_not_claim

    @property
    def validation_inputs(self) -> tuple:
        return self.canonical_machine.validation_requirements

    @property
    def uncertainty_controls(self) -> tuple:
        return self.canonical_machine.uncertainty_controls

    @property
    def safety_notes(self) -> tuple:
        return self.canonical_machine.safety_notes

    @property
    def execution_mode(self) -> str:
        return self.canonical_machine.execution_mode

    @property
    def maturity(self) -> str:
        return self.canonical_machine.maturity

    @property
    def backend_binding(self) -> str:
        return self.canonical_machine.backend_binding

    @property
    def backend_capabilities(self) -> tuple:
        return self.canonical_machine.backend_capabilities

    @property
    def runtime_requirements(self) -> tuple:
        return self.canonical_machine.runtime_requirements

    @property
    def output_data_type(self) -> tuple:
        return self.canonical_machine.output_data_type

    @property
    def active(self) -> bool:
        """Deprecated static capability hint; never a runtime-availability verdict."""
        return self.canonical_machine.has_static_implementation

    def readiness(self) -> str:
        """A static compatibility chip derived solely from the canonical contract."""
        machine = self.canonical_machine
        if machine.maturity == machine_contract.MATURITY_BLUEPRINT:
            return BLUEPRINT
        if machine.execution_mode == EXEC_TRAINED_MODEL_REQUIRED:
            return TRAINED_MODEL_REQUIRED
        if machine.execution_mode == EXEC_MEASURED_DATA_REQUIRED:
            return MEASURED_DATA_REQUIRED
        if machine.execution_mode == EXEC_EVIDENCE_REQUIRED:
            return EVIDENCE_REQUIRED
        if machine.execution_mode == EXEC_DATA_PROCESSING:
            return DATA_PROCESSING
        if machine.maturity == machine_contract.MATURITY_ADVISORY:
            return ADVISORY
        if machine.maturity == machine_contract.MATURITY_LIMITED:
            return PLANNING
        return READY

    def readiness_label(self) -> str:
        return READINESS_LABELS.get(self.readiness(), self.readiness())

    def readiness_badge(self) -> str:
        return READINESS_BADGE.get(self.readiness(), "neutral")

    def to_dict(self) -> dict:
        """JSON-safe compatibility view plus canonical backend/provenance metadata."""
        machine = self.canonical_machine
        return {
            "instrument_id": machine.machine_id,
            "machine_id": machine.machine_id,
            "display_name": machine.display_name,
            "short_description": machine.short_description,
            "category": machine.category,
            "mode": machine.mode,
            "what_it_can_do": machine.what_it_can_do,
            "required_inputs": list(machine.required_inputs),
            "optional_inputs": list(machine.optional_inputs),
            "output_types": list(machine.honest_outputs),
            "output_data_type": list(machine.output_data_type),
            "limitations": list(machine.must_not_claim),
            "validation_inputs": list(machine.validation_requirements),
            "uncertainty_controls": list(machine.uncertainty_controls),
            "safety_notes": list(machine.safety_notes),
            "execution_mode": machine.execution_mode,
            "maturity": machine.maturity,
            "backend_binding": machine.backend_binding,
            "backend_capabilities": list(machine.backend_capabilities),
            "runtime_requirements": list(machine.runtime_requirements),
            "active": self.active,
            "readiness": self.readiness(),
        }


REQUIRED_TEXT_FIELDS = ("instrument_id", "display_name", "category", "mode", "what_it_can_do",
                        "execution_mode", "maturity", "backend_binding")
REQUIRED_LIST_FIELDS = ("required_inputs", "output_types", "limitations", "safety_notes",
                        "runtime_requirements")


def is_valid_mode(mode) -> bool:
    return mode in machine_contract.MODES


def is_valid_execution_mode(execution_mode) -> bool:
    return execution_mode in machine_contract.EXECUTION_MODES


def validate_spec(spec: InstrumentSpec) -> list[str]:
    """Return compatibility-view completeness problems (empty means healthy)."""
    problems: list[str] = []
    for field_name in REQUIRED_TEXT_FIELDS:
        value = getattr(spec, field_name, None)
        if not (isinstance(value, str) and value.strip()):
            problems.append(f"{spec.instrument_id or '?'}: missing/empty text field {field_name!r}")
    for field_name in REQUIRED_LIST_FIELDS:
        value = getattr(spec, field_name, None)
        if not (isinstance(value, (tuple, list)) and value):
            problems.append(f"{spec.instrument_id or '?'}: missing/empty list field {field_name!r}")
    if not is_valid_mode(spec.mode):
        problems.append(f"{spec.instrument_id or '?'}: invalid mode {spec.mode!r}")
    if not is_valid_execution_mode(spec.execution_mode):
        problems.append(f"{spec.instrument_id or '?'}: invalid execution_mode {spec.execution_mode!r}")
    if not isinstance(spec.canonical_machine, machine_contract.VirtualLabMachine):
        problems.append(f"{spec.instrument_id or '?'}: compatibility view lacks canonical source")
    return problems
