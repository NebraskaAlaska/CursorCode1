"""Digital Lab — cross-cutting **modes** (validation / uncertainty / evidence) as design+state support.

Phase 1 implements these as *deterministic state + honest helpers*, not full engines:

* **Validation mode** — only a comparison against **measured** data counts as validation. A
  simulation (or a prediction) on its own is **never** "validated". :func:`assess_validation`
  returns the honest verdict and an ``is_validated`` flag that is True *only* when measured data is
  present to compare against.
* **Uncertainty / sensitivity mode** — suggests which variables to vary (release fraction, L/S,
  reagent concentration, composition uncertainty …). It never fabricates a statistical certainty.
* **Evidence mode** — points at sourced literature/measured evidence; it never invents sources.

These helpers are imported by the instrument router and the UI so the modes mean the same thing
everywhere. The mode flags themselves live on the agent state (see
:class:`flyash_phreeqc_ml.agent.agent_state.AgentState`).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..agent import domains

# Agent-state attribute names for the three toggles (one source of truth for UI + router).
VALIDATION_MODE_ATTR = "validation_mode"
UNCERTAINTY_MODE_ATTR = "uncertainty_mode"
EVIDENCE_MODE_ATTR = "evidence_mode"

# Validation verdict statuses.
VAL_NO_DATA = "no_data"
VAL_SIMULATION_ONLY = "simulation_only"
VAL_MEASURED_ONLY = "measured_only"
VAL_COMPARED = "compared_to_measured"


@dataclass(frozen=True)
class ValidationVerdict:
    """An honest validation verdict. ``is_validated`` is True only with a measured comparison."""

    has_measured: bool
    has_simulation: bool
    status: str
    is_validated: bool
    label: str
    note: str

    def to_dict(self) -> dict:
        return {"has_measured": self.has_measured, "has_simulation": self.has_simulation,
                "status": self.status, "is_validated": self.is_validated, "label": self.label,
                "note": self.note}


_VALIDATION_RULE = (
    "Validation requires comparing against measured data. A simulation or prediction on its own is "
    "never 'validated'.")


def assess_validation(*, has_measured: bool, has_simulation: bool) -> ValidationVerdict:
    """The honest validation verdict for what data is on hand.

    * no measured data → **not validated** (simulation-only or nothing), whatever the model says;
    * measured + a model prediction → a comparison is possible (``is_validated=True``);
    * measured only → nothing to compare against yet.

    ``is_validated`` is True **only** when both measured data and a model prediction exist — so
    validation mode can never label a bare simulation "validated".
    """
    has_measured = bool(has_measured)
    has_simulation = bool(has_simulation)
    if has_measured and has_simulation:
        return ValidationVerdict(
            True, True, VAL_COMPARED, True,
            "Comparable — measured data is available to compare against the model.",
            "Compute residuals (e.g. via the ICP Data Processor) to quantify agreement; agreement "
            "is meaningful only if the mapping is scientifically valid.")
    if has_simulation:
        return ValidationVerdict(
            False, True, VAL_SIMULATION_ONLY, False,
            "Not validated — simulation/prediction only (no measured data).", _VALIDATION_RULE)
    if has_measured:
        return ValidationVerdict(
            True, False, VAL_MEASURED_ONLY, False,
            "Measured data only — no model prediction to compare against yet.",
            "Run the model (after confirmation) to produce predictions, then compare.")
    return ValidationVerdict(
        False, False, VAL_NO_DATA, False,
        "Not validated — no measured data and no model prediction yet.", _VALIDATION_RULE)


# --------------------------------------------------------------------------- #
# Uncertainty / sensitivity
# --------------------------------------------------------------------------- #
_LEACHING_SENSITIVITY = (
    "release fraction (the dominant assumption)",
    "liquid/solid ratio",
    "leachant concentration (e.g. NaOH molarity)",
    "material composition uncertainty",
    "temperature",
)
_GENERIC_SENSITIVITY = (
    "the dominant input assumption",
    "material composition uncertainty",
    "process conditions (time, temperature)",
    "measurement / replicate spread",
)


def sensitivity_variables(domain: str | None = None) -> tuple:
    """Suggested variables to vary for a sensitivity study (no fabricated statistics).

    For the leaching domain these are the levers that actually move a PHREEQC leaching result; for
    other domains a generic list. The caller varies them and re-runs — this never asserts a
    probability or a confidence interval out of thin air.
    """
    if domain == domains.LEACHING_GEOCHEMISTRY:
        return _LEACHING_SENSITIVITY
    return _GENERIC_SENSITIVITY


UNCERTAINTY_DISCLAIMER = (
    "Sensitivity mode suggests which variables to vary and re-run — it does not invent a statistical "
    "certainty. Reported spreads come only from real repeated runs or measured replicates.")


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
EVIDENCE_NOTE = (
    "Evidence mode prefers sourced scholarly literature / measured benchmarks (the Evidence "
    "Library). Cited ranges are context for your experiment, not a substitute for measuring it, and "
    "no source or value is ever invented.")


def evidence_note() -> str:
    return EVIDENCE_NOTE


# --------------------------------------------------------------------------- #
# State helpers (read the three flags off a duck-typed agent state)
# --------------------------------------------------------------------------- #
def modes_from_state(state) -> dict:
    """Read the three mode flags off an agent state (defaulting to False); never raises."""
    return {
        "validation": bool(getattr(state, VALIDATION_MODE_ATTR, False)),
        "uncertainty": bool(getattr(state, UNCERTAINTY_MODE_ATTR, False)),
        "evidence": bool(getattr(state, EVIDENCE_MODE_ATTR, False)),
    }
