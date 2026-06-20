"""Digital Lab — the **instrument registry** (the single catalogue of virtual instruments).

Phase 1 registers ten instruments. Only three carry real behavior today — the PHREEQC leaching
simulator (existing engine), the ICP data processor, and the XRD advisory module (``active=True``).
The rest are **honest metadata/advisory placeholders**: their ``limitations`` say plainly that no
engine exists yet, and their ``execution_mode`` makes the contract explicit (a trained model or
sourced evidence the app will not fabricate). Nothing here runs anything — it is a catalogue.
"""
from __future__ import annotations

from .instrument_schema import (
    EXEC_ADVISORY_ONLY, EXEC_DATA_PROCESSING, EXEC_EVIDENCE_REQUIRED,
    EXEC_PREVIEW_THEN_CONFIRM, EXEC_TRAINED_MODEL_REQUIRED, MODE_ADVISORY_PLANNING,
    MODE_DATA_PROCESSING, MODE_PHYSICAL_SIMULATION, MODE_SIGNAL_SIMULATION, MODE_TRAINED_MODEL,
    InstrumentSpec,
)

# Stable instrument ids (referenced by the router, the UI, and tests).
PHREEQC_LEACHING = "phreeqc_leaching_simulator"
ICP_DATA_PROCESSOR = "icp_data_processor"
XRD_ADVISORY = "xrd_advisory_module"
MECHANICAL_TEST_PROCESSOR = "mechanical_test_processor"   # the brief's "mechanical_testocessor"
ML_SURROGATE_PREDICTOR = "ml_surrogate_predictor"
LITERATURE_EVIDENCE_ENGINE = "literature_evidence_engine"
SUSTAINABILITY_SCREENING = "sustainability_screening"
FTIR_RAMAN_INTERPRETER = "ftir_raman_interpreter"
SEM_EDS_PROCESSOR = "sem_eds_processor"
TGA_DSC_PROCESSOR = "tga_dsc_processor"

_NOT_VALIDATED_NOTE = ("Outputs are model estimates / planning aids, not measured data and not "
                       "validated; validation requires comparison with measured data.")
_NO_FABRICATION_NOTE = "Never fabricates composition, release fractions, measured data, or results."

