# 2D Lab Sandbox — experimental side project (scaffold)

A **separate, experimental** 2D pixel-art "digital materials lab" sandbox game built on top of the
WPI Materials Research Assistant's science. A player types a material name or chemical formula into a
**Synthesizer**, gets a **Material Card**, and carries it to virtual lab stations — **XRD**,
**PHREEQC**, **ICP** (later FTIR/TGA/SEM-EDS) — to see honest scientific outputs and learn which data
are *measured, simulated, predicted, reference, synthetic-demo, assumed, cached,* or *unknown*.

> **Status: architecture + backend scaffold only. Not a finished game.** This folder contains the
> API contract, the Material Card schema, an importable/testable Python backend scaffold, and Godot
> client notes. There is no game executable yet.

---

## This is isolated from the main app — by design

This is an **experiment**, walled off from the production Streamlit platform:

- It lives entirely under `experimental/lab_sandbox_game/`.
- **It is not imported by `app.py`** and changes nothing about the website's behavior.
- **It never executes PHREEQC.** It enforces the preview→confirm gate and *delegates* execution to
  the existing engine — this scaffold runs no simulations itself.
- It **does not change the main app's dependencies.** FastAPI/uvicorn are optional and listed only in
  `backend/requirements.txt`.
- Its tests live outside the project's `testpaths = ["tests"]`, so the default `python -m pytest`
  does not collect them. The main suite is unaffected. (Run the sandbox tests explicitly — see below.)

The only link back to the platform is a **read-only, optional** bridge (`backend/science_core.py`)
that, *when present*, lets the science delegate to `flyash_phreeqc_ml` as the single source of truth.
It imports nothing at load that can hard-fail, and it never runs PHREEQC.

---

## Honesty rules (the science layer must stay honest even though the game is playful)

These mirror the platform's core scientific rules and are enforced in code + tests:

1. **No faked lab measurements.** `measured` is never invented. ICP will not turn a solid composition
   into measured concentrations.
2. **A formula is not a phase.** XRD needs phase identity **and** a reference crystal structure. The
   Synthesizer never derives phases/structure from stoichiometry; XRD refuses an exact pattern from a
   formula alone.
3. **PHREEQC is gated.** A run needs a confirmed composition + source term + leachant + database, and
   explicit confirmation. The sandbox previews only and never executes.
4. **ICP processes data; it does not simulate plasma.** mg/L→mM, dilution/blank/detection-limit,
   measured-vs-predicted residuals — over rows you provide. No fabrication.
5. **Everything is labeled.** Every output carries a `data_status` / result-type so the player always
   knows whether they're looking at measured, simulated, predicted, reference, synthetic-demo,
   assumed, cached, or unknown data.

---

## Files

```
experimental/lab_sandbox_game/
├── README.md                  ← you are here
├── architecture.md            ← system design, data-status model, precomputed-vs-live, diagram
├── api_contract.md            ← endpoints with request/response JSON examples
├── material_card_schema.json  ← language-agnostic Material Card contract (the client reads this)
├── game_design_notes.md       ← the playful 2D sandbox concept; honesty-as-gameplay
├── backend/                   ← Python API scaffold (FastAPI optional; logic is plain functions)
│   ├── README.md
│   ├── main.py                ← optional FastAPI binding (import-safe without FastAPI)
│   ├── schemas.py             ← Material Card + vocabulary + validation + stations
│   ├── chem.py                ← formula parser + molar mass (stoichiometry only)
│   ├── routes_materials.py    ← Synthesizer
│   ├── routes_xrd.py          ← XRD expected-peaks planner
│   ├── routes_phreeqc.py      ← PHREEQC preview + confirm gate (never executes)
│   ├── routes_icp.py          ← ICP data reducer (no plasma, no fabrication)
│   ├── science_core.py        ← optional read-only bridge to flyash_phreeqc_ml
│   ├── requirements.txt       ← OPTIONAL deps (fastapi/uvicorn) — not added to the main app
│   └── tests/                 ← runs outside the main suite; explicit-path only
└── godot_client/
    ├── README.md
    └── placeholder_project_notes.md
```

## Run the tests (isolated from the main suite)

```bash
# from the repo root
python -m pytest experimental/lab_sandbox_game/backend/tests -q
```

## Serve the API (optional — needs FastAPI)

```bash
cd experimental/lab_sandbox_game/backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# GET http://localhost:8000/health
```

See `backend/README.md` for details and `api_contract.md` for the endpoint shapes.
