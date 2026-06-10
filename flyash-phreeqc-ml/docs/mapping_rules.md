# Mapping rules — how a measured condition is matched to a model scenario

*Audience: a scientist reviewing the methodology.* This document explains, in plain
terms, how the app decides which PHREEQC model scenario a measured experimental
condition should be compared against, how confident that match is, and when the
comparison should **not** be treated as model validation. There is **no machine
learning** here — every rule is hand-written and transparent, and every suggestion
the app shows is generated from a machine-readable *decision trace*, not prose written
after the fact.

Two layers work together:

1. **A rule-based score** (`scenarios.score_scenario`) ranks candidate model scenarios
   for a measured condition and bands them into high / medium / low **confidence**.
2. **A four-state mapping status** (`replicates.mapping_status`) decides whether a
   chosen match is scientifically usable: `exact`, `scenario-level only`, `unsafe`, or
   `needs new simulation`.

---

## 1. Mapping status (the four states)

`replicates.mapping_status(sample, scenario)` classifies one measured-condition →
model-scenario link, in this order (first match wins):

| Status | When | Meaning for interpretation |
|--------|------|----------------------------|
| **needs new simulation** | No model scenario is linked at all. | Nothing to compare — a new simulation (or a mapping) is required. |
| **unsafe** | The leachant is an **acid** (e.g. HCl) on a NaOH model scenario, **or** the CO₂ families are opposite (atmospheric vs reduced). | A known metadata conflict. Excluded from comparison by default. |
| **scenario-level only** | The model cannot confirm a piece of experimental metadata the experiment specifies — reaction **time**, an **OA/PF/GS cup cover** the model does not explicitly represent, or **NaOH molarity**. | A broad match; a workflow check, **not** model validation. |
| **exact** | None of the above — the comparable metadata align and nothing is left unconfirmed. | The only status that supports a validation claim. |

`exact` is deliberately hard to reach with the current PHREEQC files, because those
files do not carry time, cup-cover, or NaOH-molarity metadata. That is the honest
point: most comparisons are *scenario-level only* until matching simulations exist.

---

## 2. Scoring weights and rationale

The score ranks *which* scenario is the best candidate. Each rule contributes points
(the maximum positive total is **9**):

| Rule | Points | Rationale |
|------|-------:|-----------|
| PHREEQC state = `batch` | **+3** | An experiment measures the post-equilibration (batch) state, not the starting solution. |
| PHREEQC state = `initial` | **−4** | The initial (pre-reaction) solution is almost never what was measured — penalised hard so it sorts last. |
| `liquid_solid_ratio` matches | **+3** | L/S strongly controls dissolved concentrations; a matching L/S is a strong signal. |
| `CO2_condition` family compatible | **+2** | Same CO₂ family (atmospheric vs reduced) is necessary for a comparable carbonation regime. |
| temperature matches, or either side unknown | **+1** | Temperature has a smaller effect here and is often constant (25 °C); unknown is not penalised. |
| major metadata conflict (opposite CO₂ family, or two known-but-different L/S) | **−2** | An extra penalty so a genuinely conflicting scenario cannot tie a compatible one. |

Rules that do **not** change the score but are still recorded in the trace:

* **Leachant family normalization** (`HCl → acid`, `NaOH → base`) — informational; the
  acid-vs-NaOH safety decision is made in the mapping status, not the score.
* **CO₂ family grouping** (e.g. `OA → atmospheric`, `low_CO2 → reduced`) — shows the
  fuzzy grouping the comparison relied on.
* **Metadata-quality caps** (time / cup cover / NaOH the model can't confirm) — 0 points,
  but they lower the confidence band (see §3).

> The weights are hand-tuned, not learned. They are **not changed** by the trace work;
> if a weight looks wrong on review, raise it as a methodology question.

---

## 3. Confidence bands and caps

The raw score is banded:

* score ≥ **7** → **high**
* score ≥ **4** → **medium**
* otherwise → **low** (treated as "no good match")

Then the band is **capped** (never raised): if the experiment specifies metadata the
model cannot confirm — reaction time, an OA/PF/GS cup cover not explicitly represented,
or NaOH molarity — the confidence is capped at **medium**. So *high* confidence requires
both sides to actually align, not merely share L/S + CO₂ + batch state.

The app prints the banding math, generated from the trace, e.g.:

> `score 9 of max 9 → high; capped to medium because the model does not specify time_min, condition_code, NaOH_M`

---

## 4. Cup-cover (CO₂) semantics

CO₂ exposure is controlled by the **cup cover**, encoded as the `CO2_condition` value:

* **OA = open air** — directly exposed to atmospheric CO₂ (*atmospheric* family).
* **PF = plastic flap cover**, **GS = glass cover** — covered cups, likely reduced CO₂
  exchange (*reduced* family). **PF and GS are NOT confirmed airtight and are never
  called "sealed."**

Model scenarios use `atm_CO2` (atmospheric) and `low_CO2` / `no_CO2` (reduced).
Consequences for matching:

* **OA ↔ atmospheric model (`atm_CO2`)** is a genuine match — OA can reach **exact**.
* **PF/GS ↔ reduced model (`low_CO2`/`no_CO2`)** share the *reduced* family and so are
  compatible, but the match is **unconfirmed** (the model does not represent that
  specific cover). Such a same-family match **caps at scenario-level only** until a model
  scenario explicitly carries that PF/GS cover code.
* **Opposite families** (OA ↔ reduced, or PF/GS ↔ atmospheric) are **unsafe**.

---

## 5. Worked examples (synthetic values)

Each example lists the rules that fire (the trace) and the resulting status.

### 5a. `exact`
Measured: `leachant=NaOH, L/S=5, CO2_condition=OA` (no time/NaOH specified).
Model: `state=batch, L/S=5, CO2_condition=atm_CO2`.

| field | outcome | points |
|-------|---------|-------:|
| state | matched (batch) | +3 |
| liquid_solid_ratio | matched | +3 |
| CO2_condition | matched (atmospheric = atmospheric) | +2 |
| temperature | matched (unknown ok) | +1 |

Score **9 → high**. No cap (OA is directly represented). Status **exact**.

### 5b. `scenario-level only`
Measured: `leachant=NaOH, L/S=5, CO2_condition=PF, time_min=10`.
Model: `state=batch, L/S=5, CO2_condition=low_CO2`.

Same +3/+3/+2/+1 = **9 → high**, **capped to medium** because the model does not specify
`time_min` and the PF cover. The reduced-family CO₂ match is compatible (no conflict), so
this is **scenario-level only**, not unsafe.

### 5c. `unsafe`
Measured: `leachant=HCl, acid_M=0.5, CO2_condition=OA`.
Model: any NaOH scenario.

The leachant family is **acid** on a NaOH scenario → **unsafe** (regardless of score).
(A PF sample against an `atm_CO2` model is unsafe for the same reason — opposite CO₂
families.)

### 5d. `needs new simulation`
Measured: any condition with **no** linked model scenario (or only low-confidence,
conflicting candidates).

Status **needs new simulation** — generate a matching scenario before comparing.

---

## 6. Where this lives in code

* Scoring + trace: `flyash_phreeqc_ml/scenarios.py` (`score_scenario`, `confidence_for`,
  `confidence_explanation`, `reason_from_trace`).
* Four-state status + cup-cover families: `flyash_phreeqc_ml/replicates.py`
  (`mapping_status`) and `scenarios.co2_family` / `co2_compatible`.
* Inclusion of mapped rows into the actual comparison: `compare/inclusion.py`
  (see `docs/comparison_inclusion.md`).