# --------------------------------------------------------------------------- #
# The registry (definition order = display order).
# --------------------------------------------------------------------------- #
_INSTRUMENTS: tuple[InstrumentSpec, ...] = (
    InstrumentSpec(
        instrument_id=PHREEQC_LEACHING,
        display_name="PHREEQC Leaching Simulator",
        category="Aqueous geochemistry",
        mode=MODE_PHYSICAL_SIMULATION,
        what_it_can_do=("Simulate aqueous leaching / dissolution geochemistry (pH, element "
                        "release, speciation, saturation indices) from a reviewed material "
                        "composition and release model."),
        required_inputs=("material composition (confirmed)", "release model", "solid mass",
                         "liquid volume", "leachant + concentration", "database"),
        optional_inputs=("temperature", "time", "CO2 / cover condition", "target elements"),
        output_types=("predicted pH", "element totals (mM)", "saturation indices",
                      "candidate precipitated phases"),
        limitations=("Aqueous chemistry only — not strength, not thermal, not mechanical.",
                     "A simulation is an estimate under your assumptions, never a measurement.",
                     _NOT_VALIDATED_NOTE),
        validation_inputs=("measured ICP concentrations", "measured pH"),
        uncertainty_controls=("release fraction", "liquid/solid ratio", "leachant concentration",
                              "composition uncertainty"),
        safety_notes=("Runs only after explicit confirmation — it never auto-runs.",
                      _NO_FABRICATION_NOTE),
        execution_mode=EXEC_PREVIEW_THEN_CONFIRM,
        active=True,
    ),
    InstrumentSpec(
        instrument_id=ICP_DATA_PROCESSOR,
        display_name="ICP Data Processor",
        category="Solution chemistry / data reduction",
        mode=MODE_DATA_PROCESSING,
        what_it_can_do=("Reduce measured or predicted ICP solution data: convert mg/L (or ppm / "
                        "ppb) to mM, apply dilution and optional blank correction, flag below-"
                        "detection-limit values, and build a measured-vs-predicted residual table."),
        required_inputs=("a concentration table (sample_id, element, value, unit)",),
        optional_inputs=("dilution_factor", "blank_value", "detection_limit",
                         "measured_or_predicted label"),
        output_types=("corrected concentration table (mM)", "validation residual table",
                      "QC warnings"),
        limitations=("It does NOT simulate the ICP plasma — it only reduces concentration data.",
                     "It never generates measured ICP values from a solid composition alone.",
                     "Conversions need a known element molar mass; unknown elements are flagged."),
        validation_inputs=("measured ICP concentrations paired with model predictions",),
        uncertainty_controls=("dilution factor", "blank value", "detection limit"),
        safety_notes=("Processes only the data you provide; fabricates no measured values.",
                      _NO_FABRICATION_NOTE),
        execution_mode=EXEC_DATA_PROCESSING,
        active=True,
    ),
    InstrumentSpec(
        instrument_id=XRD_ADVISORY,
        display_name="XRD Advisory / Pattern Planning",
        category="Crystalline phase analysis (advisory)",
        mode=MODE_SIGNAL_SIMULATION,
        what_it_can_do=("Plan an XRD measurement: list expected phases, give approximate reference "
                        "2θ peak positions (Cu Kα) for common phases, and turn PHREEQC-predicted "
                        "precipitates into a 'phases to check by XRD' checklist."),
        required_inputs=("a list of expected phases, or PHREEQC-predicted phases",),
        optional_inputs=("leaching context (material + reagent)", "a measured peak list (future)"),
        output_types=("expected-phase checklist", "approximate peak table (advisory)",
                      "overlap / amorphous warnings"),
        limitations=("Expected/checklist only — NOT a measured phase identification.",
                     "Reference peaks are approximate demo values; confirm against a reference DB.",
                     "Quantitative phase fractions and Rietveld refinement are out of scope."),
        validation_inputs=("a measured XRD pattern + a reference pattern database (ICDD PDF)",),
        uncertainty_controls=("peak overlap awareness", "amorphous-content caveat"),
        safety_notes=("Never claims to have identified a phase from no data.",
                      _NO_FABRICATION_NOTE),
        execution_mode=EXEC_ADVISORY_ONLY,
        active=True,
    ),
    # ---- Metadata / advisory placeholders (no engine yet — limitations say so). ---------- #
    InstrumentSpec(
        instrument_id=MECHANICAL_TEST_PROCESSOR,
        display_name="Mechanical Test Processor",
        category="Mechanical testing",
        mode=MODE_TRAINED_MODEL,
        what_it_can_do=("Plan mechanical tests (compressive / flexural strength, modulus) and "
                        "organise measured strength data; predict only via an approved trained "
                        "model."),
        required_inputs=("specimen / mix design", "test standard", "measured strength values"),
        optional_inputs=("curing regime", "specimen geometry", "loading rate"),
        output_types=("test plan / data template", "organised measured-strength table"),
        limitations=("No validated strength engine yet — PHREEQC cannot predict strength.",
                     "Predictions require an approved trained model and remain experimental.",
                     _NOT_VALIDATED_NOTE),
        validation_inputs=("measured strength values",),
        uncertainty_controls=("replicate spread", "specimen variability"),
        safety_notes=("Never reports a strength number without a trained model + data.",
                      _NO_FABRICATION_NOTE),
        execution_mode=EXEC_TRAINED_MODEL_REQUIRED,
        active=False,
    ),
    InstrumentSpec(
        instrument_id=ML_SURROGATE_PREDICTOR,
        display_name="ML Surrogate Predictor",
        category="Surrogate modelling",
        mode=MODE_TRAINED_MODEL,
        what_it_can_do=("Give fast, experimental property estimates (with uncertainty) from an "
                        "approved trained surrogate model — a screening estimate, never a "
                        "measurement or a validated value."),
        required_inputs=("an approved trained model", "the model's input features"),
        optional_inputs=("uncertainty / sensitivity settings",),
        output_types=("experimental prediction with an uncertainty range",),
        limitations=("Available only when a model was trained on approved data for the property.",
                     "A surrogate is not an executable physical simulation and not validation.",
                     _NOT_VALIDATED_NOTE),
        validation_inputs=("held-out measured data",),
        uncertainty_controls=("prediction interval", "input sensitivity"),
        safety_notes=("Produces no number unless a trained model exists.",
                      _NO_FABRICATION_NOTE),
        execution_mode=EXEC_TRAINED_MODEL_REQUIRED,
        active=False,
    ),
    InstrumentSpec(
        instrument_id=LITERATURE_EVIDENCE_ENGINE,
        display_name="Literature Evidence Engine",
        category="Evidence / literature",
        mode=MODE_ADVISORY_PLANNING,
        what_it_can_do=("Find and organise sourced scholarly literature as benchmarks / evidence "
                        "for a material system, and help curate an evidence dataset."),
        required_inputs=("a research question or material system",),
        optional_inputs=("preferred sources", "domain filter"),
        output_types=("sourced evidence candidates", "curated evidence table"),
        limitations=("Provides sourced references — not measured data for your specific sample.",
                     "Cited ranges are context, not validation of your experiment.",
                     _NOT_VALIDATED_NOTE),
        validation_inputs=("sourced literature with extractable measured data",),
        uncertainty_controls=("range across sources",),
        safety_notes=("Never invents citations, values, or sources.", _NO_FABRICATION_NOTE),
        execution_mode=EXEC_EVIDENCE_REQUIRED,
        active=False,
    ),
    InstrumentSpec(
        instrument_id=SUSTAINABILITY_SCREENING,
        display_name="Sustainability Screening",
        category="Sustainability / LCA (screening)",
        mode=MODE_ADVISORY_PLANNING,
        what_it_can_do=("Structure a qualitative sustainability / circularity screening (waste "
                        "reuse, reagent intensity, energy) and list the inventory data a real LCA "
                        "would need."),
        required_inputs=("process route + materials",),
        optional_inputs=("reagent doses", "energy inputs", "transport"),
        output_types=("screening checklist", "LCA data-requirements list"),
        limitations=("Qualitative screening only — NOT a quantified LCA or a carbon footprint.",
                     "No emission factors are assumed or fabricated.",
                     _NOT_VALIDATED_NOTE),
        validation_inputs=("a full life-cycle inventory + characterised impact factors",),
        uncertainty_controls=("inventory completeness",),
        safety_notes=("Never reports a fabricated impact number.", _NO_FABRICATION_NOTE),
        execution_mode=EXEC_ADVISORY_ONLY,
        active=False,
    ),
    InstrumentSpec(
        instrument_id=FTIR_RAMAN_INTERPRETER,
        display_name="FTIR / Raman Interpreter",
        category="Vibrational spectroscopy (advisory)",
        mode=MODE_SIGNAL_SIMULATION,
        what_it_can_do=("Advise which functional groups / bonds to look for and approximate band "
                        "regions to expect for known phases — measurement planning, not "
                        "identification."),
        required_inputs=("expected phases or functional groups of interest",),
        optional_inputs=("a measured spectrum (future)",),
        output_types=("expected-band checklist (advisory)",),
        limitations=("Advisory band regions only — NOT a measured spectral identification.",
                     "No reference band library is shipped yet for most phases.",
                     _NOT_VALIDATED_NOTE),
        validation_inputs=("a measured FTIR/Raman spectrum + reference bands",),
        uncertainty_controls=("band overlap awareness",),
        safety_notes=("Never identifies a phase from no spectrum.", _NO_FABRICATION_NOTE),
        execution_mode=EXEC_ADVISORY_ONLY,
        active=False,
    ),
    InstrumentSpec(
        instrument_id=SEM_EDS_PROCESSOR,
        display_name="SEM / EDS Processor",
        category="Microscopy / microanalysis (advisory)",
        mode=MODE_ADVISORY_PLANNING,
        what_it_can_do=("Plan SEM/EDS imaging and help organise measured EDS elemental data; note "
                        "that EDS is semi-quantitative and surface-sensitive."),
        required_inputs=("imaging objective, or measured EDS elemental data",),
        optional_inputs=("magnification / accelerating voltage", "standards used"),
        output_types=("imaging/analysis plan", "organised EDS data table"),
        limitations=("EDS is semi-quantitative; it does not give bulk composition.",
                     "No automatic phase identification from images.",
                     _NOT_VALIDATED_NOTE),
        validation_inputs=("standard-calibrated EDS measurements",),
        uncertainty_controls=("standardless EDS caveat", "spot-to-spot variability"),
        safety_notes=("Never fabricates EDS percentages.", _NO_FABRICATION_NOTE),
        execution_mode=EXEC_ADVISORY_ONLY,
        active=False,
    ),
    InstrumentSpec(
        instrument_id=TGA_DSC_PROCESSOR,
        display_name="TGA / DSC Processor",
        category="Thermal analysis (advisory)",
        mode=MODE_ADVISORY_PLANNING,
        what_it_can_do=("Plan thermogravimetric / calorimetry runs and help organise measured "
                        "mass-loss / heat-flow curves into labelled steps for interpretation."),
        required_inputs=("a thermal program, or measured TGA/DSC curves",),
        optional_inputs=("atmosphere", "ramp rate", "sample mass"),
        output_types=("thermal-run plan", "organised mass-loss / heat-flow steps"),
        limitations=("No phase-evolution simulation engine yet.",
                     "Step attribution is advisory until confirmed against other methods.",
                     _NOT_VALIDATED_NOTE),
        validation_inputs=("measured TGA/DSC curves + complementary XRD/FTIR",),
        uncertainty_controls=("baseline / buoyancy correction awareness",),
        safety_notes=("Never invents mass-loss values.", _NO_FABRICATION_NOTE),
        execution_mode=EXEC_ADVISORY_ONLY,
        active=False,
    ),
)

