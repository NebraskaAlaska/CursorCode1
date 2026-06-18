# Assistant Mode — the AI-agent orchestration layer

The **Assistant** tab turns the manual Simulate workflow into a conversation. It is an
**AI-agent orchestration layer** (`flyash_phreeqc_ml/agent/`) wrapped around the *existing*
deterministic Simulate backend. The AI handles conversation, clarification, planning, and
explanation; **every scientific calculation and the PHREEQC execution stay deterministic and
user-confirmed**.

```
conversation state
   → LLM call (propose exactly ONE structured action)
   → policy check (allow / needs-confirmation / block)
   → optional EXPLICIT user confirmation (for execution / save)
   → deterministic tool execution (existing modules)
   → updated state
   → assistant response
```

## What the AI does — and does not — do

| The AI **does** | The AI **never** does |
| --- | --- |
| **understand messy, informal, typo-filled, incomplete** prompts (abbreviations, informal units, several variables in one sentence, follow-ups + corrections) | invent a material composition, release fraction, phase, pH, concentration, or measured value |
| converse, ask the 1–3 missing critical details | run a simulation (it only *proposes*; the policy + your confirmation run it) |
| extract a structured `understanding` block, **validated/normalized by deterministic code** | write or edit PHREEQC input (a deterministic builder does, from the scenario) |
| classify the domain (a *hint*; the rule clamps it) | bypass a material-profile / release-model / database warning |
| explain results in plain language (numbers from the tools) | claim a result is "validated" (simulation ≠ validation) |

If there is **no API key**, a robust **deterministic** parser + planner drives the same
conversation (text normalization + the tested rule parser) — the workflow works fully without AI,
with a gentle "less robust without AI" note.

## Natural-language understanding (robust to messy prompts)

`agent/nlu_extractor.py` is the assistant's understanding layer (it mirrors
`ai/scenario_parser.py` but is agent-state-aware). It turns *"im leeching class c fli ash w naoh
.5m 2g 10ml for 1hr room temp wanna ph ca si"* into the right structured set-up:

- **AI on** → one grounded LLM call returns a structured `understanding` block **and** the proposed
  next action. The AI output is then **validated + normalized deterministically** before anything
  is applied.
- **AI off / no key / call failed** → a robust deterministic parse: `normalize_text` fixes typos /
  informal units / spacing (`.5m`→`0.5 M`, `na oh`/`sodum hydroxde`→`NaOH`, `fli ash`/`CFA`→`Class
  C fly ash`, `redmud`→`red mud`, `1 hr`→`60 min`), the tested rule parser extracts values, and
  canonicalization + validation finish the job.

Hard rules (never weakened, either path):

- It extracts **only the experiment set-up** (material, leachant, masses, volumes, time,
  temperature, CO₂ cover, target elements, desired outputs). It **never** extracts (and defensively
  strips) a material composition, a release fraction, a measured value, a computed pH/result, or a
  validation status.
- It **never silently invents** a value: an *assumed* value (e.g. "room temp" → 25 °C) is flagged
  `needs_confirmation`; an *impossible* value (negative mass/volume/time, out-of-range
  temperature/concentration) is **rejected** and turned into a question.
- **Follow-ups merge, corrections win.** Sparse replies (*"0.5m, 2g, 10ml, 60 min"*) accumulate into
  one scenario; a correction (*"actually make it 40 C"*) overrides only that field, clears any prior
  assumption, and is announced. Genuinely **ambiguous** input (e.g. "acid leach" with no named
  reagent) is **asked about, never guessed**.

