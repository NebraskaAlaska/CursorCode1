# Architecture — 2D Lab Sandbox (experimental)

## 1. Goals & non-goals

**Goal.** A playful 2D sandbox that teaches honest materials science: synthesize a material, carry it
to lab stations, and see outputs that are always labeled by *what kind of data* they are.

**Non-goals (hard).**
- Not a replacement for, or a modification of, the production Streamlit app.
- Not a PHREEQC runner — this backend never executes simulations.
- Not a measurement generator — it never fabricates measured data.

## 2. Three layers, one direction of dependency

```
┌──────────────────────────────────────────────────────────────────────────┐
│  GAME CLIENT  (Godot first; engine-agnostic)                               │
│  - pixel-art world, player, stations, inventory of Material Cards          │
│  - talks ONLY over HTTP/WebSocket — never imports Python science modules   │
└───────────────▲────────────────────────────────────────────────────────────┘
                │  JSON over HTTP (REST now; WebSocket later for streaming)
┌───────────────┴────────────────────────────────────────────────────────────┐
│  SANDBOX BACKEND  (this folder — plain Python; FastAPI optional)           │
│  routes_materials  routes_xrd  routes_phreeqc  routes_icp                   │
│  schemas (Material Card + stations + validation)   chem (stoichiometry)     │
│  - owns the honesty contract: data_status, station eligibility, gates       │
│  - hermetic & self-contained; testable with no web framework                │
└───────────────▲────────────────────────────────────────────────────────────┘
                │  OPTIONAL, read-only delegation via science_core.py
┌───────────────┴────────────────────────────────────────────────────────────┐
│  SCIENCE CORE  (existing platform: flyash_phreeqc_ml)                        │
│  - instruments.xrd_advisory (reference peaks)                               │
│  - instruments.icp_processor (units + QC + residuals)                       │
│  - confirmation-gated PHREEQC engine  ← owns execution; sandbox never runs  │
└──────────────────────────────────────────────────────────────────────────┘
```

Dependencies point **downward only**. The client never reaches past the backend; the backend may
*optionally* delegate down to the science core but works without it. The science core knows nothing
about the game. This is what keeps the experiment isolated from the website.

## 3. The Material Card — the unit of play and the unit of honesty

Everything the player carries is a **Material Card** (full contract in `material_card_schema.json`,
Python in `backend/schemas.py`). Its fields:

| field | meaning |
|---|---|
| `material_id` | deterministic slug + short hash (same input → same id) |
| `display_name` | name shown on the card |
| `formula` | chemical formula if known (stoichiometry only — **not** a phase identity) |
| `phases` | known crystalline phases; **empty** unless a real structure source provides them |
| `composition` | `{basis, values, status, note}` or null |
| `structure_source` | `reference_database` / `user_supplied` / `none` — gates XRD |
| `data_status` | the card's epistemic label (see below) — **never omitted** |
| `provenance` | short tag for where the card came from |
| `uncertainty_notes` | plain-language caveats |
| `allowed_lab_stations` | per-station `{eligible, reason}` — including what you *can't* do yet, and why |
| `warnings` | honesty warnings surfaced on the card |

### data_status vocabulary

`measured` · `simulated` · `predicted` · `reference` · `synthetic_demo` · `assumed` · `cached` ·
`unknown` · `missing` · `formula_only` · `user_provided`

`measured` is the one the sandbox will **never** mint on its own — it can only ever arrive from real
data the user supplies. `formula_only` and `user_provided` are honest scaffold extensions: they keep
"I parsed your formula" and "you typed this composition" from masquerading as `reference` or
`measured`.

## 4. Station eligibility is computed, not asserted

`schemas.station_eligibility()` derives `allowed_lab_stations` deterministically from what the card
actually contains:

- **Synthesizer** — always (it's the origin).
- **XRD** — only with `phases` **and** a real `structure_source`. A formula/composition alone → not
  eligible, with a reason saying why.
- **PHREEQC** — eligible to *preview* once a composition exists. Eligibility ≠ permission to run;
  execution stays behind the confirm gate.
- **ICP** — never eligible from a solid card; it needs a measured/predicted solution table.

A test (`test_material_card_schema.py`) fails the build if a card ever claims XRD without
phases+structure, or claims ICP from a solid card.

## 5. The PHREEQC lifecycle (and why the sandbox never runs it)

```
missing_inputs ──(composition + source_term + leachant + database)──▶ ready_for_review
ready_for_review ──/phreeqc/run without confirm──▶ awaiting_confirmation
awaiting_confirmation ──/phreeqc/run confirm=true──▶ confirmed_not_executed
                                                      │
                                          (integration only) dispatch to
                                          flyash_phreeqc_ml gated executor
```

The scaffold deliberately stops at `confirmed_not_executed`. `auto_run` is always `False` and
`executed` is always `False`. In the integrated platform, a confirmed preview would be handed to the
existing confirmation-gated engine — the sandbox itself never executes PHREEQC. This is *stronger*
than the production gate, not weaker.

## 6. Precomputed vs. live calculation

To keep the game responsive while staying honest about cost and provenance:

**Precompute / cache (serve as `cached`, with the original provenance preserved):**
- Reference peak tables for common phases (quartz, calcite, portlandite, …) — they never change.
- Synthesizer cards for common names/formulas and the synthetic demo material.
- Any previously computed result, keyed by a deterministic hash of its inputs (the same hashing the
  PHREEQC preview already uses for `preview_id`).

**Compute live (never cached blindly):**
- User-supplied structures/compositions and their PHREEQC previews.
- Confirmed PHREEQC runs (in integration) — always fresh, always behind the gate.
- ICP reductions of user-provided tables (cheap, but inputs vary every time).

**Caching rules that keep it honest:**
- A cache hit is labeled `cached` **and** keeps the underlying `data_status` (a cached *simulation* is
  still a simulation, never "measured").
- Cache keys are deterministic input hashes, so identical inputs reuse results and differing inputs
  never collide.
- Reference/synthetic-demo entries are safe to ship precomputed; measured data is never precomputed
  because the sandbox never holds measured data it wasn't given.

## 7. Why FastAPI is optional

The route logic is plain functions returning plain dicts, so it is testable and liftable without a
web framework. `main.py` binds those functions to FastAPI **iff** it is installed; otherwise it
imports fine and `app is None`. This keeps the main platform's dependency surface untouched and lets
the backend later move to its own service or a different framework with no logic changes.

## 8. Extending it

New stations (FTIR, TGA, SEM-EDS) follow the same recipe: add a station to `schemas.STATIONS`, decide
its eligibility rule in `station_eligibility`, add a `routes_<x>.py` whose output is labeled and whose
limitations are explicit, and add a test that pins the honesty invariant. If a station has no honest
behavior yet, register it as advisory/metadata and say so — never ship a station that fakes data.
