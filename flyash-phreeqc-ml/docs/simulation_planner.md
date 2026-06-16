# Simulation planner (Simulate tab)

The **Simulate** tab turns a plain-language experiment description into a structured,
reviewable **scenario** and a **simulation plan/matrix**. It is a *planning layer only*.

> **This version creates a simulation plan, not a scientific prediction.** No deterministic
> simulation (e.g. PHREEQC) is run, no measured data is touched, and nothing the planner produces
> becomes verified data. Every generated matrix is labelled *"Simulation plan only — no
> deterministic simulation has been run yet."*

## What it does (the flow)

1. **Describe your experiment** — free text, e.g.
   *"I have 2 g of Class C fly ash. I add 10 mL of 0.5 M HCl for 60 minutes at room
   temperature. I centrifuge, filter the liquid, and measure pH, Ca, Si, Al and Fe."*
2. **Describe desired variables / outputs** — e.g. *"simulate what should be in the liquid
   and what may have precipitated."*
3. **Parse scenario** — extracts a structured scenario (AI if you consent, else rule-based).
4. **Review** — see exactly what the planner understood, what's missing, what it assumed,
   the warnings, the confidence, and **whether AI or rule-based parsing was used**.
5. **Edit / confirm** — correct any value, then tick the confirmation box.
6. **Generate simulation matrix** — a table of intended runs you can download as CSV.
7. **Material profile (composition)** — provide / review / **confirm** the dissolved-material
   composition so the input preview can be meaningful (see *Material profiles* below). Composition
   is **never invented**; without a confirmed profile the preview stays
   `needs_material_composition`.
8. **PHREEQC input preview** — template a reviewable, draft `.pqi` input per scenario
   (deterministic code, **not** AI). Still no execution — see *PHREEQC input preview* below.
9. **Run deterministic model** — *optionally* execute PHREEQC on the reviewed input and show the
   basic predicted outputs. Gated and explicit; results are **simulation outputs, not validated
   predictions**. See [`phreeqc_execution.md`](phreeqc_execution.md).

## What the planner extracts

| Group | Fields |
| --- | --- |
| Material | `material_name`, `material_type`, `solid_mass_g` |
| Leachant | `leachant_type`, `leachant_concentration_M`, `liquid_volume_mL`, `pH_initial` |
| Process | `time_min`, `temperature_C`, `CO2_condition`, `cover_condition`, `centrifuge_used`, `filtration_used`, `filter_size_um` |
| Outputs | `target_elements` (Ca, Si, Al, Fe, Na, K, Sc, REE), `desired_outputs` (liquid_composition, precipitated_phases, pH, …) |
| Derived | `liquid_solid_ratio` (liquid mL / solid g) |
| Bookkeeping | `confidence`, `missing inputs`, `assumptions`, `warnings` |

## AI vs. rule-based parsing

- **AI extraction** (when an API key is configured *and* you tick the consent box) sends your
  description to the model, which returns a strict-JSON scenario. The app validates that JSON;
  if it is invalid, the planner shows a controlled message and **falls back to rule-based
  parsing** — it never crashes.
- **Rule-based fallback** (always available, no network) extracts the obvious values with
  hand-written rules: masses (`2 g`), volumes (`10 mL`), molarities (`0.5 M HCl` / `NaOH`),
  times (`60 min`, `2 hours`), `room temperature`, `centrifuge`/`filter`, and element names.
  It is intentionally imperfect and **labels itself low-confidence** — always review it.

The review panel always tells you which path produced the scenario.

## Why confirmation is required

The extracted scenario is a **suggestion**. AI (or rule) output can be wrong, and assumptions
(e.g. an assumed temperature) may not match your protocol. You must review and confirm before
a matrix is generated, so nothing downstream is built on an unverified extraction. Confirmation
generates a *plan*, not a result.

## What "assumptions" mean

When a value is not stated, the planner may **assume** a default (for example, *temperature
assumed 25 °C* when you write "room temperature" or give no temperature). Every assumption is
listed explicitly with its reason and source (`ai` / `rule`), and a corresponding warning is
shown. Assumptions are editable — change them before generating the matrix.

