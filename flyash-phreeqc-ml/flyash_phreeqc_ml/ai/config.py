"""Single, import-safe source of truth for AI (LLM) configuration.

This module centralises *where the API key and model come from* and *whether the
optional AI layer is enabled*, so the UI can show an honest status panel and the AI
helper modules (:mod:`import_assist`, :mod:`assistant`, :mod:`literature`) never
duplicate key/model logic. It is deliberately:

* **Pure + import-safe** — it never hard-imports ``streamlit`` or ``anthropic`` at
  module load, never raises on a missing key / SDK / secret, and is safe to import in a
  plain script or a test with no Streamlit runtime.
* **Key-safe** — it never returns, prints, logs, or renders the API key itself. The only
  key-derived outputs are *presence* (yes/no) and *source* (env var vs Streamlit secret).
* **Off the science path** — AI is suggestion/interpretation only; nothing here can reach
  mapping status, residuals, validation status, or the comparison data.

Resolution precedence (documented + deliberate)
-----------------------------------------------
API key:   ``ANTHROPIC_API_KEY`` (environment)  >  ``st.secrets["ANTHROPIC_API_KEY"]``
Model:     explicit call arg  >  session/runtime override (set from the UI)
                              >  ``ANTHROPIC_MODEL`` (env)  >  ``st.secrets["ANTHROPIC_MODEL"]``
                              >  :data:`DEFAULT_MODEL`
Provider:  explicit arg  >  runtime override  >  ``ANTHROPIC_PROVIDER`` (env)  >  :data:`DEFAULT_PROVIDER`

**The environment wins over Streamlit secrets** so a deliberate machine-level override
always takes effect and the app's prior (env-only) behaviour is unchanged; Streamlit
secrets are the *deployment* fallback (e.g. Streamlit Community Cloud), which the app
previously could not use.
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass

from .provider_contract import (
    EndpointPolicy,
    ProviderCapabilities,
    ProviderConfigurationError,
    ProviderLocation,
    ProviderSettings,
    ProviderType,
)

# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
PROVIDER_DISABLED = ProviderType.DISABLED.value
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OLLAMA = ProviderType.OLLAMA.value
PROVIDER_OPENAI_COMPATIBLE = ProviderType.OPENAI_COMPATIBLE.value
PROVIDER_HOSTED = ProviderType.HOSTED.value
SUPPORTED_PROVIDERS = (
    PROVIDER_DISABLED,
    PROVIDER_ANTHROPIC,
    PROVIDER_OLLAMA,
    PROVIDER_OPENAI_COMPATIBLE,
    PROVIDER_HOSTED,
)
DEFAULT_PROVIDER = PROVIDER_ANTHROPIC

# --------------------------------------------------------------------------- #
# Env var names (kept identical to the historical names so nothing breaks)
# --------------------------------------------------------------------------- #
API_KEY_ENV = "ANTHROPIC_API_KEY"
MODEL_ENV = "ANTHROPIC_MODEL"
PROVIDER_ENV = "ANTHROPIC_PROVIDER"
AI_PROVIDER_ENV = "VLAB_AI_PROVIDER"
AI_MODEL_ENV = "VLAB_AI_MODEL"
AI_BASE_URL_ENV = "VLAB_AI_BASE_URL"
AI_LOCATION_ENV = "VLAB_AI_LOCATION"
AI_TIMEOUT_ENV = "VLAB_AI_TIMEOUT_S"
AI_CONTEXT_LIMIT_ENV = "VLAB_AI_CONTEXT_LIMIT"
AI_ALLOWED_HOSTS_ENV = "VLAB_AI_ALLOWED_HOSTS"
AI_ADMIN_MANAGED_ENV = "VLAB_AI_ADMIN_MANAGED"
AI_API_KEY_ENV = "VLAB_AI_API_KEY"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
DEPLOYMENT_MODE_ENV = "VLAB_DEPLOYMENT_MODE"

# Per-Streamlit-session plain keys.  Unlike widget keys these survive navigation,
# and unlike the historical module dict they cannot leak one user's choice to another.
SESSION_PROVIDER_KEY = "ai_provider_selected"
SESSION_MODEL_KEY = "ai_model_selected"
SESSION_BASE_URL_KEY = "ai_base_url_selected"
SESSION_LOCATION_KEY = "ai_location_selected"
MASTER_SWITCH_KEY = "ai_live_enabled"

# Default to the most capable model; override per-deployment (env / secrets / the UI)
# without code changes.
DEFAULT_MODEL = "claude-opus-4-8"

# Request hardening for live calls. A bounded timeout turns a network hang into a clean,
# classifiable ``timeout`` error instead of freezing a Streamlit turn; the SDK's built-in
# retries (with backoff) smooth transient 429/5xx. Both are non-secret and overridable by env.
REQUEST_TIMEOUT_S: float = float(os.environ.get("ANTHROPIC_TIMEOUT_S", "45") or "45")
MAX_RETRIES: int = int(os.environ.get("ANTHROPIC_MAX_RETRIES", "2") or "2")

# A small curated list the settings UI offers; free-text entry is also allowed.
SUGGESTED_MODELS = (
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-fable-5",
)

# Provider-specific local suggestions must never inherit an Anthropic model merely
# because the user switched providers in the same Streamlit session.  ``llama3.2``
# is an official Ollama library tag; discovery still reports what is actually
# installed, and a pull remains a separate explicitly confirmed operation.
OLLAMA_SUGGESTED_MODELS = (
    "llama3.2",
)

# Key-source labels (human-readable; never the key itself).
SOURCE_ENV = "environment variable"
SOURCE_SECRETS = "streamlit secrets"
SOURCE_NONE = "none"

# The one-line role + warning the UI must show wherever AI output appears.
AI_ROLE_LINE = (
    "suggestion / interpretation only — never affects mapping, residuals, validation "
    "status, or the comparison data"
)
AI_EXPERIMENTAL_WARNING = (
    "AI assistance is experimental. AI outputs must be reviewed and verified before any "
    "scientific use — AI cannot validate the science by itself."
)

# Compatibility overrides are context-local, not a mutable process-global dictionary.
# The Streamlit UI uses the per-session keys above; this ContextVar remains for scripts
# and older call sites/tests that use set_runtime_overrides(). It never holds a key.
_RUNTIME_OVERRIDES: ContextVar[dict[str, str]] = ContextVar(
    "virtual_lab_ai_runtime_overrides", default={})


# --------------------------------------------------------------------------- #
# Low-level, fully-guarded readers (never raise, never leak)
# --------------------------------------------------------------------------- #
def _clean(value) -> str | None:
    """Strip a value to a non-empty string, or ``None``."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _env(name: str) -> str | None:
    return _clean(os.environ.get(name))


