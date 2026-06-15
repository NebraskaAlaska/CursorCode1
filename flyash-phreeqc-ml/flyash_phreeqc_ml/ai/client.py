"""Safe Anthropic client wrapper.

Builds an Anthropic client *only* when AI is enabled, fails gracefully (a structured
result, never an exception) when the key or SDK is missing, and **never exposes the API
key** — not in a return value, an error message, or a log. Every AI helper resolves its
client through here, so the "disabled" path is identical everywhere and the key/model
logic lives in exactly one place (:mod:`.config`).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config as ai_config

# --------------------------------------------------------------------------- #
# Structured error codes (stable strings the UI / tests can switch on).
# --------------------------------------------------------------------------- #
ERROR_NONE: str | None = None
ERROR_NO_KEY = "no_api_key"
ERROR_NO_SDK = "sdk_unavailable"
ERROR_UNSUPPORTED_PROVIDER = "unsupported_provider"
ERROR_CLIENT_INIT = "client_init_failed"

_ERROR_MESSAGES = {
    ERROR_NO_KEY: (f"No API key found. Set the {ai_config.API_KEY_ENV} environment "
                   "variable or add it to your Streamlit secrets."),
    ERROR_NO_SDK: "The optional 'anthropic' SDK is not installed (pip install anthropic).",
    ERROR_UNSUPPORTED_PROVIDER: "The selected AI provider is not supported.",
    ERROR_CLIENT_INIT: "The AI client could not be initialised.",
}


@dataclass(frozen=True)
class ClientResult:
    """The outcome of a client request.

    ``client`` is ``None`` unless ``ok``; ``error`` is a stable code (see ``ERROR_*``) and
    ``message`` a human, **key-free** description. ``config`` is the resolved snapshot.
    """

    client: object | None
    ok: bool
    error: str | None
    message: str | None
    config: ai_config.AIConfig

    @property
    def enabled(self) -> bool:
        return self.config.enabled


def _construct_anthropic(api_key: str):
    """Construct the real client. Isolated so tests can monkeypatch it (and so the SDK
    import stays lazy)."""
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def get_client(injected=None, *, provider: str | None = None,
               model: str | None = None) -> ClientResult:
    """Resolve a usable client as a structured result (never raises).

    * ``injected`` (a test/fake client) is returned as-is with ``ok=True`` — the injection
      path needs no key, matching the historical helper behaviour exactly.
    * otherwise a real client is built **only** when the SDK + a key are present; any
      failure becomes a structured error with a **key-free** message.
    """
    cfg = ai_config.resolve_config(provider=provider, model=model)
    if injected is not None:
        return ClientResult(injected, True, ERROR_NONE, None, cfg)
    if cfg.provider not in ai_config.SUPPORTED_PROVIDERS:   # defensive; provider is clamped
        return ClientResult(None, False, ERROR_UNSUPPORTED_PROVIDER,
                            _ERROR_MESSAGES[ERROR_UNSUPPORTED_PROVIDER], cfg)
    if not cfg.sdk_available:
        return ClientResult(None, False, ERROR_NO_SDK, _ERROR_MESSAGES[ERROR_NO_SDK], cfg)
    key, _source = ai_config.detect_api_key()
    if not key:
        return ClientResult(None, False, ERROR_NO_KEY, _ERROR_MESSAGES[ERROR_NO_KEY], cfg)
    try:
        client = _construct_anthropic(key)
    except Exception as exc:   # never surface the key — only the exception *type*
        msg = f"{_ERROR_MESSAGES[ERROR_CLIENT_INIT]} ({type(exc).__name__})"
        return ClientResult(None, False, ERROR_CLIENT_INIT, msg, cfg)
    return ClientResult(client, True, ERROR_NONE, None, cfg)


def resolve_client(injected=None, *, provider: str | None = None, model: str | None = None):
    """Back-compat helper: return a usable client or ``None`` (the old ``_resolve_client``)."""
    return get_client(injected, provider=provider, model=model).client


def is_enabled(*, provider: str | None = None, model: str | None = None) -> bool:
    """True when a key + the ``anthropic`` SDK are present (delegates to :mod:`.config`)."""
    return ai_config.is_enabled(provider=provider, model=model)
