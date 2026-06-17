# AI framework architecture — current and intended

The Materials Research Assistant is built so that **more engines and agents can be added behind
the same conversation** without changing the safety model. This doc describes the current agent
layer and the intended direction. **No LangGraph dependency is added** — the architecture is kept
*compatible* with a graph-based orchestrator, not coupled to one.

## Current (implemented)

A small, auditable, custom agent layer (`flyash_phreeqc_ml/agent/`):

```
conversation state (AgentState)
   → LLM proposes ONE structured action  (agent_orchestrator + agent_prompts)
   → policy gate: allow / needs-confirmation / block  (agent_policy)
   → optional explicit user confirmation (for execution / save)
   → deterministic tool runs the existing backend  (tool_registry)
   → updated state → assistant reply
```

- **Custom AI agent layer** — `agent_orchestrator` runs the loop; the LLM only *proposes* one
  action per turn (it never executes, never invents chemistry, never writes PHREEQC input).
- **Tool registry** — `tool_registry` binds each action to an existing deterministic backend
  function (the input builder, executor, batch, matrix, strategy, target-matching, run registry).
- **Policy gate** — `agent_policy` decides allow / needs-confirmation / block. Execution and save
  always require explicit confirmation; PHREEQC is blocked for non-leaching / missing-composition.
- **Deterministic backend tools** — the existing, tested Simulate modules do all the chemistry.

The pieces map cleanly onto a graph: **`AgentState` is the graph state**, **each action is a
node**, **`agent_policy` is the edge/condition function**, and **`tool_registry` is the node
implementation**. That is what makes it LangGraph-compatible already.

## Intended (designed for, not yet built)

- **LangGraph-style stateful orchestrator** — re-express the propose → policy-gate → confirm →
  tool loop as an explicit graph (nodes = actions, edges = policy decisions, state = `AgentState`).
  The safety contract (one action per turn, confirmation before execution, AI invents no
  chemistry, AI writes no input) is preserved unchanged.
- **Plugin engine registry** — engines register per domain. Today the registry is
  `domains.EXECUTABLE_DOMAINS = {leaching_geochemistry → PHREEQC}` plus the planning-only
  `domains.PLANNING_DOMAIN_INFO`; new engines slot in here without touching the conversation.
- **Literature / RAG agent** — sourced benchmarks. Seam: the quarantined literature layer
  (`flyash_phreeqc_ml/ai/literature.py`).
- **ML / surrogate agent** — fast approximations of a simulator. Seam: the experimental surrogate
  (`flyash_phreeqc_ml/ml/surrogate.py`).
- **Simulation-engine agents** — atomistic / mechanical-property / thermal engines, each
  registered for the domain(s) it serves.
- **Validation agent** — measured-vs-model comparison. Seam: the existing result path
  (`compare/`, `scenarios`, `replicates`, `mapping_table`).

## Why no LangGraph dependency yet

The current loop is small enough to keep auditable in plain Python, and the boundary tests
(`tests/test_ai_boundary.py`) pin the safety properties at the import + behaviour level. Adding a
graph framework now would add a dependency without changing behaviour. The architecture is kept
*compatible* (state machine + discrete actions + a policy edge function + a per-domain engine
registry) so the migration, when worthwhile, is mechanical and the safety model is unchanged.