def _under_streamlit_runtime() -> bool:
    """True only when a Streamlit server is actually running.

    Outside a running Streamlit app ``st.secrets`` is meaningless (and noisy), so we skip
    it entirely — that keeps scripts and tests on env-only behaviour with no warnings.
    """
    try:
        from streamlit.runtime import exists
        return bool(exists())
    except Exception:
        return False


def _secrets_get(name: str) -> str | None:
    """Read ``name`` from ``st.secrets`` — only under a real Streamlit runtime.

    Returns ``None`` (never raises) when Streamlit is absent, not running, has no secrets
    configured, or lacks the key. Monkeypatchable in tests to simulate a secrets-backed
    deployment without a live Streamlit server.
    """
    if not _under_streamlit_runtime():
        return None
    try:
        import streamlit as st
        return _clean(st.secrets.get(name))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Session/context overrides (provider/model only — never the key)
# --------------------------------------------------------------------------- #
def set_runtime_overrides(*, provider: str | None = None, model: str | None = None) -> None:
    """Record provider/model for the current execution context. A blank clears a field.

    This backwards-compatible API now uses :class:`contextvars.ContextVar`, not a
    mutable process-global mapping. Streamlit uses its own session state instead.
    """
    overrides = dict(_RUNTIME_OVERRIDES.get())
    if provider is not None:
        p = _clean(provider)
        if p:
            overrides["provider"] = p
        else:
            overrides.pop("provider", None)
    if model is not None:
        m = _clean(model)
        if m:
            overrides["model"] = m
        else:
            overrides.pop("model", None)
    _RUNTIME_OVERRIDES.set(overrides)


