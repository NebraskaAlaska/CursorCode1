# Deployment guide — hosting the Materials Research Assistant

This guide explains how to run the app **locally** vs. **hosted online** so colleagues can use it
from a browser with **no local install and no PHREEQC**, with **PHREEQC running server-side** and
the **Anthropic API key kept as a server-side secret**.

> TL;DR — **Docker is the reliable path.** The provided `Dockerfile` builds PHREEQC from source so
> leaching simulations run on the server. Deploy that image to **Render / Fly.io / Google Cloud
> Run** (or any container host), set `ANTHROPIC_API_KEY` as a platform **secret**, and share the
> HTTPS URL. Streamlit Community Cloud is fine for an **AI-only** demo but cannot reliably run
> PHREEQC.

---

## 1. Local vs. hosted

| | Local (a researcher's laptop) | Hosted (this guide) |
| --- | --- | --- |
| Who installs what | Python + `pip install -r requirements.txt`; PHREEQC optional | nobody — just a browser |
| PHREEQC | each user supplies a CLI + database | **built into the server image**, runs server-side |
| API key | each user's own env var | **one server-side secret** (you pay) |
| Data | on the laptop | on the server (ephemeral unless you add a volume) |
| Access | `streamlit run app.py` → localhost | an HTTPS URL you share |

Local quick start (unchanged):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # optional (AI is opt-in, off by default)
export PHREEQC_EXE=phreeqc                  # optional (PHREEQC is opt-in)
export PHREEQC_DATABASE=/path/to/phreeqc.dat
streamlit run app.py
```

The app **degrades gracefully**: with no key it runs the deterministic planner; with no PHREEQC it
plans + builds reviewable input but doesn't execute. Both states are shown in **Settings**.

---

## 2. The environment-variable contract

The app is configured entirely by environment variables — nothing secret lives in code or the
image. (Settings → *AI assistant* / *Geochemical engine* shows each as detected/missing.)

| Variable | Purpose | In the Docker image |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | enables live AI (assistant, evidence extraction). **Secret.** | **NOT set** — inject at runtime |
| `PHREEQC_EXE` | path to the PHREEQC CLI | preset to `/usr/local/bin/phreeqc` |
| `PHREEQC_DATABASE` | path to a thermodynamic database (`.dat`) | preset to the bundled `phreeqc.dat` |
| `PHREEQC_TIMEOUT_S` | per-run PHREEQC timeout (seconds) | preset to `120` |
| `ANTHROPIC_MODEL` | optional model override (e.g. a cheaper model) | unset |
| `PORT` | serving port (most PaaS inject this) | defaults to `8501` |

The API key is read only from `ANTHROPIC_API_KEY` (env) or `st.secrets` — it is **never rendered,
logged, or sent to the browser** (Streamlit renders server-side; the key never leaves the server).

---

## 3. Docker (the reliable path)

The `Dockerfile` builds the USGS PHREEQC batch CLI from source, installs the Python app, and runs
Streamlit. It **self-tests PHREEQC during the build** (the build fails loudly if PHREEQC can't run)
and sets **no secret**.

### Build

```bash
# from the flyash-phreeqc-ml/ directory
docker build -t mra .
# Optional: pin/override the PHREEQC source version (find the URL on the USGS page):
#   docker build --build-arg PHREEQC_VERSION=3.8.6-17100 -t mra .
```

The default builds **PHREEQC 3.8.6** from the official USGS source:
`https://water.usgs.gov/water-resources/software/PHREEQC/phreeqc-3.8.6-17100.tar.gz`.

> **PHREEQC version note.** The download step fails the build **clearly** on an HTTP error or a
> non-tarball response (it uses `curl -fSL` and validates the archive). If USGS bumps the version
> and the URL 404s, find the current Linux source tarball at
> <https://www.usgs.gov/software/phreeqc-version-3> and pass it via
> `--build-arg PHREEQC_VERSION=<ver>` (or a full `--build-arg PHREEQC_URL=<url>`).

### Run locally

```bash
# key from your shell (never hard-coded); the app reads it server-side
docker run --rm -p 8501:8501 -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" mra
# or with docker compose + a gitignored .env file:
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env       # .env is gitignored + dockerignored
docker compose up --build
open http://localhost:8501
```

### CEMDATA18 (cementitious / fly-ash accuracy)

CEMDATA18 is **not redistributable**, so the image ships the open `phreeqc.dat` (fine for a working
demo, weak for cementitious alkaline chemistry). To use CEMDATA18, **mount it** and override the
database path — never bake it into the image:

```bash
docker run --rm -p 8501:8501 \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -v /host/path/databases:/databases:ro \
  -e PHREEQC_DATABASE=/databases/CEMDATA18.dat \
  mra
```

### Persistence (optional)

The container filesystem is ephemeral — user-created runs vanish on restart. To keep them, mount a
volume at `/app/experiments` (see `docker-compose.yml`). For a stateless demo this is unnecessary.

---

## 4. Where to host — the options

### (a) Streamlit Community Cloud — *good for an AI-only demo, not for PHREEQC*

- **Suitable?** Partially. It deploys a (public or private) GitHub repo, installs `requirements.txt`,
  reads secrets from its **Secrets** manager (set `ANTHROPIC_API_KEY` there → the app picks it up via
  `st.secrets`). The AI assistant, Evidence Library, ML-surrogate, and validation workflows all work.
- **PHREEQC caveat.** There is **no reliable way to run a server-side PHREEQC binary** there: no apt
  `phreeqc` package and no Docker control. The app will run in **planning-only** mode (it plans and
  builds input but the "run" step reports "PHREEQC not configured"). *Possible workaround:* add an
  `environment.yml` installing `phreeqc` from **conda-forge** and set `PHREEQC_EXE` /
  `PHREEQC_DATABASE` to the conda paths — worth trying, but unverified and less reliable than Docker.
- **Verdict:** use it for a fast, free, AI-only demo; use Docker (below) when you need PHREEQC.

### (b) Container hosts with Docker — *recommended for full PHREEQC*

All of these run the `Dockerfile` as-is, inject `ANTHROPIC_API_KEY` as a secret, and give you an
HTTPS URL:

| Platform | Why / notes | Secrets | Cost shape |
| --- | --- | --- | --- |
| **Render** | easiest: "New → Web Service → Docker", auto-builds from the repo | dashboard env vars (secret) | free (sleeps) or ~$7/mo always-on |
| **Fly.io** | `fly launch` + `fly deploy`; small always-on VM, global | `fly secrets set ANTHROPIC_API_KEY=...` | small free-ish allowance |
| **Google Cloud Run** | clean pay-per-use, scales to zero; great fit | **Secret Manager** → mounted env var | per-request; ~free at low traffic |
| **Railway** | repo → Docker, usage-based | dashboard variables | usage-based |
| **AWS App Runner / ECS Fargate** | more setup, scalable | Secrets Manager / SSM | per-resource |
| **Azure Container Apps** | similar to Cloud Run | Key Vault / secrets | per-use |

**Recommendation:** **Render** or **Fly.io** for the least setup; **Google Cloud Run** for a clean,
scale-to-zero, pay-per-use managed option with first-class Secret Manager.

> Set the platform's port to match: most inject `$PORT`, which the entrypoint already honours. On
> Cloud Run the container must listen on `$PORT` (it does). Health check path: `/_stcore/health`.

#### Render (concrete example)

1. Push this repo (private is fine).
2. Render → **New → Web Service → Build from a Dockerfile** → root = `flyash-phreeqc-ml/`.
3. **Environment** → add `ANTHROPIC_API_KEY` (mark **secret**). Optionally `PHREEQC_DATABASE` if you
   attach a CEMDATA18 disk.
4. Deploy. Share the `https://<service>.onrender.com` URL.

#### Fly.io (concrete example)

```bash
fly launch --no-deploy          # generates fly.toml; set internal_port = 8501
fly secrets set ANTHROPIC_API_KEY=sk-ant-...
fly deploy
fly open
```

---

## 5. The five questions, answered

**1. Is Streamlit Community Cloud suitable?**
For an **AI-only** demo, yes (free, repo-based, has a secrets manager). For **server-side PHREEQC**,
no — it can't reliably run the binary, so the app falls back to planning-only. Use Docker for
PHREEQC.

**2. Is Render / Railway / Fly.io / AWS / GCP with Docker better?**
Yes, when you need PHREEQC. They run the provided image (PHREEQC built in), inject the key as a
secret, and give you an HTTPS URL. Easiest: **Render / Fly.io**. Cleanest pay-per-use: **Cloud Run**.

**3. How will colleagues access the app?**
Via a single **HTTPS URL** you share (the platform's domain or a custom domain). No install, no
PHREEQC, no key. **Add access control** — the app has no login of its own, so an open URL is public
and spends your API budget. Put it behind the platform's auth, Cloudflare Access / Tailscale, an
identity-aware proxy (Cloud Run + IAP), or a reverse-proxy basic-auth / SSO.

**4. Who pays for API usage?**
**You** (whoever owns the server-side `ANTHROPIC_API_KEY`). Every colleague who turns on live AI
spends *your* Anthropic credits. Mitigate: AI is **opt-in (toggle defaults off)**; gate access;
set Anthropic **billing limits / alerts**; pick a cheaper `ANTHROPIC_MODEL`; or have heavy users
supply their own key locally. PHREEQC + the ML surrogate run on **your CPU** (hosting cost only);
Evidence-Library scholarly search uses **keyless** public APIs (free); evidence *extraction* uses
your Anthropic key (opt-in).

**5. How do I avoid exposing secrets?**
- Store `ANTHROPIC_API_KEY` only in the **platform's secret manager** (or local env / a gitignored
  `.env`). It is **never** in code, the repo, or the image — the `Dockerfile` sets no key, and
  `.dockerignore` + `.gitignore` exclude `.env` / `.streamlit/secrets.toml`.
- The key never reaches the **browser** (Streamlit renders server-side); the UI only ever shows
  "detected / not detected" + the source, never the value.
- Rotate immediately if a key is ever exposed; prefer a **dedicated deployment key with a spend
  cap**.

---

## 6. Smoke tests

After building / deploying, verify:

```bash
# 1) PHREEQC built + runs inside the image (self-tested at build; re-check at runtime):
docker run --rm mra sh -c \
  'printf "SOLUTION 1\n pH 7\n temp 25\nEND\n" > /tmp/t.pqi && \
   "$PHREEQC_EXE" /tmp/t.pqi /tmp/t.pqo "$PHREEQC_DATABASE" && test -s /tmp/t.pqo && echo PHREEQC_OK'

# 2) The app boots + is healthy:
docker run --rm -d -p 8501:8501 -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" --name mra_smoke mra
curl -fsS http://localhost:8501/_stcore/health && echo " HEALTH_OK"
docker logs mra_smoke | grep '\[startup\]'      # debug-safe booleans (no key printed)
docker rm -f mra_smoke

# 3) (optional) run the test suite inside the image:
docker run --rm mra python -m pytest -q tests/test_ai_boundary.py tests/test_app_tabs_smoke.py
```

In the browser:
- **Settings** → *Geochemical engine — PHREEQC* shows **Ready** (executable + database Found);
  *AI assistant* shows **API key Detected** + **AI SDK Available** (turn on **Enable live AI
  assistant** to use AI).
- **Assistant** → a leaching prompt ("leach Class C fly ash in 0.5 M NaOH, predict pH and Ca")
  builds a preview and, on your confirmation, **runs PHREEQC server-side**.
- Plastic-strength prompts stay planning-only (no PHREEQC); demo ML models are labelled synthetic.

---

## 7. Security & operations checklist

- [ ] `ANTHROPIC_API_KEY` set **only** as a platform secret (not in repo/image/browser).
- [ ] Access control in front of the app (auth / SSO / IAP) — it has no built-in login.
- [ ] Anthropic billing limit + alert configured; a dedicated capped deployment key.
- [ ] Decide on data persistence (volume at `/app/experiments`) or accept ephemeral runs.
- [ ] Confidential raw research data is **not** in the image (`.dockerignore` excludes `data/raw`).
- [ ] CEMDATA18 mounted (not committed) if cementitious accuracy is needed.
- [ ] HTTPS only (the platforms above terminate TLS for you).
- [ ] Resource limits set (PHREEQC is CPU-bound; `PHREEQC_TIMEOUT_S` caps a single run).

Nothing in this deployment changes the scientific logic: PHREEQC remains the deterministic engine,
AI stays opt-in + suggestion-only, and every result is labelled simulation/prediction, not
validated — exactly as documented in the README and `CLAUDE.md`.
