"""Safe provider-neutral AI client wrapper.

Builds the selected provider client *only* when AI is enabled, fails gracefully (a
structured result, never an exception) when required configuration is missing, and
**never exposes credentials** in a return value, error message, or log. Every live AI
helper resolves its client through here, so the disabled path is identical everywhere
and provider/model rules live in exactly one place (:mod:`.config`).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config as ai_config
from . import provider_contract

# --------------------------------------------------------------------------- #
# Structured error codes (stable strings the UI / tests can switch on).
# --------------------------------------------------------------------------- #
ERROR_NONE: str | None = None
ERROR_DISABLED = "ai_disabled"
ERROR_NO_KEY = "no_api_key"
ERROR_NO_SDK = "sdk_unavailable"
ERROR_UNSUPPORTED_PROVIDER = "unsupported_provider"
ERROR_CONFIGURATION = "unsafe_configuration"
ERROR_CLIENT_INIT = "client_init_failed"

_ERROR_MESSAGES = {
    ERROR_DISABLED: "AI is disabled by the global live-AI setting.",
    ERROR_NO_KEY: "No administrator/user API credential was found for this provider.",
    ERROR_NO_SDK: "The optional 'anthropic' SDK is not installed (pip install anthropic).",
    ERROR_UNSUPPORTED_PROVIDER: "The selected AI provider is not supported.",
    ERROR_CONFIGURATION: "The selected AI provider configuration is unavailable or unsafe.",
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
    import stays lazy).

    A bounded ``timeout`` makes a network hang fail cleanly (a classifiable ``timeout`` error
    instead of a frozen turn); ``max_retries`` lets the SDK smooth transient 429/5xx with
    backoff. Both are non-secret config values.
    """
    import anthropic
    inner = anthropic.Anthropic(api_key=api_key, timeout=ai_config.REQUEST_TIMEOUT_S,
                                max_retries=ai_config.MAX_RETRIES)
    return provider_contract.guard_client(inner)


def _construct_provider(settings: provider_contract.ProviderSettings, secret: str | None):
    """Construct a non-Anthropic adapter client without contacting the endpoint."""
    return provider_contract.create_adapter(settings, secret=secret).client()


def get_client(injected=None, *, provider: str | None = None,
               model: str | None = None, respect_master_switch: bool = True) -> ClientResult:
    """Resolve a usable client as a structured result (never raises).

    * ``injected`` (a test/fake client) is returned as-is with ``ok=True`` — the injection
      path needs no key, matching the historical helper behaviour exactly.
    * otherwise a real client is built **only** when the SDK + a key are present; any
      failure becomes a structured error with a **key-free** message.
    """
    cfg = ai_config.resolve_config(provider=provider, model=model)
    if injected is not None:
        return ClientResult(injected, True, ERROR_NONE, None, cfg)
    if cfg.provider not in ai_config.SUPPORTED_PROVIDERS:
        return ClientResult(None, False, ERROR_UNSUPPORTED_PROVIDER,
                            _ERROR_MESSAGES[ERROR_UNSUPPORTED_PROVIDER], cfg)
    if cfg.configuration_error:
        return ClientResult(None, False, ERROR_CONFIGURATION,
                            _ERROR_MESSAGES[ERROR_CONFIGURATION], cfg)
    if cfg.provider == ai_config.PROVIDER_DISABLED:
        return ClientResult(None, False, ERROR_DISABLED, _ERROR_MESSAGES[ERROR_DISABLED], cfg)
    if respect_master_switch and ai_config._under_streamlit_runtime() \
            and not ai_config.is_enabled(provider=provider, model=model):
        return ClientResult(None, False, ERROR_DISABLED, _ERROR_MESSAGES[ERROR_DISABLED], cfg)
    if cfg.provider == ai_config.PROVIDER_ANTHROPIC and not cfg.sdk_available:
        return ClientResult(None, False, ERROR_NO_SDK, _ERROR_MESSAGES[ERROR_NO_SDK], cfg)
    key, _source = ai_config.detect_api_key(cfg.provider)
    key_required = cfg.provider == ai_config.PROVIDER_ANTHROPIC \
        or (cfg.provider == ai_config.PROVIDER_OPENAI_COMPATIBLE and cfg.location == "cloud")
    if key_required and not key:
        return ClientResult(None, False, ERROR_NO_KEY, _ERROR_MESSAGES[ERROR_NO_KEY], cfg)
    try:
        if cfg.provider == ai_config.PROVIDER_ANTHROPIC:
            client = _construct_anthropic(key)
        else:
            settings = ai_config.resolve_provider_settings(provider=cfg.provider, model=cfg.model)
            client = _construct_provider(settings, key)
    except Exception as exc:   # never surface the key — only the exception *type*
        msg = f"{_ERROR_MESSAGES[ERROR_CLIENT_INIT]} ({type(exc).__name__})"
        return ClientResult(None, False, ERROR_CLIENT_INIT, msg, cfg)
    return ClientResult(client, True, ERROR_NONE, None, cfg)


def resolve_client(injected=None, *, provider: str | None = None, model: str | None = None):
    """Back-compat helper: return a usable client or ``None`` (the old ``_resolve_client``)."""
    return get_client(injected, provider=provider, model=model).client


