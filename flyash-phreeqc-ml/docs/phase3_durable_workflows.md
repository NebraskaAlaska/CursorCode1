# Phase 3 durable review and advisory workflows

## Scope and authority

Phase 3 adds durable review records and end-to-end workspace flows around existing scientific
authorities. It does not add a second ICP processor, XRD matcher, evidence store, experiment-plan
engine, or sustainability calculator.

| Workflow | Scientific/domain authority | Durable coordinator |
| --- | --- | --- |
| ICP review | `instruments.icp_processor` | `instruments.icp_review` |
| Measured XRD and matching | `instruments.xrd_advisory` | `instruments.xrd_records` |
| Evidence curation | `literature.evidence_schema` and `literature.evidence_store` | `literature.evidence_review` |
| Experimental design | `experiments.plan_generator` | `phase3_artifacts` |
| Sustainability screening | `experiments.sustainability_score` | `phase3_artifacts` |

All durable records are scoped to a project and, where applicable, a material. Literature remains
contextual evidence, imported instrument data remains user-supplied data, an advisory result remains
advisory, and a simulation remains distinct from experimental validation.

## Durable `ArtifactRecord` contract

`WorkspaceStore` remains the sole project/material/run authority. Phase 3 adds one typed
`ArtifactRecord` envelope rather than expanding `RunRecord` into an unstructured store. The six
record types are:

- `icp_review`
- `xrd_measured_pattern`
- `xrd_reference`
- `evidence`
- `experimental_plan`
- `sustainability_screen`

Artifact IDs use the validated `art_<uuid>` prefix. Each envelope records a stable logical ID,
project and optional material binding, type, revision, lifecycle status, exact input snapshot and
SHA-256 identity hash, validated payload and payload hash, source identity, creator/reviewer/reason,
revision ancestry, optional related run, provenance, timestamps, and schema version.

The shared lifecycle vocabulary is `draft`, `needs_review`, `finalized`, `reviewed`, `rejected`, and
`superseded`. `finalized`, `reviewed`, `rejected`, and `superseded` artifacts are immutable. A change
to a terminal artifact creates a new linked revision; it never rewrites the historical JSON record.
Terminal review transitions require a named reviewer/resolver and a reason.

Final advisory runs retain exact artifact dependencies in `input_snapshot.artifact_identities`:

```json
{
  "artifact_id": "art_<uuid>",
  "logical_id": "art_<uuid>",
  "record_type": "<typed record>",
  "revision": 1,
  "input_hash": "<sha256>",
  "payload_hash": "<sha256>"
}
```

Reopening checks the project/material scope and every supplied revision/input/payload hash. It fails
closed instead of silently rebinding to a newer artifact. `WorkspaceStore.run_staleness()` also
checks the saved material revisions and material identity hash. A changed material or artifact
dependency leaves the old run visible as historical/stale; it does not mutate the old result.

New artifacts and runs also carry an integrity digest over the complete durable envelope, including
lifecycle, review, epistemic/validation, lineage, context, dependency, source, provenance, warning,
and related-run fields. This prevents a local JSON edit from changing `needs_review` to `reviewed` or
an advisory run to validated while leaving its scientific payload hash intact. Historical schema-v1
records without the new digest remain explicit legacy compatibility records; Phase 3 records cannot
downgrade themselves into that path. Multi-write coordinators use deterministic identities and exact
field validation to reconcile an interrupted create/revise/finalize/link operation on retry.

Runtime records live under `outputs/virtual_lab_workspace/`, including `artifacts/art_<uuid>.json`,
and that entire directory is gitignored. These records can contain private structured ICP/XRD data
and evidence, so they are runtime research state, not repository fixtures. Do not add uploaded
instrument files, user reference files, evidence records, generated plans/screens, or their runtime
workspace JSON to Git.

## Durable ICP review

The Phase 1B processor remains the only authority for concentration correction, unit handling,
censoring, row roles, duplicate resolution, residual pairing, and validation eligibility. The
durable layer stores enough information to re-run that authority exactly:

- original rows and deterministic row IDs;
- exact file-byte SHA-256 (for uploads) or deterministic row-table SHA-256 (for manual entry);
- source filename/import identity where supplied;
- blank/dilution/correction inputs and original/replacement values;
- every duplicate candidate, the selected row ID, resolver, reason, and timestamp;
- Phase 1B QC contract version, processed output, output hash, and reviewable issues.

