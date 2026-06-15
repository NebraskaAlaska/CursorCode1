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

## Boundaries (what it never does)

- It **never runs PHREEQC** (pinned by `tests/test_ai_boundary.py`). To actually run a
  simulation, use the deliberate PHREEQC generation step in the **Match** tab.
- It **never overwrites measured data** and **never becomes verified data**.
- It **never affects** mapping status, residuals, validation status, the comparison CSV, or any
  scientific claim — the planner is off the scientific result path.

## Implementation

- `flyash_phreeqc_ml/simulation/scenario_schema.py` — the dataclasses + vocabulary + caveats.
- `flyash_phreeqc_ml/simulation/safety.py` — deterministic missing-field + warning analysis.
- `flyash_phreeqc_ml/simulation/rule_parser.py` — the non-AI fallback parser.
- `flyash_phreeqc_ml/simulation/matrix.py` — the plan-matrix builder (range-ready).
- `flyash_phreeqc_ml/ai/scenario_parser.py` — the AI extractor + AI-or-fallback orchestrator
  (uses the shared, key-safe AI client; see [`ai_configuration.md`](ai_configuration.md)).
