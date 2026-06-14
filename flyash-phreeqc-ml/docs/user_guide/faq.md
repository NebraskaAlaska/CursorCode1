# FAQ

### Why is my mapping only "scenario-level"?

Because the model result is **missing some metadata that your measurement specifies**. The
match is broadly right (same leachant family, same L/S, compatible CO₂), but the model
doesn't pin down something the measurement does — most often the **reaction time**, the
**exact concentration**, or a **cup-cover condition** the model doesn't represent. For the
fly-ash data, **PF** (plastic flap) and **GS** (glass) covers cap at scenario-level until a
model scenario explicitly represents the cover, because they reduce CO₂ exchange but are
not confirmed airtight. A scenario-level comparison is a useful **workflow check**, not
validation. To reach *exact*, generate (or supply) a model result that matches the
condition exactly — the app lists which conditions need one. See the **Mapping guide**.

### Why are rows excluded from the comparison?

The comparison only plots rows it can honestly compare, and it shows **every** excluded row
with **exactly one reason**:

- **no saved mapping** — the measured row isn't linked to a model result yet (do this in
  the **Match** tab);
- **mapping is unsafe** — a known conflict (e.g. acid measurement on a base scenario);
  excluded by default;
- **model prediction missing this variable** — the linked model result has no value for
  this variable (e.g. the model didn't predict Fe);
- **measured value missing / non-numeric** — the cell is blank or not a number.

Plotted + excluded always equals the total, so nothing is silently dropped. See
**Interpreting results**.

### Why does the app refuse to call the model "validated"?

Because validation is a strong claim, and most comparisons don't earn it. The app reserves
the word **valid** for the single case where it is justified: **every plotted mapping is
exact** and there are **enough rows**. In every other case the comparison is labelled
*preliminary*, *single-sample*, *unsafe*, *needs new simulations*, or *nothing to compare*,
and a standing banner says: *"This comparison is {status} — it is a workflow check, not
model validation."*

This is deliberate. A near-zero residual or a tidy-looking plot is **not** proof: it can
come from a coarse mapping, compensating errors, or a single sample. Showing the honest
status is the point of the app — see **Interpreting results → What this app does not
prove**.
