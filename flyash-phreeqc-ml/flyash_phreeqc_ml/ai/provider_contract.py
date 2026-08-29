"""Provider-neutral AI configuration, adapters, and scientific output boundary.

Provider construction is inert: creating settings/adapters performs no network I/O,
model discovery, download, or process start.  Health/discovery/pull are explicit
methods.  All HTTP endpoints pass a conservative URL policy before use and redirects
are refused, preventing an approved URL from silently hopping to a different host.
"""
from __future__ import annotations

import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol


class ProviderType(str, Enum):
    DISABLED = "disabled"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"
    HOSTED = "hosted"


class ProviderLocation(str, Enum):
    DISABLED = "disabled"
    LOCAL = "local"
    SERVER = "server"
    CLOUD = "cloud"


class ProviderConfigurationError(ValueError):
    """Provider settings are invalid or unsafe."""


class ProviderUnavailableError(RuntimeError):
    """An explicit provider operation failed without exposing response contents."""


class ModelPullConfirmationError(PermissionError):
    """A local model download was not exactly and explicitly confirmed."""


class ScientificOutputBoundaryError(ValueError):
    """Untrusted model prose attempted to cross a deterministic science boundary."""


def _validate_request_envelope(
    *, model: str, max_tokens: int, messages: list[dict], context_limit: int,
    extra: dict | None = None,
) -> None:
    """Bound provider-bound input without retaining or echoing its private contents."""
    if not isinstance(messages, list) or len(messages) > 256:
        raise ProviderConfigurationError("AI request has too many message turns")
    try:
        requested_tokens = int(max_tokens)
    except (TypeError, ValueError) as exc:
        raise ProviderConfigurationError("AI request output limit is invalid") from exc
    if requested_tokens < 1 or requested_tokens > int(context_limit):
        raise ProviderConfigurationError("AI request output limit exceeds the provider context")
    model_id = str(model or "")
    if len(model_id) > 256 or re.search(r"[\x00-\x1f\x7f]", model_id):
        raise ProviderConfigurationError("AI request model id is invalid")
    try:
        encoded = json.dumps(
            {"messages": messages, "extra": extra or {}}, default=str,
            ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProviderConfigurationError("AI request could not be safely serialized") from exc
    maximum = min(4 * 1024 * 1024, max(8 * 1024, int(context_limit) * 8))
    if len(encoded) > maximum:
        raise ProviderConfigurationError("AI request exceeds the provider context limit")


@dataclass(frozen=True)
class ProviderCapabilities:
    chat: bool = True
    structured_output: bool = True
    model_discovery: bool = False
    model_pull: bool = False
    tool_calling: bool = False

    def to_safe_dict(self) -> dict:
        return {
            "chat": self.chat,
            "structured_output": self.structured_output,
            "model_discovery": self.model_discovery,
            "model_pull": self.model_pull,
            "tool_calling": self.tool_calling,
        }


@dataclass(frozen=True)
class EndpointPolicy:
    """Limits which addresses an adapter may contact."""

    deployment_mode: str = "local"
    administrator_configured: bool = False
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    container_host_gateway: bool = False


def _normalize_host(host: str) -> str:
    return str(host or "").strip().rstrip(".").lower()


def validate_base_url(
    value: str,
    *,
    location: ProviderLocation,
    policy: EndpointPolicy,
) -> str:
    """Return a normalized HTTP(S) base URL or reject it as an SSRF risk."""
    raw = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise ProviderConfigurationError("AI endpoint URL is malformed") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ProviderConfigurationError("AI endpoint must use http or https")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ProviderConfigurationError("AI endpoint must have a host and no embedded credentials")
    if parsed.query or parsed.fragment:
        raise ProviderConfigurationError("AI endpoint may not contain a query or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderConfigurationError("AI endpoint port is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ProviderConfigurationError("AI endpoint port is invalid")

    host = _normalize_host(parsed.hostname)
    allowed = frozenset(_normalize_host(item) for item in policy.allowed_hosts if item)
    hosted = str(policy.deployment_mode).lower() == "hosted"
    if hosted:
        if not policy.administrator_configured:
            raise ProviderConfigurationError(
                "hosted AI endpoints must be configured by an administrator")
        if not allowed or host not in allowed:
            raise ProviderConfigurationError("AI endpoint host is not administrator-approved")

    if any(part == ".." for part in urllib.parse.unquote(parsed.path).split("/")):
        raise ProviderConfigurationError("AI endpoint path may not contain traversal segments")

    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None:
        if address.is_unspecified or address.is_multicast or address.is_link_local \
                or address.is_reserved:
            raise ProviderConfigurationError("AI endpoint address is not permitted")
        if address.is_loopback and location is not ProviderLocation.LOCAL:
            raise ProviderConfigurationError("loopback AI endpoints must be classified as local")
        if address.is_private and location not in {ProviderLocation.LOCAL, ProviderLocation.SERVER}:
            raise ProviderConfigurationError("private AI endpoints cannot be classified as cloud")
    elif host in {"localhost", "localhost.localdomain"} \
            and location is not ProviderLocation.LOCAL:
        raise ProviderConfigurationError("localhost AI endpoints must be classified as local")

    if location is ProviderLocation.LOCAL:
        is_loopback = bool(address is not None and address.is_loopback)
        reviewed_container_gateway = (
            policy.container_host_gateway
            and host == "host.docker.internal"
            and host in allowed
        )
        if host not in {"localhost", "localhost.localdomain"} \
                and not is_loopback and not reviewed_container_gateway:
            raise ProviderConfigurationError(
                "local AI endpoints must use loopback or the reviewed container host gateway")
    elif not hosted and (not allowed or host not in allowed):
        # No DNS lookup happens during configuration, so exact operator/user
        # allow-listing is the fail-closed defense against DNS rebinding and
        # browser-supplied server/cloud SSRF targets.
        raise ProviderConfigurationError(
            "server/cloud AI endpoint host must be explicitly allow-listed")

    if location is ProviderLocation.CLOUD and parsed.scheme != "https":
        raise ProviderConfigurationError("cloud AI endpoints must use https")
    # Retain an optional deployment path (some gateways mount under /v1) but
    # normalize away a trailing slash so endpoint joins remain deterministic.
    normalized_path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, normalized_path, "", ""))


