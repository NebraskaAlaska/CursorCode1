# Input format & unit contract (generic importer)

This documents the **defined input contract** for the Data tab's generic importer
(`flyash_phreeqc_ml/import_mapping.py`). The importer does **not** "handle everything":
it accepts a declared set of formats and units, and on anything undeclared it asks you
to resolve it rather than guessing. All unit conversion goes through one authority,
`flyash_phreeqc_ml/units.py`, and every converted value keeps full provenance.

## Accepted file formats

| Format | Notes |
| --- | --- |
| `.csv`  | One header row. |
| `.xlsx` | Pick **one** sheet; the chosen sheet has one header row. Needs `openpyxl`. |
| `.xls`  | Same as `.xlsx`; needs `xlrd`. |

After sheet selection the importer reads **one header row** (configurable, default the
first row) and treats every row below it as a data record.

## Supported units per variable family

Concentration columns (Ca, Si, Al, Fe, Na, K) accept exactly these source units and are
all converted to **mM**:

| Source unit | Meaning | Conversion to mM | Conversion id |
| --- | --- | --- | --- |
| `mM`   | already molar | value unchanged | `identity` |
| `mg/L` | mass / volume | `mM = (mg/L) / M_element` | `mgL_to_mM` |
| `ppm`  | ≈ mg/L for dilute aqueous | `mM = (ppm) / M_element` | `ppm_to_mM` |
| `ppb`  | µg/L | `mM = (ppb / 1000) / M_element` | `ppb_to_mM` |

Molar masses `M_element` are the IUPAC standard atomic weights in `units.MOLAR_MASSES`
(surfaced in **Audit / Help → Calculation verification → Unit registry**). `mol/kgw`
(PHREEQC molality) → mM (`mM = molality × 1000`) is also registered, but it is a
**model-side** unit and is *not* accepted as a lab import unit.

pH columns are dimensionless (`pH`) and are never converted. `Sc` and total `REE` stay in
`ppb` (no molar mass is defined for them here).

### Unrecognised unit → no guess

If a column is offered a unit outside its accepted set, the importer **refuses** with a
typed error, e.g.:

```
unit 'g/L' not recognized for Ca; supported: mg/L, ppm, ppb, mM
```

You then pick a supported unit or abort the import. There is **no silent fallback** in
unit handling anywhere.

## Conversion provenance (stored per converted column)

When the importer converts a column `X_mM`, the saved run data also keeps three wide
companion columns so the conversion is auditable after the fact:

| Companion | Holds |
| --- | --- |
| `X_mM_orig_value` | the original numeric value, before conversion |
| `X_mM_orig_unit`  | the original unit (`mg/L` / `ppm` / `ppb` / `mM`) |
| `X_mM_conversion_id` | the registry id used (`mgL_to_mM`, …, or `identity`) |

Values imported already in mM get `conversion_id = identity`. These companions survive
`run_manager.save_lab_dataframe` and are **not** treated as unknown columns. The
**Audit / Help → Unit-conversion re-derivation check** recomputes each `X_mM` from its
companions through the registry and reports pass / warning / fail per column — this is the
mechanism that catches a wrong molar mass or a changed formula later.

**Legacy runs** imported before provenance existed have no companions; they still load and
are reported with `conversion_id = unknown(legacy)` and flagged (never errored).

## Unknown columns

Columns that do not map onto a schema target are **kept, not dropped**: they are preserved
with an `extra__` prefix (e.g. `extra__operator`). Provenance and conversion companions are
recognised columns and are never prefixed `extra__`.

## What is *not* guessed

- A unit outside a column's accepted set (refused, see above).
- A `CO2_condition` of legacy `sealed` — flagged for you to set OA / PF / GS, never
  auto-mapped to a cover.
- An element with no molar mass in the registry (refused with `UnknownElementError`).
