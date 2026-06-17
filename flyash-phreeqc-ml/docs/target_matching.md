# Target matching / inverse search (Simulate Step 10)

The forward planner asks *"given these inputs, what does the model predict?"*. **Target
matching** asks the inverse: *"what inputs get me close to the output I want?"*. You define a
**target**, choose a few **reviewed parameter values** to try, then — on an explicit click —
run a small, capped grid of simulations and **rank each candidate by how well it matches**.

> **This is inverse search over model predictions, not validation.** The ranking is computed
> from executed PHREEQC outputs under *your* assumptions (the parameter ranges, the release
> fractions, the database, the phase list). It is **not** validation against measured data, and
> nothing here affects mapping, residuals, validation status, or the comparison. Validation
> lives in the **Compare Results** tab.

## Target matching vs. prediction vs. validation

| | What it answers | Trust |
| --- | --- | --- |
| **Prediction** (Steps 8–9) | given fixed inputs, the model's output | a model prediction under assumptions |
| **Target matching** (Step 10) | which inputs (within your grid) best hit a target | an **optimisation over predictions** |
| **Validation** (Compare Results) | does the model agree with *measured* data | the only step that can say "validated" |

A best match is the best **within the grid you chose** — not a global optimum, and not a
measured fact.

## Defining a target

The target is parsed (deterministically, no AI) from your **desired-outputs** text, then shown
in an **editable table** so you can correct or add to it. Recognised target types:

| Type | Example text | Meaning |
| --- | --- | --- |
| **target_range** | "target pH 10 to 12" | score 1 inside `[low, high]`, linear falloff outside |
| **target_value** | "Ca around 5 mM" | score 1 at the value, linear falloff over a ± tolerance |
| **maximize** | "maximize Si", "highest Ca" | prefer higher (normalised across candidates) |
| **minimize** | "minimize Al", "low Fe" | prefer lower (normalised across candidates) |
| **constraint** | "Fe below 0.1 mM", "Al ≥ 1" | a **hard bound** that gates feasibility |

You can combine them — e.g. *"maximize Si while keeping Fe below 0.1 mM"* becomes a **maximize**
objective plus a **constraint**. The `column` is `pH` or `<Element>_mM` (e.g. `Ca_mM`).

> **AI may *suggest* a target, but you review, edit and confirm it.** The app never invents a
> target value, and the AI never runs a simulation. Target parsing here is rule-based.

## Search parameters (your assumptions)

You choose a small set of **reviewed values** for one or both axes:

- **Leachant concentration (M)** — e.g. `0.1, 0.5, 1.0`.
- **Global release fraction (%)** — e.g. `0.1, 0.5, 1, 2, 5` — how much of the material you
  *assume* dissolves into the liquid.

The grid is the **Cartesian product** of the chosen values (one candidate per combination).

> **Release fractions remain assumptions.** The release fraction directly controls the predicted
> dissolved totals — it is the single biggest modelling assumption (see
> [`material_release.md`](material_release.md)). It is never measured truth, and the AI never
> picks it for you. A concentration-only search inherits the release model you set in Step 7b.

### The grid is capped

The search grid is capped (currently **20** candidates — the same small-sweep limit the batch
executor enforces). If your chosen values would exceed the cap, the extra candidates are
**dropped with a visible warning**, never run silently. Large/adaptive/global inverse search
(many parameters, fine grids, optimiser-in-the-loop) is **future work** — it needs a dedicated
optimisation backend or a surrogate, not this prototype.

## Running + scoring

Nothing runs until you **review the grid and explicitly confirm**. On the click, the app builds
one deterministic `.pqi` per candidate (its own release fraction → source term), runs them
through the gated executor, parses the outputs, and scores each candidate:

1. **Objective metrics** are each scored in `[0, 1]`:
   - *range* → 1 inside the band, then `1 − distance/span` outside (clamped at 0);
   - *value* → `1 − |x − value| / tolerance` (clamped at 0);
   - *maximize / minimize* → min-max normalised across the executed candidates.
   The **weighted mean** of the objective metrics is the candidate's **objective score**.
2. **Constraints** gate **feasibility** — a candidate is *feasible* only if **every checkable
   constraint holds**.
3. Candidates are ranked **feasible-first, then by objective score**, so the best match is the
   highest-scoring *feasible* candidate. If **no** candidate satisfies every constraint, the app
   says so prominently (and suggests loosening a constraint or widening the grid).

Each row carries a **score breakdown**: per-metric value → score, and per-constraint pass/fail.

### Missing outputs cannot be scored

If a target references a variable that is **not in the executed results** (e.g. you target `Sc`
but the run did not emit Sc), the app **warns and does not score it** — it never fabricates a
value. If *every* target metric is missing, nothing can be scored and the app tells you to add
the matching `SELECTED_OUTPUT` / parser support and re-run. A constraint on a missing column is
flagged and **does not penalise** a candidate (it could not be checked).

## Why the best match still isn't validated

Even the top candidate is a **model prediction under your assumptions**. Its rank depends
entirely on:

- the **parameter ranges** you chose (the optimum may lie outside the grid);
- the **release fractions** (assumptions, not measurements);
- the **thermodynamic database** + **candidate phases** (see
  [`database_compatibility.md`](database_compatibility.md));
- the **equilibrium-not-kinetic** source term.

So the result is labelled *"inverse search over model predictions, not validation"* throughout.
To make a validation claim you still need measured data and the mapping/residual workflow in
**Compare Results**.

## Provenance

Saving a target-search run records the full search separately from measured-data validation
runs: the **target spec**, the **search parameters**, the **candidate grid**, the **scoring
method**, the **best candidate**, the **feasibility count**, and any **warnings** — under the
record's `target_match` block (a *simulation search record, not validation*).

## Boundaries (what it never does)

- It **never runs anything by itself** — `target_matching.py` imports no executor and no
  subprocess; execution happens only via the batch executor on an explicit, confirmed click.
- It **never invents** a target value or a release fraction.
- It is **off the scientific result path** — it imports no comparison / residual / mapping
  module, never writes a validation CSV, and never calls a result "validated" (pinned by
  `tests/test_ai_boundary.py`).

## Implementation

- `flyash_phreeqc_ml/simulation/target_matching.py` — `TargetSpec` / `TargetMetric` /
  `SearchParameter` / `CandidateScenario` / `MatchScoreBreakdown` / `TargetMatchResult`,
  `parse_target_spec` (deterministic), `build_search_grid` (capped), `score_results` (pure), and
  `target_match_provenance`. Imports only stdlib + pandas — no AI, no executor, no result-path.
- Simulate **Step 10** UI is `app.py :: _render_target_matching` (+ `_target_spec_editor`,
  `_build_target_previews`, `_render_target_match_results`, `_render_save_target_run`). It reuses
  the deterministic input builder, the gated executor (`batch_executor`), and the run registry.
- Run provenance: `run_registry.build_run_record(..., target_match=…)` stores the search.
- Covered by `tests/test_target_matching.py`; boundaries by `tests/test_ai_boundary.py`.
