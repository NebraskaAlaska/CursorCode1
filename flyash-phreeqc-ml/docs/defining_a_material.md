# Defining a new material (no code)

The batch-reaction **closure** (Prompt 22), the PHREEQC **gap attribution** (Prompt 24),
the **recovery report** (Prompt 25), and the **model-incompleteness** estimator (Prompt 27)
are all material-agnostic: they read the elements, candidate precipitate phases, the
filtration flag, and the reagents from the **active profile's material**, never from
hard-coded fly-ash values. So you can run the whole batch → closure → attribution →
recovery workflow on a new material by writing one small spec file — no Python.

This is enforced by `tests/test_material_profile_agnostic.py` (those modules may not
hard-code an element or phase name) and proved end-to-end on a second material (red mud)
by `tests/matrix/test_g_second_material.py`.

## The spec file

Write a **JSON** file (no extra dependencies) or a **YAML** file (only if `pyyaml` is
installed). It has two blocks — `material` (the material side) and `dataset` (the
measured-data shape). A complete example is shipped at
[`docs/examples/red_mud_material.json`](examples/red_mud_material.json).

```jsonc
{
  "material": {
    "material_id": "red_mud",                       // required, machine id
    "display_name": "Bauxite residue (red mud)",    // required, shown in the UI/report
    "relevant_elements": ["Fe","Al","Ti","V","Na","Ca","REE"],  // what the chemistry involves
    "mass_balance_elements": ["Ti","V","Fe","Al"],  // which elements get a closure (opt-in)
    "candidate_phases": {"Anatase":"Ti","Hematite":"Fe","Gibbsite":"Al"},  // phase -> element
    "precipitate_in_measured_solid": true,          // Prompt-23 filtration flag (see below)
    "default_reagents": ["NaOH","H2SO4"],
    "starting_content_unit": "wt%",                 // unit of {el}_starting_content
    "solid_residue_unit": "wt%",                    // unit of {el}_solid_residue
    "liquid_conc_unit": "mM",                       // unit of the measured liquid {el}_mM
    "declared_assay": {                             // typical bulk assay, provenance-flagged
      "Fe": {"value": 30.0, "unit": "wt%", "provenance": "literature-confirmed",
             "citation": "https://doi.org/..."},
      "Ti": {"value": 5.0,  "unit": "wt%", "provenance": "literature-proposed",
             "citation": "https://example.org/preprint"}
    }
  },
  "dataset": {
    "grouping": "generic",                          // "fly_ash" only for the fly-ash key
    "condition_column": "reagent",
    "important_fields": ["reagent","reagent_conc_M","liquid_solid_ratio"],
    "overview_variables": ["Ti_mM","V_mM","Fe_mM","Al_mM"],
    "comparison_variable_spec": {"Ti_mM": ["Ti_mM","phreeqc_Ti_mM"]},
    "feature_numeric_fields": ["reagent_conc_M","liquid_solid_ratio"],
    "feature_categorical_fields": ["reagent"]
  }
}
```

### Loading it

```python
from flyash_phreeqc_ml import profiles
profile = profiles.load_dataset_profile("my_material.json")   # JSON or YAML
```

`profile` is a normal `DatasetProfile` with the material attached; pass it anywhere the
pipeline takes a `profile=` (closure, attribution, the recovery report, the incompleteness
model). The shipped `RED_MUD_PROFILE` is the same object built in code.

## Field reference

### `material`

| field | meaning |
| --- | --- |
| `material_id`, `display_name` | **required** ids; the display name appears in the report. |
| `relevant_elements` | every element the material's chemistry cares about (display/docs). |
| `mass_balance_elements` | the subset that gets a batch closure. **Empty = mass balance OFF** (the fly-ash default). |
| `candidate_phases` | `{ "<PHREEQC phase>": "<element>" }` — the phases attribution looks for. |
| `precipitate_in_measured_solid` | **Prompt-23 filtration flag** (see below). |
| `default_reagents` | reagents typically used to leach/activate the material. |
| `declared_assay` | a typical bulk assay per element, each `{value, unit, provenance, citation}`. |
| `*_unit`, `*_column` | assay/liquid units and the batch column names (default to the shipped schema). |

### The filtration flag (`precipitate_in_measured_solid`)

This decides how a PHREEQC-predicted precipitate `P` affects the **measured gap**
(`gap = n_in − n_liquid − n_solid`):

- `false` — the precipitate leaves with the **filtrate** (not in the assayed solid), so it
  **explains** the gap: `attribution_to_gap = min(P, gap)` (this is the fly-ash protocol).
- `true` — the precipitate is **retained in the assayed solid** (already in `n_solid`), so
  it explains the solid's *composition*, **not** the gap: `attribution_to_gap = 0`.

Pick the value your filtration/recovery protocol actually implements; it is recorded and
drives the recovery status. See [`mass_balance.md`](mass_balance.md).

### Declared-assay provenance (the quarantine rule)

Each declared assay carries a `provenance`:

- `measured` — a real measurement; **usable** in a calculation.
- `literature-confirmed` — a literature value a human confirmed; **usable** (must carry a
  `citation` DOI/URL).
- `literature-proposed` — a literature value **not yet confirmed**; it is kept for display
  but is **quarantined** — `is_usable` is `False`, so it can **never** enter a closure or
  recovery number until a human confirms it (the same Prompt-24/26 rule the per-run
  literature store enforces). Promote it by changing `provenance` to `literature-confirmed`
  once you have checked the source.

A literature provenance without a `citation` is rejected at load time.

## Data columns your run must provide

For each element `<X>` in `mass_balance_elements`, the measured rows need
`<X>_starting_content`, `<X>_solid_residue`, and `<X>_mM`, plus the batch columns
`material_mass_g`, `liquid_volume_mL`, and (optionally) `solid_mass_g`. Element molar
masses must exist in `units.MOLAR_MASSES` (Ti and V were added for red mud; add others
there if your material needs them).

## What does *not* move into the material (yet)

Generating a PHREEQC `.pqi` for a new material/reagent (the on-demand runner) still uses
the fly-ash NaOH + CO₂-cover templating — that part is the **current PHREEQC implementation**,
not a universal contract. The *measured* closure, the *attribution arithmetic* on a parsed
selected output, the *recovery report*, and the *incompleteness* training frame are all
fully material-driven and need no PHREEQC binary.
