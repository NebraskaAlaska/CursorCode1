# Godot client — 2D Lab Sandbox (placeholder)

> **No Godot project exists yet.** This folder holds notes for the first 2D prototype. The client is
> the missing piece; the backend (`../backend/`) already returns everything the UI needs.

## Why a separate client over HTTP

The game is a **client** of the sandbox backend. It communicates **only over HTTP/WebSocket** and
**must not import the Python science modules directly.** That separation is deliberate:

- keeps the science (and its honesty guarantees) in one audited place;
- keeps the engine swappable — Godot first, but the API is engine-agnostic, so Unity or a web client
  could use the exact same endpoints;
- keeps the experiment isolated from the main platform.

## Engine choice

- **Godot 4.x** is preferred for the first 2D pixel-art prototype (GDScript, lightweight, great 2D
  tooling, free/open-source). Use `HTTPRequest` for REST now; add `WebSocketPeer` later for streaming.
- The contract in `../api_contract.md` and `../material_card_schema.json` is the single source of
  truth. Any engine that can speak HTTP + JSON can be the client.

## How the client maps to the API

| In-game action | Endpoint | Render |
|---|---|---|
| Type into the Synthesizer | `POST /materials/synthesize` | a Material Card sprite, border colored by `data_status` |
| Dock a card at XRD | `POST /xrd/expected` | expected peak picks (or a "reference needed" refusal) stamped **EXPECTED** |
| Configure + preview PHREEQC | `POST /phreeqc/preview` | a clipboard showing `preview_text`; a **Confirm-to-Run** lever |
| Pull the lever | `POST /phreeqc/run` (`confirm:true`) | "gate satisfied — not executed here" (delegates in integration) |
| Feed a concentration table to ICP | `POST /icp/process` | a reduced table + residuals; refusal if asked to fabricate |
| Boot / status | `GET /health` | which stations + whether the science core is wired |

## Honesty rendering rules (client-side contract)

The client must **surface, not hide**, the backend's honesty fields:

- Always show the `data_status` chip/border. Never recolor a `simulated`/`assumed`/`unknown` result
  to look `measured`.
- For XRD, always print the **EXPECTED** stamp and the disclaimer; never label a pick "identified".
- For PHREEQC, never auto-pull the Confirm lever; require a real player action. Show `executed:false`.
- For ICP, show the `fabricated:false` reductions; render refusals as in-world messages, not errors to
  paper over.

## Getting started (when you build it)

1. Install Godot 4.x. Create a project here (`godot_client/project.godot`, scenes, scripts).
2. Add a small `ApiClient` autoload that wraps the six endpoints and returns parsed dictionaries.
3. Build one vertical slice first: Synthesizer → Material Card → XRD readout, with the honesty chips.
4. Keep all science calls behind `ApiClient`. No business logic in the UI; the backend decides what's
   honest.

See `placeholder_project_notes.md` for a fuller proposed project structure and scene breakdown.