@dataclass(frozen=True)
class ProviderSettings:
    provider_type: ProviderType
    location: ProviderLocation
    model_id: str
    base_url: str | None = None
    timeout_s: float = 45.0
    context_limit: int = 8_192
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    privacy_notice: str = "AI input is sent to the configured provider for this request."
    administrator_managed: bool = False
    endpoint_policy: EndpointPolicy = field(default_factory=EndpointPolicy)

    def __post_init__(self) -> None:
        provider = ProviderType(self.provider_type)
        location = ProviderLocation(self.location)
        model = str(self.model_id or "").strip()
        if not 0.1 <= float(self.timeout_s) <= 300:
            raise ProviderConfigurationError("AI timeout must be between 0.1 and 300 seconds")
        if not 256 <= int(self.context_limit) <= 2_000_000:
            raise ProviderConfigurationError("AI context limit is outside the supported range")
        if provider is ProviderType.DISABLED:
            if location is not ProviderLocation.DISABLED:
                raise ProviderConfigurationError("disabled AI must use the disabled classification")
            object.__setattr__(self, "model_id", "")
            object.__setattr__(self, "base_url", None)
        else:
            if location is ProviderLocation.DISABLED or not model:
                raise ProviderConfigurationError("enabled providers require a model and location")
            if len(model) > 256 or re.search(r"[\x00-\x1f\x7f]", model):
                raise ProviderConfigurationError("AI model id is invalid")
            object.__setattr__(self, "model_id", model)
        if provider is ProviderType.HOSTED and not self.administrator_managed:
            raise ProviderConfigurationError("hosted model endpoints must be administrator-managed")
        if provider in {ProviderType.OLLAMA, ProviderType.OPENAI_COMPATIBLE, ProviderType.HOSTED}:
            if not self.base_url:
                raise ProviderConfigurationError("this provider requires a base URL")
            normalized = validate_base_url(
                self.base_url,
                location=location,
                policy=self.endpoint_policy,
            )
            object.__setattr__(self, "base_url", normalized)
        object.__setattr__(self, "provider_type", provider)
        object.__setattr__(self, "location", location)

    @property
    def enabled(self) -> bool:
        return self.provider_type is not ProviderType.DISABLED

    def to_safe_dict(self) -> dict:
        return {
            "provider_type": self.provider_type.value,
            "location": self.location.value,
            "endpoint_configured": bool(self.base_url),
            "model_id": self.model_id,
            "timeout_s": self.timeout_s,
            "context_limit": self.context_limit,
            "capabilities": self.capabilities.to_safe_dict(),
            "privacy_notice": self.privacy_notice,
            "administrator_managed": self.administrator_managed,
        }


@dataclass(frozen=True)
class ProviderHealth:
    ok: bool
    category: str | None = None
    message: str | None = None

    def to_safe_dict(self) -> dict:
        return {"ok": self.ok, "category": self.category, "message": self.message}


