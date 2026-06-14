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

### Filtration convention (CONFIRMED for the fly-ash protocol)

Whether a PHREEQC-predicted precipitate reduces the gap depends on whether it is in the
**measured solid residue** (`n_solid`). This is the profile flag
`precipitate_in_measured_solid`:

| Flag | Meaning | Gap arithmetic |
| --- | --- | --- |
| **`False`** — *fly-ash default (confirmed)* | precipitate leaves with the **filtrate**, NOT in the assayed solid | `attribution_to_gap = min(P, gap)` — the precipitate **explains** the gap |
| `True` | precipitate is retained in the assayed solid (already in `n_solid`) | `attribution_to_gap = 0` — the precipitate explains the solid's *composition*, not the gap |

For fly ash we use **`False`**: filtration carries the secondary precipitates out with
the liquid/filtrate, so they are not double-counted in the measured solid residue and
they legitimately attribute the unaccounted element. (Set `True` only for a protocol
where precipitates are demonstrably retained in the assayed solid.)

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
