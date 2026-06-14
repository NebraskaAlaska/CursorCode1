# Mapping guide — what the four statuses mean

To compare measured data with a model prediction, the app must **link** each measured
record (or condition) to the model result for the *same* conditions. That link is a
**mapping**. The app suggests mappings for you using transparent rules (no machine
learning), and labels each one with one of four statuses. This page explains them in plain
language and what to do about each. (The technical scoring rules are in
[`docs/mapping_rules.md`](../mapping_rules.md).)

## The four statuses

### 🟢 exact
The measured record and the model prediction agree on all the metadata that matters
(leaching conditions, cover/CO₂ condition, L/S, time where relevant). This is the only
status that can support a *validated* comparison.
**What to do:** accept it.

### 🟡 scenario-level only
The match is broadly right, but the model result is **missing** some metadata the
measurement specifies (for example, the measurement has a reaction time the model result
doesn't pin down, or a cup-cover condition the model doesn't represent exactly). The
comparison is still useful as a **workflow check**, but it is not validation.
**What to do:** you can accept it for a preliminary look, or generate a model result that
matches the condition exactly to reach *exact*.

### 🔴 unsafe
There is a **known conflict** between the measured record and the model prediction — for
example an acid (HCl) measurement linked to a base (NaOH) model scenario, or opposite
CO₂ conditions. Comparing these would be misleading.
**What to do:** do **not** accept it from the table. The app routes these to a manual
override that requires explicit confirmation; usually you need a different (or new) model
result instead.

### 🔵 needs new simulation
No suitable model result exists for this condition at all.
**What to do:** generate a model result for the condition (the app lists exactly which
conditions need one), then map to it.

## A note on cup-cover conditions (fly-ash dataset)

For the fly-ash data, the condition codes are CO₂-exposure **cup covers**: **OA** = open
air (atmospheric CO₂), **PF** = plastic flap cover, **GS** = glass cover. PF and GS likely
reduce CO₂ exchange but are **not** confirmed airtight — the app never calls them "sealed".
This is why an OA condition can reach *exact* against an atmospheric model scenario, while
PF/GS cap at *scenario-level only* until a model scenario explicitly represents the cover.

## Why this matters

The mapping status flows straight into the comparison: a near-perfect-looking residual is
only meaningful if the mapping behind it is *exact*. See **Interpreting results** for how
the statuses turn into the overall validity line.
