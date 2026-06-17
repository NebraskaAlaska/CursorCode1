# Material release / source-term modelling (Simulate Step 7b)

A confirmed [material profile](material_profiles.md) gives the material's **bulk composition**
(e.g. 19.4 wt% CaO). On its own that tells PHREEQC *nothing* — the solid has to be **introduced
into the reaction system** before any material-derived element can be predicted. The **Material
release model** (Step 7b) converts a confirmed profile + a *user-chosen, reviewable* release
assumption into a deterministic PHREEQC source-term block.

## Why composition alone is not enough

PHREEQC predicts the chemistry of a *solution*. If the input only contains the leachant
(e.g. 0.5 M NaOH), PHREEQC sees pure NaOH and predicts ~0 Ca/Si/Al/Fe — no matter how detailed the
material profile is. (This was exactly the gap the first real end-to-end smoke test exposed: the
assay was written as comments only.) To get a meaningful prediction, the model must know **how much
of the material dissolves** into the liquid.

## Bulk composition vs. assumed release vs. measured liquid vs. prediction

These are four different things — the app keeps them distinct and labelled:

| | What it is | Trust |
| --- | --- | --- |
| **Bulk composition** | what the *solid* contains (XRF assay) | a property of the material |
| **Assumed release** | *what fraction* you assume dissolves into the liquid | **your assumption**, not measured |
| **Measured liquid** | concentrations you actually measured in the leachate | measured **input** data |
| **PHREEQC prediction** | the speciation/SIs PHREEQC computes from the above | a model **prediction**, not validated |

A release model bridges *bulk composition* → *what enters the solution*; PHREEQC then predicts the
rest. None of it is a validated result until compared to measured data.

## The release models

The default is **no material release** — the assay stays comment-only and **no material elements
enter the simulation** (the app warns that predicted Ca/Si/Al/Fe will be ≈ 0). The whole material
is **never silently dissolved**. The other modes:

- **Global release fraction** — assume one fraction (e.g. **1%**) of *every* element dissolves.
- **Per-element release fractions** — a default fraction plus per-element overrides (e.g. Ca 1%,
  Si 0.5%, Al 0.2%, Fe 0.01%).
- **Literature / measured release fraction** — the same maths, but the fractions are marked as
  sourced; they are **blocked until you explicitly confirm them** (AI/literature values never enter
  a calculation unconfirmed).
- **Measured liquid composition** — if you already measured the leachate, build the solution from
  those concentrations directly, labelled **measured input, not a prediction**.

### Why 100% dissolution is usually unrealistic

In a real fly-ash + NaOH batch, only a small, kinetically- and solubility-limited fraction of each
element actually releases over the experiment — often **<1–5%**, and very different per element
(Si/Al/Ca release differently from Fe). Assuming 100% (full dissolution) would massively over-predict
dissolved totals. So the app **never** defaults to 100%, requires you to enter the fraction, and
warns (and by default rejects) a fraction > 100%.

### Why release fractions must be reviewed

The release fraction directly controls the predicted dissolved totals — it is the single biggest
assumption in the model. Because it is an assumption (not a measurement), the app treats every
non-measured fraction as **user-assumed**, writes that into the input comments, and never lets AI
choose it for you.

## How it becomes a PHREEQC source term

The conversion is deterministic (and was validated against real PHREEQC 3.8.6):

1. oxide wt% → element wt% (the profile's gravimetric factors, e.g. CaO → Ca × 0.715);
2. element wt% → grams of element in the solid sample (`solid_mass_g × wt%/100`);
3. grams → moles (`÷ molar mass`);
4. moles × **release fraction** → **released moles**;
5. released moles are added through a PHREEQC **`REACTION` block as oxides** (`CaO`, `SiO2`,
   `Al2O3`, …) — charge-safe, because each oxide reacts with water (`CaO + H₂O → Ca²⁺ + 2OH⁻`);
6. the `SOLUTION`'s **`-water`** is set to the actual liquid volume, so released *moles* map to the
   correct dissolved *concentration* (the real L/S ratio).

This is an **equilibrium** source term — **not** a kinetic dissolution model (kinetics are explicit
future work). The generated `.pqi` carries comment lines stating: *material release is user-assumed,
not measured; release fractions control the predicted totals; this is not a kinetic model; solid
phases / precipitation depend on the database + selected phases; expert review required.*

### Candidate phases & the database

Precipitation and saturation-index predictions depend on the **candidate phases** and the
**thermodynamic database**. The app does **not** invent a phase list — if the profile declares no
candidate phases, released elements stay fully dissolved (no precipitation), and the app warns that
SI prediction is limited. It also warns that **high-pH cement/fly-ash phases need CEMDATA18** — the
standard `phreeqc.dat` predicts them weakly or not at all.

## Selected output

The generated input requests `pH`, `pe`, and the **target + released element totals** in
`SELECTED_OUTPUT`, so they can be parsed back. If a requested element was never introduced (no
release model, or it is not in the assay), it will simply read ~0 — which is the honest answer.

## Worked example (the verified case)

*2 g Class C fly ash, 10 mL 0.5 M NaOH, 1% global release* (example oxide assay) → the source term
introduces ≈ 6.9e-5 mol Ca, 1.0e-4 mol Si, 7.2e-5 mol Al, 1.5e-5 mol Fe, and a real PHREEQC run
returns **Ca ≈ 6.9, Si ≈ 10.3, Al ≈ 7.2, Fe ≈ 1.5 mM** at pH ≈ 13.5. With *no* release model the
same case returns ≈ 0 for all four — the difference is entirely the (reviewed) release assumption.

## Why these are still model predictions

Even with a release model and CEMDATA18, the output is a **prediction under assumptions** (the
release fractions, the equilibrium-not-kinetic model, the database, the phase list). It is **not**
validated against your sample. A validation claim still requires measured data and the
[mapping/residual workflow](comparison_inclusion.md) in the **Compare Results** tab.

## Implementation

- `flyash_phreeqc_ml/simulation/source_terms.py` — `DissolutionModel` / `ElementReleaseFraction` /
  `ReleasedElement` / `SourceTermResult` / `SourceTermWarning`, `compute_source_terms`, and the
  REACTION/SOLUTION block builders. Pure: imports only `materials.profile_schema` (atomic weights /
  oxides) — no AI, no executor, no comparison module.
- `phreeqc_input_builder.build_phreeqc_input_preview(..., dissolution_model=…)` injects the source
  term; the Simulate **Step 7b** UI is `app.py :: _render_release_model_section`.
- Covered by `tests/test_source_terms.py` (incl. an optional real-PHREEQC test); boundaries by
  `tests/test_ai_boundary.py`.