The workflow is:

1. Import a long-form UTF-8 CSV or enter a JSON list of row objects in **ICP → Prepare**.
2. Process the supplied rows through `icp_processor`; inspect usable, censored, review-required,
   and excluded rows.
3. Record only supported metadata corrections for fields the authoritative processor marked
   missing/invalid, plus explicit duplicate selections. Already-valid sample, analyte, role, unit, or
   dilution metadata cannot be rewritten to relabel a row or evade duplicate ambiguity. The original
   row remains in the durable payload.
4. Save as `draft` or `needs_review`, then reload and deterministically reprocess it as needed.
5. Finalize with the exact artifact ID as confirmation, a named resolver, and a reason. Finalization
   refuses unresolved review-required metadata or conflicting duplicate selections.
6. The finalized artifact is immutable and is linked to an immutable ICP `RunRecord` and the
   material's measurement references. Further edits create a new artifact revision.

Censored/non-detect rows stay censored and validation-ineligible; they are never replaced by zero.
Hard-invalid rows remain excluded and cannot be approved or made validation-eligible. Unselected
duplicate candidates also stay excluded. A changed current file/table cannot reuse the saved
resolution automatically: file-backed finalization and validation require a fresh re-upload of the
exact source bytes, and manual-entry reuse requires the exact rows. `finalized_validation_output()` additionally requires a finalized,
source-matching artifact and returns only rows that Phase 1B marked validation-eligible. It cannot
promote an advisory, predicted, censored, or QC-blocked row to measured validation evidence.

## Measured XRD pattern import

### Measured CSV format

The measured-pattern importer accepts a UTF-8 CSV with one finite, physical degrees-2θ column and
an optional intensity column. Common headings are mapped automatically:

- 2θ: `2theta`, `2θ`, `two_theta`, `2-theta`, `Angle`, `Position`, and degree-labelled variants;
- intensity: `Intensity`, `Counts`, `Count`, `cps`, `relative_intensity`, and common variants.

For uncommon headings, provide an explicit mapping such as
`{"two_theta": "Diffraction angle", "intensity": "Detector counts"}`. The exact original headings,
resolved mapping, and mapping method are retained.

Illustrative schema only (not measured research data):

```csv
2theta,Intensity
20.0,125
20.1,
20.2,-2
```

`two_theta_unit` must be degrees 2θ; the importer performs no angle-unit conversion. Invalid or
physically impossible 2θ rows are rejected with row-specific reasons. Missing or invalid intensity
remains `null`, never zero. Negative intensity is retained with a background-correction warning and
is not clamped. Source order is preserved, including a warning when it is not monotonic.

Duplicate 2θ values require the explicit `keep_all` or `reject_later` policy. Neither policy
averages or silently sorts the signal. The durable pattern retains the stable pattern ID, project,
material, sample, exact filename/SHA-256, signal rows and decisions, units/type, radiation and
wavelength, instrument/method, scan range/step, date/operator/lab, row counts, and all warnings.
Its plot is a plot of supplied measured signal, not a measured phase identity.

### User peak list

Matching uses an explicit user-supplied peak list; raw scan points are not silently treated as
peaks. Each entry has a 2θ position, optional relative intensity/note, and the
`user_supplied` selection method. Peak-selection provenance retains the source pattern/hash,
provider, timestamp, notes, user edits, and any visible parameters. Adding or editing a peak list
creates a new immutable measured-pattern revision and keeps the original pattern authoritative.

## External XRD reference adapter and licensing

References are user-supplied CSV or JSON peak tables. A CSV uses the same 2θ/intensity heading
rules as the measured importer. JSON may be an array of peak objects/numbers or an object containing
`peaks`, `reference_peaks`, or `rows`; `reference_positions` plus optional
`relative_intensities` is also accepted.

Illustrative JSON schema only (not licensed reference content):

```json
{
  "source_name": "user-supplied reference source",
  "phase_name": "example phase label",
  "radiation_source": "Cu Kalpha",
  "wavelength_angstrom": 1.5406,
  "license_status": "unknown",
  "redistribution_permission_status": "unknown",
  "peaks": [
    {"two_theta": 20.0, "relative_intensity": 100}
  ]
}
```

