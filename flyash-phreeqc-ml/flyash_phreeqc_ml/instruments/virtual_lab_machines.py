"""Virtual LAB — the **machine capability catalogue** (backend-only metadata; no execution, no UI).

Virtual LAB gives researchers a mini virtual lab of scientific *machines* (PHREEQC, XRD, ICP,
FTIR/Raman, SEM-EDS, TGA/DSC, Mechanical Testing, ML Prediction, Literature Evidence, Sustainability,
Experimental Design, Validation/Uncertainty). Its purpose is **estimates, simulations, screening, data
processing, and experiment prioritisation** — to help decide which few *physical* experiments are worth
doing. It must **never** claim to replace real experimental validation.

This module is pure, import-safe metadata. It does **not** import Streamlit, does **not** run PHREEQC,
does **not** call external APIs, and is **not** wired into the live website (every machine's
``ui_activation_status`` is :data:`UI_NOT_ACTIVATED`). It only declares — for each machine — what it
can honestly do, what inputs it needs, how its output must be labelled, what it must never claim, and
how a result would be verified in the real world.

The one non-negotiable it encodes: every output carries an honest :data:`OUTPUT_DATA_TYPES` label
(user-provided assumption / synthetic demo / literature evidence / measured lab data / simulated model
estimate / ML prediction / advisory interpretation / validated result), and **a "validated result" is
only ever possible with measured data** — see :func:`machine_can_produce_validated_result` and
:func:`audit_virtual_lab_machines`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# mode — what KIND of machine this is.
# --------------------------------------------------------------------------- #
MODE_PHYSICAL_SIMULATION = "physical_simulation"
MODE_DATA_PROCESSING = "data_processing"
MODE_ADVISORY_PLANNING = "advisory_planning"
MODE_TRAINED_MODEL_PREDICTION = "trained_model_prediction"
MODE_EVIDENCE_ENGINE = "evidence_engine"
MODE_CROSS_CUTTING_VALIDATION = "cross_cutting_validation"

MODES = frozenset({
    MODE_PHYSICAL_SIMULATION, MODE_DATA_PROCESSING, MODE_ADVISORY_PLANNING,
    MODE_TRAINED_MODEL_PREDICTION, MODE_EVIDENCE_ENGINE, MODE_CROSS_CUTTING_VALIDATION,
})

# --------------------------------------------------------------------------- #
# execution_mode — HOW (and whether) a machine may be run. The safety contract.
# --------------------------------------------------------------------------- #
EXEC_ADVISORY_ONLY = "advisory_only"
EXEC_DATA_PROCESSING = "data_processing"
EXEC_PREVIEW_THEN_CONFIRM = "preview_then_confirm"
EXEC_TRAINED_MODEL_REQUIRED = "trained_model_required"
EXEC_EVIDENCE_REQUIRED = "evidence_required"
EXEC_MEASURED_DATA_REQUIRED = "measured_data_required"

EXECUTION_MODES = frozenset({
    EXEC_ADVISORY_ONLY, EXEC_DATA_PROCESSING, EXEC_PREVIEW_THEN_CONFIRM,
    EXEC_TRAINED_MODEL_REQUIRED, EXEC_EVIDENCE_REQUIRED, EXEC_MEASURED_DATA_REQUIRED,
})

# --------------------------------------------------------------------------- #
# status — implementation maturity (honest about what exists vs. blueprint).
# --------------------------------------------------------------------------- #
STATUS_ACTIVE_EXISTING = "active_existing"
STATUS_PHASE_1_ADVISORY = "phase_1_advisory"
STATUS_BLUEPRINT_ONLY = "blueprint_only"
STATUS_REQUIRES_REFERENCE_DATA = "requires_reference_data"
STATUS_REQUIRES_TRAINED_MODEL = "requires_trained_model"
STATUS_REQUIRES_MEASURED_DATA = "requires_measured_data"

STATUSES = frozenset({
    STATUS_ACTIVE_EXISTING, STATUS_PHASE_1_ADVISORY, STATUS_BLUEPRINT_ONLY,
    STATUS_REQUIRES_REFERENCE_DATA, STATUS_REQUIRES_TRAINED_MODEL, STATUS_REQUIRES_MEASURED_DATA,
})

# --------------------------------------------------------------------------- #
# output_data_type — the honest epistemic label every output must carry.
# --------------------------------------------------------------------------- #
OUT_USER_PROVIDED_ASSUMPTION = "user_provided_assumption"
OUT_SYNTHETIC_DEMO_DATA = "synthetic_demo_data"
OUT_LITERATURE_EVIDENCE = "literature_evidence"
OUT_MEASURED_LAB_DATA = "measured_lab_data"
OUT_SIMULATED_MODEL_ESTIMATE = "simulated_model_estimate"
OUT_ML_PREDICTION = "ml_prediction"
OUT_ADVISORY_INTERPRETATION = "advisory_interpretation"
OUT_VALIDATED_RESULT = "validated_result"

OUTPUT_DATA_TYPES = frozenset({
    OUT_USER_PROVIDED_ASSUMPTION, OUT_SYNTHETIC_DEMO_DATA, OUT_LITERATURE_EVIDENCE,
    OUT_MEASURED_LAB_DATA, OUT_SIMULATED_MODEL_ESTIMATE, OUT_ML_PREDICTION,
    OUT_ADVISORY_INTERPRETATION, OUT_VALIDATED_RESULT,
})

# Backend-only: no machine here is wired into the live website UI.
UI_NOT_ACTIVATED = "not_activated_backend_only"

# Stable machine ids.
PHREEQC_LEACHING = "phreeqc_leaching_simulator"
XRD_ADVISORY = "xrd_advisory"
ICP_PROCESSOR = "icp_data_processor"
FTIR_RAMAN = "ftir_raman_interpreter"
SEM_EDS = "sem_eds_processor"
TGA_DSC = "tga_dsc_processor"
MECHANICAL = "mechanical_testing_processor"
ML_SURROGATE = "ml_surrogate_predictor"
LITERATURE_ENGINE = "literature_evidence_engine"
SUSTAINABILITY = "sustainability_cost_screening"
EXPERIMENTAL_DESIGN = "experimental_design_assistant"
VALIDATION_UNCERTAINTY = "validation_uncertainty_assistant"


@dataclass(frozen=True)
class VirtualLabMachine:
    """One Virtual LAB machine's honest capability + safety contract. Immutable metadata; runs nothing.

    Every field is plain data. The safety-critical promise is that ``output_data_type`` always carries
    an honest label, ``must_not_claim`` / ``safety_notes`` / ``verification_required`` are never empty,
    and a ``validated_result`` is gated on measured data (enforced by the audit + helpers).
    """

    machine_id: str
    display_name: str
    short_description: str
    category: str
    mode: str
    execution_mode: str
    status: str
    what_it_can_do: str
    required_inputs: tuple = ()
    optional_inputs: tuple = ()
    honest_outputs: tuple = ()
    output_data_type: tuple = ()
    verification_required: tuple = ()
    real_world_verification_method: str = ""
    must_not_claim: tuple = ()
    needs_measured_data: bool = False
    needs_trained_model: bool = False
    needs_reference_database: bool = False
    can_run_live: bool = False
    should_use_cached_or_precomputed_data: bool = False
    uncertainty_controls: tuple = ()
    safety_notes: tuple = ()
    example_user_prompts: tuple = ()
    future_backend_dependencies: tuple = ()
    ui_activation_status: str = UI_NOT_ACTIVATED

    def to_dict(self) -> dict:
        """A JSON-safe view (lists, not tuples). Pure metadata; never any secret or measured value."""
        return {
            "machine_id": self.machine_id,
            "display_name": self.display_name,
            "short_description": self.short_description,
            "category": self.category,
            "mode": self.mode,
            "execution_mode": self.execution_mode,
            "status": self.status,
            "what_it_can_do": self.what_it_can_do,
            "required_inputs": list(self.required_inputs),
            "optional_inputs": list(self.optional_inputs),
            "honest_outputs": list(self.honest_outputs),
            "output_data_type": list(self.output_data_type),
            "verification_required": list(self.verification_required),
            "real_world_verification_method": self.real_world_verification_method,
            "must_not_claim": list(self.must_not_claim),
            "needs_measured_data": self.needs_measured_data,
            "needs_trained_model": self.needs_trained_model,
            "needs_reference_database": self.needs_reference_database,
            "can_run_live": self.can_run_live,
            "should_use_cached_or_precomputed_data": self.should_use_cached_or_precomputed_data,
            "uncertainty_controls": list(self.uncertainty_controls),
            "safety_notes": list(self.safety_notes),
            "example_user_prompts": list(self.example_user_prompts),
            "future_backend_dependencies": list(self.future_backend_dependencies),
            "ui_activation_status": self.ui_activation_status,
        }


# Fields that must be a non-empty string / non-empty tuple for every machine (audit completeness).
REQUIRED_TEXT_FIELDS = ("machine_id", "display_name", "short_description", "category", "mode",
                        "execution_mode", "status", "what_it_can_do",
                        "real_world_verification_method", "ui_activation_status")
REQUIRED_TUPLE_FIELDS = ("required_inputs", "honest_outputs", "output_data_type",
                         "verification_required", "must_not_claim", "safety_notes")


# --------------------------------------------------------------------------- #
# The catalogue (definition order = display order). Pure metadata; nothing runs.
# --------------------------------------------------------------------------- #
_MACHINES: tuple[VirtualLabMachine, ...] = (
    VirtualLabMachine(
        machine_id=PHREEQC_LEACHING,
        display_name="PHREEQC Leaching Simulator",
        short_description=("Estimate aqueous leaching / dissolution chemistry from a reviewed "
                           "composition and release assumptions — a simulation, not a measurement."),
        category="Aqueous geochemistry",
        mode=MODE_PHYSICAL_SIMULATION,
        execution_mode=EXEC_PREVIEW_THEN_CONFIRM,
        status=STATUS_ACTIVE_EXISTING,
        what_it_can_do=("Estimate pH, element release (mM), speciation, and saturation indices for a "
                        "confirmed composition + leachant + source term/release assumption + database "
                        "+ temperature + liquid/solid ratio. Builds a reviewable input preview first."),
        required_inputs=("confirmed material composition", "leachant + concentration",
                         "release model / source term assumption", "thermodynamic database",
                         "liquid/solid ratio"),
        optional_inputs=("temperature", "time", "CO2 / cover condition", "target elements"),
        honest_outputs=("predicted pH (model estimate)", "element totals in mM (model estimate)",
                        "saturation indices", "candidate precipitated phases to check by XRD"),
        output_data_type=(OUT_SIMULATED_MODEL_ESTIMATE,),
        verification_required=("measured pH", "measured ICP leachate concentrations",
                               "measured leachate chemistry for comparison"),
        real_world_verification_method=("Run the bench leaching test and measure pH + ICP solution "
                                        "concentrations, then compare against the simulated estimate."),
        must_not_claim=("validation (a simulation alone is never validated)",
                        "compressive strength or any mechanical property",
                        "XRD phase identification", "ICP plasma simulation",
                        "real lab behaviour without a measured comparison"),
        needs_measured_data=False,
        needs_trained_model=False,
        needs_reference_database=True,
        can_run_live=True,
        should_use_cached_or_precomputed_data=True,
        uncertainty_controls=("release fraction", "liquid/solid ratio", "leachant concentration",
                              "composition uncertainty"),
        safety_notes=("Runs only after an explicit preview → confirmation; it never auto-runs.",
                      "A simulation is an estimate under your assumptions, never a measurement.",
                      "Never fabricates composition, release fractions, or measured chemistry."),
        example_user_prompts=("leach Class C fly ash with 0.5 M NaOH and estimate pH and Ca, Si",
                              "preview a PHREEQC input for my confirmed composition"),
        future_backend_dependencies=("existing confirmation-gated PHREEQC executor",),
    ),
    VirtualLabMachine(
        machine_id=XRD_ADVISORY,
        display_name="XRD Advisory / Pattern Planning",
        short_description=("Plan an XRD measurement: approximate expected peaks, tentative matching of "
                           "user-measured peaks, and a PHREEQC-derived phase checklist — advisory only."),
        category="Crystalline phase analysis (advisory)",
        mode=MODE_ADVISORY_PLANNING,
        execution_mode=EXEC_ADVISORY_ONLY,
        status=STATUS_PHASE_1_ADVISORY,
        what_it_can_do=("Suggest approximate Cu Kα reference peaks for known phase NAMES, tentatively "
                        "match a user-provided measured peak list, and turn PHREEQC-predicted phases "
                        "into a 'phases to check by XRD' checklist."),
        required_inputs=("a list of expected phase names, OR a user-measured 2θ peak list, OR "
                         "PHREEQC-predicted phases",),
        optional_inputs=("leaching context (material + reagent)", "match tolerance (degrees 2θ)"),
        honest_outputs=("approximate expected peaks (advisory)", "tentative possible-phase matches",
                        "phase checklist to verify by measured XRD"),
        output_data_type=(OUT_ADVISORY_INTERPRETATION,),
        verification_required=("a measured XRD pattern", "a reference pattern database (ICDD PDF)",
                               "human expert review"),
        real_world_verification_method=("Collect a measured XRD pattern and compare against reference "
                                        "patterns (ICDD PDF / structure data) with expert review."),
        must_not_claim=("confirmed phase identification from a formula alone",
                        "an exact diffraction pattern without phase/structure/reference data",
                        "validation or a measured identification"),
        needs_measured_data=False,
        needs_trained_model=False,
        needs_reference_database=True,
        can_run_live=True,
        should_use_cached_or_precomputed_data=True,
        uncertainty_controls=("peak overlap awareness", "amorphous-content caveat",
                              "tentative match confidence (never 'high' from one peak)"),
        safety_notes=("A FORMULA alone cannot fix a pattern (e.g. CaCO3 = calcite / aragonite / "
                      "vaterite — polymorph/phase ambiguity); a phase + reference structure is needed.",
                      "Matches are 'tentatively consistent with', never 'identified / confirmed'.",
                      "Internal peaks are approximate teaching/advisory references, not ICDD standards."),
        example_user_prompts=("expected XRD peaks for calcite, quartz, portlandite",
                              "I measured peaks at 26.6, 29.4, 34.1 2theta — what might match?"),
        future_backend_dependencies=("external reference patterns (ICDD PDF / CIF / pymatgen) — deferred",),
    ),
    VirtualLabMachine(
        machine_id=ICP_PROCESSOR,
        display_name="ICP-OES / ICP-MS Data Processor",
        short_description=("Reduce measured or user-supplied ICP concentration tables — it processes "
                           "data you provide; it does not simulate the plasma."),
        category="Solution chemistry / data reduction",
        mode=MODE_DATA_PROCESSING,
        execution_mode=EXEC_DATA_PROCESSING,
        status=STATUS_ACTIVE_EXISTING,
        what_it_can_do=("Convert mg/L (or ppm / ppb) to mM, apply dilution and optional blank "
                        "correction, flag below-detection-limit values, and build measured-vs-predicted "
                        "residuals from the rows you provide."),
        required_inputs=("a concentration table (sample_id, element, value, unit)",),
        optional_inputs=("dilution_factor", "blank_value", "detection_limit",
                         "measured_or_predicted label"),
        honest_outputs=("corrected concentration table (mM)", "QC warnings (advisory)",
                        "measured-vs-predicted residual table"),
        output_data_type=(OUT_MEASURED_LAB_DATA, OUT_ADVISORY_INTERPRETATION),
        verification_required=("the actual instrument run / user-uploaded measured data",
                               "QA/QC standards where relevant"),
        real_world_verification_method=("Compare against the calibrated ICP-OES/ICP-MS instrument run "
                                        "with standards, blanks, and certified reference materials."),
        must_not_claim=("simulation of the ICP plasma",
                        "fabricated / invented measured concentrations",
                        "a measured label for predicted or user-assumed values"),
        needs_measured_data=False,
        needs_trained_model=False,
        needs_reference_database=False,
        can_run_live=True,
        should_use_cached_or_precomputed_data=False,
        uncertainty_controls=("dilution factor", "blank value", "detection limit"),
        safety_notes=("It reduces only the rows you provide; it never fabricates measured values.",
                      "The output label mirrors the INPUT: measured in → measured out; predicted in → "
                      "model-estimate out. It never derives measured ICP values from a solid alone.",
                      "Validation residuals come only from real measured + predicted pairs."),
        example_user_prompts=("convert my ICP Ca 84 mg/L and Si 28 mg/L to mM with dilution 10",
                              "compare my measured ICP with PHREEQC predicted concentrations"),
        future_backend_dependencies=("existing ICP processor module",),
    ),
    VirtualLabMachine(
        machine_id=FTIR_RAMAN,
        display_name="FTIR / Raman Interpreter",
        short_description=("Help interpret USER-SUPPLIED FTIR/Raman spectra against reference bands — "
                           "advisory band assignment, never a definitive identification."),
        category="Vibrational spectroscopy (advisory)",
        mode=MODE_ADVISORY_PLANNING,
        execution_mode=EXEC_EVIDENCE_REQUIRED,
        status=STATUS_BLUEPRINT_ONLY,
        what_it_can_do=("Given a user-supplied spectrum, suggest possible functional groups / bonds and "
                        "approximate band regions to look for, and compare against reference bands."),
        required_inputs=("a user-supplied (measured) FTIR/Raman spectrum or peak list",
                         "a reference band library / sourced reference assignments"),
        optional_inputs=("expected phases / functional groups of interest", "instrument + resolution"),
        honest_outputs=("candidate functional-group / band assignments (advisory)",
                        "a 'bands to confirm' checklist"),
        output_data_type=(OUT_ADVISORY_INTERPRETATION,),
        verification_required=("the measured spectrum itself", "a reference band library",
                               "human expert review"),
        real_world_verification_method=("Compare the measured spectrum against reference standards and "
                                        "complementary methods (XRD/TGA) under expert review."),
        must_not_claim=("definitive compound identification from weak or single bands",
                        "fabricated spectra or invented peaks",
                        "phase confirmation from spectroscopy alone"),
        needs_measured_data=True,
        needs_trained_model=False,
        needs_reference_database=True,
        can_run_live=True,
        should_use_cached_or_precomputed_data=True,
        uncertainty_controls=("band overlap awareness", "signal-to-noise / weak-band caveat"),
        safety_notes=("Strong claims require a MEASURED spectrum + a REFERENCE band library + human "
                      "review; weak/single bands are advisory only.",
                      "Never fabricates a spectrum and never identifies a compound from no data."),
        example_user_prompts=("what FTIR bands suggest carbonate vs C-S-H in my spectrum?",
                              "help interpret my Raman peaks against reference bands"),
        future_backend_dependencies=("a sourced reference band library", "spectrum upload/parsing"),
    ),
    VirtualLabMachine(
        machine_id=SEM_EDS,
        display_name="SEM-EDS Processor",
        short_description=("Organise MEASURED SEM images and EDS elemental tables/maps and compare "
                           "regions — semi-quantitative, never an exact phase identification."),
        category="Microscopy / microanalysis",
        mode=MODE_DATA_PROCESSING,
        execution_mode=EXEC_MEASURED_DATA_REQUIRED,
        status=STATUS_REQUIRES_MEASURED_DATA,
        what_it_can_do=("Organise measured SEM images and EDS elemental tables/maps, compare regions, "
                        "and surface QC warnings (semi-quantitative, surface-sensitive)."),
        required_inputs=("measured SEM images and/or measured EDS elemental data",),
        optional_inputs=("magnification / accelerating voltage", "standards used", "region labels"),
        honest_outputs=("organised EDS elemental tables/maps (measured data)",
                        "region comparisons + QC warnings (advisory)"),
        output_data_type=(OUT_MEASURED_LAB_DATA, OUT_ADVISORY_INTERPRETATION),
        verification_required=("measured SEM/EDS data", "calibration standards where relevant",
                               "complementary phase methods (XRD)"),
        real_world_verification_method=("Acquire calibrated SEM/EDS with standards and confirm phases "
                                        "with complementary XRD; expert review of maps."),
        must_not_claim=("exact mineral phase identification from EDS elements alone",
                        "fabricated images or elemental maps",
                        "quantitative certainty without standards (EDS is semi-quantitative)"),
        needs_measured_data=True,
        needs_trained_model=False,
        needs_reference_database=False,
        can_run_live=True,
        should_use_cached_or_precomputed_data=False,
        uncertainty_controls=("standardless-EDS caveat", "spot-to-spot variability",
                              "surface-sensitivity caveat"),
        safety_notes=("Requires MEASURED SEM/EDS data — it never fabricates images or maps.",
                      "EDS gives elements, not phases; exact phase ID needs XRD + standards + review."),
        example_user_prompts=("organise my EDS spot data and compare two regions",
                              "what QC issues are in my standardless EDS table?"),
        future_backend_dependencies=("image/EDS upload + parsing", "optional standards database"),
    ),
    VirtualLabMachine(
        machine_id=TGA_DSC,
        display_name="TGA / DSC Processor",
        short_description=("Process USER-SUPPLIED mass-loss and heat-flow curves: mark thermal events, "
                           "compare samples, estimate event temperatures — advisory attribution only."),
        category="Thermal analysis",
        mode=MODE_DATA_PROCESSING,
        execution_mode=EXEC_MEASURED_DATA_REQUIRED,
        status=STATUS_REQUIRES_MEASURED_DATA,
        what_it_can_do=("Process measured TGA/DSC curves: identify mass-loss / heat-flow steps, "
                        "estimate event temperatures, and compare samples."),
        required_inputs=("measured TGA and/or DSC curves (temperature vs mass / heat flow)",),
        optional_inputs=("atmosphere", "ramp rate", "sample mass"),
        honest_outputs=("labelled thermal events / steps (from measured curves)",
                        "estimated event temperatures + sample comparisons (advisory attribution)"),
        output_data_type=(OUT_MEASURED_LAB_DATA, OUT_ADVISORY_INTERPRETATION),
        verification_required=("measured TGA/DSC curves",
                               "complementary methods (XRD/FTIR) for event attribution"),
        real_world_verification_method=("Run TGA/DSC on the instrument and corroborate step "
                                        "attributions with complementary XRD/FTIR and expert review."),
        must_not_claim=("fabricated mass-loss or heat-flow curves",
                        "definitive phase assignment of a step without supporting evidence",
                        "a validated reaction mechanism from a single curve"),
        needs_measured_data=True,
        needs_trained_model=False,
        needs_reference_database=False,
        can_run_live=True,
        should_use_cached_or_precomputed_data=False,
        uncertainty_controls=("baseline / buoyancy correction awareness", "overlapping-event caveat"),
        safety_notes=("Requires MEASURED curves — it never fabricates a curve.",
                      "Step attributions are advisory until confirmed by other methods."),
        example_user_prompts=("mark the mass-loss steps in my TGA curve and estimate temperatures",
                              "compare DSC heat flow between my two cured samples"),
        future_backend_dependencies=("curve upload + parsing",),
    ),
    VirtualLabMachine(
        machine_id=MECHANICAL,
        display_name="Mechanical Testing Processor",
        short_description=("Process MEASURED compressive/flexural strength data: averages, std dev, "
                           "strength-vs-age plots, formulation comparisons — never predicts strength."),
        category="Mechanical testing",
        mode=MODE_DATA_PROCESSING,
        execution_mode=EXEC_MEASURED_DATA_REQUIRED,
        status=STATUS_REQUIRES_MEASURED_DATA,
        what_it_can_do=("Process measured strength data: compute averages and standard deviation, plot "
                        "strength vs curing age, and compare formulations from replicate measurements."),
        required_inputs=("measured compressive / flexural strength values (with specimen + age)",),
        optional_inputs=("curing regime", "specimen geometry", "test standard", "loading rate"),
        honest_outputs=("organised measured-strength tables, means and std dev",
                        "strength-vs-age plots and formulation comparisons (from measured data)"),
        output_data_type=(OUT_MEASURED_LAB_DATA,),
        verification_required=("physical mechanical testing to a standard",
                               "enough replicates for a meaningful spread"),
        real_world_verification_method=("Cast and test specimens to the relevant standard "
                                        "(e.g. ASTM/EN) with adequate replicates."),
        must_not_claim=("any strength value before specimens are physically tested",
                        "code/standard compliance without testing to that standard",
                        "validated performance without enough replicates"),
        needs_measured_data=True,
        needs_trained_model=False,
        needs_reference_database=False,
        can_run_live=True,
        should_use_cached_or_precomputed_data=False,
        uncertainty_controls=("replicate spread (std dev)", "specimen variability"),
        safety_notes=("Requires MEASURED strength data — it never reports a strength number without "
                      "physical testing.",
                      "PHREEQC and chemistry tools cannot predict strength; only measured data or an "
                      "approved trained model can estimate it (and an estimate is not a measurement)."),
        example_user_prompts=("average my 7/28-day compressive strength and plot vs age",
                              "compare measured strength of two mix designs with std dev"),
        future_backend_dependencies=("strength-data upload + schema",),
    ),
    VirtualLabMachine(
        machine_id=ML_SURROGATE,
        display_name="ML Surrogate Predictor",
        short_description=("Give an experimental property estimate (with uncertainty) ONLY when an "
                           "approved trained model + provenance + domain limits exist — never validated."),
        category="Surrogate modelling",
        mode=MODE_TRAINED_MODEL_PREDICTION,
        execution_mode=EXEC_TRAINED_MODEL_REQUIRED,
        status=STATUS_REQUIRES_TRAINED_MODEL,
        what_it_can_do=("Predict a property only when a trained model, a feature schema, training-data "
                        "provenance, and uncertainty/domain limits are all present — a screening "
                        "estimate with an uncertainty range, never a measurement."),
        required_inputs=("an approved trained model", "the model's feature schema",
                         "training-data provenance", "the input features for the prediction"),
        optional_inputs=("uncertainty / domain-of-applicability settings",),
        honest_outputs=("an experimental ML prediction with an uncertainty range and a domain flag",),
        output_data_type=(OUT_ML_PREDICTION,),
        verification_required=("a measured validation dataset (held-out / external)",),
        real_world_verification_method=("Compare predictions against measured held-out / external lab "
                                        "data; report error metrics with provenance."),
        must_not_claim=("accuracy or reliability without a measured validation comparison",
                        "predictions outside the training domain as reliable",
                        "validated material performance without measured comparison"),
        needs_measured_data=False,
        needs_trained_model=True,
        needs_reference_database=False,
        can_run_live=False,
        should_use_cached_or_precomputed_data=False,
        uncertainty_controls=("prediction interval", "domain-of-applicability flag", "input sensitivity"),
        safety_notes=("Produces no number unless an approved trained model + provenance exist.",
                      "A surrogate prediction is an experimental estimate, not a simulation and not "
                      "validation."),
        example_user_prompts=("predict 28-day strength from my mix features (if a model exists)",
                              "what uncertainty does the surrogate give for this composition?"),
        future_backend_dependencies=("a trained, approved surrogate model + feature schema + provenance",),
    ),
    VirtualLabMachine(
        machine_id=LITERATURE_ENGINE,
        display_name="Literature Evidence Engine",
        short_description=("Ingest DOI metadata, user-uploaded PDFs, and allowed APIs to surface "
                           "candidate evidence with provenance — unreviewed until a human checks it."),
        category="Evidence / literature",
        mode=MODE_EVIDENCE_ENGINE,
        execution_mode=EXEC_EVIDENCE_REQUIRED,
        status=STATUS_BLUEPRINT_ONLY,
        what_it_can_do=("Ingest DOI metadata, user-uploaded PDFs, and permitted/licensed APIs; extract "
                        "candidate evidence with source provenance and a confidence flag for review."),
        required_inputs=("a research question or material system",
                         "permitted sources (DOIs, user-uploaded PDFs, or licensed APIs)"),
        optional_inputs=("domain filter", "preferred sources"),
        honest_outputs=("candidate (unreviewed) evidence with provenance and a confidence flag",
                        "a curated evidence table after human review"),
        output_data_type=(OUT_LITERATURE_EVIDENCE,),
        verification_required=("human expert review before any evidence is used for a claim",
                               "source provenance for every extracted value"),
        real_world_verification_method=("A human reviews each candidate against the cited source and "
                                        "confirms provenance before it informs training or claims."),
        must_not_claim=("that surfaced evidence is automatically reviewed truth",
                        "validation of your specific experiment from literature",
                        "scraping restricted sources such as Google Scholar"),
        needs_measured_data=False,
        needs_trained_model=False,
        needs_reference_database=False,
        can_run_live=False,
        should_use_cached_or_precomputed_data=True,
        uncertainty_controls=("range across sources", "per-claim confidence flag"),
        safety_notes=("Every extracted value keeps its SOURCE PROVENANCE and stays 'candidate / "
                      "unreviewed' until HUMAN REVIEW.",
                      "Never invents citations, values, or sources; never scrapes restricted sources "
                      "(e.g. Google Scholar) — only DOIs, user PDFs, and licensed APIs.",
                      "Literature evidence is context, not validation of your sample."),
        example_user_prompts=("find sourced leaching benchmarks for Class C fly ash with DOIs",
                              "extract candidate strength ranges from these uploaded PDFs"),
        future_backend_dependencies=("a provenance-tracked evidence store", "licensed citation APIs"),
    ),
    VirtualLabMachine(
        machine_id=SUSTAINABILITY,
        display_name="Sustainability / Cost Screening",
        short_description=("Order-of-magnitude sustainability/cost screening from USER assumptions — "
                           "not a certified LCA/TEA and not a feasibility verdict."),
        category="Sustainability / cost (screening)",
        mode=MODE_ADVISORY_PLANNING,
        execution_mode=EXEC_ADVISORY_ONLY,
        status=STATUS_BLUEPRINT_ONLY,
        what_it_can_do=("Do an ORDER-OF-MAGNITUDE screening when the user provides assumptions "
                        "(energy, reagent use, transport, waste handling, CO2 factors, cost factors), "
                        "and list the inventory data a real LCA/TEA would need."),
        required_inputs=("user-provided assumptions (energy, reagents, transport, CO2 / cost factors)",),
        optional_inputs=("process route + materials", "allocation choices"),
        honest_outputs=("an order-of-magnitude screening estimate (from your assumptions)",
                        "an LCA/TEA data-requirements checklist (advisory)"),
        output_data_type=(OUT_ADVISORY_INTERPRETATION, OUT_USER_PROVIDED_ASSUMPTION),
        verification_required=("full LCA/TEA system boundaries, inventory data, and review",
                               "characterised impact / cost factors from sourced data"),
        real_world_verification_method=("Build a full life-cycle inventory and techno-economic model "
                                        "with defined boundaries and reviewed, sourced factors."),
        must_not_claim=("a final / certified LCA or TEA",
                        "certified carbon savings",
                        "economic feasibility without reviewed assumptions and boundaries"),
        needs_measured_data=False,
        needs_trained_model=False,
        needs_reference_database=False,
        can_run_live=True,
        should_use_cached_or_precomputed_data=False,
        uncertainty_controls=("inventory completeness", "assumption sensitivity / ranges"),
        safety_notes=("ORDER-OF-MAGNITUDE screening only — never a quantified LCA, carbon footprint, "
                      "or feasibility verdict.",
                      "Every number is derived from YOUR assumptions and is labelled as such; no "
                      "emission/cost factors are invented."),
        example_user_prompts=("rough CO2 screening for replacing cement with fly ash, my assumptions",
                              "what inventory data would a full LCA of this route need?"),
        future_backend_dependencies=("a sourced emission/cost-factor library", "an LCA/TEA boundary model"),
    ),
    VirtualLabMachine(
        machine_id=EXPERIMENTAL_DESIGN,
        display_name="Experimental Design Assistant",
        short_description=("Suggest experiment matrices, controls, replicates, missing measurements, "
                           "and verification plans — planning advice, not results."),
        category="Experiment planning (advisory)",
        mode=MODE_ADVISORY_PLANNING,
        execution_mode=EXEC_ADVISORY_ONLY,
        status=STATUS_BLUEPRINT_ONLY,
        what_it_can_do=("Suggest an experiment matrix, controls, replicate counts, the measurements "
                        "still missing, and a verification plan to reduce trial-and-error."),
        required_inputs=("a research goal and the candidate materials / factors",),
        optional_inputs=("constraints (budget, instruments, time)", "prior results"),
        honest_outputs=("a proposed experiment matrix + controls + replicates (advisory)",
                        "a list of missing measurements and a verification plan"),
        output_data_type=(OUT_ADVISORY_INTERPRETATION,),
        verification_required=("actually running the experiments",),
        real_world_verification_method="Execute the designed experiments and record measured results.",
        must_not_claim=("experimental results before the experiments are run",
                        "that a plan substitutes for measured data"),
        needs_measured_data=False,
        needs_trained_model=False,
        needs_reference_database=False,
        can_run_live=True,
        should_use_cached_or_precomputed_data=False,
        uncertainty_controls=("replicate planning", "control / confounder identification"),
        safety_notes=("Plans experiments; it never reports outcomes of experiments not yet run.",
                      "Prioritises which FEW physical experiments are worth doing — it does not "
                      "replace them."),
        example_user_prompts=("design a leaching study matrix with controls and replicates",
                              "what measurements am I missing to validate this hypothesis?"),
        future_backend_dependencies=("optional DOE helper library",),
    ),
    VirtualLabMachine(
        machine_id=VALIDATION_UNCERTAINTY,
        display_name="Validation & Uncertainty Assistant",
        short_description=("Compare MEASURED vs simulated/predicted values, compute residuals and "
                           "sensitivity, and label validation status — validated only WITH measured data."),
        category="Validation / uncertainty (cross-cutting)",
        mode=MODE_CROSS_CUTTING_VALIDATION,
        execution_mode=EXEC_MEASURED_DATA_REQUIRED,
        status=STATUS_REQUIRES_MEASURED_DATA,
        what_it_can_do=("Compare measured values against simulated/predicted ones, compute residuals, "
                        "show sensitivity to assumptions, and label the validation status honestly."),
        required_inputs=("measured data", "the simulated / predicted values to compare against"),
        optional_inputs=("uncertainty / sensitivity settings", "tolerance bands"),
        honest_outputs=("residuals + sensitivity (advisory) when only estimates exist",
                        "a 'validated_result' label ONLY when a measured comparison exists"),
        output_data_type=(OUT_ADVISORY_INTERPRETATION, OUT_VALIDATED_RESULT),
        verification_required=("measured data for the comparison",),
        real_world_verification_method=("Pair model/predicted values with measured lab data and "
                                        "evaluate residuals against a stated acceptance criterion."),
        must_not_claim=("validation without measured data",
                        "that a simulation, ML prediction, or literature value is 'validated' on its own"),
        needs_measured_data=True,
        needs_trained_model=False,
        needs_reference_database=False,
        can_run_live=True,
        should_use_cached_or_precomputed_data=False,
        uncertainty_controls=("residual distribution", "assumption sensitivity", "acceptance tolerance"),
        safety_notes=("'validated_result' is possible ONLY with measured data — no measured data, no "
                      "validation.",
                      "Simulation is not validation; ML prediction is not validation; literature is not "
                      "validation — only a measured comparison is."),
        example_user_prompts=("compare my measured pH with the PHREEQC estimate and show residuals",
                              "is my model validated? (only if I provide measured data)"),
        future_backend_dependencies=("reuse of the existing measured-vs-PHREEQC comparison logic",),
    ),
)

_BY_ID: dict[str, VirtualLabMachine] = {m.machine_id: m for m in _MACHINES}


# --------------------------------------------------------------------------- #
# Lookups / helpers
# --------------------------------------------------------------------------- #
def list_virtual_lab_machines() -> tuple[VirtualLabMachine, ...]:
    """Every Virtual LAB machine, in catalogue order."""
    return _MACHINES


def get_virtual_lab_machine(machine_id) -> VirtualLabMachine | None:
    """The machine for ``machine_id``, or ``None`` if unknown (never raises)."""
    return _BY_ID.get(machine_id)


def list_machines_by_mode(mode) -> tuple[VirtualLabMachine, ...]:
    """All machines with the given :data:`MODES` value (empty tuple if none / unknown mode)."""
    return tuple(m for m in _MACHINES if m.mode == mode)


def list_machines_by_status(status) -> tuple[VirtualLabMachine, ...]:
    """All machines with the given :data:`STATUSES` value (empty tuple if none / unknown status)."""
    return tuple(m for m in _MACHINES if m.status == status)


def machine_requires_measured_data(machine_id) -> bool:
    """True if the machine needs measured data to operate (flag or ``measured_data_required`` mode)."""
    m = _BY_ID.get(machine_id)
    return bool(m and (m.needs_measured_data or m.execution_mode == EXEC_MEASURED_DATA_REQUIRED))


def machine_requires_trained_model(machine_id) -> bool:
    """True if the machine needs an approved trained model (flag or ``trained_model_required`` mode)."""
    m = _BY_ID.get(machine_id)
    return bool(m and (m.needs_trained_model or m.execution_mode == EXEC_TRAINED_MODEL_REQUIRED))


def machine_requires_reference_database(machine_id) -> bool:
    """True if the machine needs a reference/structure/thermodynamic database to operate."""
    m = _BY_ID.get(machine_id)
    return bool(m and m.needs_reference_database)


def machine_can_produce_validated_result(machine_id, has_measured_data: bool) -> bool:
    """A machine can yield a ``validated_result`` only if it lists that output AND measured data exists.

    This is the single gate behind the platform rule *validation requires measured data*: no machine —
    not a simulation, an ML prediction, or a literature lookup — can ever return a validated result
    without ``has_measured_data=True``.
    """
    m = _BY_ID.get(machine_id)
    if m is None:
        return False
    return OUT_VALIDATED_RESULT in m.output_data_type and bool(has_measured_data)


def audit_virtual_lab_machines() -> list[str]:
    """Return a list of completeness/safety problems for the catalogue (empty == healthy).

    Enforces: unique ids; required text + list fields present; valid enum values; ``must_not_claim`` /
    ``safety_notes`` / ``verification_required`` / ``real_world_verification_method`` non-empty; a
    ``validated_result`` is gated on measured data; and the machine-specific safety invariants (ML needs
    a trained model; literature needs provenance + human review; sustainability is order-of-magnitude;
    ICP cannot fabricate measured data; XRD states the formula-only limitation).
    """
    problems: list[str] = []

    ids = [m.machine_id for m in _MACHINES]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"duplicate machine_id: {dup!r}")

    for m in _MACHINES:
        tag = m.machine_id or "?"
        for f in REQUIRED_TEXT_FIELDS:
            v = getattr(m, f, None)
            if not (isinstance(v, str) and v.strip()):
                problems.append(f"{tag}: empty/invalid text field {f!r}")
        for f in REQUIRED_TUPLE_FIELDS:
            v = getattr(m, f, None)
            if not (isinstance(v, tuple) and len(v) > 0):
                problems.append(f"{tag}: empty/invalid list field {f!r}")
        if m.mode not in MODES:
            problems.append(f"{tag}: invalid mode {m.mode!r}")
        if m.execution_mode not in EXECUTION_MODES:
            problems.append(f"{tag}: invalid execution_mode {m.execution_mode!r}")
        if m.status not in STATUSES:
            problems.append(f"{tag}: invalid status {m.status!r}")
        if m.ui_activation_status != UI_NOT_ACTIVATED:
            problems.append(f"{tag}: ui_activation_status must be backend-only ({UI_NOT_ACTIVATED!r})")
        for o in m.output_data_type:
            if o not in OUTPUT_DATA_TYPES:
                problems.append(f"{tag}: invalid output_data_type {o!r}")

        # Validation gate: a validated_result is impossible without measured data.
        if machine_can_produce_validated_result(m.machine_id, has_measured_data=False):
            problems.append(f"{tag}: can produce validated_result WITHOUT measured data")
        if OUT_VALIDATED_RESULT in m.output_data_type and \
                not machine_can_produce_validated_result(m.machine_id, has_measured_data=True):
            problems.append(f"{tag}: lists validated_result but cannot produce it even with measured data")
        # A machine that needs measured data must not short-circuit the validation gate.
        if machine_requires_measured_data(m.machine_id) and \
                machine_can_produce_validated_result(m.machine_id, has_measured_data=False):
            problems.append(f"{tag}: measured-data machine yields validated_result without measured data")

    # Machine-specific safety invariants.
    ml = _BY_ID.get(ML_SURROGATE)
    if not (ml and machine_requires_trained_model(ML_SURROGATE) and ml.needs_trained_model):
        problems.append("ML surrogate must require a trained model")

    lit = _BY_ID.get(LITERATURE_ENGINE)
    if lit:
        blob = " ".join(lit.verification_required + lit.safety_notes + lit.must_not_claim).lower()
        if "provenance" not in blob:
            problems.append("Literature engine must require source provenance")
        if "human review" not in blob and "review" not in blob:
            problems.append("Literature engine must require human review")
        if "google scholar" not in " ".join(lit.must_not_claim + lit.safety_notes).lower():
            problems.append("Literature engine must forbid scraping restricted sources (Google Scholar)")

    sus = _BY_ID.get(SUSTAINABILITY)
    if sus:
        if sus.mode != MODE_ADVISORY_PLANNING:
            problems.append("Sustainability screening must be advisory_planning")
        sus_blob = (sus.short_description + " " + sus.what_it_can_do + " "
                    + " ".join(sus.safety_notes) + " " + " ".join(sus.must_not_claim)).lower()
        if "order-of-magnitude" not in sus_blob and "order of magnitude" not in sus_blob:
            problems.append("Sustainability screening must be order-of-magnitude, not a final LCA/TEA")

    icp = _BY_ID.get(ICP_PROCESSOR)
    if icp and "fabricat" not in " ".join(icp.must_not_claim + icp.safety_notes).lower():
        problems.append("ICP must forbid fabricating measured data")

    xrd = _BY_ID.get(XRD_ADVISORY)
    if xrd and "formula" not in " ".join(xrd.safety_notes + xrd.must_not_claim).lower():
        problems.append("XRD must state the formula-only / polymorph limitation")

    return problems
