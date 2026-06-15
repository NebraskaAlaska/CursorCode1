# AI configuration (optional, experimental)

The app has a small set of **optional** AI helpers (messy-file import suggestions, a
grounded Q&A assistant, and sourced literature retrieval). They are **off by default** and
the app runs fully without them. This page explains how to turn them on and what they can
and cannot do.

> **AI is suggestion / interpretation only.** It never affects mapping status, residuals,
> validation status, or the comparison data, and **AI cannot validate the science by
> itself** — every AI output must be reviewed and verified before any scientific use.

## What you need

1. The optional `anthropic` SDK: `pip install anthropic` (already in `requirements.txt`).
2. An Anthropic API key, supplied via **one** of:
   - the `ANTHROPIC_API_KEY` environment variable (local use), or
   - a Streamlit secret named `ANTHROPIC_API_KEY` (deployment).

If neither is present, the AI features stay disabled with a clear note — nothing breaks.

## Enable AI locally

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # your key; never commit this
export ANTHROPIC_MODEL=claude-opus-4-8     # optional — overrides the default model
streamlit run app.py
```

Open the **🤖 AI settings** panel in the sidebar to confirm: it shows *enabled / disabled*,
the active provider and model, whether a key was detected (yes/no — never the key itself),
and whether the SDK is available.

## Enable AI in a Streamlit deployment

On Streamlit Community Cloud (or any Streamlit deploy), add the key as a **secret** instead
of an environment variable. In the app's **Settings → Secrets**, add:

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
ANTHROPIC_MODEL = "claude-opus-4-8"   # optional
```

(Equivalently, create `.streamlit/secrets.toml` with the same contents for a local secrets
test — **do not commit it**; it is gitignored by convention.)

The app reads `st.secrets["ANTHROPIC_API_KEY"]` automatically when running under Streamlit.

## Choosing the model (and provider)

- **Model** — set `ANTHROPIC_MODEL` (env or secret), or pick/enter a model in the sidebar
  **AI settings** panel. The sidebar choice applies for the current session.
- **Provider** — Anthropic only today. The selector exists for future providers but has a
  single option.

### Resolution precedence

| Setting | Order (first one that exists wins) |
| --- | --- |
| **API key** | `ANTHROPIC_API_KEY` env  →  `st.secrets["ANTHROPIC_API_KEY"]` |
| **Model** | explicit call arg  →  sidebar choice  →  `ANTHROPIC_MODEL` env  →  `st.secrets["ANTHROPIC_MODEL"]`  →  `claude-opus-4-8` (default) |
| **Provider** | explicit arg  →  sidebar choice  →  `ANTHROPIC_PROVIDER` env  →  `anthropic` (default) |

**The environment wins over Streamlit secrets** so a deliberate machine-level override
always takes effect, and the app's prior (env-only) behaviour is unchanged. Streamlit
secrets are the deployment fallback.

## Safety properties

- **The key is never shown, logged, or entered in the UI.** The AI settings panel reports
  only *presence* (yes/no) and *source* (environment variable vs Streamlit secret).
- **No hard-coded keys.** Keys come only from the environment or Streamlit secrets.
- **Graceful when unavailable.** No key, no SDK, or a client-construction failure all
  produce a clean disabled state / structured error — never a crash.
- **Off the science path.** The configuration and client layers (`ai/config.py`,
  `ai/client.py`) cannot reach mapping, residual, validation, inclusion, or comparison
  code. This is pinned by `tests/test_ai_boundary.py`.

## Cost note

The default model is the most capable (and most expensive) tier. For routine import
suggestions you can set a cheaper model, e.g.:

```bash
export ANTHROPIC_MODEL=claude-haiku-4-5-20251001
```

## Implementation

- `flyash_phreeqc_ml/ai/config.py` — the single configuration authority (key detection,
  model/provider resolution, the key-safe `AIConfig` snapshot).
- `flyash_phreeqc_ml/ai/client.py` — the safe client wrapper (structured errors, never
  exposes the key).
- `flyash_phreeqc_ml/ai/{import_assist,assistant,literature}.py` — the AI helpers; they
  resolve their key/model/client through the shared layer above.