`source_name` (or database/provider alias) and `phase_name` are mandatory. Retained provenance
includes formula/polymorph when supplied, radiation/wavelength, source record/card ID,
title/authors/year, DOI/URL, license and redistribution states, exact user filename/SHA-256, notes,
rejected rows, and review status. A formula alone does not establish a polymorph.

Every imported reference starts `needs_review` regardless of a review label inside the uploaded
file. A named reviewer may mark it `reviewed` or `rejected`; rejected references cannot be matched.
A needs-review reference can only participate in a visibly warned, tentative advisory comparison
and is not treated as trusted.

Licensing is a provenance field, not a permission inference. License and redistribution states use
a closed vocabulary; unrecognized natural-language permission claims are refused rather than trusted:

- `unknown` stays unknown; it never means public domain or redistributable;
- a proprietary/restricted license cannot be recorded as redistributable;
- no source metadata means no accepted external reference;
- the repository ships and scrapes no ICDD PDF or other restricted database content;
- no license key or user reference file is stored in source control;
- users are responsible for access and use rights for the references they supply.

The adapter does not implement arbitrary CIF import or automatic database retrieval.

## Tentative XRD matching

`xrd_advisory.match_measured_peaks()` remains the one matcher. The durable path compares one exact
finalized pattern revision with one or more exact non-rejected reference revisions and records the
tolerance, radiation check, one-to-one measured/reference peak pairs and degree differences,
unmatched peaks, overlap ambiguity, dominant-peak check, tentative confidence, limitations, source
identities, reference provenance, and payload hash.

Radiation identity is compared without wavelength conversion. Unknown or incompatible radiation
forces low confidence and blocks a stronger comparison. A single peak is always low-confidence;
overlap remains ambiguous; missing dominant reflections can cap confidence; amorphous content,
background, broadening, preferred orientation, solid-solution shifts, and lack of full-pattern fit
remain explicit limitations. Even a displayed `high` value means only high *tentative* consistency.

The saved output must contain “tentative”, “advisory”, “possible match”, and “check against an
appropriate reference source” language. Guard code rejects generated text that claims a confirmed,
identified, validated, or quantified phase. The resulting XRD `RunRecord` has
`advisory_interpretation` epistemic type and cannot become a phase-identification or validation
claim.

## Manual evidence and human review

Manual evidence works with AI disabled. A durable evidence record requires either a DOI or a title
plus source/provider. Citation provenance supports authors, year, URL, query, and discovery route;
`executed_queries` contains only searches actually sent to a source. Exact source location supports
page, page range, table, figure, section, supplementary item, dataset/record identifier, and a note.

The structured payload also records project/material, topic/claim, leaching or composite schema,
reported values, units, conditions, notes, conflicts, overall and per-field confidence, extraction
scope/status, lifecycle review state, reviewer/time/reason, and revision ancestry. Missing values
remain missing.

The durable evidence boundary accepts only the closed schema fields and bounded structured JSON.
Conservative per-string/word, container, nesting, and total-record limits prevent a full abstract,
paper, or raw model response from being hidden in `claim`, `notes`, an unknown field, or a nested
response envelope. Concise summaries and structured scientific values remain supported.

The lifecycle is:

1. Manual creation starts as `draft`; AI-origin extraction starts as `needs_review`.
2. A named editor may edit a draft in place, then a named submitter moves it to `needs_review`.
3. Only an explicit named human action with a reason can mark needs-review evidence `reviewed` or
   `rejected`.
4. Reviewed/rejected records are immutable. Editing one creates a linked `needs_review` revision
   whose previous artifact ID/hash remains visible.
5. Evidence can be linked to or unlinked from its material. Only linked, explicitly reviewed
   evidence can create an evidence `RunRecord` in Results/History.

Rejected and unreviewed revisions remain visible in history but never export or render as reviewed.
The current AI extractor receives an abstract only: its result is forced to abstract scope and
needs-review, even if model output claims `full_text` or `reviewed`. The store rejects source text,
full papers/abstracts, raw model/LLM output, chain-of-thought, non-finite values, and missing
provenance. Literature evidence stays contextual; it is not a measurement of the active material
and cannot validate a simulation or performance claim.

Available exports are deterministic structured JSON, schema-specific CSV, and a provenance-rich
JSON package. They include artifact/revision/review identity and source locations. CSV text cells
are protected against spreadsheet-formula execution. The historical per-run JSONL evidence files
are read-only compatibility input; the durable editor neither rewrites nor appends to them.

