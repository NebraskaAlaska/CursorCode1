# Simulation strategy & ranking (Simulate tab)

After you run a small sweep, the Simulate tab can **rank** the executed scenarios against an
objective you choose, and suggest a **cautious refined sweep**. This is an *optimisation over model
predictions* — it is **not** validation against measured data.

## What "ranking" means (optimisation vs. prediction vs. validation)

| | What it is | Trust |
| --- | --- | --- |
| **Prediction** | one PHREEQC run's output (pH / totals / SIs) under your assumptions | a model prediction |
| **Optimisation / ranking** (this layer) | scoring + ordering those predictions against an objective | *which prediction looks best* — still a prediction |
| **Validation** (Validate / Compare) | comparing measured data to model predictions | the only "validated" claim |

Ranking tells you *which simulated scenario best matches what you asked for*. It says nothing about
whether the model is right — every ranking carries the caption *"Ranking is based only on executed
PHREEQC simulation outputs. It is not validation against measured data."*

## Choosing an objective

The objective is **parsed deterministically** from your *desired-outputs* text (no AI is involved in
this layer, and it never invents an output — it only ranks values PHREEQC produced). Recognised
phrasings include:

| You write… | Objective |
| --- | --- |
| `maximize Ca` / `highest Ca` | maximise the Ca total |
| `minimize Fe` / `reduce impurity dissolution` | minimise Fe (impurity → Fe/Al by default) |
| `target pH 10 to 12` | keep pH inside [10, 12] |
| `avoid unsafe pH 12 to 14` | keep pH outside [12, 14] |
| `highest Si with lowest Al` / `maximize Ca while minimizing Fe` | weighted (two metrics) |
| `minimize reagent concentration` | minimise the leachant molarity |
| `maximize Sc/Fe selectivity` | maximise the Sc/Fe ratio |

If the text is ambiguous (e.g. *"best concentration"*), the objective is left **unset** and the UI
asks you to choose. Whatever is detected is shown in an **editable** form — you pick the objective
type and its element(s) / pH range, then **confirm** it before ranking. The objective is data, never
an action: confirming it ranks the existing results and **runs nothing**.

## How objective scores are calculated

Each objective is one or more **metrics**, each scored to `[0, 1]` (higher = better) across the
successful scenarios, then combined by weight into a single score:

- **maximise**: `(value − min) / (max − min)` over the scenarios.
- **minimise**: `1 − that` (lowest value scores highest).
- **target [lo, hi]**: inside the band → `1.0`; outside → falls off linearly with distance
  (one band-width away → `0`).
- **avoid [lo, hi]**: inside → `0` (bad); outside → `1`.
- **selectivity A/B**: a derived `A/B` ratio, scored as a maximise.
- **weighted**: the weighted average of the above.

