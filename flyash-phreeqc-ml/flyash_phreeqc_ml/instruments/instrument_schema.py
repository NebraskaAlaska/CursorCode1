"""Digital Lab — virtual-instrument **metadata schema** (pure data; no AI, no execution).

A "virtual instrument" in this app is deliberately **not** a fake lab device. It is one of a few
honest things:

* a **physical-simulation** engine (PHREEQC aqueous chemistry),
* a **data-processing** module over *measured or predicted* data (the ICP processor),
* a **signal/pattern advisory** that plans a measurement from known references (XRD expected peaks),
* an **advisory / planning** helper, or a **trained-model** predictor.

This module owns the one vocabulary every instrument is described with, so the registry, the
router, and the UI all agree — and so each instrument's *limitations*, *safety notes*, and
*execution mode* travel with it and are impossible to omit. It runs nothing and imports no AI; a
spec is plain metadata. The hard safety rule it encodes: an instrument's ``execution_mode`` is the
contract for *how* (and whether) it may ever run — ``advisory_only`` and ``data_processing`` never
touch the simulation/confirmation path; ``preview_then_confirm`` is the PHREEQC gate; the rest
require a trained model or evidence that the app does not fabricate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Mode — what KIND of instrument this is.
# --------------------------------------------------------------------------- #
MODE_PHYSICAL_SIMULATION = "physical_simulation"     # e.g. PHREEQC aqueous chemistry
MODE_DATA_PROCESSING = "data_processing"             # e.g. ICP mg/L→mM, dilution/blank correction
MODE_SIGNAL_SIMULATION = "signal_simulation"         # e.g. expected XRD peaks from known phases
MODE_ADVISORY_PLANNING = "advisory_planning"         # e.g. sample-prep / QC planning
MODE_TRAINED_MODEL = "trained_model"                 # e.g. an ML surrogate (needs a trained model)

MODES = (MODE_PHYSICAL_SIMULATION, MODE_DATA_PROCESSING, MODE_SIGNAL_SIMULATION,
         MODE_ADVISORY_PLANNING, MODE_TRAINED_MODEL)

# --------------------------------------------------------------------------- #
# Execution mode — HOW (and whether) an instrument may be run. The safety contract.
# --------------------------------------------------------------------------- #
EXEC_ADVISORY_ONLY = "advisory_only"                 # produces guidance/checklists only; never runs
EXEC_DATA_PROCESSING = "data_processing"             # transforms data the user provides; no sim, no run
EXEC_PREVIEW_THEN_CONFIRM = "preview_then_confirm"   # builds a reviewed preview; runs only on confirm
EXEC_TRAINED_MODEL_REQUIRED = "trained_model_required"  # needs an approved trained model to predict
EXEC_EVIDENCE_REQUIRED = "evidence_required"         # needs sourced literature/measured evidence

EXECUTION_MODES = (EXEC_ADVISORY_ONLY, EXEC_DATA_PROCESSING, EXEC_PREVIEW_THEN_CONFIRM,
                   EXEC_TRAINED_MODEL_REQUIRED, EXEC_EVIDENCE_REQUIRED)

# --------------------------------------------------------------------------- #
# Readiness — the short status chip the UI shows (derived from active + execution_mode).
# --------------------------------------------------------------------------- #
READY = "ready"                                      # active physical-sim engine (PHREEQC)
DATA_PROCESSING = "data_processing"                  # active data processor (ICP)
ADVISORY = "advisory"                                # active advisory module (XRD)
PLANNING = "advisory_planning"                       # advisory-only placeholder (no engine yet)
TRAINED_MODEL_REQUIRED = "trained_model_required"    # needs a trained model first
EVIDENCE_REQUIRED = "evidence_required"              # needs sourced evidence first

READINESS_LABELS = {
    READY: "Ready",
    DATA_PROCESSING: "Data processing",
    ADVISORY: "Advisory",
    PLANNING: "Advisory / planning",
    TRAINED_MODEL_REQUIRED: "Trained model required",
    EVIDENCE_REQUIRED: "Evidence required",
}
# Map a readiness value onto the shared app_ui status palette keyword (success/info/warning).
READINESS_BADGE = {
    READY: "success",
    DATA_PROCESSING: "success",
    ADVISORY: "info",
    PLANNING: "warning",
    TRAINED_MODEL_REQUIRED: "warning",
    EVIDENCE_REQUIRED: "warning",
}


@dataclass(frozen=True)
class InstrumentSpec:
    """One virtual instrument's metadata. Immutable; carries its own limitations + safety notes.

    ``active`` is True only for instruments with real Phase-1 behavior (PHREEQC / ICP / XRD); the
    rest are honest metadata/advisory placeholders whose ``limitations`` say so. ``execution_mode``
    is the safety contract — it is *never* relaxed by the router or UI.
    """

    instrument_id: str
    display_name: str
    category: str
    mode: str
    what_it_can_do: str
    required_inputs: tuple = ()
    optional_inputs: tuple = ()
    output_types: tuple = ()
    limitations: tuple = ()
    validation_inputs: tuple = ()
    uncertainty_controls: tuple = ()
    safety_notes: tuple = ()
    execution_mode: str = EXEC_ADVISORY_ONLY
    active: bool = False

    def readiness(self) -> str:
        """The short readiness value (one of the readiness constants), derived deterministically."""
        if not self.active:
            if self.execution_mode == EXEC_TRAINED_MODEL_REQUIRED:
                return TRAINED_MODEL_REQUIRED
            if self.execution_mode == EXEC_EVIDENCE_REQUIRED:
                return EVIDENCE_REQUIRED
            return PLANNING
        if self.execution_mode == EXEC_PREVIEW_THEN_CONFIRM:
            return READY
        if self.execution_mode == EXEC_DATA_PROCESSING:
            return DATA_PROCESSING
        return ADVISORY

    def readiness_label(self) -> str:
        return READINESS_LABELS.get(self.readiness(), self.readiness())

    def readiness_badge(self) -> str:
        """The app_ui status keyword (success/info/warning) for this instrument's chip."""
        return READINESS_BADGE.get(self.readiness(), "neutral")

    def to_dict(self) -> dict:
        """A JSON-safe view (lists, not tuples) for the UI / provenance. No secrets ever."""
        return {
            "instrument_id": self.instrument_id,
            "display_name": self.display_name,
            "category": self.category,
            "mode": self.mode,
            "what_it_can_do": self.what_it_can_do,
            "required_inputs": list(self.required_inputs),
            "optional_inputs": list(self.optional_inputs),
            "output_types": list(self.output_types),
            "limitations": list(self.limitations),
            "validation_inputs": list(self.validation_inputs),
            "uncertainty_controls": list(self.uncertainty_controls),
            "safety_notes": list(self.safety_notes),
            "execution_mode": self.execution_mode,
            "active": self.active,
            "readiness": self.readiness(),
        }