## Experimental-design advisory

The Experimental Design Assistant requires an explicit mode:

- `cfa_leaching_preset` exposes the existing Class C fly ash/NaOH leaching screen as a named preset.
  It is not universal. Its assumptions identify the nominal 0.5 M NaOH, 60 minute, 25 °C,
  liquid/solid ratio 5, and open-air cover unless a named series varies them.
- `generic_user_defined` requires the active material ID, goal, at least one factor with explicit
  levels, explicit fixed conditions (which may be empty), positive replicate count, and a positive
  maximum-run cap. Controls, date, and sample prefix are optional and never invented.

Generic factor names/values are finite JSON-safe inputs. Duplicate levels and JSON-equivalent
conditions are removed transparently before replicate expansion, and dedup counts are returned.
The cap is a refusal boundary: the plan is never silently truncated. Sample IDs are deterministic
hash identities of the condition/date/replicate, and replicate identity remains explicit.

Both modes return a concise summary, assumptions, condition table, dedup statistics, and blank
measurement/result columns. They make no optimality, outcome, prediction, or guarantee claim. CSV
and JSON exports are available, with spreadsheet-formula protection after leading whitespace/control
characters while genuine negative numeric values remain numeric. Saving creates a
finalized `experimental_plan` artifact and linked advisory run for the active material.

## Sustainability screening advisory

The Sustainability/Cost Screening machine also requires an explicit mode.

### User-supplied inventory screen

Each row supplies item/process, amount and amount unit, optional factor, factor unit, factor source,
boundary, and `source_type` (`literature_evidence` or `user_assumption`). Literature factors also
require their durable evidence artifact ID. Geography, year, notes, and a factor min/max range are
optional.

The only calculation is `amount × supplied factor`. No factor, unit conversion, boundary, source,
transport distance, price, emission, energy, or uncertainty is inferred. Factor units must state an
output per the exact normalized amount unit (for example `kg CO2e/kg`); incompatible units are
blocked rather than converted. Missing factors remain missing and do not enter totals. An explicit
zero is accepted as zero; absence is not interpreted as zero. Sensitivity is calculated only when
both supplied bounds are non-negative, ordered, and contain the selected factor. Totals stay
separate by boundary and output unit and report included, excluded, missing-factor, and blocked
rows.

### Experimental-condition screening proxy

This mode applies the historical `compute_sustainability_scores()` formula only to supplied rows
explicitly selected by a caller-named eligibility column. Non-eligible rows are counted as excluded.
It is a condition-screening proxy, not a lifecycle inventory.

Both modes remain advisory. They are not LCA, TEA, carbon-footprint, certified sustainability,
cost, or feasibility results, and cannot become validated results. Inventory CSV/JSON exports and
durable `sustainability_screen` artifact/run saving are available; the saved assumptions,
factor/evidence identities, boundary, inclusions, exclusions, and exact input hash reopen with the
run.

The shared advisory coordinator fixes the permitted output, epistemic, and validation states for
each advisory record type. A caller cannot supply `validated_result`, measured output, or a stronger
validation label. Formula-safe exports apply the same leading-whitespace/control-character rule as
the ICP and evidence exports without altering negative numeric values.

## Results, History, and reopening

Final ICP reviews, tentative XRD comparisons, experimental plans, sustainability screens, and
reviewed-evidence links create immutable `RunRecord` entries with their canonical machine ID,
project/material binding, exact input and artifact identities, output/epistemic type, warnings,
validation state, and creation time.

**Results** indexes project-scoped runs and distinguishes current from historical/stale state.
**Run History** and each machine's **History** area can reopen a saved run by restoring its original
project, material, run, and machine context. Reopening presents the saved immutable payload; it does
not recalculate against the current material or silently replace an artifact revision. A stale run
remains inspectable with reasons and must be deliberately superseded by a new workflow execution.

## PHREEQC boundary in Phase 3

Phase 3 does not install or execute PHREEQC. Its generic machine path probes configuration only with
`check_availability(run_smoke=False)`, authors no PHREEQC input, and always reports
`executed=False`. When the runtime is absent the UI reports it as unavailable and leaves
preview/review available. Any future execution still belongs exclusively to the established
preview → review → exact confirmation → executor contract. A PHREEQC result would remain a
simulation estimate, not measured data or experimental validation.