Scenarios are ranked by the combined score. The result table shows the score and rank, the metric
that **drove** the top scenario, and — when metrics conflict — **tradeoff notes** (e.g. *"Top
scenario is best overall but not the highest on Ca"*). All values stay in mM / pH, as predicted.

## Why rankings require parsed PHREEQC outputs

A ranking is only as real as the values it ranks. If a requested metric is **not present** in the
parsed results (e.g. you asked to maximise Sc but no Sc total was produced), the layer **does not
invent a ranking** — it drops that metric and warns *"Requested metric 'Sc' is not available in the
executed results"*. If no metric is rankable, it says so rather than producing a false order. So
ranking depends on having actually executed the sweep and parsed its outputs (pH / totals / SIs).

## Refined sweep suggestions (review before running)

After ranking, the layer proposes a **cautious** next sweep — it never runs it:

- **best at the lower edge** → extend the range downward by one step (clamped above zero for a
  concentration);
- **best at the upper edge** → extend the range upward by one step;
- **best internal** → refine with finer values around the optimum;
- **many failures** → narrow the range and re-check the PHREEQC input assumptions (database,
  material composition, leachant template);
- **requested metric missing** → add the matching `SELECTED_OUTPUT` definitions (or parser support)
  so it can be ranked.

The suggestion lists concrete values to *consider*. You review them and decide — nothing runs
automatically, and the [small-sweep cap](phreeqc_execution.md) still applies.

## The refined-sweep loop (suggestion → confirmed plan)

A suggestion can be turned into a **new confirmed simulation matrix** — without anything running
automatically. This closes the loop *rank → refine → re-run* while keeping every step a deliberate,
reviewed action.

**Ranking vs. refined sweep.** Ranking *scores the sweep you already ran*. A refined sweep *proposes
the next sweep to run*. Neither is validation, and neither executes anything on its own.

**How the loop works:**

1. After ranking, the tab shows the suggested **sweep parameter**, concrete **suggested values**,
   the **reason**, and any **warnings**.
2. `strategy.refined_sweep_plan` converts the suggestion into concrete values deterministically:
   - `extend_upper` → a couple of higher values beyond the edge;
   - `extend_lower` → lower values, **never nonphysical** (a concentration/time is never taken to
     ≤ 0 — a halved value is used instead, and the clamp is flagged);
   - `refine_internal` → a finer grid around the best region;
   - `narrow_failures` → a smaller, safer range;
   - `add_selected_output` → **no chemistry values are generated** (it is blocked with a note to
     add the missing `SELECTED_OUTPUT` / parser definitions instead).
3. The values are **editable** — you can change them. You then tick a confirmation checkbox and
   press **Generate refined matrix**.
4. The new matrix is **plan-only** (every row `status = plan_only`). It does **not** overwrite the
   current plan unless you choose *Replace the current sweep plan*. To actually run it you go back
   through the input previews (Step 8) and the explicit run step (Step 9) — exactly as for any
   sweep.

**Why confirmation is required.** The refined values are a *suggestion*, possibly edited by you,
and they will (eventually) drive real PHREEQC runs. Confirming them generates a *plan*, never a
result — so nothing is built on an unreviewed extrapolation, and AI never runs anything.

**Provenance is preserved.** When you later run and **save** the refined sweep, the saved
[simulation run](simulation_runs.md) records the refinement chain: the **parent run id**, the
**parent top-ranked scenario**, the **objective** + its **ranking score**, the **reason** for the
refinement, the **suggested vs. applied** values (so your edits are visible), and a timestamp.

**Why this is not full automatic optimization.** Each refinement is one small, reviewed step that
*you* confirm. The app never chains refinements on its own, never runs a search loop, and keeps the
small-sweep cap. Repeating the loop by hand — rank, refine, confirm, run, rank again — *approximates*
an adaptive search, but every step stays a deliberate human decision over model predictions, with
the standing label *"This is a small reviewed refinement, not large-scale automatic optimization."*

## Why large-scale / adaptive search is future work

This is a small-sweep prototype. For hundreds or thousands of scenarios, or an *adaptive* search
that proposes the next most-informative scenario automatically, the app shows:

> Large-scale/adaptive search requires a dedicated batch backend or surrogate-assisted workflow.
> This app currently supports small confirmed sweeps.

Those backends (a real batch/optimisation runner, or a surrogate-assisted explorer that scans a wide
space cheaply and runs PHREEQC only at promising points) are out of scope for this in-app prototype.

## Implementation

- `flyash_phreeqc_ml/simulation/strategy.py` — `SimulationObjective` / `ObjectiveMetric` /
  `RankingResult` / `RefinedSweepSuggestion` / `RefinedSweepPlan`, `parse_objective` (rule-based),
  `rank_results`, `suggest_refined_sweep`, and `refined_sweep_plan` (suggestion → physical, capped
  sweep values; `is_physical_value`). Pure: it imports only `re` / `dataclasses` / `pandas` — no
  executor (so it cannot run anything), no AI, no comparison module.
- `flyash_phreeqc_ml/simulation/run_registry.py` — a saved run's `refinement` block records the
  parent run / objective / score / reason / suggested-vs-applied values / timestamp.
- The Simulate ranking + refined-loop UI lives in `app.py` (`_render_rank_simulation_results` /
  `_objective_editor` / `_render_ranking` / `_render_refined_sweep` /
  `_render_refined_matrix_builder`).
- Covered by `tests/test_strategy.py` + `tests/test_refined_sweep.py`; boundaries by
  `tests/test_ai_boundary.py`.
