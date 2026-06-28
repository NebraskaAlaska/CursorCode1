# Godot client — placeholder project notes

> A planning document, not code. It sketches the proposed Godot 4.x project so the first prototype can
> start from a clear shape. Nothing here is built yet; the backend is the dependency, the client is TBD.

## Proposed project structure

```
godot_client/                 (a Godot 4.x project will live here later)
├── project.godot
├── autoload/
│   └── ApiClient.gd          # the ONLY place that talks to the backend (HTTP/JSON)
├── scenes/
│   ├── World.tscn            # the side-scrolling lab; player + stations placed here
│   ├── Player.tscn           # movement, "carrying a card" state
│   ├── stations/
│   │   ├── Synthesizer.tscn
│   │   ├── XrdStation.tscn
│   │   ├── PhreeqcStation.tscn
│   │   └── IcpStation.tscn
│   └── ui/
│       ├── MaterialCard.tscn # card sprite + data_status border + icon
│       ├── ReadoutScreen.tscn
│       ├── ConfirmLever.tscn # the physical PHREEQC confirm-to-run object
│       └── LabNotebook.tscn  # provenance log
├── scripts/                  # per-scene scripts (UI only; no science logic)
└── assets/                   # pixel-art sprites, tilesets, fonts, sfx
```

## `ApiClient` autoload (the seam)

One autoload owns every backend call so no scene ever embeds science logic or talks HTTP directly.
Proposed surface (GDScript sketch — illustrative, not final):

```gdscript
extends Node
# ApiClient.gd  — wraps the six endpoints; returns parsed Dictionaries.

const BASE_URL := "http://localhost:8000"

func synthesize(name: String, formula: String, composition) -> Dictionary: ...
func xrd_expected(card_or_phases) -> Dictionary: ...
func phreeqc_preview(setup: Dictionary) -> Dictionary: ...
func phreeqc_run(preview_id: String, confirm: bool) -> Dictionary: ...
func icp_process(rows: Array, apply_blank: bool) -> Dictionary: ...
func health() -> Dictionary: ...
```

Implementation notes:
- Use `HTTPRequest` nodes (one per in-flight call, or a small pool) and `await` the `request_completed`
  signal. Parse with `JSON.parse_string`.
- Add `WebSocketPeer` later only when a station needs streaming/live feedback (e.g. a long delegated
  PHREEQC run in integration). REST is enough for the scaffold.
- Treat every response as data to **display honestly** — see the rendering rules in `README.md`.

## Material Card rendering

- Card border color is driven by `data_status` (see `../game_design_notes.md` §4 for the palette).
- Show an icon for the card's nature: phases known / composition only / formula only / unknown.
- Surface `warnings[0]` as a small banner; full `warnings` + `uncertainty_notes` on inspect.
- Render `allowed_lab_stations`: eligible stations glow; ineligible stations show a "Why not?" tooltip
  built from each entry's `reason`.

## Station behaviors (client side)

- **Synthesizer**: a text field → `synthesize()` → spawn a `MaterialCard`.
- **XRD**: docking a card → `xrd_expected(card)` → draw expected picks on a faux diffractogram with the
  **EXPECTED** stamp; if `result_type == "reference_data_needed"`, show the refusal, not a fake plot.
- **PHREEQC**: a setup panel (leachant / source term / database / T) → `phreeqc_preview()` shows the
  `preview_text` on a clipboard. The **ConfirmLever** calls `phreeqc_run(confirm=true)` only on a real
  pull; render `executed:false` plainly. Never auto-pull.
- **ICP**: a table input → `icp_process(rows)`; render corrected rows + residuals; show refusals as
  in-world messages.

## First vertical slice (suggested milestone)

1. `ApiClient.synthesize` + `MaterialCard` rendering with the data_status palette.
2. Walk a card into the XRD station and render expected peaks vs. a refusal.
3. Add the PHREEQC preview clipboard + ConfirmLever (no execution).
4. Only then: ICP table input, the lab notebook, and the locked future stations.

## Engine-agnostic reminder

Keep all of the above behind `ApiClient`. Because the contract is HTTP+JSON (`../api_contract.md`), the
same prototype could be rebuilt in Unity or a browser with no backend changes. Do not let science
logic leak into the client.