## What cannot be concluded from liquid data alone

If you ask about precipitated or retained solids, the planner shows this standing caveat:

> *Precipitation or retention cannot be proven from liquid data alone. It requires PHREEQC
> phase predictions, solid residue data, or mass-balance assumptions.*

The **scientific warnings are computed by deterministic code, not the AI** — so AI output can
never weaken them. Other code-generated warnings include: missing solid mass / liquid volume /
leachant concentration; *temperature assumed*; *material composition is not part of a text
description*; and *the PHREEQC template may not support this leachant yet* (the on-demand
generator currently templates NaOH activation only — acid/water leaching is recorded in the
plan but cannot be auto-generated).

## What the matrix contains

One row per planned run (one row in this version; the builder is designed to later fan out over
ranges of concentration / time / temperature / L:S):

`scenario_id, material, solid_mass_g, liquid_volume_mL, liquid_solid_ratio, leachant_type,
leachant_concentration_M, time_min, temperature_C, CO2_condition, target_elements,
desired_outputs, status` — where `status = plan_only`.

## Material profiles (composition)

**Step 7 — Material profile** is where you supply the material's bulk composition so the input
preview stops being "structural only". It is a small composition manager (see
[`material_profiles.md`](material_profiles.md) for the full reference):

- **Provide** composition four ways — *manual entry* (an editable oxide/element table),
  *paste* a `species value` table, *upload* a `.csv`/`.xlsx`, or *Literature (AI)* (consent-gated,
  proposes **unverified** values only).
- Composition can be expressed as **oxide wt %**, **element wt %**, **mg/kg**, or **mol/kg**; the
  manager converts everything to element wt % (oxides via gravimetric factors, e.g. CaO → Ca ×
  0.715) and validates it (negatives rejected; oxide totals checked against a plausible range;
  unrecognized species flagged).
- A profile is **draft** until you **confirm** it. A literature-sourced profile is
  `literature_unverified` and requires a second acknowledgement (you reviewed every value *and its
  citation*) before it can be confirmed.
- **Only a confirmed profile feeds the preview.** A draft / unverified profile is ignored — the
  preview stays `needs_material_composition`, because composition is **never invented**.

Profiles live in the **session only** — nothing is written to disk — and they are **off the
scientific result path** (they never touch mapping, residuals, validation, or the comparison).

## PHREEQC input preview (draft)

After you confirm the plan, **Step 8 — PHREEQC input preview** templates a reviewable, draft
`.pqi` PHREEQC input for each scenario. **PHREEQC is still not run** — this is input *text* for
you to review and download, not a result.

> **AI extracts the scenario context; deterministic, rule-based code writes the PHREEQC input.**
> The LLM never writes `.pqi` text. The preview is produced by
> `flyash_phreeqc_ml/simulation/phreeqc_input_builder.py`, which is pure and testable.

Every generated input carries comment lines stating it is a **draft preview**, that **PHREEQC has
not been run**, that it **requires expert review**, that the **thermodynamic database choice
matters**, that **material composition controls prediction quality**, that **kinetic dissolution is
not represented** (the draft is equilibrium-only), and that **precipitation predictions depend on
the selected phases + database**.

### Supported draft templates + their assumptions

| Leachant | Draft solution | Stated assumptions |
| --- | --- | --- |
| **water** | neutral / DI water, no molarity | pH ≈ 7 (DI), equilibrium-only |
| **NaOH** | `Na` set to the NaOH molarity, charge-balanced on a high pH | full dissociation; mol/L ≈ mol/kgw (density ≈ 1); high pH from NaOH |
| **HCl** | `Cl` set to the HCl molarity, charge-balanced on a low pH | full dissociation; **preview-only** — the on-demand runner templates NaOH only, so an HCl input is **not validated against the runner** |
| anything else | a generic placeholder solution | `unsupported_leachant` — define the chemistry manually |

### Status (what the preview is and is not)

