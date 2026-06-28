"""Digital Lab — the **instrument router** (deterministic prompt → instrument mapping; no AI, no run).

Given a user prompt (and, optionally, the current agent state), this decides which virtual
instrument(s) fit, what the detected objective is, what inputs are still missing, and what the next
action is. It is **advisory only**:

* it imports **no executor** and **no AI** — pure keyword + domain classification (reusing
  :mod:`flyash_phreeqc_ml.agent.domains` so engine selection stays consistent with the rest of the
  app), and
* it **never runs anything** — :attr:`RoutingResult.auto_run` is always ``False``. The router
  recommends; execution still happens only through the existing confirmation-gated path.

Priority is deliberate and safety-first:

1. An explicit **XRD** intent → the XRD Advisory module (PHREEQC added as *context* if the prompt
   is also about leaching — e.g. "phases to check after NaOH leaching").
2. An explicit **ICP / measured-concentration** intent → the ICP Data Processor (with a
   measured-vs-model *validation* comparison when the prompt asks to compare with PHREEQC/predicted).
3. Other explicit instrument cues (FTIR/Raman, SEM/EDS, TGA/DSC, sustainability, literature).
4. Otherwise fall back to the **domain** classifier: leaching → PHREEQC; a mechanical/strength
   framing → the ML surrogate / mechanical processor (**never** PHREEQC); thermal → TGA/DSC.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..agent import domains
from . import instrument_registry as reg
from . import lab_modes
from . import xrd_advisory

# --------------------------------------------------------------------------- #
# Intent signals (deterministic).
# --------------------------------------------------------------------------- #
_XRD_RE = re.compile(
    r"\b(xrd|x[-\s]?ray\s+diffraction|diffractogram|diffraction\s+pattern|2\s*theta|2θ|"
    r"d[-\s]?spacing|bragg|rietveld|crystalline\s+phase\w*)\b", re.I)
# Measured-concentration / ICP signal: the literal "ICP", or a mass-concentration unit. Millimolar
# ("mM") is checked case-sensitively below so a molarity like ".5M" is never mistaken for it.
_ICP_WORD_RE = re.compile(r"\bicp(?:[-\s]?(?:oes|ms|aes))?\b", re.I)
_CONC_UNIT_RE = re.compile(r"(?:\bmg\s*/\s*l\b|\bmg\s*l\s*-?\s*1\b|\bµg\s*/\s*l\b|\bug\s*/\s*l\b|"
                           r"\bppm\b|\bppb\b|\bmg\s*/\s*kg\b)", re.I)
_COMPARE_RE = re.compile(
    r"\b(compare|comparison|validat\w*|residual\w*|measured\s+vs|vs\.?\s*(?:phreeqc|model|"
    r"predicted|prediction)|against\s+(?:phreeqc|the\s+model|prediction\w*))\b", re.I)
_PREDICTION_REF_RE = re.compile(r"\b(phreeqc|predicted|prediction\w*|model(?:led|ed)?)\b", re.I)
_MEASURED_RE = re.compile(r"\b(measured|i\s+measured|lab\s+result\w*|my\s+(?:icp|data|result\w*))\b",
                          re.I)

_FTIR_RE = re.compile(r"\b(ftir|ft[-\s]?ir|raman|infrared\s+spectro\w*|vibrational\s+spectro\w*)\b",
                      re.I)
_SEM_RE = re.compile(r"\b(sem\b|scanning\s+electron|eds\b|edx\b|edax\b|backscatter\w*|micrograph\w*)\b",
                     re.I)
_TGA_RE = re.compile(r"\b(tga\b|dsc\b|dta\b|thermogravimetr\w*|differential\s+scanning|"
                     r"mass\s+loss\s+curve|calorimetr\w*)\b", re.I)
_SUSTAIN_RE = re.compile(r"\b(sustainab\w*|life[-\s]?cycle|lca\b|carbon\s+footprint|co2\s+footprint|"
                         r"circular\w*|embodied\s+(?:carbon|energy))\b", re.I)
_LIT_RE = re.compile(r"\b(literature|paper\w*|reference\w*|citation\w*|evidence|benchmark\w*|"
                     r"prior\s+work|published)\b", re.I)


@dataclass
class RoutingResult:
    """An advisory routing decision. ``auto_run`` is always False — the router never executes."""

    objective: str
    primary: str | None
    instruments: tuple = ()                  # ordered instrument ids (primary first)
    missing_inputs: tuple = ()
    next_action: str = ""
    warnings: tuple = ()
    validation: bool = False                 # a measured-vs-model comparison is in play
    validation_options: tuple = ()
    uncertainty_options: tuple = ()
    evidence_suggested: bool = False
    rationale: str = ""
    xrd_mode: str = ""                       # which XRD Advisory v2 mode the prompt wants (if XRD)
    xrd_request: dict = field(default_factory=dict)  # detected XRD inputs (measured 2θ, phases)
    auto_run: bool = field(default=False)    # INVARIANT: routing never triggers a run

    def instrument_specs(self) -> list:
        """The :class:`InstrumentSpec` objects for the routed ids (skips any unknown id)."""
        return [s for s in (reg.get(i) for i in self.instruments) if s is not None]

    def primary_spec(self):
        return reg.get(self.primary) if self.primary else None

    def to_card(self) -> dict:
        """A plain dict for the Assistant 'recommended instrument' card (UI renders it)."""
        return {
            "objective": self.objective,
            "primary": self.primary,
            "recommended": [{"id": s.instrument_id, "display_name": s.display_name,
                             "readiness": s.readiness(), "readiness_label": s.readiness_label(),
                             "badge": s.readiness_badge(), "what_it_can_do": s.what_it_can_do}
                            for s in self.instrument_specs()],
            "missing_inputs": list(self.missing_inputs),
            "next_action": self.next_action,
            "warnings": list(self.warnings),
            "validation": self.validation,
            "validation_options": list(self.validation_options),
            "uncertainty_options": list(self.uncertainty_options),
            "evidence_suggested": self.evidence_suggested,
            "rationale": self.rationale,
            "xrd_mode": self.xrd_mode,
            "xrd_request": dict(self.xrd_request),
            "auto_run": self.auto_run,
        }


def _icp_intent(prompt: str) -> bool:
    """True when the prompt is about ICP / measured solution concentrations (not a bare molarity)."""
    return bool(_ICP_WORD_RE.search(prompt) or _CONC_UNIT_RE.search(prompt) or "mM" in prompt)


def _missing_for(primary: str, state) -> tuple:
    """Best-effort 'still needed' inputs for the primary instrument (uses state when available)."""
    if primary == reg.PHREEQC_LEACHING and state is not None:
        try:
            labels = tuple(m["label"] for m in state.missing_card())
            if labels:
                return labels
            if not getattr(state, "composition_usable", False):
                return ("a confirmed material composition",)
            return ()
        except Exception:                                   # noqa: BLE001 - advisory only
            pass
    spec = reg.get(primary)
    return tuple(spec.required_inputs) if spec is not None else ()


def route(prompt, *, state=None, ml_model_available: bool = False,
          validation_mode: bool = False, uncertainty_mode: bool = False,
          evidence_mode: bool = False) -> RoutingResult:
    """Map ``prompt`` to the recommended virtual instrument(s) (advisory; never runs anything).

    ``state`` (optional) sharpens the 'missing inputs' for a PHREEQC route. The three mode flags
    default off; when on they add validation/uncertainty/evidence options to the result. The
    returned :class:`RoutingResult` always has ``auto_run=False``.
    """
    text = str(prompt or "")
    low = text.lower()
    domain = domains.classify(text)
    uncertainty_opts = lab_modes.sensitivity_variables(domain) if uncertainty_mode else ()
    evidence = bool(evidence_mode or _LIT_RE.search(low))

    def _result(objective, primary, instruments, *, next_action, rationale,
                warnings=(), validation=False, missing=None, xrd_mode="", xrd_request=None):
        validation_opts = ()
        if validation or validation_mode:
            validation_opts = ("provide measured ICP / pH data to compare",
                               "enable validation mode (never labels a simulation 'validated')")
        return RoutingResult(
            objective=objective, primary=primary, instruments=tuple(instruments),
            missing_inputs=tuple(missing) if missing is not None else _missing_for(primary, state),
            next_action=next_action, warnings=tuple(warnings),
            validation=bool(validation or validation_mode), validation_options=validation_opts,
            uncertainty_options=uncertainty_opts, evidence_suggested=evidence, rationale=rationale,
            xrd_mode=xrd_mode, xrd_request=dict(xrd_request or {}))

    leaching = domain == domains.LEACHING_GEOCHEMISTRY or bool(
        re.search(r"\bleach\w*|naoh|koh|hcl|dissolv\w*", low))

    # 1) Explicit XRD intent → XRD Advisory v2 (sub-mode detected; PHREEQC as context). ----- #
    if _XRD_RE.search(text):
        xrd_req = xrd_advisory.classify_request(text)
        mode = xrd_req["mode"]
        instruments = [reg.XRD_ADVISORY]
        if mode == xrd_advisory.MODE_MATCH_MEASURED:
            objective = "XRD measured-peak matching (tentative)"
            next_action = ("Open Digital Lab → XRD Advisory → Match Measured Peaks: enter your 2θ "
                           "positions to get TENTATIVE possible phases (never an identification).")
            rationale = ("Prompt gives measured 2θ peaks and asks what phases might match — a "
                         "tentative, position-only matching task, not an identification.")
            missing = ("measured 2θ peak positions (degrees, Cu Kα)",)
        elif mode == xrd_advisory.MODE_PHREEQC_CHECKLIST:
            objective = "XRD phase checklist from a PHREEQC prediction"
            next_action = ("Open Digital Lab → XRD Advisory → PHREEQC Phase Checklist: PHREEQC-"
                           "suggested phases to CHECK by XRD. A saturation prediction is not XRD "
                           "validation.")
            rationale = ("Prompt references PHREEQC-predicted / saturated phases — these become "
                         "candidate phases to check by XRD, not observed phases.")
            missing = ("the PHREEQC-predicted / saturated phase names",)
        elif mode == xrd_advisory.MODE_CONTEXT_CHECKLIST:
            objective = "XRD phase checklist (leaching context)"
            next_action = ("Open Digital Lab → XRD Advisory → Expected Peaks: use the suggested "
                           "context checklist of phases to look for, then compare with measured XRD.")
            rationale = ("Leaching / fly-ash context — an advisory checklist of phases worth checking "
                         "by XRD (planning, not observed phases).")
            missing = ("(optional) the specific phases you expect",)
        elif mode == xrd_advisory.MODE_REFERENCE_NOTES:
            objective = "XRD reference-data coverage"
            next_action = ("Open Digital Lab → XRD Advisory → Reference Data Notes: see which phases "
                           "the internal approximate table covers and which need external data.")
            rationale = "Prompt asks which phases the internal reference table covers."
            missing = ()
        else:
            objective = "XRD expected-peak planning"
            next_action = ("Open Digital Lab → XRD Advisory → Expected Peaks: list the phases to see "
                           "approximate reference peaks, then compare with your measured XRD.")
            rationale = ("Prompt asks about expected XRD peaks for named phases — advisory planning, "
                         "not identification.")
            missing = ("a list of expected phases (or PHREEQC-predicted phases)",)

        warns = ()
        # PHREEQC context: explicit (the prompt names a PHREEQC prediction) or implied by leaching.
        if mode == xrd_advisory.MODE_PHREEQC_CHECKLIST or leaching:
            if reg.PHREEQC_LEACHING not in instruments:
                instruments.append(reg.PHREEQC_LEACHING)
            warns = ("PHREEQC-predicted / saturated phases are MODEL candidates — a saturation index "
                     "is not XRD validation; confirm each by measured XRD.",)
            if mode not in (xrd_advisory.MODE_PHREEQC_CHECKLIST,):
                rationale += (" Leaching context detected — PHREEQC can predict candidate precipitates "
                              "to add to the XRD checklist (after confirmation).")
        return _result(objective, reg.XRD_ADVISORY, instruments, next_action=next_action,
                       rationale=rationale, warnings=warns, missing=missing,
                       xrd_mode=mode, xrd_request=xrd_req)

    # 2) Explicit ICP / measured-concentration intent → ICP Data Processor. ---------------- #
    if _icp_intent(text):
        wants_compare = bool(_COMPARE_RE.search(low)
                             or (_MEASURED_RE.search(low) and _PREDICTION_REF_RE.search(low)))
        instruments = [reg.ICP_DATA_PROCESSOR]
        if wants_compare:
            instruments.append(reg.PHREEQC_LEACHING)
            objective = "ICP data processing + measured-vs-model validation"
            next_action = ("Open Digital Lab → ICP Data Processor: enter measured and predicted "
                           "values; it builds a residual table. PHREEQC supplies the predictions "
                           "(after confirmation).")
            rationale = ("Prompt has measured ICP concentrations and asks to compare with a model "
                         "prediction — data reduction plus a validation comparison.")
        else:
            objective = "ICP unit conversion / data reduction"
            next_action = ("Open Digital Lab → ICP Data Processor: paste your concentration table "
                           "(mg/L, ppm, ppb, or mM) for dilution/blank correction and mM conversion.")
            rationale = "Prompt is about ICP / solution concentrations — a data-reduction task."
        return _result(objective, reg.ICP_DATA_PROCESSOR, instruments, next_action=next_action,
                       rationale=rationale, validation=wants_compare,
                       warnings=("The ICP processor reduces data only — it does not simulate the "
                                 "plasma and never invents measured values.",),
                       missing=("a concentration table (sample_id, element, value, unit)",))

    # 3) Other explicit instrument cues. --------------------------------------------------- #
    if _FTIR_RE.search(low):
        return _result("FTIR / Raman planning", reg.FTIR_RAMAN_INTERPRETER,
                       [reg.FTIR_RAMAN_INTERPRETER],
                       next_action="Open Digital Lab → FTIR / Raman Interpreter (advisory bands).",
                       rationale="Prompt mentions vibrational spectroscopy (advisory only).")
    if _SEM_RE.search(low):
        return _result("SEM / EDS planning", reg.SEM_EDS_PROCESSOR, [reg.SEM_EDS_PROCESSOR],
                       next_action="Open Digital Lab → SEM / EDS Processor (advisory).",
                       rationale="Prompt mentions electron microscopy / EDS (advisory only).")
    if _TGA_RE.search(low):
        return _result("Thermal analysis (TGA/DSC) planning", reg.TGA_DSC_PROCESSOR,
                       [reg.TGA_DSC_PROCESSOR],
                       next_action="Open Digital Lab → TGA / DSC Processor (advisory).",
                       rationale="Prompt mentions thermal analysis (advisory only).")
    if _SUSTAIN_RE.search(low):
        return _result("Sustainability screening", reg.SUSTAINABILITY_SCREENING,
                       [reg.SUSTAINABILITY_SCREENING],
                       next_action="Open Digital Lab → Sustainability Screening (qualitative).",
                       rationale="Prompt mentions sustainability / LCA (qualitative screening only).")

    # 4) Domain fallback. ------------------------------------------------------------------ #
    if domain == domains.LEACHING_GEOCHEMISTRY:
        return _result(
            "Aqueous leaching simulation", reg.PHREEQC_LEACHING, [reg.PHREEQC_LEACHING],
            next_action=("Review and confirm the set-up in the Assistant; I'll build the PHREEQC "
                         "preview and run it only after you confirm."),
            rationale="Leaching / geochemistry framing → the executable PHREEQC engine.")

    if domain in (domains.POLYMER_COMPOSITE, domains.MECHANICAL_TESTING):
        instruments = [reg.ML_SURROGATE_PREDICTOR, reg.MECHANICAL_TEST_PROCESSOR]
        note = ("PHREEQC cannot predict strength — this routes to the (trained-model) surrogate / "
                "mechanical processor, not PHREEQC.")
        next_action = ("This needs a trained mechanical-property model. Use Prediction Models for "
                       "an experimental estimate, or plan the test + data template here.")
        if ml_model_available:
            next_action = ("A trained surrogate may be available — Prediction Models can give an "
                           "experimental (not validated) estimate. PHREEQC is not used for strength.")
        return _result("Mechanical-property prediction", reg.ML_SURROGATE_PREDICTOR, instruments,
                       next_action=next_action, rationale=note, warnings=(note,))

    if domain == domains.THERMAL_TREATMENT:
        return _result("Thermal treatment planning", reg.TGA_DSC_PROCESSOR, [reg.TGA_DSC_PROCESSOR],
                       next_action="Plan TGA/DSC and (optionally) XRD; no thermal engine yet.",
                       rationale="Thermal framing → advisory thermal analysis (no engine yet).")

    if evidence:
        return _result("Literature / evidence support", reg.LITERATURE_EVIDENCE_ENGINE,
                       [reg.LITERATURE_EVIDENCE_ENGINE],
                       next_action="Use the Evidence Library to find sourced benchmarks.",
                       rationale="Prompt asks for literature / evidence support.")

    # Nothing matched confidently — recommend nothing (the Assistant asks a clarifying question).
    return RoutingResult(
        objective="Unclear objective", primary=None, instruments=(),
        missing_inputs=("a clearer description of the goal and material",),
        next_action="Describe the material and what you want to measure or predict.",
        warnings=(), uncertainty_options=uncertainty_opts, evidence_suggested=evidence,
        rationale="No explicit instrument cue and no executable domain detected.")