@dataclass(frozen=True)
class DiscoveredModel:
    model_id: str
    size_bytes: int | None = None

    def to_safe_dict(self) -> dict:
        return {"model_id": self.model_id, "size_bytes": self.size_bytes}


@dataclass(frozen=True)
class ScientificBoundaryDecision:
    allowed: bool
    category: str | None = None
    safe_message: str | None = None


_PHREEQC_BLOCK = re.compile(
    r"(?im)^\s*(SOLUTION|EQUILIBRIUM_PHASES|KINETICS|RATES|SELECTED_OUTPUT|USER_PUNCH)\b")
_EXECUTABLE_INSTRUCTION = re.compile(
    r"(?im)(?:^|[`$;]\s*)(?:sudo\s+)?(?:curl|wget|rm|chmod|docker|podman|pip|conda|brew)\s+")
_AUTHORITY_CLAIM = re.compile(
    r"(?i)\b(?:validated?|validation\s+(?:passed|complete)|confirmed?\s+(?:xrd\s+)?phase|"
    r"xrd\s+confirms?|qc\s+(?:passed|approved)|experimentally\s+proven)\b")
_WARNING_BYPASS = re.compile(
    r"(?i)\b(?:ignore|disregard|remove|suppress|weaken|bypass)\b.{0,48}\b"
    r"(?:warning|review|confirmation|qc|validation|safety)\b")
_UPDATE_ACTIVATION = re.compile(
    r"(?i)\b(?:activate|promote|install|switch(?:\s+to)?)\b.{0,64}\b"
    r"(?:runtime|database|cemdata|update|model binary)\b")
_PROMPT_INJECTION = re.compile(
    r"(?i)\b(?:ignore|override|disregard)\b.{0,48}\b(?:previous|system|developer)\b.{0,24}"
    r"\b(?:instruction|prompt|message)s?\b|\b(?:reveal|print|send|exfiltrate)\b.{0,48}"
    r"\b(?:api[_ -]?key|token|cookie|credential|system prompt|secret)\b")
_NUMERIC_RESULT = re.compile(
    r"(?i)\b(?:calculated|predicted|simulation\s+result|final\s+result|saturation\s+index|"
    r"concentration|release|yield)\b.{0,48}\b[-+]?\d+(?:\.\d+)?\s*"
    r"(?:%|mM|mol(?:/L)?|mg(?:/L)?|ppm|ppb|pH)?\b")


