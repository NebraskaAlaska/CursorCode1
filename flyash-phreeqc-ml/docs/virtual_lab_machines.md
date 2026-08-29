# Virtual LAB — authoritative 12-machine contract

`flyash_phreeqc_ml/instruments/virtual_lab_machines.py` is the one authoritative source for machine
identity, deterministic order, scientific metadata, static maturity, safety, provenance requirements,
validation requirements, and backend bindings. It is pure import-safe metadata: it does not import
Streamlit, inspect this computer, load a model, call an external API, or execute a scientific engine.

The Phase 2/3 Machines page renders these definitions as its canonical gallery and shared
**Overview · Prepare · Results · History** workspace. Hands-on ICP and XRD workflows delegate to
their existing scientific authorities; Phase 3 persistence wraps rather than replaces them. See
[`phase3_durable_workflows.md`](phase3_durable_workflows.md).

## Canonical IDs and order

1. `phreeqc_leaching_simulator`
2. `xrd_advisory`
3. `icp_data_processor`
4. `ftir_raman_interpreter`
5. `sem_eds_processor`
6. `tga_dsc_processor`
7. `mechanical_testing_processor`
8. `ml_surrogate_predictor`
9. `literature_evidence_engine`
10. `sustainability_cost_screening`
11. `experimental_design_assistant`
12. `validation_uncertainty_assistant`

`machine_ids()` returns this exact order. `canonical_machine_id(value)` is the sole alias normalizer.
It accepts canonical IDs and the following saved/historical aliases, and returns `None` for unknown
values without fuzzy matching:

| historical ID | canonical ID |
|---|---|
| `xrd_advisory_module` | `xrd_advisory` |
| `mechanical_test_processor` | `mechanical_testing_processor` |
| `sustainability_screening` | `sustainability_cost_screening` |

New router decisions and machine results use canonical IDs. A runner request made with an alias also
records the supplied value as `provenance.input_machine_id`; historical files are not rewritten.

## Contract fields and vocabulary

Each immutable `VirtualLabMachine` carries identity and UI text, `mode`, `execution_mode`, static
`maturity`, required/optional inputs, honest outputs and allowed epistemic types, verification and
real-world methods, prohibited claims, measured/model/reference requirements, uncertainty controls,
safety notes, a stable `backend_binding`, backend capability classifications, provenance requirements,
validation requirements, runtime requirements, and example prompts where useful.

One vocabulary is defined here for:

- mode: physical simulation, data processing, advisory planning, trained-model prediction, evidence,
  and cross-cutting validation;
- execution mode: advisory, data processing, preview-then-confirm, approved-model-required,
  evidence-required, and measured-data-required;
- static maturity: implemented backend, limited workflow, advisory backend, or blueprint only;
- epistemic type: user assumption, synthetic demo, literature evidence, measured lab data, simulated
  estimate, ML prediction, advisory interpretation, or validated result.

`instrument_schema.py` re-exports this vocabulary. It does not define a second one.

## Static maturity is not runtime availability

Static maturity says whether repository code exists. `MachineRuntimeAvailability` is a separate,
lightweight observation for environment-specific state. For example, the PHREEQC builder/executor is
implemented even when no executable/database is configured on this computer. Likewise, the ML
prediction backend exists even when no approved trained model is selected. Runtime state is calculated
outside the immutable catalogue and is never stored as `active=True/False` metadata.

`instrument_registry.py` remains available for older callers but is a deprecated compatibility
adapter. Its 12 read-only `InstrumentSpec` objects wrap the canonical `VirtualLabMachine` objects
directly; it owns no independent scientific descriptions.

## Canonical maturity and deprecated query compatibility

Canonical code uses `maturity`, `execution_mode`, the measured/model/reference requirement flags,
`backend_capabilities`, and `runtime_requirements`. `list_machines_by_maturity(maturity)` filters only
the canonical static maturity field.

Historical `STATUS_*` strings remain distinct deprecated values; they are not aliases for maturity.
`list_machines_by_status(status)` is a compatibility query that interprets requirement statuses
through the corresponding canonical flags, execution modes, and backend capabilities. For example,
the trained-model status returns only machines that require an approved trained model, while the
measured-data and reference-data statuses return only machines carrying those actual prerequisites.
`STATUS_ACTIVE_EXISTING` excludes an implemented backend that still lacks a required runtime,
reference/evidence source, measured dataset, or approved model. `STATUS_PHASE_1_ADVISORY` selects the
specific advisory capability rather than every machine whose broad canonical mode is advisory
planning.

Deprecated mode values are likewise compatibility inputs rather than canonical aliases.
`signal_simulation` deterministically selects the historical XRD/FTIR signal-advisory meaning; it is
not equal to canonical `advisory_planning` and therefore does not pull in sustainability or
experimental-design workflows. These compatibility queries return the same canonical machine
objects and do not constitute a second catalogue.

## Specialized backend ownership

The contract coordinates capabilities; it does not reimplement their science:

| Machine | authoritative or delegated backend |
|---|---|
| PHREEQC | `simulation.phreeqc_input_builder`, `simulation.phreeqc_run_contract`, `simulation.phreeqc_executor` |
| XRD | `instruments.xrd_advisory`, with durable coordination in `instruments.xrd_records` |
| ICP | `instruments.icp_processor` and the Phase 1B QC contract, with durable coordination in `instruments.icp_review` |
| FTIR/Raman, SEM/EDS, TGA/DSC, mechanical | explicitly limited runner-native processors over supplied data |
| ML | `ml_models.predict` with an approved non-demo trained model |
| Literature | `literature.research_agent`, `literature.evidence_store`, and durable `literature.evidence_review` |
| Sustainability | limited assumption-based screening plus `experiments.sustainability_score` |
| Experimental design | limited deterministic planning plus `experiments.plan_generator` |
| Validation | existing comparison/QC/criteria paths plus a limited standard-envelope adapter |

PHREEQC is never automatically executed. The generic runner authors no PHREEQC chemistry and cannot
treat its bare `confirm` Boolean as Phase 1A review/environment confirmation evidence. The only
scientific path remains preview → review → exact confirmation → existing executor.

## Validation and audit gates

`machine_can_produce_validated_result(machine_id, has_measured_data,
has_explicit_criteria=False)` permits that epistemic type only for a machine that declares it and only
when QC-eligible measured data and explicit criteria exist. The criteria-bearing workflow must still
prove that the criteria were met. A simulation, advisory result, literature record, or ML prediction is
never validated merely by passing through the machine runner.

`audit_virtual_lab_machines()` checks the exact count, unique IDs, metadata completeness, vocabulary,
backend/requirement consistency, aliases, and the scientific safety invariants. Architecture tests also
prove the compatibility registry wraps these same 12 objects rather than maintaining a second catalogue.