def clear_runtime_overrides() -> None:
    """Forget context-local provider/model overrides."""
    _RUNTIME_OVERRIDES.set({})


def get_runtime_overrides() -> dict:
    """A copy of the current context-local overrides (no key, ever)."""
    return dict(_RUNTIME_OVERRIDES.get())


def _session_get(name: str) -> str | None:
    """Read a non-secret setting from the current Streamlit session only."""
    if not _under_streamlit_runtime():
        return None
    try:
        import streamlit as st
        return _clean(st.session_state.get(name))
    except Exception:
        return None


def _hosted_mode() -> bool:
    return str(os.environ.get(DEPLOYMENT_MODE_ENV, "local")).strip().lower() == "hosted"


def _bool_env(name: str, default: bool = False) -> bool:
    value = _clean(os.environ.get(name))
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if 0.1 <= value <= 300 else default


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if 256 <= value <= 2_000_000 else default


# --------------------------------------------------------------------------- #
# Detection (the key is resolved here only for internal client construction)
# --------------------------------------------------------------------------- #
def detect_api_key(provider: str | None = None) -> tuple[str | None, str]:
    """Return ``(key, source)`` for a provider without ever displaying it.

    Anthropic retains its historical env/secrets precedence. OpenAI-compatible and
    hosted gateways use administrator-owned environment/secrets. Ollama/disabled do
    not use an API key.
    """
    chosen = resolve_provider(provider)
    if chosen in {PROVIDER_DISABLED, PROVIDER_OLLAMA}:
        return None, SOURCE_NONE
    if chosen == PROVIDER_ANTHROPIC:
        names = (API_KEY_ENV,)
    elif chosen == PROVIDER_OPENAI_COMPATIBLE:
        names = (AI_API_KEY_ENV, OPENAI_API_KEY_ENV)
    else:
        names = (AI_API_KEY_ENV,)
    env = next((_env(name) for name in names if _env(name)), None)
    if env:
        return env, SOURCE_ENV
    sec = next((_secrets_get(name) for name in names if _secrets_get(name)), None)
    if sec:
        return sec, SOURCE_SECRETS
    return None, SOURCE_NONE


def api_key_present(provider: str | None = None) -> bool:
    return detect_api_key(provider)[0] is not None


def api_key_source(provider: str | None = None) -> str:
    return detect_api_key(provider)[1]


def key_length(provider: str | None = None) -> int:
    """Length of the detected API key (an integer only — **never** the key itself).

    A safe diagnostic: a 0 means no key; a plausible non-zero length (≈100) confirms a key is
    actually present and non-trivial, without exposing any of its characters.
    """
    key, _source = detect_api_key(provider)
    return len(key) if key else 0


def sdk_available() -> bool:
    """True when the optional ``anthropic`` SDK is importable. Never raises."""
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def resolve_model(model: str | None = None) -> str:
    """Resolve model with hosted settings fixed by the deployment administrator."""
    if _hosted_mode():
        return (_env(AI_MODEL_ENV) or _env(MODEL_ENV) or _secrets_get(AI_MODEL_ENV)
                or _secrets_get(MODEL_ENV) or DEFAULT_MODEL)
    overrides = _RUNTIME_OVERRIDES.get()
    return (_clean(model) or _session_get(SESSION_MODEL_KEY) or overrides.get("model")
            or _env(AI_MODEL_ENV) or _env(MODEL_ENV) or _secrets_get(AI_MODEL_ENV)
            or _secrets_get(MODEL_ENV) or DEFAULT_MODEL)