# The metadata fields every spec must carry (used by the registry-completeness test).
REQUIRED_TEXT_FIELDS = ("instrument_id", "display_name", "category", "mode", "what_it_can_do",
                        "execution_mode")
REQUIRED_LIST_FIELDS = ("required_inputs", "output_types", "limitations", "safety_notes")


def is_valid_mode(mode) -> bool:
    return mode in MODES


def is_valid_execution_mode(execution_mode) -> bool:
    return execution_mode in EXECUTION_MODES


def validate_spec(spec: InstrumentSpec) -> list[str]:
    """Return a list of completeness/validity problems for ``spec`` (empty == complete).

    Pure metadata validation: every required text field is a non-empty string, every required list
    field is a non-empty tuple/list, and ``mode`` / ``execution_mode`` are from the known vocabulary.
    Used by the registry-completeness test so a new instrument can never be registered half-described.
    """
    problems: list[str] = []
    for f in REQUIRED_TEXT_FIELDS:
        value = getattr(spec, f, None)
        if not (isinstance(value, str) and value.strip()):
            problems.append(f"{spec.instrument_id or '?'}: missing/empty text field '{f}'")
    for f in REQUIRED_LIST_FIELDS:
        value = getattr(spec, f, None)
        if not (isinstance(value, (tuple, list)) and len(value) > 0):
            problems.append(f"{spec.instrument_id or '?'}: missing/empty list field '{f}'")
    if not is_valid_mode(spec.mode):
        problems.append(f"{spec.instrument_id or '?'}: invalid mode {spec.mode!r}")
    if not is_valid_execution_mode(spec.execution_mode):
        problems.append(f"{spec.instrument_id or '?'}: invalid execution_mode {spec.execution_mode!r}")
    return problems
