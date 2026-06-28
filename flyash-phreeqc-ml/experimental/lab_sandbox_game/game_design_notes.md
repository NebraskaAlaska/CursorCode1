# Game design notes — 2D Lab Sandbox (experimental)

> Design sketch for the playful layer. The science layer underneath stays honest (see
> `architecture.md`). Nothing here overrides a scientific rule — if a mechanic and a rule conflict,
> the rule wins and the mechanic changes.

## 1. Pitch

A cozy 2D pixel-art lab. You're a researcher in a side-scrolling facility. Walk up to the
**Synthesizer**, type a material ("quartz", `SiO2`, or a custom oxide mix), and it prints a
**Material Card** into your inventory. Carry that card to lab stations and watch honest science come
out — expected XRD peaks, a PHREEQC input you must review and confirm, ICP data reduction. The twist:
**the game teaches you to tell real from modeled.** Cards and readouts are color-coded by how much you
actually know.

## 2. Core loop

```
   ┌─────────────┐     ┌──────────────┐     ┌────────────────────┐
   │  SYNTHESIZE │ ──▶ │  READ CARD   │ ──▶ │  CARRY TO STATION  │
   │ name/formula│     │ data_status, │     │ XRD / PHREEQC / ICP │
   │ /composition│     │ warnings     │     │                    │
   └─────────────┘     └──────────────┘     └─────────┬──────────┘
          ▲                                            │
          │                ┌───────────────────────────▼─────────────┐
          │                │  STATION REACTS HONESTLY                  │
          │                │  - eligible? do the honest thing          │
          └────────────────┤  - not eligible? explain what's missing   │
        learn & iterate    │  - always labeled (measured/sim/ref/...)  │
                           └────────────────────────────────────────────┘
```

## 3. Stations (Phase 1) and what the player sees

| Station | Player action | Honest output | What it refuses (visible in-game) |
|---|---|---|---|
| **Synthesizer** | type a material | a Material Card with a colored `data_status` chip | inventing phases/structure for unknown inputs |
| **XRD** | dock a card | expected/approximate peak picks on a faux diffractogram, stamped **EXPECTED** | an exact pattern from a formula; any "identified"/"measured" stamp |
| **PHREEQC** | dock a card + set leachant/source/db | a printed **input preview** on a clipboard; a glowing **Confirm to Run** lever | auto-running; it previews first and waits for the lever |
| **ICP** | feed a concentration table | a reduced table (mM, blank/dilution, residuals) | simulating plasma; making measured values from a solid card |

Later stations (FTIR, TGA, SEM-EDS) are drawn but **locked**, with a tooltip that says "advisory
placeholder — no honest behavior yet." Honesty includes admitting what isn't built.

## 4. Honesty as the actual game mechanic

The teaching goal is *epistemic literacy*. Mechanics that encode it:

- **Color-coded data_status chips** on every card and readout:
  - `measured` = solid green (rare — only from data you import)
  - `reference` = blue (known, but about the reference, not your sample)
  - `simulated` / `predicted` = amber (a model said so)
  - `synthetic_demo` = striped/teal ("demo, not real")
  - `assumed` / `user_provided` / `formula_only` = gray (you told me / I only parsed it)
  - `unknown` / `missing` = red outline (nothing invented to fill it)
  - `cached` = any color with a small clock overlay (reused, original status kept)
- **The Confirm-to-Run lever** for PHREEQC is a physical object the player must pull. There is no
  auto-run. Pulling it logs an explicit confirmation (and, in integration, dispatches to the real
  engine). The scaffold shows "gate satisfied — not executed here."
- **"Why not?" tooltips.** Every *ineligible* station tells the player exactly what's missing (a
  phase + structure for XRD, a solution table for ICP, a full setup for PHREEQC). Failure is a lesson,
  not a dead end.
- **A lab notebook** records the provenance chain of each result, so the player can always answer
  "where did this number come from?"

## 5. Example sessions

**A) The demo material (matches the platform's demo test input).**
Synthesize "demo fly ash" → a `synthetic_demo` card (oxide wt%; no phases — fly ash is amorphous).
- XRD: offers a *checklist* of phases to look for, stamped EXPECTED, with the amorphous-hump caveat.
- PHREEQC: dock it, set `0.5 M NaOH`, `1% release`, `phreeqc.dat`, 25 °C → input **preview** →
  pull Confirm-to-Run → "gate satisfied; execution delegated to the real engine."
- ICP: locked until you bring a measured/predicted concentration table.

**B) A bare formula.**
Type `NaCl` → a `formula_only` card (Na:1, Cl:1; no phases). XRD refuses an exact pattern and explains
why. The player learns: *stoichiometry ≠ structure.*

**C) Nonsense.**
Type "Unobtainium" → an `unknown` card, red outline, "nothing invented." The game never rewards
fabrication with fake science.

## 6. Art & feel (pixel-art direction)

- 16×16 or 32×32 tile world, warm lab palette, CRT-ish station monitors for readouts.
- Material Cards as collectible item sprites with a status-colored border and a tiny icon for phase
  vs composition vs unknown.
- Stations are machines with an "intake" slot (dock a card) and a "readout" screen.
- Diegetic honesty: stamps ("EXPECTED", "PREVIEW — NOT RUN", "SYNTHETIC DEMO") are printed on the
  in-world paper/screens, not just in menus.

## 7. Progression ideas (non-binding)

- Unlock stations by demonstrating you can read provenance (e.g. correctly tag a readout's
  data_status before the station unlocks the next tool).
- "Validation quests": import a measured table, run a (delegated) PHREEQC prediction, and use the ICP
  station to compute residuals — the game celebrates *comparing*, never *asserting*.
- No "win by faking." There is intentionally no mechanic that converts assumed/unknown into measured.

## 8. What this scaffold already supports

The backend already returns everything the UI needs to render the above honestly: cards with
`data_status` + per-station eligibility + reasons; XRD expected-peak picks vs refusals; the PHREEQC
preview/confirm lifecycle; ICP reductions and refusals. The game client is the missing piece — see
`godot_client/`.
