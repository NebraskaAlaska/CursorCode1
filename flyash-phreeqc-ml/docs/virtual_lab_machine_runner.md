# Virtual LAB — limited machine runner

`flyash_phreeqc_ml/instruments/virtual_lab_machine_runner.py` is a coordination layer over the
authoritative contract in `virtual_lab_machines.py`. It normalizes a request ID, validates top-level
inputs, delegates where a specialized backend exists, runs only explicitly limited data/advisory
workflows, and returns one standard result envelope. It is not a giant generic scientific engine and is
not a replacement for the specialized PHREEQC, ICP, XRD, ML, literature, or validation authorities.

The runner is not a dedicated UI. The shared machine workspace calls it for narrow generic
workflows, while durable ICP, measured-XRD, evidence, planning, and sustainability presentation uses
the corresponding typed Phase 3 services. See
[`phase3_durable_workflows.md`](phase3_durable_workflows.md).

## Result envelope and identity

`run_virtual_lab_machine(machine_id, payload, confirm=False)` returns:

- `machine_id` (canonical, or `None` for an unknown request);
- `status` and honest `output_data_type`;
- `result_summary` and `results`;
- `warnings`, `missing_inputs`, and `assumptions`;
- `provenance`, including canonical ID and backend binding;
- `validation_status` and `can_be_used_for_validation_claim`.

Known historical aliases are accepted through `canonical_machine_id()`. A result uses the canonical ID
and retains a differing supplied alias as `provenance.input_machine_id`. Unknown values produce a
controlled `unknown_machine` result; the runner never guesses.

No result receives a stronger epistemic type merely because it passes through this envelope. Only the
criteria-bearing validation workflow can set `can_be_used_for_validation_claim=True`, and only with
QC-eligible measured data, compatible predictions, explicit criteria, and criteria met.

## Dispatch boundaries

- ICP delegates correction, canonical row-role normalization, censoring, duplicate resolution,
  residual eligibility, and QC provenance to `instruments.icp_processor`. Its table-level epistemic
  type is derived from those processed row roles and QC state: all-measured may be measured,
  all-predicted may be a simulated/model estimate, and mixed or role-unresolved tables are advisory
  (or a user assumption when no row role is trustworthy). The optional top-level `source` is retained
  as provenance only; it cannot relabel rows, and a contradiction produces an actionable warning and
  a weaker advisory result. Corrected rows retain their own role and `row_output_data_type`.
- XRD delegates expected-peak/checklist advice to `instruments.xrd_advisory`; wording remains tentative
  and advisory. The durable measured-pattern/reference path uses the same matcher through
  `instruments.xrd_records`, not a second runner-native matcher.
- ML delegates a number to `ml_models.predict` only for an approved, non-demo `TrainedModel` with
  features. Missing, demo, exploratory, and legacy models produce no prediction. Delegation also has
  a narrow fail-closed artifact boundary: an approved-status object with a missing, unfitted, corrupt,
  or incompatible pipeline returns an advisory prerequisite result with no prediction value and no
  exception detail. Result provenance distinguishes `model_status_appeared_approved` from
  `model_artifact_usable`; the artifact is not rewritten.
- Literature compatibility dispatch records supplied candidate metadata only. Durable manual/AI
  evidence lifecycle authority is `literature.evidence_review`; only an explicit human action can
  produce reviewed evidence.
- Sustainability either multiplies user-supplied amount/factor pairs with compatible units and
  sources, or applies the existing condition proxy only to explicitly eligible supplied rows.
  Missing factors remain missing. Neither mode is LCA, TEA, a cost result, or a certified assessment.
- Experimental design requires an explicit CFA preset or a complete generic user-defined factor
  plan. It enforces the run cap and leaves outcomes blank; it does not invent levels, truncate a
  plan, or claim an optimum.
- FTIR/Raman, SEM/EDS, TGA/DSC, mechanical testing, and validation are narrow workflows over supplied
  data. They do not invent measurements or outcomes.

## PHREEQC boundary

The runner deliberately builds no PHREEQC input text. It reports top-level missing inputs, checks
environment availability without a smoke simulation, and routes the caller to the existing
Assistant/Workspace path. It always returns `executed=False`, `auto_run=False`, and `preview=None`.

A bare `confirm=True` on the generic runner is ignored because it is not the Phase 1A reviewed-input,
environment-identity, and source-term confirmation evidence. Input authoring and execution remain with:

1. `simulation.phreeqc_input_builder`;
2. `simulation.phreeqc_run_contract`;
3. `simulation.phreeqc_executor`.

That preserves preview → review → exact confirmation → existing executor and prevents a second drifting
chemistry template.

## Public helpers

- `runner_machine_ids()` returns the exact canonical 12-ID order.
- `validate_machine_inputs()` and `explain_missing_inputs()` accept canonical or known legacy IDs.
- `get_machine_result_label()` queries the canonical contract.
- `VirtualLabMachineRequest` and `VirtualLabMachineResult` provide request/result representations.

The runner imports no Streamlit and performs no external API calls. Environment availability and static
contract maturity remain separate concepts.
