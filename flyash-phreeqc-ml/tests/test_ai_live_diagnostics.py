"""Live-AI failure classification, safe diagnostics, and the smoke test (no network, no key).

Pins the post-demo live-AI debugging work:

* a live API failure is **classified** into a stable, sanitized category (auth / rate-limit /
  network / timeout / model / truncated …) instead of a silent generic "unavailable";
* the orchestrator threads that sanitized reason into the state + the one-turn fallback note,
  and **never disables** the live-AI toggle on a failure;
* a successful (mocked) call makes the assistant use live AI;
* a missing key falls back deterministically;
* the safe diagnostics + smoke test expose the model/config but **never** the API key value.

Every AI call is faked — no real request, no network, no key.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from flyash_phreeqc_ml.agent import agent_orchestrator as orch
from flyash_phreeqc_ml.agent import agent_state
from flyash_phreeqc_ml.ai import client as ai_client
from flyash_phreeqc_ml.ai import config as ai_config

FAKE_KEY = "sk-ant-TESTKEY-not-real-do-not-leak-diag-0002"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
# Anthropic-like exception classes (classify_exception keys off class names + status_code).
class AuthenticationError(Exception):
    status_code = 401


class RateLimitError(Exception):
    status_code = 429


class APIConnectionError(Exception):
    pass


class APITimeoutError(APIConnectionError):
    pass


class NotFoundError(Exception):
    status_code = 404


class BadRequestError(Exception):
    status_code = 400


class InternalServerError(Exception):
    status_code = 500


class _RaisingClient:
    """A client whose messages.create always raises a given exception."""

    def __init__(self, exc):
        self._exc = exc
        self.messages = self

    def create(self, **kwargs):
        raise self._exc


class _ReplyClient:
    """A client whose messages.create returns one scripted text + stop_reason."""

    def __init__(self, text, stop_reason="end_turn"):
        self._text = text
        self._stop = stop_reason
        self.messages = self

    def create(self, **kwargs):
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=self._text)], stop_reason=self._stop)


def _valid_payload():
    return json.dumps({"assistant_message": "ok", "reasoning_summary": "ok", "confidence": 0.8,
                       "understanding": {"material_name": "fly ash", "leachant_type": "NaOH"},
                       "action": {"action_name": "ASK_USER"}})


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    """Start every test with no env key / secrets (injected clients bypass the key check)."""
    for name in (ai_config.API_KEY_ENV, ai_config.MODEL_ENV, ai_config.PROVIDER_ENV):
        monkeypatch.delenv(name, raising=False)
    ai_config.clear_runtime_overrides()
    monkeypatch.setattr(ai_config, "_secrets_get", lambda name: None)
    yield
    ai_config.clear_runtime_overrides()


# --------------------------------------------------------------------------- #
# classify_exception → stable, sanitized categories
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("exc,expected", [
    (AuthenticationError("bad key"), ai_client.CALL_AUTH),
    (RateLimitError("slow down"), ai_client.CALL_RATE_LIMIT),
    (APITimeoutError("timed out"), ai_client.CALL_TIMEOUT),
    (APIConnectionError("no route"), ai_client.CALL_NETWORK),
    (NotFoundError("no model"), ai_client.CALL_MODEL),
    (BadRequestError("bad arg"), ai_client.CALL_BAD_REQUEST),
    (InternalServerError("boom"), ai_client.CALL_SERVER),
    (ValueError("???"), ai_client.CALL_UNKNOWN),
])
def test_classify_exception_categories(exc, expected):
    cat, msg = ai_client.classify_exception(exc)
    assert cat == expected
    assert isinstance(msg, str) and msg
    # The message is built from the type name + status only — never the raw exception text.
    assert "bad key" not in msg and "no route" not in msg


def test_classify_exception_never_contains_key():
    cat, msg = ai_client.classify_exception(AuthenticationError(f"key={FAKE_KEY} rejected"))
    assert cat == ai_client.CALL_AUTH
    assert FAKE_KEY not in msg


# --------------------------------------------------------------------------- #
# 1) key present + SDK + a mocked successful call → the assistant uses live AI
# --------------------------------------------------------------------------- #
def test_successful_mocked_call_uses_live_ai():
    s = agent_state.AgentState()
    r = orch.respond(s, "leach 2 g fly ash in 10 mL 0.5 M NaOH",
                     client=_ReplyClient(_valid_payload()), use_ai=True)
    assert r.used_ai is True
    assert s.last_ai_fell_back is False
    assert s.last_ai_error_type is None and s.last_ai_error_message is None


# --------------------------------------------------------------------------- #
# 2) mocked authentication error → one-turn fallback with the sanitized auth reason
# --------------------------------------------------------------------------- #
def test_auth_error_falls_back_with_sanitized_reason():
    s = agent_state.AgentState()
    r = orch.respond(s, "leach 2 g fly ash in 10 mL 0.5 M NaOH",
                     client=_RaisingClient(AuthenticationError(f"invalid key {FAKE_KEY}")),
                     use_ai=True)
    assert r.used_ai is False
    assert s.last_ai_fell_back is True
    assert s.last_ai_error_type == ai_client.CALL_AUTH
    assert "authentication failed" in (s.last_ai_error_message or "").lower()
    # The fallback note carries the stable prefix AND the specific reason; never the key.
    assert orch.AI_FALLBACK_NOTE in r.assistant_message
    assert "authentication failed" in r.assistant_message.lower()
    assert FAKE_KEY not in r.assistant_message


# --------------------------------------------------------------------------- #
# 3) rate-limit / network / model errors → one-turn fallback with a specific reason
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("exc,cat,needle", [
    (RateLimitError("429"), ai_client.CALL_RATE_LIMIT, "rate limited"),
    (APIConnectionError("net"), ai_client.CALL_NETWORK, "network error"),
    (NotFoundError("model"), ai_client.CALL_MODEL, "model not found"),
    (APITimeoutError("t/o"), ai_client.CALL_TIMEOUT, "timed out"),
])
def test_various_errors_fall_back_with_specific_reason(exc, cat, needle):
    s = agent_state.AgentState()
    r = orch.respond(s, "leach 2 g fly ash in 10 mL 0.5 M NaOH",
                     client=_RaisingClient(exc), use_ai=True)
    assert s.last_ai_fell_back is True
    assert s.last_ai_error_type == cat
    assert needle in (s.last_ai_error_message or "").lower()
    assert needle in r.assistant_message.lower()


def test_truncated_and_invalid_json_are_classified():
    # A non-JSON reply cut off at max_tokens → truncated.
    s = agent_state.AgentState()
    orch.respond(s, "leach fly ash naoh",
                 client=_ReplyClient("{ partial json …", stop_reason="max_tokens"), use_ai=True)
    assert s.last_ai_fell_back and s.last_ai_error_type == ai_client.CALL_TRUNCATED
    # A non-JSON reply that finished normally → invalid JSON.
    s2 = agent_state.AgentState()
    orch.respond(s2, "leach fly ash naoh",
                 client=_ReplyClient("Sure! Here is some prose, not JSON.", stop_reason="end_turn"),
                 use_ai=True)
    assert s2.last_ai_fell_back and s2.last_ai_error_type == ai_client.CALL_INVALID_JSON


# --------------------------------------------------------------------------- #
# 4) a failed AI call does NOT permanently disable live AI (next call recovers)
# --------------------------------------------------------------------------- #
def test_failure_does_not_disable_live_ai():
    s = agent_state.AgentState()
    orch.respond(s, "leach fly ash naoh", client=_RaisingClient(RateLimitError("429")), use_ai=True)
    assert s.last_ai_fell_back is True
    # The very next turn, with a working client, uses live AI again (nothing was disabled).
    r = orch.respond(s, "0.5 M, 2 g, 10 mL", client=_ReplyClient(_valid_payload()), use_ai=True)
    assert r.used_ai is True
    assert s.last_ai_fell_back is False
    assert s.last_ai_error_type is None       # the stale error is cleared on a clean live turn


# --------------------------------------------------------------------------- #
# 6) missing key → deterministic fallback (no AI attempted, no error, no crash)
# --------------------------------------------------------------------------- #
def test_missing_key_uses_deterministic_planner():
    s = agent_state.AgentState()
    r = orch.respond(s, "leach 2 g fly ash in 10 mL 0.5 M NaOH", use_ai=True)   # no client, no key
    assert r.used_ai is False
    assert s.last_ai_fell_back is False        # AI was never *attempted*, so it is not a fallback
    assert s.last_ai_error_type is None
    assert r.assistant_message                 # still a useful reply


# --------------------------------------------------------------------------- #
# Safe smoke test (same client path; sanitized; never the key)
# --------------------------------------------------------------------------- #
def test_smoke_test_success(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, FAKE_KEY)
    monkeypatch.setattr(ai_config, "sdk_available", lambda: True)
    monkeypatch.setattr(ai_client, "_construct_anthropic", lambda key: _ReplyClient("ok"))
    res = ai_client.smoke_test()
    assert res.ok is True and res.category is None
    assert res.model and FAKE_KEY not in json.dumps(res.to_safe_dict())


def test_smoke_test_classifies_failure(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, FAKE_KEY)
    monkeypatch.setattr(ai_config, "sdk_available", lambda: True)
    monkeypatch.setattr(ai_client, "_construct_anthropic",
                        lambda key: _RaisingClient(RateLimitError("429")))
    res = ai_client.smoke_test()
    assert res.ok is False and res.category == ai_client.CALL_RATE_LIMIT
    assert FAKE_KEY not in (res.message or "")


def test_smoke_test_no_key(monkeypatch):
    # Pin the (OPTIONAL) anthropic SDK present so this isolates the *no-key* path — matching
    # test_smoke_test_success / _classifies_failure. Without it, an env where anthropic is not
    # installed short-circuits to ERROR_NO_SDK before the key check this test asserts on.
    monkeypatch.setattr(ai_config, "sdk_available", lambda: True)
    res = ai_client.smoke_test()               # no key (the autouse fixture cleared it)
    assert res.ok is False and res.category == ai_client.ERROR_NO_KEY


# --------------------------------------------------------------------------- #
# 7) model/config is visible safely; the key value is NEVER visible
# --------------------------------------------------------------------------- #
def test_diagnostics_show_model_safely_never_key(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, FAKE_KEY)
    monkeypatch.setattr(ai_config, "sdk_available", lambda: True)
    diag = ai_config.diagnostics(toggle_on=True, last_ai_error_type=ai_client.CALL_AUTH,
                                 last_ai_error_message="authentication failed [AuthenticationError, HTTP 401]",
                                 fallback_this_turn=True)
    d = diag.to_safe_dict()
    assert d["key_present"] is True
    assert d["key_length"] == len(FAKE_KEY) and isinstance(d["key_length"], int)
    assert d["sdk_available"] is True
    assert d["selected_model"] == ai_config.resolve_config().model       # model visible
    assert d["live_ai_enabled"] is True and d["fallback_this_turn"] is True
    assert d["last_ai_error_type"] == ai_client.CALL_AUTH
    # The key value never appears anywhere in the snapshot.
    assert FAKE_KEY not in json.dumps(d)


def test_key_length_is_int_only(monkeypatch):
    assert ai_config.key_length() == 0                                   # no key
    monkeypatch.setenv(ai_config.API_KEY_ENV, FAKE_KEY)
    assert ai_config.key_length() == len(FAKE_KEY)


# --------------------------------------------------------------------------- #
# 5) no API key appears in messages, provenance, state, or committed source
# --------------------------------------------------------------------------- #
def test_no_key_in_messages_state_or_provenance(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, FAKE_KEY)
    s = agent_state.AgentState()
    r = orch.respond(s, "leach fly ash naoh",
                     client=_RaisingClient(AuthenticationError(f"key {FAKE_KEY} invalid")),
                     use_ai=True)
    blob = json.dumps([m.to_dict() for m in s.history]
                      + [e.to_dict() for e in s.provenance]
                      + [s.to_provenance_dict(),
                         {"err_type": s.last_ai_error_type, "err_msg": s.last_ai_error_message}])
    assert FAKE_KEY not in blob and FAKE_KEY not in r.assistant_message


def test_no_real_api_key_in_new_source():
    """The new diagnostics modules carry no real Anthropic key prefix (needle built from parts so
    this scanner never self-matches)."""
    needle = "sk-ant-" + "api03-"
    root = Path(__file__).resolve().parent.parent
    targets = [root / "flyash_phreeqc_ml" / "ai" / "client.py",
               root / "flyash_phreeqc_ml" / "ai" / "config.py",
               root / "flyash_phreeqc_ml" / "agent" / "nlu_extractor.py",
               root / "ui" / "settings.py", root / "ui" / "assistant_tab.py",
               Path(__file__)]
    for p in targets:
        assert needle not in p.read_text(encoding="utf-8", errors="ignore"), p.name