def resolve_provider(provider: str | None = None) -> str:
    """Resolve provider; hosted mode ignores user/session/context overrides.

    Clamped to :data:`SUPPORTED_PROVIDERS` (unknown values fall back to the default), so
    a usable provider is always returned.
    """
    overrides = _RUNTIME_OVERRIDES.get()
    if _hosted_mode():
        chosen = (_env(AI_PROVIDER_ENV) or _env(PROVIDER_ENV) or DEFAULT_PROVIDER).lower()
    else:
        chosen = (_clean(provider) or _session_get(SESSION_PROVIDER_KEY)
                  or overrides.get("provider") or _env(AI_PROVIDER_ENV)
                  or _env(PROVIDER_ENV) or DEFAULT_PROVIDER).lower()
    return chosen if chosen in SUPPORTED_PROVIDERS else DEFAULT_PROVIDER


def _default_location(provider: str) -> ProviderLocation:
    if provider == PROVIDER_DISABLED:
        return ProviderLocation.DISABLED
    if provider == PROVIDER_ANTHROPIC:
        return ProviderLocation.CLOUD
    if provider == PROVIDER_HOSTED:
        return ProviderLocation.SERVER
    return ProviderLocation.LOCAL


def _default_base_url(provider: str) -> str | None:
    if provider == PROVIDER_OLLAMA:
        return "http://127.0.0.1:11434"
    if provider == PROVIDER_OPENAI_COMPATIBLE:
        return "http://127.0.0.1:8000/v1"
    return None


def resolve_provider_settings(
    *, provider: str | None = None, model: str | None = None,
    base_url: str | None = None, location: str | None = None,
) -> ProviderSettings:
    """Resolve and validate the provider-neutral contract (per-session in local mode)."""
    chosen = resolve_provider(provider)
    hosted = _hosted_mode()
    if hosted:
        chosen_url = _env(AI_BASE_URL_ENV)
        chosen_location = _env(AI_LOCATION_ENV)
    else:
        chosen_url = (_clean(base_url) or _session_get(SESSION_BASE_URL_KEY)
                      or _env(AI_BASE_URL_ENV) or _default_base_url(chosen))
        chosen_location = (_clean(location) or _session_get(SESSION_LOCATION_KEY)
                           or _env(AI_LOCATION_ENV))
    try:
        location_value = ProviderLocation(
            (chosen_location or _default_location(chosen).value).lower())
    except ValueError as exc:
        raise ProviderConfigurationError("AI provider location is invalid") from exc
    administrator_managed = hosted or _bool_env(AI_ADMIN_MANAGED_ENV, False)
    allowed_hosts = frozenset(
        item.strip().lower() for item in str(os.environ.get(AI_ALLOWED_HOSTS_ENV, "")).split(",")
        if item.strip())
    if chosen == PROVIDER_OLLAMA:
        capabilities = ProviderCapabilities(model_discovery=True, model_pull=not hosted)
        notice = ("Requests stay on the configured local Ollama service."
                  if location_value is ProviderLocation.LOCAL
                  else "Requests are sent to the administrator-managed private model service.")
    elif chosen in {PROVIDER_OPENAI_COMPATIBLE, PROVIDER_HOSTED}:
        capabilities = ProviderCapabilities(model_discovery=True)
        notice = ("Requests are sent to the administrator-managed private model service."
                  if location_value is ProviderLocation.SERVER
                  else "Requests are sent to the configured API service.")
    elif chosen == PROVIDER_DISABLED:
        capabilities = ProviderCapabilities(chat=False, structured_output=False)
        notice = "AI is disabled; no prompt or scientific data leaves the application."
    elif chosen == PROVIDER_ANTHROPIC:
        capabilities = ProviderCapabilities(tool_calling=True)
        notice = "Requests are sent to Anthropic when live AI is explicitly enabled."
    else:
        capabilities = ProviderCapabilities()
        notice = "Requests are sent to the configured provider when live AI is explicitly enabled."
    return ProviderSettings(
        provider_type=ProviderType(chosen),
        location=location_value,
        model_id="" if chosen == PROVIDER_DISABLED else resolve_model(model),
        base_url=chosen_url,
        timeout_s=_float_env(AI_TIMEOUT_ENV, REQUEST_TIMEOUT_S),
        context_limit=_int_env(AI_CONTEXT_LIMIT_ENV, 8_192),
        capabilities=capabilities,
        privacy_notice=notice,
        administrator_managed=administrator_managed,
        endpoint_policy=EndpointPolicy(
            deployment_mode="hosted" if hosted else "local",
            administrator_configured=administrator_managed,
            allowed_hosts=allowed_hosts,
            container_host_gateway=_bool_env("APP_IN_DOCKER", False),
        ),
    )


