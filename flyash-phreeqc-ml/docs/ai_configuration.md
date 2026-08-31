# AI configuration (optional and advisory)

All deterministic tools work with AI disabled. AI output is suggestion/interpretation only: it cannot author or change confirmed PHREEQC input, execute a run, alter mapping/QC/residuals, create a measurement, train a model silently, or grant validation.

## Canonical settings

Release bundles use these variables:

| Variable | Meaning |
| --- | --- |
| `VLAB_AI_PROVIDER` | `disabled`, `anthropic`, `ollama`, `openai_compatible`, or `hosted` |
| `VLAB_AI_MODEL` | reviewed provider model identifier |
| `VLAB_AI_BASE_URL` | endpoint for local/private compatible providers |
| `VLAB_AI_LOCATION` | `disabled`, `local`, `server`, or `cloud` |
| `VLAB_AI_ALLOWED_HOSTS` | comma-separated endpoint host allowlist |
| `VLAB_AI_ADMIN_MANAGED` | fixes provider settings for an operator-managed deployment |
| `VLAB_AI_API_KEY` | runtime secret for compatible/hosted providers |
| `VLAB_AI_TIMEOUT_S` | bounded request timeout |
| `VLAB_AI_CONTEXT_LIMIT` | bounded context size |

Anthropic retains `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` compatibility. Compose maps the canonical runtime secret for that provider; never store either value in YAML, a Docker build argument, `.env` committed to Git, logs, screenshots, support bundles, or release manifests.

## Local modes

AI defaults to `disabled`. Local Ollama through Docker Desktop:

```bash
export VLAB_AI_PROVIDER=ollama
export VLAB_AI_MODEL=REVIEWED_LOCAL_MODEL_ID
export VLAB_AI_BASE_URL=http://host.docker.internal:11434
export VLAB_AI_LOCATION=local
export VLAB_AI_ALLOWED_HOSTS=host.docker.internal
./scripts/launch-local.sh
```

The endpoint is reached from the container; it is not bundled into the app image. For a compatible private server, set the exact HTTPS/base URL and allowed host. Endpoint policy rejects loopback/private/cloud combinations that conflict with the declared location.

For Anthropic, set `VLAB_AI_PROVIDER=anthropic`, the reviewed model, and inject `VLAB_AI_API_KEY` only for the process. Live calls still require the UI consent switch.

## Hosted mode

`VLAB_DEPLOYMENT_MODE=hosted` makes provider/model/base URL administrator-controlled. Ordinary users cannot pull a model or redirect the provider. Optional Compose Ollama has no host port and is accessible only on the internal backend network. Cloud/private gateway credentials come from the deployment secret manager. Configure spending limits, retention/privacy review, outbound host policy, timeouts, and incident rotation before enabling a cloud provider.

OIDC authentication secrets are separate from AI credentials. Mount the reviewed Streamlit `[auth]` TOML with `VLAB_STREAMLIT_SECRETS_FILE`; never put AI keys into that example file in Git.

Diagnostics show provider, location, endpoint host, model identifier, capabilities, and key presence/source only. They never reveal the key, OIDC token, prompt, uploaded file, or raw model response. AI conversations are request/user scoped in hosted mode and must not cross tenants.
