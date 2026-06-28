# API contract — 2D Lab Sandbox (experimental)

JSON over HTTP. The game client talks to these endpoints and **never** imports Python science modules
directly. The shapes below match the scaffold's actual route functions (`backend/routes_*.py`); the
FastAPI binding in `backend/main.py` exposes them. WebSocket is reserved for later (streaming long
runs / live station feedback) and is not part of this scaffold.

Conventions:
- All responses are JSON objects. Every science-bearing response carries an honesty field
  (`data_status`, or `measured`/`exact_pattern`/`executed`/`fabricated` as appropriate).
- Nothing here ever returns `measured` data the caller didn't provide, and nothing executes PHREEQC.

---

## `GET /health`

Liveness + whether the optional science core is wired in.

```json
{
  "status": "ok",
  "scaffold": true,
  "executes_phreeqc": false,
  "science_core": { "has_science_core": false, "executes_phreeqc": false },
  "stations": ["synthesizer", "xrd", "phreeqc", "icp"]
}
```

---

## `POST /materials/synthesize`  — Synthesizer

**Request** (any one of `name`, `formula`, `composition`):

```json
{ "name": null, "formula": "SiO2", "composition": null }
```

**Response** — a Material Card (see `material_card_schema.json`). Known formula → `reference`:

```json
{
  "material_id": "quartz-1a2b3c4d",
  "display_name": "Quartz",
  "data_status": "reference",
  "data_status_label": "Reference",
  "formula": "SiO2",
  "phases": [{ "name": "Quartz", "formula": "SiO2", "source": "reference_database",
               "note": "trigonal; dominant 26.6° 2θ reflection" }],
  "composition": null,
  "structure_source": "reference_database",
  "provenance": "reference_catalog",
  "uncertainty_notes": ["Phases come from an internal reference, not from your sample."],
  "allowed_lab_stations": [
    { "station_id": "synthesizer", "eligible": true,  "reason": "Origin station ..." },
    { "station_id": "xrd",         "eligible": true,  "reason": "Phases with a known structure ..." },
    { "station_id": "phreeqc",     "eligible": false, "reason": "PHREEQC needs a composition ..." },
    { "station_id": "icp",         "eligible": false, "reason": "ICP reduces a solution table ..." }
  ],
  "warnings": ["Phase identity is REFERENCE data ...", "Confirm against measured XRD ..."]
}
```

**Honest edge cases:**
- `{"name": "Unobtainium"}` → `data_status: "unknown"`, `phases: []`, `structure_source: "none"`,
  warnings explaining nothing was invented.
- `{"formula": "NaCl"}` (parses, not in catalog) → `data_status: "formula_only"`, `phases: []`,
  composition `{ "basis": "element_mol_ratio", "values": {"Na":1,"Cl":1} }`.
- `{"composition": {...}}` → `data_status: "assumed"`, composition `status: "user_provided"`.
- `{"name": "demo fly ash"}` → `data_status: "synthetic_demo"`, oxide composition, no phases asserted.

---

## `POST /xrd/expected`  — XRD Station

**Request** — a card (`{"card": {...}}`) or an explicit phase list (`{"phases": [...]}`):

```json
{ "phases": [{ "name": "Quartz" }, { "name": "Blargite" }] }
```

**Response** — EXPECTED / approximate reference peaks. Never measured, never an exact pattern:

```json
{
  "station": "xrd",
  "result_type": "reference_available",
  "measured": false,
  "exact_pattern": false,
  "peaks": [
    { "phase": "Quartz", "formula": "SiO2", "approx_2theta_deg": 26.6, "basis": "approximate reference 2θ for Cu Kα ..." }
  ],
  "entries": [
    { "phase": "Quartz",   "status": "reference_available",   "approx_2theta_deg": [20.9, 26.6, 50.1] },
    { "phase": "Blargite", "status": "reference_data_needed", "approx_2theta_deg": [] }
  ],
  "unknown_phases": ["Blargite"],
  "warnings": ["These are EXPECTED phases / APPROXIMATE peaks — never a measured identification.", "..."],
  "disclaimer": "EXPECTED phases and APPROXIMATE reference peaks ..."
}
```

**Refusal** — no phases / no structure (e.g. a `formula_only` card):

```json
{
  "station": "xrd",
  "result_type": "reference_data_needed",
  "measured": false,
  "exact_pattern": false,
  "peaks": [],
  "message": "No phase identity available. XRD needs phases + a reference crystal structure; a formula or composition alone cannot produce an exact diffractogram."
}
```

