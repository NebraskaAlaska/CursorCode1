# Phase 2 Virtual LAB product shell

## Scope and authority

The Phase 2 shell replaces the previous ten-section presentation with one persistent nine-page
Streamlit information architecture. It does not replace or duplicate scientific engines. Dependency
direction remains `app.py → ui/ → flyash_phreeqc_ml/`.

The product shell uses a compact product/context header and concise default page surfaces. A default
surface shows the page or machine purpose, active context, current state, the most important blocker,
and the next action. Complete scientific information remains available under clearly named detail
controls, while canonical IDs, backend bindings, raw envelopes, hashes, paths, and schemas remain
collapsed under **Technical details** by default. A critical caution is never hidden when it applies:
simulation is not measured data or validation, XRD is advisory rather than confirmed identification,
ML requires an approved trained model, and measured-data, QC, criteria, runtime, and stale-state gates
remain explicit.

The sole machine catalogue is
`flyash_phreeqc_ml/instruments/virtual_lab_machines.py`. The Machines gallery reads its exact ordered
12 records and presents each as a concise, comparable card with human-readable status, its primary
blocker or readiness statement, and one open action. Selecting a card places its workspace before the
collapsed alternate-machine chooser. The shared workspace has exactly four primary areas:
**Overview**, **Prepare**, **Results**, and **History**. Scientific limits, inputs, provenance,
validation, and verification remain reachable through named detail controls inside those areas; no
scientific metadata is duplicated in presentation code. The workspace delegates non-PHREEQC
preparation to `virtual_lab_machine_runner.py`; PHREEQC is routed to the existing Phase 1A input
builder, review, exact-confirmation, and executor path. Existing ICP and XRD presentation is adapted
without rendering the old Digital Lab catalogue.

## Primary pages and legacy mapping

| Primary page | Durable/product responsibility | Existing workflow retained |
| --- | --- | --- |
| Home | active research context, blockers, recent runs, next actions | Assistant quick route |
| Projects | durable project create/read/update/archive | legacy run reports/export/audit |
| Material Workspace | durable material record and revisions | Assistant, PHREEQC Workspace, measured import |
| Machines | concise canonical gallery and one four-area shared machine workspace | ICP/XRD specialized panels; ML model panel |
| Results | project-scoped result index and filters | current legacy simulation-result viewer |
| Validation & Uncertainty | validation language and state gates | Import, Validate, Match, Compare |
| Evidence | material evidence references | Evidence Library |
| Run History | immutable durable run reopening | references to existing artifacts where supplied |
| Settings & Diagnostics | storage/runtime blockers and safe diagnostics | AI settings and Engine Library |

There is one sidebar navigation radio. Active project, material, and durable run selectors appear
with the compact context surface and persist through `active_context.json`. Page titles and one-line
purposes lead the main content instead of a large repeated global hero.

## Durable store

Default location: `outputs/virtual_lab_workspace/` (gitignored).

```text
outputs/virtual_lab_workspace/
├── active_context.json
├── projects/prj_<uuid>.json
├── materials/mat_<uuid>.json
└── runs/run_<uuid>.json
```

All records carry `schema_version = 1`, stable generated IDs, UTC timestamps, and deterministic JSON.
Display names never become paths. Writes use a same-directory temporary file, file flush/fsync, and
atomic `os.replace`; an interrupted replacement leaves the prior record intact. Abandoned
`.tmp-*.json` files are ignored and may be removed through the explicit cleanup method.

The store rejects traversal/absolute IDs, symlink components, duplicate IDs, malformed/non-finite
JSON, future schema versions, cross-project material/run bindings, and secret-like fields. It copies
no raw dataset or artifact. Existing experiment/evidence/model/simulation storage is not migrated;
durable records carry explicit references where relevant.

### Project record

Project ID, name, description, status, archive flag, metadata, timestamps, and schema version.
Archiving requires an exact project-ID confirmation and preserves all records.

### Material record

Material ID/project ID, name/description/type, composition and per-row units, composition provenance,
verification status, assumptions, process conditions, measurement/evidence/model/run references,
unresolved issues, timestamps, and three versions: overall material revision, composition revision,
and assumption/process revision.

### Run record

Run/project/material/machine IDs; exact input snapshot and SHA-256; material/composition/assumption
versions and material identity hash; model/environment/evidence identity; status, output and
epistemic type; result envelope, warnings, validation state, external result location/references, and
creation time.

## Result identity and staleness

Every durable result is immutable. A run is current only when its project/material binding,
material revision, composition revision, assumption revision, and material identity hash still match
the active material. A scientifically meaningful material edit never overwrites an old result: the
run remains visible in Results/Run History as historical/stale and is excluded from `current_runs()`.
Display-only material edits and appending the run's own association do not create false staleness.

Prepared-but-unsaved Streamlit results are also bound to the exact project ID, material ID, material
revision, machine ID, input snapshot, and input hash. A prepared value from another project,
material, or revision is not rendered as current.

## Epistemic and validation presentation

`app_ui.epistemic_badge()` maps only explicit result metadata to the approved labels. Unknown or
missing metadata renders as “Epistemic type unspecified”; the UI never guesses measured/validated
state from a page or color. Empty values stay absent.

Validation remains separately gated: comparison availability is advisory without explicit criteria;
unmet criteria are not validated; a validated-result envelope requires QC-eligible measured and
predicted values plus explicit criteria that are met. Literature remains context rather than measured
sample validation. XRD remains advisory/tentative. PHREEQC remains a simulation estimate and has no
generic Run button in the shared workspace.
