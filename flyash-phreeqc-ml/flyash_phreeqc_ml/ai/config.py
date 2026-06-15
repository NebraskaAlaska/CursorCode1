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
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Providers (Anthropic only today; the structure leaves room to grow)
# --------------------------------------------------------------------------- #
PROVIDER_ANTHROPIC = "anthropic"
SUPPORTED_PROVIDERS = (PROVIDER_ANTHROPIC,)
DEFAULT_PROVIDER = PROVIDER_ANTHROPIC

# --------------------------------------------------------------------------- #
# Env var names (kept identical to the historical names so nothing breaks)
# --------------------------------------------------------------------------- #
API_KEY_ENV = "ANTHROPIC_API_KEY"
MODEL_ENV = "ANTHROPIC_MODEL"
PROVIDER_ENV = "ANTHROPIC_PROVIDER"

# Default to the most capable model; override per-deployment (env / secrets / the UI)
# without code changes.
DEFAULT_MODEL = "claude-opus-4-8"

# A small curated list the settings UI offers; free-text entry is also allowed.
SUGGESTED_MODELS = (
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-fable-5",
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

# Process-level overrides set from the UI (the provider/model chosen in the settings
# panel). Kept module-level so a Streamlit rerun (same process) preserves the choice
# without threading it through every AI call site. It NEVER holds the API key.
_RUNTIME_OVERRIDES: dict = {}


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
# Runtime overrides (set from the settings UI; provider/model only — never the key)
# --------------------------------------------------------------------------- #
def set_runtime_overrides(*, provider: str | None = None, model: str | None = None) -> None:
    """Record the UI-chosen provider/model for this process. A blank value clears a field.

    Passing ``None`` for a field leaves it unchanged; passing an empty string clears it.
    This never stores the API key.
    """
    if provider is not None:
        p = _clean(provider)
        if p:
            _RUNTIME_OVERRIDES["provider"] = p
        else:
            _RUNTIME_OVERRIDES.pop("provider", None)
    if model is not None:
        m = _clean(model)
        if m:
            _RUNTIME_OVERRIDES["model"] = m
        else:
            _RUNTIME_OVERRIDES.pop("model", None)


def clear_runtime_overrides() -> None:
    """Forget any UI-chosen provider/model (back to env / secrets / defaults)."""
    _RUNTIME_OVERRIDES.clear()


def get_runtime_overrides() -> dict:
    """A copy of the current process-level overrides (no key, ever)."""
    return dict(_RUNTIME_OVERRIDES)


# --------------------------------------------------------------------------- #
# Detection (the key is resolved here only for internal client construction)
# --------------------------------------------------------------------------- #
def detect_api_key() -> tuple[str | None, str]:
    """Return ``(key, source)`` — environment first, then Streamlit secrets.

    ``(None, SOURCE_NONE)`` when absent. The key is returned only so the client wrapper
    can construct a real client; callers must never display or log it.
    """
    env = _env(API_KEY_ENV)
    if env:
        return env, SOURCE_ENV
    sec = _secrets_get(API_KEY_ENV)
    if sec:
        return sec, SOURCE_SECRETS
    return None, SOURCE_NONE


def api_key_present() -> bool:
    return detect_api_key()[0] is not None


def api_key_source() -> str:
    return detect_api_key()[1]


def sdk_available() -> bool:
    """True when the optional ``anthropic`` SDK is importable. Never raises."""
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def resolve_model(model: str | None = None) -> str:
    """arg > runtime override > ``ANTHROPIC_MODEL`` env > secret > :data:`DEFAULT_MODEL`."""
    return (_clean(model)
            or _RUNTIME_OVERRIDES.get("model")
            or _env(MODEL_ENV)
            or _secrets_get(MODEL_ENV)
            or DEFAULT_MODEL)


def resolve_provider(provider: str | None = None) -> str:
    """arg > runtime override > ``ANTHROPIC_PROVIDER`` env > :data:`DEFAULT_PROVIDER`.

    Clamped to :data:`SUPPORTED_PROVIDERS` (unknown values fall back to the default), so
    a usable provider is always returned.
    """
    chosen = (_clean(provider)
              or _RUNTIME_OVERRIDES.get("provider")
              or _env(PROVIDER_ENV)
              or DEFAULT_PROVIDER).lower()
    return chosen if chosen in SUPPORTED_PROVIDERS else DEFAULT_PROVIDER


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

    @property
    def enabled(self) -> bool:
        """AI is usable only with both a key and the SDK present."""
        return self.key_present and self.sdk_available

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
        if not self.sdk_available:
            return "the optional 'anthropic' SDK is not installed (pip install anthropic)"
        if not self.key_present:
            return (f"no API key found (set the {API_KEY_ENV} environment variable or a "
                    "Streamlit secret)")
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
            "role": AI_ROLE_LINE,
        }


def resolve_config(*, provider: str | None = None, model: str | None = None) -> AIConfig:
    """Resolve the active AI configuration (key presence + source, model, provider, SDK)."""
    key, source = detect_api_key()
    return AIConfig(
        provider=resolve_provider(provider),
        model=resolve_model(model),
        key_present=key is not None,
        key_source=source,
        sdk_available=sdk_available(),
    )


def is_enabled(*, provider: str | None = None, model: str | None = None) -> bool:
    """True when a key + the ``anthropic`` SDK are available."""
    return resolve_config(provider=provider, model=model).enabled