---

## `POST /phreeqc/preview`  — PHREEQC Station (build a reviewable input; runs nothing)

**Request** (`{"setup": {...}}` or the setup directly):

```json
{
  "composition": { "basis": "oxide_wt_percent", "values": { "SiO2": 34, "CaO": 24 } },
  "source_term": "1% release of Ca, Si, Al, Fe, Na, K",
  "leachant": "0.5 M NaOH",
  "database": "phreeqc.dat",
  "temperature_c": 25
}
```

**Response — all inputs present** → `ready_for_review` with a deterministic `preview_id`:

```json
{
  "station": "phreeqc",
  "status": "ready_for_review",
  "preview_id": "pqprev_9f2c1a0b7e34",
  "preview_text": "# PHREEQC INPUT PREVIEW — generated for REVIEW ONLY. Not executed ...",
  "executed": false,
  "auto_run": false,
  "message": "Preview built. Nothing was executed. Review it, then call /phreeqc/run with confirm=true ..."
}
```

**Response — missing inputs** → no preview:

```json
{
  "station": "phreeqc",
  "status": "missing_inputs",
  "missing": ["source_term", "leachant", "database"],
  "preview_id": null,
  "preview_text": null,
  "executed": false
}
```

---

## `POST /phreeqc/run`  — the confirmation gate (NEVER executes in this scaffold)

**Request:**

```json
{ "preview_id": "pqprev_9f2c1a0b7e34", "confirm": true }
```

**Response — confirm omitted/false** → held:

```json
{ "station": "phreeqc", "status": "awaiting_confirmation", "executed": false, "auto_run": false,
  "message": "Explicit confirmation is required before any run. Nothing was executed." }
```

**Response — confirm true** → gate satisfied, but the scaffold still does not run:

```json
{
  "station": "phreeqc",
  "status": "confirmed_not_executed",
  "executed": false,
  "auto_run": false,
  "dispatch_target": "flyash_phreeqc_ml PHREEQC executor (NOT invoked by this scaffold)",
  "message": "Confirmation gate satisfied. In the integrated platform this dispatches to the existing confirmation-gated PHREEQC engine. This experimental scaffold does NOT execute PHREEQC ..."
}
```

**Response — unknown `preview_id`** → `{"status": "error", "executed": false, "message": "Unknown preview_id. Call /phreeqc/preview first ..."}`.

---

## `POST /icp/process`  — ICP Processor Station (reduce data; never fabricate)

**Request** — a concentration table you already have (measured and/or predicted):

```json
{
  "rows": [
    { "sample_id": "S1", "element": "Ca", "concentration": 40.078, "unit": "mg/L", "role": "measured" },
    { "sample_id": "S1", "element": "Ca", "concentration": 80.156, "unit": "mg/L", "role": "predicted" }
  ],
  "apply_blank": true
}
```

**Response** — corrected rows (same count as input — nothing invented) + residuals:

```json
{
  "station": "icp",
  "corrected": [
    { "sample_id": "S1", "element": "Ca", "role": "measured",  "value_mM": 1.0, "below_detection_limit": false },
    { "sample_id": "S1", "element": "Ca", "role": "predicted", "value_mM": 2.0, "below_detection_limit": false }
  ],
  "residuals": [
    { "sample_id": "S1", "element": "Ca", "measured_mM": 1.0, "predicted_mM": 2.0, "residual_mM": -1.0, "percent_difference": -50.0 }
  ],
  "fabricated": false,
  "warnings": [],
  "explanation": "The ICP station reduces measured/predicted concentration data; it does not simulate the plasma ..."
}
```

**Refusal** — asking ICP to invent measured data from a solid composition (`{"from_composition": {...}}`):

```json
{
  "station": "icp",
  "accepted": false,
  "fabricated": false,
  "reason": "ICP concentrations describe a measured (or model-predicted) liquid. This station will not generate measured ICP values from a solid composition alone — that would be fabricating measured data ..."
}
```

---

## Error & honesty field summary

| field | appears on | meaning |
|---|---|---|
| `data_status` | material cards | epistemic label; `measured` is never minted by the sandbox |
| `measured` | XRD | always `false` — XRD here is a plan, not an identification |
| `exact_pattern` | XRD | always `false` — no exact diffractogram from a formula |
| `executed` / `auto_run` | PHREEQC | always `false` — the scaffold never runs PHREEQC |
| `fabricated` | ICP | always `false` — no invented measured values |
| `result_type` / `status` | XRD / PHREEQC | lifecycle / refusal state |
