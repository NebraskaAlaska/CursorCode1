# Material profiles (composition manager)

The **Simulate → Step 7 — Material profile** section lets you provide, review, and confirm a
material's bulk composition so the deterministic [PHREEQC input preview](simulation_planner.md)
can include the dissolved-material chemistry instead of stopping at
`needs_material_composition`.

It is a **planning-layer** helper only:

- it **runs no PHREEQC** and writes **nothing to disk** (profiles live in the session);
- it is **off the scientific result path** — a material profile **never** affects mapping,
  residuals, validation status, or the comparison (those are driven by measured data + model
  results, not by a Simulate composition);
- it **never invents composition** — every value comes from you, an uploaded file, or a
  reviewed-and-confirmed literature value.

> **Two different "material profiles".** This composition manager
> (`flyash_phreeqc_ml.materials.MaterialProfile`) is *user-created and mutable*. It is distinct
> from the frozen, code-defined `flyash_phreeqc_ml.profiles.MaterialProfile` that drives the
> *scientific* batch-chemistry result path (mass balance / attribution / recovery). They share a
> small `usable_assay` interface so a confirmed composition can feed the input preview, but the
> Simulate composition is stamped with a `user-confirmed` provenance — deliberately **not** a
> science `measured` / `literature-confirmed` provenance — so it can never be mistaken for a
> measured assay.

## Why composition is required

A geochemical equilibrium prediction needs to know *what is dissolving*. Without the material's
elemental composition the input preview can only template the leachant + the structural blocks; it
cannot represent the elements the solid releases. Rather than fill in plausible-looking numbers
(which would produce a confident-looking but fictional result), the preview stays
`needs_material_composition` until you supply a real, confirmed composition.

## Accepted composition formats

| Basis | What the numbers mean | Conversion to element wt % |
| --- | --- | --- |
| **oxide wt %** (`oxide_wt_percent`) | XRF-style oxide assay (`SiO2`, `Al2O3`, `CaO`, …) | oxide wt % × gravimetric factor (e.g. CaO → Ca × 0.715, Fe₂O₃ → Fe × 0.699) |
| **element wt %** (`element_wt_percent`) | element mass fraction (`Ca`, `Si`, …) | used directly |
| **mg/kg** (`mg_per_kg`) | element mass per kg of material | ÷ 10,000 (1 wt % = 10,000 mg/kg) |
| **mol/kg** (`mol_per_kg`) | moles of element per kg of material | × molar mass ÷ 10 |

You can provide composition four ways:

- **Manual entry** — an editable table seeded with the common oxides (values blank — nothing is
  pre-filled).
- **Paste table** — paste `species value` rows (commas, tabs, `=`, `:`, or spaces all work; a
  trailing `± unc` and a unit word like `wt%` are tolerated). The species token is never scanned
  for a number, so the `2` in `SiO2` is not mistaken for the value.
- **Upload file** — a `.csv` / `.xlsx`; pick the species and value columns.
- **Literature (AI)** — consent-gated; proposes **unverified** values only (see below).

Multiple species that map to one element (e.g. `FeO` + `Fe2O3`) are summed. `LOI`, `moisture`,
`total`, etc. are counted toward the oxide total but are **not** treated as elements.

### Validation

Every profile is validated deterministically (by code, not the AI):

- a missing material name, an unknown basis, a negative value, or *no* resolvable element is an
  **error** (the profile cannot be confirmed);
- an oxide total outside ~90–102 wt % (incl. LOI/moisture), a value above 100 wt %, or an
  unrecognized species is a **warning** (surfaced, never silently fixed);
- LOI/moisture handling and the basis conversion that will be applied are stated as **info**.

## Draft vs. user-confirmed vs. verified

`verification_status` is the **trust gate**:

| Status | Meaning | Usable for the preview? |
| --- | --- | --- |
| `draft` | entered/imported but not yet reviewed | **No** |
| `literature_unverified` | proposed from literature, awaiting review | **No** (needs the double acknowledgement) |
| `user_confirmed` | you reviewed it and confirmed it for planning | **Yes** |
| `verified` | reserved for an externally-verified composition | **Yes** |

Confirming is a **deliberate, separate step**: you tick an acknowledgement, then press *Mark as
user-confirmed*. Only `user_confirmed` / `verified` profiles expose a usable assay; a `draft` /
`literature_unverified` profile returns nothing, so the preview stays
`needs_material_composition`. (You can *Revert to draft* at any time.)

## Why AI / literature values require review

The **Literature (AI)** path proposes a typical bulk assay per element with a citation, but every
value lands as `literature_unverified` and **cannot** feed the preview. To use it you must
acknowledge — explicitly — that you reviewed each value **and its citation** and take
responsibility for using a literature-sourced (typical, not measured-on-your-sample) composition.
This mirrors the project-wide rule: **AI / literature output is suggestion-only and never silently
becomes verified data.** The citation is retained and written into the input comments when the
composition is used.

## How a profile connects to the PHREEQC input preview

When you select a **confirmed** profile in Step 7 and generate the preview (Step 8):

- the resolved **element wt %** assay is written into the input's *dissolved material composition*
  comment block (as a bulk assay — you still apply a dissolution model before running);
- the **composition basis**, **source / verification status**, and any **citation** are written
  as comment lines, so the provenance travels with the input text;
- the preview status improves from `needs_material_composition` to `ready_for_review` (NaOH) or
  `template_warning` (water / HCl, which are preview-only).

If no profile is selected (or the selected one is not confirmed), behaviour is unchanged: the
composition is not included and the preview stays `needs_material_composition`.

## Why PHREEQC is still not executed

Providing composition makes the input *meaningful*, not *run*. The draft is still equilibrium-only,
assumption-laden, and database-dependent — an expert must check the thermodynamic database, the
dissolution/kinetics model, and the candidate-phase list before running. Executing PHREEQC remains
a separate, deliberate step (today, the on-demand runner in the **Match** tab), never something the
planner does for you. Because nothing is executed, the pH / residual / comparison graphs in
**Validate** and **Compare Results** do not change from a material profile or an input preview.

## Implementation

- `flyash_phreeqc_ml/materials/profile_schema.py` — `MaterialProfile`, `CompositionEntry`,
  `CompositionSource`, `ResolvedAssay`, `ProfileValidationResult`, the verification / source /
  basis vocabularies, the oxide→element stoichiometry, and the paste/record parsers. Pure; depends
  only on `units` for molar masses.
- `flyash_phreeqc_ml/materials/profile_validation.py` — `validate_profile`, the deterministic
  error/warning/info checks + the confirmability / usability decision.
- The composition reaches the preview through the same `relevant_elements` / `usable_assay` /
  `display_name` interface the `phreeqc_input_builder` already reads — the builder imports no
  material-manager code (boundary pinned by `tests/test_ai_boundary.py`).

Covered by `tests/test_material_profile.py`.