# --------------------------------------------------------------------------- #
# The resolved configuration (key-free by construction)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AIConfig:
    """A resolved AI configuration snapshot. Holds **no** API key — only its presence
    and source — so the whole object is safe to display, log, or serialise."""

    provider: str
    model: str
    key_present: bool
    key_source: str          # SOURCE_ENV / SOURCE_SECRETS / SOURCE_NONE
    sdk_available: bool
    location: str = ProviderLocation.CLOUD.value
    base_url_configured: bool = False
    timeout_s: float = REQUEST_TIMEOUT_S
    context_limit: int = 8_192
    capabilities: ProviderCapabilities = ProviderCapabilities()
    privacy_notice: str = "AI input is sent to the configured provider for this request."
    administrator_managed: bool = False
    configuration_error: str | None = None

    @property
    def enabled(self) -> bool:
        """Whether the selected provider is technically usable (before the master switch)."""
        if self.configuration_error or self.provider == PROVIDER_DISABLED:
            return False
        if self.provider == PROVIDER_ANTHROPIC:
            return self.key_present and self.sdk_available
        if self.provider == PROVIDER_OPENAI_COMPATIBLE and self.location == "cloud":
            return self.base_url_configured and self.key_present
        return self.base_url_configured

    @property
    def key_display(self) -> str:
        """A key-SAFE description — presence + source, never any part of the key."""
        return f"detected ({self.key_source})" if self.key_present else "not detected"

    def status_line(self) -> str:
        state = "enabled" if self.enabled else "disabled"
        return (f"AI {state} · provider: {self.provider} · model: {self.model} "
                f"· key: {self.key_display}")

    def disabled_reason(self) -> str | None:
        """Why AI is off (key-free), or ``None`` when enabled."""
        if self.enabled:
            return None
        if self.configuration_error:
            return self.configuration_error
        if self.provider == PROVIDER_DISABLED:
            return "AI is disabled by provider selection"
        if self.provider == PROVIDER_ANTHROPIC and not self.sdk_available:
            return "the optional 'anthropic' SDK is not installed (pip install anthropic)"
        if self.provider == PROVIDER_ANTHROPIC and not self.key_present:
            return (f"no API key found (set the {API_KEY_ENV} environment variable or a "
                    "Streamlit secret)")
        if self.provider == PROVIDER_OPENAI_COMPATIBLE and self.location == "cloud" \
                and not self.key_present:
            return "no administrator-configured API credential was found"
        if not self.base_url_configured:
            return "no safe AI endpoint is configured"
        return "AI is disabled"

    def to_safe_dict(self) -> dict:
        """A key-free dict suitable for display, logging, or an audit note."""
        return {
            "provider": self.provider,
            "model": self.model,
            "enabled": self.enabled,
            "key_present": self.key_present,
            "key_source": self.key_source,
            "sdk_available": self.sdk_available,
            "location": self.location,
            "endpoint_configured": self.base_url_configured,
            "timeout_s": self.timeout_s,
            "context_limit": self.context_limit,
            "capabilities": self.capabilities.to_safe_dict(),
            "privacy_notice": self.privacy_notice,
            "administrator_managed": self.administrator_managed,
            "configuration_error": self.configuration_error,
            "role": AI_ROLE_LINE,
        }


