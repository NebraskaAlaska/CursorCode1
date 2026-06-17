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
| converse, ask for missing critical details | invent a material composition, release fraction, phase, pH, concentration, or measured value |
| classify the domain (a *hint*; the rule clamps it) | run a simulation (it only *proposes*; the policy + your confirmation run it) |
| propose **one** structured action per turn | write or edit PHREEQC input (a deterministic builder does, from the scenario) |
| explain results in plain language (numbers from the tools) | bypass a material-profile / release-model / database warning |
| — | claim a result is "validated" (simulation ≠ validation) |

If there is **no API key**, a **deterministic planner** drives the same conversation (rule-based
phrasing) — the workflow works fully without AI.

## Modules (`flyash_phreeqc_ml/agent/`)

| Module | Role | Imports |
| --- | --- | --- |
| `agent_state.py` | conversation + structured scenario + deterministic, correction-aware **merge** of a natural reply (only fields the reply explicitly states; later corrections win); provenance trace | rule_parser / safety / scenario_schema (pure) |
| `agent_actions.py` | the **action vocabulary** + `AgentAction` + `parse_action` (strips forbidden keys like `phreeqc_input_text`) + per-action **metadata** (risk / confirmation / domain / preconditions) | pure |
| `domains.py` | **domain classification** + the engine map (only `leaching_geochemistry → PHREEQC`) | pure |
| `agent_prompts.py` | the system prompt + a **grounded** per-turn prompt (the model sees the deterministic state, never invents it) | scenario_schema / actions / domains |
| `tool_registry.py` | binds each action to a **deterministic backend function** (the existing builder/executor/registry/strategy/target-matching) | simulation modules · **no AI** |
| `agent_policy.py` | the **gate** (allow / needs-confirmation / block) + the **deterministic planner** | actions / domains / state · **no AI, no executor** |
| `agent_orchestrator.py` | the **loop** — the only module that touches AI | ai.client / import_assist + the above |

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

- `tests/test_agent.py` — behaviour: asks for missing details, natural replies merge (corrections
  win), AI can't run without confirmation, PHREEQC blocked for a plastic-strength scenario,
  planning-only response, preview built through the existing builder, confirmed execution routes
  through the existing executor, explanation carries the not-validated caveat, missing
  composition blocks execution, the agent writes no PHREEQC input, a saved run stores no raw model
  response.
- `tests/test_ai_boundary.py` — imports: the agent's pure modules reach no AI/executor; the tool
  registry reaches no AI; no scientific/result-path module imports the agent; the planner/executor
  layers don't depend on the agent.
- `tests/test_app_tabs_smoke.py` — the full seven-tab app renders end-to-end.

## Disabling AI

Unset `ANTHROPIC_API_KEY` (or don't tick the Assistant-tab consent box). The deterministic planner
then chooses the next action with rules only, and the whole conversation + simulation workflow
still works.