# --------------------------------------------------------------------------- #
# Live-call error categories (stable; the UI + tests switch on these).
#
# These describe a failure of an actual `messages.create` request OR of the response shape —
# distinct from the build-time ERROR_* codes above (key/SDK/init problems before any request).
# The category + a fixed human message are derived from the exception *type name* + HTTP status
# ONLY — never from ``str(exc)`` — so no request detail (and never the key) can leak.
# --------------------------------------------------------------------------- #
CALL_AUTH = "authentication_error"
CALL_RATE_LIMIT = "rate_limited"
CALL_NETWORK = "network_error"
CALL_TIMEOUT = "timeout"
CALL_MODEL = "model_error"
CALL_BAD_REQUEST = "bad_request"
CALL_OVERLOADED = "overloaded"
CALL_SERVER = "api_server_error"
CALL_TRUNCATED = "response_truncated"
CALL_INVALID_JSON = "invalid_json_response"
CALL_PROCESSING = "response_processing_error"
CALL_EMPTY = "empty_response"
CALL_UNKNOWN = "unknown_error"
CALL_SCIENTIFIC_BOUNDARY = "scientific_boundary"

_CALL_MESSAGES = {
    CALL_AUTH: "authentication failed — the API key was rejected (check ANTHROPIC_API_KEY).",
    CALL_RATE_LIMIT: "rate limited by the API — too many requests; wait briefly and retry.",
    CALL_NETWORK: "network error reaching the API (no connection / DNS / egress blocked).",
    CALL_TIMEOUT: "the API request timed out.",
    CALL_MODEL: "model not found / not available for this key (check the selected model).",
    CALL_BAD_REQUEST: "the API rejected the request (model or parameter problem).",
    CALL_OVERLOADED: "the API is overloaded — try again shortly.",
    CALL_SERVER: "the API returned a server error — try again shortly.",
    CALL_TRUNCATED: "the model's reply was cut off before it finished (raise max_tokens).",
    CALL_INVALID_JSON: "the model did not return valid JSON for this message.",
    CALL_PROCESSING: "the model's reply could not be processed.",
    CALL_EMPTY: "the API returned an empty reply.",
    CALL_UNKNOWN: "the live AI call failed for an unexpected reason.",
    CALL_SCIENTIFIC_BOUNDARY: (
        "AI output was withheld because scientific claims and actions must come from reviewed "
        "deterministic state."),
}


def call_error_message(category: str | None) -> str:
    """A fixed, key-free human message for a call-error category (or the category itself)."""
    if category is None:
        return ""
    return _CALL_MESSAGES.get(category, str(category))


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """Map a request/transport exception to a stable ``(category, key-free message)``.

    Classification uses the exception's class-name chain + ``status_code`` only (never
    ``str(exc)``), so the returned message can never contain request details or the API key.
    """
    names = {c.__name__ for c in type(exc).__mro__}
    status = getattr(exc, "status_code", None)

    def has(*subs: str) -> bool:
        return any(any(sub in n for n in names) for sub in subs)

    if has("ScientificOutputBoundaryError"):
        cat = CALL_SCIENTIFIC_BOUNDARY
    elif has("AuthenticationError", "PermissionDenied") or status in (401, 403):
        cat = CALL_AUTH
    elif has("RateLimitError") or status == 429:
        cat = CALL_RATE_LIMIT
    elif has("APITimeoutError", "Timeout"):                     # subclass of connection error
        cat = CALL_TIMEOUT
    elif has("APIConnectionError", "ConnectionError", "ConnectError"):
        cat = CALL_NETWORK
    elif has("NotFoundError") or status == 404:
        cat = CALL_MODEL
    elif has("BadRequestError", "UnprocessableEntity") or status in (400, 422):
        cat = CALL_BAD_REQUEST
    elif has("Overloaded") or status == 529:
        cat = CALL_OVERLOADED
    elif has("InternalServerError", "APIStatusError") or (isinstance(status, int) and status >= 500):
        cat = CALL_SERVER
    else:
        cat = CALL_UNKNOWN
    detail = (f"{_CALL_MESSAGES[cat]} [{type(exc).__name__}"
              + (f", HTTP {status}" if isinstance(status, int) else "") + "]")
    return cat, detail


@dataclass(frozen=True)
class SmokeResult:
    """Sanitized outcome of a minimal live call (no key, no raw response text)."""

    ok: bool
    category: str | None      # None on success, else an ERROR_* / CALL_* code
    message: str | None       # key-free
    model: str

    def to_safe_dict(self) -> dict:
        return {"ok": self.ok, "category": self.category, "message": self.message,
                "model": self.model}


def smoke_test(*, model: str | None = None,
               prompt: str = "Reply with the single word: ok.",
               max_tokens: int = 16) -> SmokeResult:
    """Send a harmless one-sentence prompt through the **same** client path the assistant uses.

    Returns a sanitized :class:`SmokeResult` — it **never** logs/returns the API key or the raw
    response text (only whether a textual reply arrived). Any failure is classified into a safe
    category. Never raises.
    """
    used_model = ai_config.resolve_model(model)
    res = get_client(model=model, respect_master_switch=False)
    if not res.ok or res.client is None:
        return SmokeResult(False, res.error, res.message, used_model)
    try:
        resp = res.client.messages.create(
            model=used_model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}])
    except Exception as exc:                                    # noqa: BLE001 — never raise
        cat, msg = classify_exception(exc)
        return SmokeResult(False, cat, msg, used_model)
    content = getattr(resp, "content", None) or []
    has_text = any(getattr(b, "type", None) == "text" and getattr(b, "text", "") for b in content)
    if not has_text:
        return SmokeResult(False, CALL_EMPTY, _CALL_MESSAGES[CALL_EMPTY], used_model)
    return SmokeResult(True, None, None, used_model)


def is_enabled(*, provider: str | None = None, model: str | None = None) -> bool:
    """Return the global provider capability/master-switch decision."""
    return ai_config.is_enabled(provider=provider, model=model)