def resolve_config(*, provider: str | None = None, model: str | None = None,
                   base_url: str | None = None, location: str | None = None) -> AIConfig:
    """Resolve the active AI configuration (key presence + source, model, provider, SDK)."""
    chosen = resolve_provider(provider)
    key, source = detect_api_key(chosen)
    settings = None
    configuration_error = None
    try:
        settings = resolve_provider_settings(
            provider=chosen, model=model, base_url=base_url, location=location)
    except ProviderConfigurationError as exc:
        # Provider settings are user/admin configuration, so surface only the
        # controlled policy message; never raw transport/secret material.
        configuration_error = str(exc)
    chosen_model = resolve_model(model) if chosen != PROVIDER_DISABLED else ""
    chosen_location = (settings.location.value if settings else
                       (_clean(location) or _env(AI_LOCATION_ENV)
                        or _default_location(chosen).value))
    return AIConfig(
        provider=chosen,
        model=chosen_model,
        key_present=key is not None,
        key_source=source,
        sdk_available=sdk_available() if chosen == PROVIDER_ANTHROPIC else True,
        location=chosen_location,
        base_url_configured=bool(settings and settings.base_url),
        timeout_s=settings.timeout_s if settings else _float_env(AI_TIMEOUT_ENV, REQUEST_TIMEOUT_S),
        context_limit=settings.context_limit if settings else _int_env(AI_CONTEXT_LIMIT_ENV, 8_192),
        capabilities=(settings.capabilities if settings else ProviderCapabilities()),
        privacy_notice=(settings.privacy_notice if settings else
                        "AI is unavailable because provider configuration is unsafe."),
        administrator_managed=bool(settings and settings.administrator_managed),
        configuration_error=configuration_error,
    )


def is_enabled(*, provider: str | None = None, model: str | None = None) -> bool:
    """Global live-AI gate: provider capability *and* the per-session master switch.

    Outside Streamlit this preserves the historical capability-only behavior for
    scripts and tests. Inside the app every AI feature sees the same master switch,
    so disabling AI is system-wide rather than assistant-tab-only.
    """
    cfg = resolve_config(provider=provider, model=model)
    if not cfg.enabled:
        return False
    if not _under_streamlit_runtime():
        return True
    try:
        import streamlit as st
        return bool(st.session_state.get(MASTER_SWITCH_KEY, False))
    except Exception:
        return False


def live_ai_active(config: AIConfig, toggle_on: bool) -> bool:
    """Whether **live** AI may actually run this turn.

    Two conditions, both required: the configuration is *capable* (``config.enabled`` — a key and
    the SDK are present) **and** the user has explicitly turned on the live-AI switch
    (``toggle_on``, the Settings toggle). A stale ``toggle_on`` never overrides a lost capability,
    so a removed key / SDK disables AI even if the toggle was left on. Pure + key-free."""
    return live_ai_status(config, toggle_on).active


@dataclass(frozen=True)
class LiveAIStatus:
    """The single, shared answer to 'is live AI on right now, and why' (key-free).

    Both Settings (the status card) and the Assistant (the per-turn ``use_ai`` decision +
    caption) derive their state from this one helper, so they can never drift apart.
    """

    active: bool          # live AI will actually run this turn (capable AND toggle on)
    capable: bool         # a key + the SDK are present (the toggle is only operable then)
    toggle_on: bool       # the user's Settings switch
    reason: str           # short, human, key-free — safe to display anywhere

    def to_safe_dict(self) -> dict:
        return {"active": self.active, "capable": self.capable, "toggle_on": self.toggle_on,
                "reason": self.reason}