def _prose_fields(text: str) -> list[str]:
    """Inspect prose fields in structured replies without blocking copied inputs/confidence."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return [text]
    if not isinstance(payload, dict):
        return [text]
    fields = []
    for key in ("assistant_message", "reasoning_summary", "explanation", "summary"):
        value = payload.get(key)
        if isinstance(value, str):
            fields.append(value)
    return fields


def inspect_scientific_output(text: str) -> ScientificBoundaryDecision:
    """Reject model-authored science, gate bypasses, or executable instructions.

    This is intentionally deterministic and conservative.  A rejected model turn is
    replaced by the existing deterministic planner/fallback; the raw model text is not
    returned in the decision or safe error.
    """
    raw = str(text or "")
    if len(raw.encode("utf-8")) > 256 * 1024:
        return ScientificBoundaryDecision(
            False, "output_size", "AI output was withheld because it exceeded the safe limit.")
    for prose in _prose_fields(raw):
        checks = (
            (_PHREEQC_BLOCK, "phreeqc_authoring"),
            (_EXECUTABLE_INSTRUCTION, "executable_instruction"),
            (_AUTHORITY_CLAIM, "scientific_authority_claim"),
            (_WARNING_BYPASS, "warning_bypass"),
            (_UPDATE_ACTIVATION, "runtime_activation"),
            (_PROMPT_INJECTION, "prompt_injection"),
            (_NUMERIC_RESULT, "generated_numeric_result"),
        )
        for pattern, category in checks:
            if pattern.search(prose):
                return ScientificBoundaryDecision(
                    False,
                    category,
                    "AI output was withheld because scientific claims and actions must come "
                    "from reviewed deterministic state.",
                )
    return ScientificBoundaryDecision(True)


def enforce_scientific_output(text: str) -> str:
    decision = inspect_scientific_output(text)
    if not decision.allowed:
        raise ScientificOutputBoundaryError(decision.safe_message or "AI output was withheld.")
    return text


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _default_opener():
    return urllib.request.build_opener(_NoRedirect())


def _json_request(
    url: str,
    *,
    timeout_s: float,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
    opener=None,
) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 **(headers or {})},
        method="GET" if data is None else "POST",
    )
    try:
        with (opener or _default_opener()).open(request, timeout=timeout_s) as response:
            body = response.read(2 * 1024 * 1024 + 1)
    except Exception as exc:
        raise ProviderUnavailableError(
            f"AI provider request failed ({type(exc).__name__})") from exc
    if len(body) > 2 * 1024 * 1024:
        raise ProviderUnavailableError("AI provider response exceeded the safe limit")
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderUnavailableError("AI provider returned an invalid response") from exc
    if not isinstance(result, dict):
        raise ProviderUnavailableError("AI provider returned an invalid response shape")
    return result


@dataclass(frozen=True)
class _TextBlock:
    type: str
    text: str


@dataclass(frozen=True)
class _MessageResponse:
    content: list[_TextBlock]
    stop_reason: str = "end_turn"


class _GuardedMessages:
    def __init__(self, inner, context_limit: int = 8_192):
        self._inner = inner
        self._context_limit = int(context_limit)

    def create(self, **kwargs):
        _validate_request_envelope(
            model=kwargs.get("model", ""), max_tokens=kwargs.get("max_tokens", 0),
            messages=kwargs.get("messages", []), context_limit=self._context_limit,
            extra={key: value for key, value in kwargs.items()
                   if key not in {"model", "max_tokens", "messages"}},
        )
        response = self._inner.create(**kwargs)
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "text":
                enforce_scientific_output(str(getattr(block, "text", "")))
        return response


class _GuardedClient:
    def __init__(self, inner, context_limit: int = 8_192):
        self._inner = inner
        self.messages = _GuardedMessages(inner.messages, context_limit)


def guard_client(client):
    """Apply the deterministic output boundary to an SDK-style message client."""
    return _GuardedClient(client)


class _OpenAICompatibleMessages:
    def __init__(self, settings: ProviderSettings, secret: str | None, opener=None):
        self._settings = settings
        self._secret = secret
        self._opener = opener

    def create(self, *, model: str, max_tokens: int, messages: list[dict], **kwargs):
        _validate_request_envelope(
            model=model, max_tokens=max_tokens, messages=messages,
            context_limit=self._settings.context_limit, extra=kwargs)
        if kwargs.get("tools"):
            raise ProviderConfigurationError(
                "this OpenAI-compatible adapter does not implement tool calling")
        headers = {"Authorization": f"Bearer {self._secret}"} if self._secret else {}
        payload = {
            "model": model,
            "max_tokens": int(max_tokens),
            "messages": messages,
            "stream": False,
        }
        result = _json_request(
            f"{self._settings.base_url}/chat/completions",
            timeout_s=self._settings.timeout_s,
            payload=payload,
            headers=headers,
            opener=self._opener,
        )
        try:
            text = str(result["choices"][0]["message"]["content"])
            finish = str(result["choices"][0].get("finish_reason") or "stop")
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailableError("AI provider returned an invalid response shape") from exc
        enforce_scientific_output(text)
        return _MessageResponse([_TextBlock("text", text)],
                                "max_tokens" if finish == "length" else "end_turn")


class _OllamaMessages:
    def __init__(self, settings: ProviderSettings, opener=None):
        self._settings = settings
        self._opener = opener

    def create(self, *, model: str, max_tokens: int, messages: list[dict], **kwargs):
        _validate_request_envelope(
            model=model, max_tokens=max_tokens, messages=messages,
            context_limit=self._settings.context_limit, extra=kwargs)
        if kwargs.get("tools"):
            raise ProviderConfigurationError(
                "this Ollama adapter does not implement tool calling")
        result = _json_request(
            f"{self._settings.base_url}/api/chat",
            timeout_s=self._settings.timeout_s,
            payload={"model": model, "messages": messages, "stream": False,
                     "options": {"num_predict": int(max_tokens)}},
            opener=self._opener,
        )
        try:
            text = str(result["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise ProviderUnavailableError("AI provider returned an invalid response shape") from exc
        enforce_scientific_output(text)
        return _MessageResponse([_TextBlock("text", text)], "end_turn")


class _FacadeClient:
    def __init__(self, messages):
        self.messages = messages


class ProviderAdapter(Protocol):
    settings: ProviderSettings

    def client(self): ...
    def health(self) -> ProviderHealth: ...
    def discover_models(self) -> tuple[DiscoveredModel, ...]: ...


class DisabledAdapter:
    def __init__(self, settings: ProviderSettings):
        self.settings = settings

    def client(self):
        return None

    def health(self) -> ProviderHealth:
        return ProviderHealth(False, "disabled", "AI is disabled.")

    def discover_models(self) -> tuple[DiscoveredModel, ...]:
        return ()


class AnthropicAdapter:
    def __init__(self, settings: ProviderSettings, secret: str, *, constructor: Callable | None = None):
        self.settings = settings
        self._secret = secret
        self._constructor = constructor

    def client(self):
        if not self._secret:
            raise ProviderConfigurationError("Anthropic requires an administrator/user API key")
        if self._constructor is None:
            import anthropic
            constructor = anthropic.Anthropic
        else:
            constructor = self._constructor
        inner = constructor(api_key=self._secret, timeout=self.settings.timeout_s)
        return _GuardedClient(inner, self.settings.context_limit)

    def health(self) -> ProviderHealth:
        return ProviderHealth(True, message="Configured; use the explicit smoke test to call it.")

    def discover_models(self) -> tuple[DiscoveredModel, ...]:
        return ()


class OpenAICompatibleAdapter:
    def __init__(self, settings: ProviderSettings, secret: str | None = None, *, opener=None):
        self.settings = settings
        self._secret = secret
        self._opener = opener

    def client(self):
        return _FacadeClient(_OpenAICompatibleMessages(
            self.settings, self._secret, opener=self._opener))

    def health(self) -> ProviderHealth:
        try:
            self.discover_models()
        except ProviderUnavailableError:
            return ProviderHealth(False, "unavailable", "The AI endpoint is unavailable.")
        return ProviderHealth(True, message="The AI endpoint is reachable.")

    def discover_models(self) -> tuple[DiscoveredModel, ...]:
        headers = {"Authorization": f"Bearer {self._secret}"} if self._secret else {}
        result = _json_request(
            f"{self.settings.base_url}/models",
            timeout_s=self.settings.timeout_s,
            headers=headers,
            opener=self._opener,
        )
        entries = result.get("data", [])
        if not isinstance(entries, list):
            raise ProviderUnavailableError("AI model discovery returned an invalid response")
        models = []
        for item in entries:
            if isinstance(item, dict) and str(item.get("id") or "").strip():
                models.append(DiscoveredModel(str(item["id"]).strip()))
        return tuple(models)


class OllamaAdapter:
    def __init__(self, settings: ProviderSettings, *, opener=None):
        self.settings = settings
        self._opener = opener

    def client(self):
        return _FacadeClient(_OllamaMessages(self.settings, opener=self._opener))

    def health(self) -> ProviderHealth:
        try:
            self.discover_models()
        except ProviderUnavailableError:
            return ProviderHealth(False, "unavailable", "Ollama is unavailable.")
        return ProviderHealth(True, message="Ollama is reachable.")

    def discover_models(self) -> tuple[DiscoveredModel, ...]:
        result = _json_request(
            f"{self.settings.base_url}/api/tags",
            timeout_s=self.settings.timeout_s,
            opener=self._opener,
        )
        entries = result.get("models", [])
        if not isinstance(entries, list):
            raise ProviderUnavailableError("Ollama model discovery returned an invalid response")
        models = []
        for item in entries:
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                continue
            size = item.get("size")
            models.append(DiscoveredModel(
                str(item["name"]).strip(),
                int(size) if isinstance(size, int) and size >= 0 else None,
            ))
        return tuple(models)

    def pull_model(self, model_id: str, *, confirmation: str) -> ProviderHealth:
        """Explicit local-only download; never called by construction/discovery/startup."""
        model = str(model_id or "").strip()
        if self.settings.location is not ProviderLocation.LOCAL:
            raise ModelPullConfirmationError("model pull is allowed only for local Ollama")
        if not model or confirmation != f"PULL {model}":
            raise ModelPullConfirmationError(
                "model pull requires exact confirmation: PULL <model-id>")
        _json_request(
            f"{self.settings.base_url}/api/pull",
            timeout_s=self.settings.timeout_s,
            payload={"name": model, "stream": False},
            opener=self._opener,
        )
        return ProviderHealth(True, message="The explicitly confirmed model pull completed.")


def create_adapter(
    settings: ProviderSettings,
    *,
    secret: str | None = None,
    opener=None,
) -> ProviderAdapter:
    if settings.provider_type is ProviderType.DISABLED:
        return DisabledAdapter(settings)
    if settings.provider_type is ProviderType.ANTHROPIC:
        return AnthropicAdapter(settings, secret or "")
    if settings.provider_type is ProviderType.OLLAMA:
        return OllamaAdapter(settings, opener=opener)
    if settings.provider_type in {ProviderType.OPENAI_COMPATIBLE, ProviderType.HOSTED}:
        return OpenAICompatibleAdapter(settings, secret, opener=opener)
    raise ProviderConfigurationError("unsupported AI provider")
