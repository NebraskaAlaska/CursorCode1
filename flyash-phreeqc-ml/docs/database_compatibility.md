# PHREEQC database compatibility & candidate phases (Simulate Step 7c)

A PHREEQC prediction is only as good as its **thermodynamic database** and the **candidate
phases** it is allowed to precipitate. The same input run with `phreeqc.dat` and with a
cementitious database (CEMDATA18) can give very different precipitation / saturation-index
results — and many cement/fly-ash phases simply **do not exist** in the general databases.
Step 7c makes this transparent and refuses to pretend.

## Why the database choice matters

PHREEQC computes which phases precipitate (and the saturation indices) from the phases the
database defines. If a relevant phase (e.g. **Portlandite**, Ca(OH)₂, or **Ettringite**) is not in
the database, PHREEQC cannot predict it forming — the dissolved totals then look artificially high
and the SI picture is incomplete. So the answer depends on the database, not just the input.

## phreeqc.dat vs. a cementitious database

`phreeqc.dat` (the standard PHREEQC database) is fine for a **technical smoke test** — it runs, it
speciates Na/Ca/Si/Al/Fe, and it defines common minerals (Calcite, Gibbsite, SiO2(a), Gypsum,
Hematite, Goethite…). But it is **weak for high-pH cementitious systems**: it lacks the cement
phases (**Portlandite, Ettringite, C-S-H**) that actually buffer Ca/Al/Si at pH ~13. So with
`phreeqc.dat`, a fly-ash alkaline-activation prediction under-constrains Ca/Al/Si solubility.

**CEMDATA18** is the cementitious thermodynamic database designed for exactly these systems. It is
**not redistributable**, so the project never ships it.

## Configuring CEMDATA18 (if you have it locally)

```bash
export PHREEQC_DATABASE=/path/to/CEMDATA18-xx.dat   # your local copy (not shipped)
```

The Simulate tab reads this configured database, detects its family (phreeqc / llnl / wateq /
cemdata / unknown — from the file name and header), and reports which of a phase template's phases
it actually defines. The same database is what the executor uses, so the preview's phase list is
verified against the database the run will use.

## Candidate phase templates (small, reviewed, non-exhaustive)

Precipitation is only modelled if candidate phases are declared. Step 7c offers small **starting**
templates — they are **not** complete phase assemblages:

| Template | Phases (starting set) |
| --- | --- |
| **Aqueous only** (default) | none — speciation + SIs only, no precipitation |
| **Class C fly ash / cementitious** | Portlandite, Calcite, Gibbsite, SiO2(a), Gypsum, Ettringite |
| **Red mud / bauxite residue** | Hematite, Goethite, Gibbsite, Boehmite, Calcite |
| **Generic** | none (aqueous only) |

Each phase carries *why* it is included and whether it typically needs a specific database family.
**The list is deliberately not claimed to be complete** — it is a reviewed starting point.

### Why candidate phases must be reviewed

Which phases are *allowed* to precipitate is a modelling choice that strongly shapes the result.
Adding the wrong phase, or omitting an important one, changes the predicted dissolved totals. So the
template is shown to you, you choose it, and you can see exactly which phases will be added.

### Why missing phases limit interpretation — and how they're handled

The builder checks **every** template phase against the configured database and **adds only the
ones it actually defines**. The rest are **never added silently** — they are listed as warnings and
as `# SKIPPED (absent from this database, NOT added)` comments in the `.pqi`. For example, the
fly-ash template against `phreeqc.dat`:

- **added** (defined): `Calcite`, `Gibbsite`, `SiO2(a)`, `Gypsum`
- **skipped** (absent — needs CEMDATA18): `Portlandite`, `Ettringite`

The compatibility report grades this as `partial` (some phases present). Because Portlandite is
absent, Ca solubility at high pH is **not** properly constrained — the report says so rather than
implying a precipitation that the database cannot support. If **no** database is configured, phases
are **not** added at all (availability cannot be verified) — they are listed as candidates with a
warning, and you set `PHREEQC_DATABASE` and re-generate.

## Aqueous-only vs. phase-constrained simulations

- **Aqueous-only** (the conservative default): PHREEQC reports only aqueous speciation and the
  saturation indices it computes from the dissolved elements. **No precipitation is modelled** — the
  predicted dissolved totals equal the released amounts. Honest and database-light.
- **Phase-constrained**: with reviewed candidate phases (that the database defines), PHREEQC also
  precipitates supersaturated phases, lowering the dissolved totals and giving a fuller picture —
  but **only as good as the database + phase list**.

Neither is a validated result. Precipitation claims are only as strong as the phases and database
support them, and the whole output remains a **model prediction** until compared to measured data.

## Implementation

- `flyash_phreeqc_ml/simulation/database_compatibility.py` — `DatabaseInfo` / `PhaseAvailability` /
  `DatabaseCompatibilityReport`, `read_database_text`, `detect_family`, `phase_present`,
  `check_phases`, `database_defines_phases`, `build_report`. Pure text inspection — no execution, no
  AI, no comparison module.
- `flyash_phreeqc_ml/simulation/phase_templates.py` — the small `PhaseTemplate` sets.
- `phreeqc_input_builder.build_phreeqc_input_preview(..., phase_template=…, database_path=…)` adds
  only available phases and comments the rest; the Simulate **Step 7c** UI is
  `app.py :: _render_database_phases_section`.
- The Match-tab on-demand runner's CEMDATA gate (`phreeqc_runner.is_cemdata_compatible`) delegates to
  the same `database_compatibility.database_defines_phases`, so its integration test runs **only**
  when the configured database defines the CEMDATA phases (`Cal`, `Portlandite`) it templates — it
  **skips** on `phreeqc.dat` instead of failing.
- Covered by `tests/test_database_compatibility.py`; boundaries by `tests/test_ai_boundary.py`.