The result carries a `status`: `ready_for_review` (NaOH + all fields + a *usable* material
assay), `template_warning` (water/HCl preview-only), `needs_material_composition` (a known
material but no approved assay), `draft_only` (a generic material), `missing_required_field`
(a hard field absent), or `unsupported_leachant`. Missing data never crashes — it returns a
labelled draft plus warnings.

### Why material composition is required

A meaningful PHREEQC prediction needs the **dissolved material composition**. The planner
**never invents it** — the composition comes only from a *usable* assay: either a frozen profile's
declared assay (`measured` / `literature-confirmed`) **or** a **Step-7 material profile you
confirmed** (the composition manager exposes its confirmed assay through the same `usable_assay`
interface). A draft / `literature_unverified` / `literature-proposed` assay is ignored. The project
ships **no committed fly-ash assay**, so until you supply and confirm a material profile a fly-ash
preview lands at `needs_material_composition` with the composition left as a labelled placeholder —
honest, not silently filled with assumed values. Confirm a profile and the same preview becomes
`ready_for_review`, with the composition + its **basis and source** written into the input comments.

### Why review is required before running, and why graphs don't update yet

The draft is structural and assumption-laden — an expert must check the database, the dissolution
model (kinetics), and the phase list before running. And because **PHREEQC is not executed**, the
pH / residual / comparison graphs in **Validate** and **Compare Results** do not change from a
Simulate plan or its input preview: those graphs are driven by measured data + model results, not
by a plan (see *graph provenance*).

The preview is **in-memory and downloadable only** (a `<scenario_id>_preview.pqi` download) — it
**writes nothing to a run folder**.

## Boundaries (what it never does)

- The **planning** modules (Steps 1–8) **never run PHREEQC** (pinned by
  `tests/test_ai_boundary.py`). Execution is a *separate*, gated step — **Step 9** (see
  [`phreeqc_execution.md`](phreeqc_execution.md)) — that runs only on an explicit click and only
  the reviewed input text; **AI never writes or runs the input.**
- It **never overwrites measured data** and **never becomes verified data**. A Step-9 execution
  result is a **simulation output, not a validated prediction**.
- It **never affects** mapping status, residuals, validation status, the comparison CSV, or any
  scientific claim — the planner *and* the executor are off the scientific result path. The pH /
  residual graphs in **Validate** and **Compare Results** are separate and a Simulate run changes
  none of them.

## Implementation

- `flyash_phreeqc_ml/simulation/scenario_schema.py` — the dataclasses + vocabulary + caveats.
- `flyash_phreeqc_ml/simulation/safety.py` — deterministic missing-field + warning analysis.
- `flyash_phreeqc_ml/simulation/rule_parser.py` — the non-AI fallback parser.
- `flyash_phreeqc_ml/simulation/matrix.py` — the plan-matrix builder (range-ready).
- `flyash_phreeqc_ml/simulation/phreeqc_input_builder.py` — the **deterministic** PHREEQC
  input-preview templater (water / NaOH / HCl drafts; no execution, no AI, never invents
  composition; pinned by `tests/test_phreeqc_input_builder.py`).
- `flyash_phreeqc_ml/materials/` — the **material composition manager** (Step 7): build / review /
  confirm a material profile (oxide / element / mg-kg / mol-kg → element wt %), gated so only a
  *confirmed* profile feeds the preview. Off the result path; writes nothing. See
  [`material_profiles.md`](material_profiles.md); pinned by `tests/test_material_profile.py`.
- `flyash_phreeqc_ml/simulation/phreeqc_executor.py` — the gated **execution** layer (Step 9):
  `check_availability` / `execute_preview` / `parse_outputs`, structured + never-crashing, writing
  only to `outputs/simulations/`. Off the result path (no AI, no comparison module). See
  [`phreeqc_execution.md`](phreeqc_execution.md); pinned by `tests/test_phreeqc_executor.py`.
- `flyash_phreeqc_ml/ai/scenario_parser.py` — the AI extractor + AI-or-fallback orchestrator
  (uses the shared, key-safe AI client; see [`ai_configuration.md`](ai_configuration.md)).
