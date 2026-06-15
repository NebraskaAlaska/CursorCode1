"""Tests for the safe AI client wrapper (ai/client.py).

Pins: no key → structured error (no crash); no SDK → structured error; an injected client
is returned as-is; a construction failure is controlled and **never leaks the key**; the
back-compat resolver returns ``None`` when disabled. No network; the constructor is mocked.
"""
from __future__ import annotations

import pytest

from flyash_phreeqc_ml.ai import client as ai_client
from flyash_phreeqc_ml.ai import config as ai_config

SECRET = "sk-ant-NEVER-LEAK-this-value-987"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (ai_config.API_KEY_ENV, ai_config.MODEL_ENV, ai_config.PROVIDER_ENV):
        monkeypatch.delenv(name, raising=False)
    ai_config.clear_runtime_overrides()
    monkeypatch.setattr(ai_config, "_secrets_get", lambda name: None)
    yield
    ai_config.clear_runtime_overrides()


class _Fake:
    """A stand-in for an injected client."""


def test_no_key_returns_structured_error(monkeypatch):
    monkeypatch.setattr(ai_config, "sdk_available", lambda: True)   # SDK present, key absent
    res = ai_client.get_client()
    assert res.ok is False
    assert res.client is None
    assert res.error == ai_client.ERROR_NO_KEY
    assert res.message and SECRET not in res.message
    assert res.config.enabled is False


def test_no_sdk_returns_structured_error(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    monkeypatch.setattr(ai_config, "sdk_available", lambda: False)
    res = ai_client.get_client()
    assert res.ok is False
    assert res.error == ai_client.ERROR_NO_SDK
    assert res.client is None
    assert SECRET not in (res.message or "")


def test_injected_client_returned_even_without_key():
    fake = _Fake()
    res = ai_client.get_client(injected=fake)
    assert res.ok is True
    assert res.client is fake
    assert res.error is None


def test_client_init_failure_is_controlled(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    monkeypatch.setattr(ai_config, "sdk_available", lambda: True)

    def _boom(api_key):
        # Even an exception text that embeds the key must not leak through.
        raise RuntimeError(f"init blew up with {api_key}")

    monkeypatch.setattr(ai_client, "_construct_anthropic", _boom)

    res = ai_client.get_client()
    assert res.ok is False
    assert res.client is None
    assert res.error == ai_client.ERROR_CLIENT_INIT
    assert "RuntimeError" in res.message            # the controlled message names the type…
    assert SECRET not in res.message                # …never the key


def test_get_client_builds_when_enabled(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    monkeypatch.setattr(ai_config, "sdk_available", lambda: True)
    sentinel = _Fake()
    captured = {}

    def _construct(api_key):
        captured["key"] = api_key
        return sentinel

    monkeypatch.setattr(ai_client, "_construct_anthropic", _construct)
    res = ai_client.get_client()
    assert res.ok is True
    assert res.client is sentinel
    assert captured["key"] == SECRET                # the key reaches only the constructor


def test_resolve_client_none_when_disabled(monkeypatch):
    monkeypatch.setattr(ai_config, "sdk_available", lambda: True)
    assert ai_client.resolve_client() is None        # no key → None (back-compat behaviour)


def test_resolve_client_returns_injected():
    fake = _Fake()
    assert ai_client.resolve_client(fake) is fake


def test_is_enabled_reflects_key_and_sdk(monkeypatch):
    monkeypatch.setattr(ai_config, "sdk_available", lambda: True)
    assert ai_client.is_enabled() is False           # no key
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    assert ai_client.is_enabled() is True
