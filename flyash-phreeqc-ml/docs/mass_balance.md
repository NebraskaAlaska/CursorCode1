# Batch-reaction mass balance & gap attribution

Two layers, kept strictly separate: a **measured** element closure (arithmetic, no
model) and an **optional modeled** explanation of the gap (PHREEQC). The modeled layer
never overwrites the measured numbers.

## 1. Measured closure (`mass_balance.py`)

For a batch reaction (solid material + reagent → leached liquid + residual solid), per
element, all in **mmol**:

```
n_in     = starting solid assay × material mass          (mg → mmol via units.convert)
n_liquid = measured liquid conc (mM) × liquid volume
n_solid  = residue assay × recovered solid mass          (mg → mmol via units.convert)
gap      = n_in − n_liquid − n_solid
```

The **gap is a measured fact** — element *not yet attributed* to liquid or solid, with
**no mechanism attached**.

Honesty rules:

- Every mass→amount conversion goes through `units.convert` (the `mg→mmol` registry
  entry), so each derived term carries a `conversion_id` + the molar mass used.
- A **missing required term** → `status = incomplete`, the fields are listed, and `gap`
  is `None` — a partial number is never shown as if real.
- An absent recovered `solid_mass_g` is **assumed = material mass** (no mass-change
  correction) and the assumption is recorded — never silently fabricated.
- **Uncertainty** is propagated (relative-quadrature for products, sum-in-quadrature for
  the gap) only when per-input sigmas exist; otherwise `gap_sigma = None` and the result
  is labelled `uncertainty = "unknown"` — never implied to be zero.
- Sanity warnings (negative gap beyond gap_sigma → names a likely culprit;
  `gap_fraction > 1.0`; implausible over-recovery) are surfaced, never silent fixes.

Schema: the optional batch block is appended to `config.EXPERIMENTAL_RELEASE_COLUMNS`
(`material_mass_g`, `material_id`, `reagent`, `reagent_conc_M`, `reagent_volume_mL`,
`liquid_volume_mL`, `solid_mass_g`, and per element `{el}_starting_content` /
`{el}_solid_residue`). It is **optional** in the parser; a profile opts in by setting
`mass_balance_elements`. `FLY_ASH_PROFILE` does **not** opt in (unchanged).

## 2. Modeled gap attribution (`attribution.py`)

When PHREEQC is configured, the batch chemistry is simulated (extending
`phreeqc_runner.build_single_input`: dissolved material as SOLUTION inputs, a
profile-declared candidate-precipitate list in `EQUILIBRIUM_PHASES`, and a per-element
`SELECTED_OUTPUT`/`USER_PUNCH`). `attribute_gap` reports **how much of the measured gap**
the predicted precipitation accounts for.

### Filtration convention (per element)

Whether a PHREEQC-predicted precipitate reduces the gap depends on whether it is in the
**measured solid residue** (`n_solid`). The state is **per element** — a profile-level
default `precipitate_in_measured_solid` plus a `precipitate_in_measured_solid_overrides`
dict (`{element -> True | False | "uncertain"}`):

| State | Meaning | Gap arithmetic |
| --- | --- | --- |
| **`True`** — *retained* | precipitate is in the assayed solid (already in `n_solid`) | `attribution_to_gap = 0` — it explains the solid's *composition*, **not** the gap |
| `False` — *passes* | precipitate leaves with the **filtrate**, NOT in the assayed solid | `attribution_to_gap = min(P, gap)` — it **explains** the gap |
| `"uncertain"` | retention **not verified** (a colloid/complex that may pass the filter) | credited **conservatively as retained** (`0`), but the result is **flagged** (`filtration_uncertain`) and carries `gap_explained_if_passes` (what it *could* explain if it passes) |

**Fly-ash default = `True`.** Set to `True` for the fly-ash protocol **per the
experimenter's filtration procedure** (secondary precipitates are retained on the filter,
so they are part of the assayed solid). The crystalline precipitates — Ca as
calcite/portlandite (micron-scale) — are reliably retained.

**Colloid-former overrides (`Si`, `Al`, `Fe`) = `"uncertain"`.** Si and Al form colloidal
silica / aluminosilicate gels and Fe forms nanocolloids that **can pass the 0.45 µm
filter** this protocol uses (`filter_cutoff_um = 0.45`), so their retention is *not*
verified. They are marked `"uncertain"`: the gap math does **not** credit them (treated as
retained → `0`), but every rendering flags the assumption so a reviewer knows those
elements' gap attribution is unverified. **Knowing the cutoff is 0.45 µm does not resolve
this** — that pore size is exactly where these colloids may pass — so the `True`/`False`
resolution for Si/Al/Fe remains a **placeholder** pending the comparison below.

> **These colloid-former overrides should be confirmed by a filtrate-vs-ultrafiltrate
> comparison** (e.g. 0.45 µm vs ~3–10 kDa) if any Si/Al/Fe conclusion depends on whether
> their precipitates explain the gap. Until then, treat the `"uncertain"` flag as a
> standing caveat.
>
> Filtration procedure confirmed by: ______________________  (date: __________)

(No part of the code asserts this is confirmed — the line above is for the experimenter
to fill in; the tool only records the flag and surfaces the uncertainty.)

### Status (parallels the mapping-status system)

- **closed** — the measured gap is within its uncertainty (nothing to explain).
- **model-explained** — attribution explains ≈ all of the gap.
- **partially-explained** — attribution explains part of the gap.
- **unexplained** — attribution explains ≈ none of the gap.

This status folds into the report's overall validity (one source of truth in
`report._overall_validity`): a run whose element budget is **not measured-closed** cannot
be reported `valid` — it is capped at `preliminary`.

### Three-way display

Per element, three **never-merged** provenance bands:

1. **measured** — liquid + solid + closure gap (from the measured closure, immutable);
2. **model attribution** — gap split by predicted phase (e.g. calcite, gibbsite);
3. **unexplained** — `gap − attribution`.

### Honesty

All attribution text says *"model attributes"* / *"predicted to precipitate"* — never
*"the element was X"*. When PHREEQC cannot run (no binary/database), the UI degrades to
the measured gap with **"attribution unavailable — configure PHREEQC"**, and **no modeled
value is ever written into a measured-labelled field**.
