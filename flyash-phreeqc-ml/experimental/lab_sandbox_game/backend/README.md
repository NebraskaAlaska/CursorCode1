# Backend — 2D Lab Sandbox (experimental scaffold)

A small, **hermetic** Python API scaffold for the sandbox. The science/contract logic is plain
functions returning plain dicts (no web framework required); FastAPI is an *optional* HTTP binding.

## Design in one paragraph

`schemas.py` owns the Material Card + the station vocabulary + validation (the honesty contract).
`chem.py` does stoichiometry only (formula → element counts → molar mass). The four `routes_*.py`
modules are the stations: `routes_materials` (Synthesizer), `routes_xrd` (expected peaks),
`routes_phreeqc` (preview + confirm gate, **never executes**), `routes_icp` (data reduction, never
fabricates). `science_core.py` is an optional, read-only bridge to `flyash_phreeqc_ml` for when the
sandbox is run inside the platform. `main.py` exposes everything over FastAPI if it's installed.

## Layout

```
backend/
├── schemas.py            # Material Card, data_status vocab, stations, validate_material_card()
├── chem.py               # parse_formula(), molar_mass()  — stoichiometry ONLY
├── routes_materials.py   # synthesize()  — Synthesizer; never invents phases/structure
├── routes_xrd.py         # expected()    — expected peaks or an honest refusal
├── routes_phreeqc.py     # preview(), run()  — gate; executed/auto_run always False
├── routes_icp.py         # process(), refuse_measured_from_composition()
├── science_core.py       # optional read-only bridge (HAS_SCIENCE_CORE)
├── main.py               # optional FastAPI app (import-safe without FastAPI)
├── requirements.txt      # OPTIONAL deps (fastapi/uvicorn) — NOT added to the main app
└── tests/                # runs outside the main pytest suite (explicit path only)
```

## Run the tests

No third-party deps required (the tests exercise the route functions directly):

```bash
# from the repo root
python -m pytest experimental/lab_sandbox_game/backend/tests -q
```

These tests live outside the project's `testpaths = ["tests"]`, so the default `python -m pytest`
does **not** pick them up — the main suite stays exactly as it was.

## Serve over HTTP (optional)

```bash
cd experimental/lab_sandbox_game/backend
python -m venv .venv && source .venv/bin/activate    # optional, to avoid touching the main env
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
curl http://localhost:8000/health
```

If FastAPI is **not** installed, `import main` still works and `main.app is None`
(`create_app()` raises a clear, actionable error). That is intentional: the scaffold must never force
a dependency onto the main platform.

## The science-core bridge

`science_core.py` tries to `import flyash_phreeqc_ml.instruments.{xrd_advisory, icp_processor}` and
sets `HAS_SCIENCE_CORE`. It is **read-only** — importing those modules does not run or change the
Streamlit app — and it has **no PHREEQC `run` passthrough**: execution always stays in the platform's
confirmation-gated engine. To enable the bridge, run with the repo root importable (e.g. from the repo
root, or with `PYTHONPATH` set, or after `pip install -e .`). When it isn't importable, the routes
fall back to their self-contained behavior and nothing breaks.

## Honesty invariants the tests pin

- Synthesizer never invents phases/structure/data for unknown materials (`unknown` / `formula_only`).
- XRD refuses an exact pattern without phase + structure; output is always `measured: false`.
- PHREEQC previews only; `run` enforces the confirm gate and `executed`/`auto_run` stay `false`.
- ICP processes only supplied rows (no invented rows) and refuses to fabricate measured data from a
  composition; `can_synthesize_measured_from_composition()` is permanently `False`.
- Every Material Card validates against `schemas.validate_material_card` and the JSON Schema enums
  match the Python vocabulary.
