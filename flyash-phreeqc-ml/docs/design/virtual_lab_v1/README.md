# Virtual LAB v1 — Frontend Design References

This folder contains the **visual frontend design references** for **Virtual LAB v1**.

The design is produced **separately** using **Claude Design / Fable**. The files here are a
**visual and UX source of truth** for implementing the final Streamlit frontend — they describe how
the product should *look and feel*, not how the science should work.

> This README was **merged** during the design import: the governance / scientific-safety rules
> below come from the repository's placeholder README; the *Design export contents* and *Rules baked
> into the design* sections below come from the Claude Design export's own README. Nothing in the
> exported `.dc.html` / `support.js` / `_ds/` files was altered during import.

## Ground rules

- Exported **HTML, CSS, React, or other frontend code is reference material only** unless it has been
  explicitly reviewed. It does **not** get wired into the app just because it lives here.
- The existing **scientific backend must not be replaced by design exports**. Design exports change
  presentation; they never touch PHREEQC, XRD, ICP, ML, validation, or the Virtual LAB machine logic.
- The implementation rule is:

  > **Replace presentation. Preserve scientific engines.**

Anything in this folder is design intent. Turning it into shipped UI is a separate, reviewed step
that edits `app.py` / `ui/` deliberately — never an automatic copy-paste of an export.

---

## Design export contents (Claude Design / Fable)

**Virtual LAB — UI mockups v1.** AI-assisted materials research platform · private beta. Dark
research-cockpit theme on the Industry design system (Barlow Condensed / Barlow, steel-blue accent,
blueprint registration marks).

**Self-contained:** open any part directly in a browser. The following must stay **alongside** the
part files for the mockups to render — do not separate them: `_ds/` (design-system tokens +
component styles), `support.js` (component runtime), and `VL Sidebar.dc.html` (shared navigation).

### Exported files (do not edit — reference only)

1. `Virtual LAB - 1 Core Screens.dc.html` — Home cockpit (1a), Projects (1b), Material Workspace (1c) + assumptions
2. `Virtual LAB - 2 Machines.dc.html` — Machines gallery (2a), workspace anatomy Z1–Z11 (2b), PHREEQC configure → preview → confirm → results (2c–2e)
3. `Virtual LAB - 3 Instruments.dc.html` — XRD 4 modes (3a), ICP processor (3b), FTIR / SEM-EDS / TGA / Mechanical / ML / Literature / Sustainability / Experimental Design bodies (3c–3j)
4. `Virtual LAB - 4 Analysis and System.dc.html` — Results (4a), Validation & Uncertainty (4b), Evidence Library (4c), Run History (4d), Settings & Diagnostics (4e), 768 px narrow variants (4f)
5. `Virtual LAB - 5 Demo and Handoff.dc.html` — professor demo flow (5a), component inventory (5b), 17-section Streamlit handoff spec (5c)

Plus `VL Sidebar.dc.html` (shared navigation), `support.js` (component runtime), and
`_ds/industry-.../` (design-system tokens + component styles).

> **Implementation spec for the Streamlit agent:** section **5c** of Part 5
> (`Virtual LAB - 5 Demo and Handoff.dc.html`).

### Rules baked into the design

- Every value carries an epistemic badge; all demo numbers are marked **SYNTHETIC DEMO** — bind real
  backend outputs 1:1, never fabricate.
- Missing data renders as **visibly missing**; nothing auto-fills.
- No machine executes without explicit confirmation (configure → preview → assumptions → confirm → run).
- **VALIDATED** comes only from measured data meeting criteria — never simulation, ML, or literature alone.

---

## Subfolders

These placeholder subfolders exist for material that is **not** part of the self-contained export
above (the export lives directly in this directory so its relative `_ds/` / `support.js` links keep
working):

### `screenshots/`
- Full-screen screenshots of the final design.

### `mockups/`
- Individual page / component mockups from Claude Design.

### `exported_code/`
- Any *additional* HTML / CSS / React or other code exported by the design tool.
- This code **should not automatically replace the Streamlit application**. It is a reference for a
  reviewed, manual re-implementation only.

### `assets/`
- Safe visual assets used by the design (icons, small images, fonts, etc.).
- **Excludes** large generated build artifacts (e.g. `node_modules/`, bundler output, large media).

## Related

- Design notes and tokens: [`design_notes.md`](design_notes.md)
- Product / backend contract: `../../../CLAUDE.md`
- Virtual LAB machine backend: `docs/virtual_lab_machines.md`, `docs/virtual_lab_machine_runner.md`