_BY_ID: dict[str, InstrumentSpec] = {spec.instrument_id: spec for spec in _INSTRUMENTS}


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #
def all_instruments() -> tuple[InstrumentSpec, ...]:
    """Every registered instrument, in display order."""
    return _INSTRUMENTS


def instrument_ids() -> tuple[str, ...]:
    return tuple(_BY_ID)


def get(instrument_id: str) -> InstrumentSpec | None:
    """The spec for ``instrument_id``, or ``None`` if unknown (never raises)."""
    return _BY_ID.get(instrument_id)


def require(instrument_id: str) -> InstrumentSpec:
    """The spec for ``instrument_id`` or a ``KeyError`` (for callers that need it to exist)."""
    spec = _BY_ID.get(instrument_id)
    if spec is None:
        raise KeyError(f"unknown instrument {instrument_id!r}; known: {', '.join(_BY_ID)}")
    return spec


def active_instruments() -> tuple[InstrumentSpec, ...]:
    """The instruments with real Phase-1 behavior (PHREEQC / ICP / XRD)."""
    return tuple(s for s in _INSTRUMENTS if s.active)


def by_mode(mode: str) -> tuple[InstrumentSpec, ...]:
    return tuple(s for s in _INSTRUMENTS if s.mode == mode)


def display_name(instrument_id: str) -> str:
    spec = _BY_ID.get(instrument_id)
    return spec.display_name if spec is not None else instrument_id