The UI shows an **"I understood this as…"** card after each turn (domain, material, conditions,
target elements, what was normalized, assumptions to confirm, what's missing) with an inline
**"✏️ Edit what I understood / ✋ That's not right"** corrector — so a misread field is fixed in one
click, without hunting advanced tabs.

## Modules (`flyash_phreeqc_ml/agent/`)

| Module | Role | Imports |
| --- | --- | --- |
| `agent_state.py` | conversation + structured scenario + the pure `apply_delta` **merge** (scalars overwrite so corrections win; lists union; assumptions flagged/cleared); the "I understood this as…" card state; provenance trace | rule_parser / safety / scenario_schema (pure) |
| `nlu_extractor.py` | **natural-language understanding** — AI-first extraction (one grounded call → `understanding` + action) with a deterministic fallback; text normalization, canonicalization, validation/schema-repair, change/conflict detection, the understanding card | ai.client + actions / prompts / state / domains · **AI-first, no executor** |
| `agent_actions.py` | the **action vocabulary** + `AgentAction` + `parse_action` (strips forbidden keys like `phreeqc_input_text`) + per-action **metadata** (risk / confirmation / domain / preconditions) | pure |
| `domains.py` | **domain classification** + the engine map (only `leaching_geochemistry → PHREEQC`) | pure |
| `agent_prompts.py` | the system prompt (incl. the `understanding` schema + messy-text rules) + a **grounded** per-turn prompt (the model sees the deterministic state, never invents it) | scenario_schema / actions / domains |
| `tool_registry.py` | binds each action to a **deterministic backend function** (the existing builder/executor/registry/strategy/target-matching) | simulation modules · **no AI** |
| `agent_policy.py` | the **gate** (allow / needs-confirmation / block) + the **deterministic planner** (asks 1–3 prioritized questions) | actions / domains / state · **no AI, no executor** |
| `agent_council.py` | the **advisory council** — five role assessments + one synthesis; deterministic baseline + an optional AI enrichment merged so canonical facts/caveats stay code-generated; reinforces the robustness fixes + rejects unsafe asks | ai.client + state / domains / nlu_extractor · **AI-first, no executor, no tool registry** |
| `agent_orchestrator.py` | the **loop**: extract (via `nlu_extractor`) → merge → classify → gate → run/park/block; runs the advisory council (`council=True`); surfaces change/clarify/limited notes + the `apply_correction` edit path | nlu_extractor + agent_council + the above |

> **Three AI-touching agent modules.** Only `agent_orchestrator`, `nlu_extractor`, and
> `agent_council` import the AI client. All three still import **no executor and no result-path**
> code (and the council imports **no tool registry** — it can't run anything); the pure modules
> (state / actions / prompts / policy / domains) and the tool registry import **no AI** at all.

## Agent Council (advisory review layer)

`agent/agent_council.py` makes the assistant feel like a **team of research advisors**, not one
parser. After the orchestrator has understood a message and chosen an action, the council produces
a short structured **review** from five role perspectives — *Experiment Understanding · Domain &
Engine Router · Scientific Critic · Experiment Design Advisor · Results & Validation Critic* — plus
one synthesized recommendation (`understood_scenario`, `likely_domain`, `executable_engine_status`,
`planning_or_execution_status`, `key_missing_details`, `assumptions_to_confirm`,
`scientific_warnings`, `recommended_next_user_question`, `safe_next_action`).

It is **purely advisory** and cannot weaken the safety model:

- It **never executes a tool**, runs nothing, and imports no executor / tool registry.
- It **never decides the action** — the orchestrator + policy gate own that; the council's
  `safe_next_action` only *mirrors* the orchestrator's choice for the user.
- It **never fabricates** a composition, release fraction, measured value, pH/strength result,
  validation status, or certainty.
- The **safety-critical synthesis fields** (engine status, scientific warnings, missing details,
  safe next action) are **code-generated** from the existing validators. With AI on, the council
  call enriches only the **role prose + understood-scenario + the one next question** — the AI can
  never weaken a canonical fact or caveat (they are taken from the deterministic baseline).

Modes: **AI on** → one grounded call, validated + merged onto the deterministic baseline; **AI off
/ failed** → a deterministic lightweight council from the domain / safety / missing-field
validators, labelled *"AI council unavailable; using deterministic review."* It also reinforces the
robustness behaviours — a thermal-pretreatment temperature is reported separately from the leach
temperature, out-of-scope elements (e.g. Ni/Co/Mn) are surfaced, a geopolymer-strength study is
planning-only (PHREEQC only as optional pore-solution support), and an **unsafe** ask ("run
everything automatically", "assume data", "validate my result") is **explicitly rejected**.

The UI shows a **Council Review** card (synthesis up top; the five roles under *"Show council
reasoning"*; no raw JSON). When a run is saved, only the **derived** council fields (`to_safe_dict`)
are stored in provenance — never a raw model response.

## Planning-only domains → literature + evidence (not a fake prediction)

For a domain with **no validated engine yet** (composite strength, thermal, durability…), the
assistant's planning-only response says plainly it **cannot run a validated model yet** and offers to
**search reliable scholarly literature** and **build an evidence / training dataset** (alongside the
existing structure-the-plan / data-template / missing-variables offers). That search + extraction +
curation happens in the **Evidence Library** (`flyash_phreeqc_ml/literature/`), which uses **official
scholarly APIs** (never Google Scholar scraping), keeps every value's **source + confidence**, never
fabricates, and is **off the scientific result path** — it builds a dataset the **Prediction Models**
ML surrogate engine can learn from; it does not predict strength itself. See
[`docs/literature_agent.md`](literature_agent.md).

### When a trained ML surrogate exists → offer the experimental estimate

Once enough evidence/lab rows are **approved** and a model is **trained in Prediction Models**
(`flyash_phreeqc_ml/ml_models/`), the assistant additionally *offers* that surrogate for a
mechanical-property prompt: an **experimental (not validated)** estimate with an uncertainty range,
in the **Prediction Models** section. Hard rules:

* **PHREEQC is never the strength engine** — the domain gate still routes a strength prompt to
  `polymer_composite` and blocks PHREEQC; the ML surrogate is a *separate* prediction engine.
* **The LLM never produces the number.** A scikit-learn model does; the assistant only routes and
  offers. With **no** trained model it offers literature / data-building instead, never a fabricated
  prediction.
* The "is a model available?" check is a **read-only registry query in the UI**, passed to
  `orchestrator.respond(..., ml_model_available=…)` — the agent imports nothing from `ml_models`.

See [`docs/ml_surrogate_engine.md`](ml_surrogate_engine.md).

## The safety rules (enforced by the policy + tests)

1. **Execution / save always require explicit confirmation.** When the model proposes a run/save,
   the orchestrator **parks** it (`pending_action`, `confirmation_required=True`) and returns
   *without executing*. Execution happens only via `confirm_pending_action` — the UI's
   **"Yes, run it"** button, or an unambiguous affirmative reply to a parked action. The model
   cannot both propose and confirm in one turn.
2. **PHREEQC is blocked for non-leaching domains.** A polymer-composite *strength* test, a thermal,
   mechanical, corrosion, or battery scenario — or a cementitious binder not framed as leaching — is
   **planning-only**: the assistant structures the variables, offers a data template/checklist, and
   says plainly that no executable engine exists for that domain yet.
3. **A run is blocked when required fields are missing** or the input preview's **material
   composition is not usable** (no run on an invented composition / release fraction). The preview
   stops at `needs_material_composition` until you provide and confirm a composition.
4. **The AI never writes PHREEQC input.** The deterministic `phreeqc_input_builder` templates the
   `.pqi` from the *structured scenario*; any input text the model tries to supply is **stripped**
   (`agent_actions.FORBIDDEN_ARGUMENT_KEYS`) and never reaches the builder/executor.
5. **Numbers come from tools, not the model.** The assistant prose is led by the model, but the
   factual summary appended to it (pH, element totals, status) is the deterministic tool outcome.
6. **Simulation is not validation.** Every result explanation carries the standing not-validated
   caveat.

## States

`idle → collecting_context → asking_clarification → planning →
awaiting_preview_confirmation → preview_ready → awaiting_execution_confirmation →
running_tool → results_ready → validation_recommended`, plus
`unsupported_domain_planning_only` for planning-only domains.

## Actions

`ASK_USER`, `UPDATE_SCENARIO`, `CLASSIFY_DOMAIN`, `PLAN_EXPERIMENT`, `REQUEST_MATERIAL_PROFILE`,
`REQUEST_RELEASE_MODEL`, `CHECK_DATABASE`, `BUILD_PHREEQC_PREVIEW`, `REQUEST_RUN_CONFIRMATION`,
`RUN_SINGLE_SIMULATION`, `BUILD_SWEEP_MATRIX`, `REQUEST_SWEEP_CONFIRMATION`, `RUN_SWEEP`,
`RANK_RESULTS`, `TARGET_MATCH`, `SAVE_SIMULATION_RUN`, `CREATE_VALIDATION_TEMPLATE`,
`EXPLAIN_RESULTS`, `OPEN_ADVANCED_WORKFLOW`. Each has a **risk level** (safe / preview / execute /
save), whether it **requires confirmation**, the **domains** it is allowed in, and its **state
preconditions** (`agent_actions.ACTION_SPECS`).

## Provenance (saved runs)

When a run is saved from the assistant, the run registry stores an additive `agent_provenance`
block: the **transcript summary** (user-visible messages only), the **action trace** (action,
policy decision, confirmation required/given, tool, status), the **confirmed assumptions**, and
the standing **not-validated** label. It stores **no raw model responses, no API key, and no
measured data** (the assistant never holds those). It is additive — no scientific field changes.

## Boundaries (tests)

- `tests/test_nlu_extractor.py` — the understanding layer: text normalization (typos/units), the
  careful-acid rule, impossible-value rejection (negatives + out-of-range), the three messy example
  prompts (leaching / plastic-strength / red-mud), AI `understanding` validation + forbidden-key
  stripping, invalid-JSON safe fallback, change/conflict detection, the JSON-safe card.
- `tests/test_agent.py` — behaviour: asks for the 1–3 missing details, messy prompts populate the
  scenario (rules **and** AI understanding), follow-ups merge + corrections win and are announced,
  the inline corrector edits without running, "run everything automatically" never auto-executes,
  ambiguous "acid" is asked, the limited-without-AI note shows once, no raw model text in the card,
  PHREEQC blocked for a plastic-strength scenario, preview/execution route through the existing
  builder/executor, the agent writes no PHREEQC input, a saved run stores no raw model response.
- `tests/test_agent_council.py` — the council: five role summaries, the synthesis (missing
  details + safe next action), no raw response stored, leaching (PHREEQC offered but asks for
  composition/release), plastic-composite planning-only, thermal+leach temperature separation,
  geopolymer not over-routed, out-of-scope elements captured, unsafe-intent rejection, the
  question cap, and the AI path (roles merged, canonical fields kept, advisory only).
- `tests/test_ai_boundary.py` — imports: the agent's pure modules reach no AI/executor; the tool
  registry **and** `nlu_extractor` **and** `agent_council` reach no executor (they may import AI,
  like `ai/scenario_parser`, but run nothing; the council also imports no tool registry); no agent
  module imports the result path; no scientific/result-path module imports the agent.
- `tests/test_app_tabs_smoke.py` — the full seven-section app renders end-to-end.

## Disabling AI

Unset `ANTHROPIC_API_KEY` (or don't tick the Assistant-tab consent box). The deterministic planner
then chooses the next action with rules only, and the whole conversation + simulation workflow
still works.