def live_ai_status(config: AIConfig, toggle_on: bool) -> LiveAIStatus:
    """Resolve the **one** live-AI status used by the UI everywhere (pure, key-free).

    Capability (``config.enabled`` = key + SDK) and the user's Settings toggle are combined into a
    single ``active`` flag plus a human ``reason`` that explains *why* AI is on/off — so Settings
    and the Assistant always agree. A lost key/SDK forces ``active=False`` even with the toggle
    left on; a per-turn API failure is reported separately (it never flips this status off).
    """
    capable = bool(getattr(config, "enabled", False))
    toggle = bool(toggle_on)
    active = capable and toggle
    if active:
        reason = f"Live AI is on (model {getattr(config, 'model', '—')})."
    elif not capable:
        reason = getattr(config, "disabled_reason", lambda: None)() or "AI is disabled."
    else:
        reason = "Live AI is off — turn on 'Enable live AI assistant' in Settings."
    return LiveAIStatus(active=active, capable=capable, toggle_on=toggle, reason=reason)


# --------------------------------------------------------------------------- #
# Safe diagnostics (every field is non-secret; the key value never appears)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AIDiagnostics:
    """A key-free snapshot for the 'why is live AI off / failing' panel.

    Holds only safe facts: key *presence* + *length* (never the key), SDK availability, the
    selected model/provider, whether live AI is enabled, and the last sanitized AI error +
    whether this turn fell back. Safe to display, log, or serialise anywhere.
    """

    key_present: bool
    key_length: int
    sdk_available: bool
    selected_model: str
    provider: str
    live_ai_enabled: bool
    capable: bool
    last_ai_error_type: str | None
    last_ai_error_message: str | None
    fallback_this_turn: bool
    location: str = ProviderLocation.CLOUD.value
    endpoint_configured: bool = False
    timeout_s: float = REQUEST_TIMEOUT_S
    context_limit: int = 8_192
    capabilities: dict | None = None
    administrator_managed: bool = False
    configuration_error: str | None = None

    def to_safe_dict(self) -> dict:
        return {
            "key_present": self.key_present,
            "key_length": self.key_length,
            "sdk_available": self.sdk_available,
            "selected_model": self.selected_model,
            "provider": self.provider,
            "live_ai_enabled": self.live_ai_enabled,
            "capable": self.capable,
            "last_ai_error_type": self.last_ai_error_type,
            "last_ai_error_message": self.last_ai_error_message,
            "fallback_this_turn": self.fallback_this_turn,
            "location": self.location,
            "endpoint_configured": self.endpoint_configured,
            "timeout_s": self.timeout_s,
            "context_limit": self.context_limit,
            "capabilities": dict(self.capabilities or {}),
            "administrator_managed": self.administrator_managed,
            "configuration_error": self.configuration_error,
        }


def diagnostics(*, toggle_on: bool = False, last_ai_error_type: str | None = None,
                last_ai_error_message: str | None = None, fallback_this_turn: bool = False,
                provider: str | None = None, model: str | None = None) -> AIDiagnostics:
    """Resolve the safe AI diagnostics snapshot (pure, key-free, never raises).

    ``last_ai_error_*`` and ``fallback_this_turn`` are passed in from the conversation state
    (per-turn facts the config layer doesn't own); everything else is resolved here.
    """
    cfg = resolve_config(provider=provider, model=model)
    status = live_ai_status(cfg, toggle_on)
    return AIDiagnostics(
        key_present=cfg.key_present, key_length=key_length(cfg.provider),
        sdk_available=cfg.sdk_available,
        selected_model=cfg.model, provider=cfg.provider, live_ai_enabled=status.active,
        capable=status.capable, last_ai_error_type=last_ai_error_type,
        last_ai_error_message=last_ai_error_message, fallback_this_turn=bool(fallback_this_turn),
        location=cfg.location, endpoint_configured=cfg.base_url_configured,
        timeout_s=cfg.timeout_s, context_limit=cfg.context_limit,
        capabilities=cfg.capabilities.to_safe_dict(),
        administrator_managed=cfg.administrator_managed,
        configuration_error=cfg.configuration_error)
