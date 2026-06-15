"""Tests for the AI configuration authority (ai/config.py).

No network, no real key, no running Streamlit. Pins: disabled-without-key, env detection
that never exposes the value, a secrets-backed deployment (simulated), model fallback +
precedence, the missing-SDK path (disabled, not a crash), and runtime overrides.
"""
from __future__ import annotations

import pytest

from flyash_phreeqc_ml.ai import config as ai_config

# A sentinel "key" — assertions check it never appears in any displayed/serialised surface.
SECRET = "sk-ant-SECRETVALUE-do-not-leak-123"


@pytest.fixture(autouse=True)
def _clean_ai_env(monkeypatch):
    """Every test starts env-only: no key, no overrides, no Streamlit secrets."""
    for name in (ai_config.API_KEY_ENV, ai_config.MODEL_ENV, ai_config.PROVIDER_ENV):
        monkeypatch.delenv(name, raising=False)
    ai_config.clear_runtime_overrides()
    # Outside a Streamlit runtime this already returns None; pin it so a stray secrets.toml
    # on the dev machine can never make these tests flaky. Individual tests re-patch it.
    monkeypatch.setattr(ai_config, "_secrets_get", lambda name: None)
    yield
    ai_config.clear_runtime_overrides()


# --------------------------------------------------------------------------- #
# Disabled without a key
# --------------------------------------------------------------------------- #
def test_no_key_means_disabled():
    cfg = ai_config.resolve_config()
    assert cfg.key_present is False
    assert cfg.key_source == ai_config.SOURCE_NONE
    assert cfg.enabled is False
    assert ai_config.is_enabled() is False
    assert cfg.disabled_reason()                       # a non-empty, key-free reason
    assert SECRET not in cfg.disabled_reason()


def test_detect_api_key_none_when_absent():
    key, source = ai_config.detect_api_key()
    assert key is None and source == ai_config.SOURCE_NONE


def test_enabled_with_key_and_sdk(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    monkeypatch.setattr(ai_config, "sdk_available", lambda: True)
    cfg = ai_config.resolve_config()
    assert cfg.enabled is True
    assert cfg.disabled_reason() is None


# --------------------------------------------------------------------------- #
# Env key detected, never exposed
# --------------------------------------------------------------------------- #
def test_env_key_detected_from_env(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    key, source = ai_config.detect_api_key()
    assert key == SECRET                               # internal resolution still sees it
    assert source == ai_config.SOURCE_ENV
    cfg = ai_config.resolve_config()
    assert cfg.key_present is True
    assert cfg.key_source == ai_config.SOURCE_ENV
    assert cfg.key_display == f"detected ({ai_config.SOURCE_ENV})"


def test_config_never_exposes_key(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    cfg = ai_config.resolve_config()
    # Not in any safe surface, and there is no key field on the dataclass.
    assert SECRET not in cfg.key_display
    assert SECRET not in cfg.status_line()
    assert SECRET not in repr(cfg)
    assert SECRET not in str(cfg.to_safe_dict())
    assert not hasattr(cfg, "api_key")
    assert "api_key" not in cfg.to_safe_dict()


# --------------------------------------------------------------------------- #
# Streamlit secrets (simulated) + precedence
# --------------------------------------------------------------------------- #
def test_secrets_backed_key_detected(monkeypatch):
    monkeypatch.setattr(ai_config, "_secrets_get",
                        lambda name: SECRET if name == ai_config.API_KEY_ENV else None)
    key, source = ai_config.detect_api_key()
    assert key == SECRET
    assert source == ai_config.SOURCE_SECRETS
    cfg = ai_config.resolve_config()
    assert cfg.key_present and cfg.key_source == ai_config.SOURCE_SECRETS
    assert SECRET not in cfg.status_line()


def test_env_takes_precedence_over_secrets(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, "env-key")
    monkeypatch.setattr(ai_config, "_secrets_get",
                        lambda name: "secret-key" if name == ai_config.API_KEY_ENV else None)
    key, source = ai_config.detect_api_key()
    assert key == "env-key"                            # env wins (documented precedence)
    assert source == ai_config.SOURCE_ENV


def test_secrets_get_is_inert_without_streamlit_runtime(monkeypatch):
    # The real reader returns None when no Streamlit server is running (the test case).
    monkeypatch.setattr(ai_config, "_under_streamlit_runtime", lambda: False)
    assert ai_config._secrets_get(ai_config.API_KEY_ENV) is None


# --------------------------------------------------------------------------- #
# Model fallback + precedence
# --------------------------------------------------------------------------- #
def test_model_defaults_when_unset():
    assert ai_config.resolve_model() == ai_config.DEFAULT_MODEL


def test_model_from_env(monkeypatch):
    monkeypatch.setenv(ai_config.MODEL_ENV, "claude-sonnet-4-6")
    assert ai_config.resolve_model() == "claude-sonnet-4-6"


def test_model_secret_fallback_when_no_env(monkeypatch):
    monkeypatch.setattr(ai_config, "_secrets_get",
                        lambda name: "claude-sonnet-4-6" if name == ai_config.MODEL_ENV else None)
    assert ai_config.resolve_model() == "claude-sonnet-4-6"


def test_model_runtime_override_beats_env(monkeypatch):
    monkeypatch.setenv(ai_config.MODEL_ENV, "claude-sonnet-4-6")
    ai_config.set_runtime_overrides(model="claude-haiku-4-5-20251001")
    assert ai_config.resolve_model() == "claude-haiku-4-5-20251001"


def test_model_explicit_arg_beats_everything(monkeypatch):
    monkeypatch.setenv(ai_config.MODEL_ENV, "claude-sonnet-4-6")
    ai_config.set_runtime_overrides(model="claude-haiku-4-5-20251001")
    assert ai_config.resolve_model("claude-opus-4-8") == "claude-opus-4-8"


# --------------------------------------------------------------------------- #
# Missing SDK → disabled, not a crash
# --------------------------------------------------------------------------- #
def test_sdk_missing_disables_without_crash(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    monkeypatch.setattr(ai_config, "sdk_available", lambda: False)
    cfg = ai_config.resolve_config()
    assert cfg.sdk_available is False
    assert cfg.key_present is True
    assert cfg.enabled is False
    assert "anthropic" in cfg.disabled_reason().lower()
    assert ai_config.is_enabled() is False


def test_sdk_available_never_raises():
    # Whatever the environment, this must return a bool and not raise.
    assert isinstance(ai_config.sdk_available(), bool)


# --------------------------------------------------------------------------- #
# Runtime overrides + provider clamping
# --------------------------------------------------------------------------- #
def test_runtime_overrides_set_get_clear():
    ai_config.set_runtime_overrides(provider="anthropic", model="claude-haiku-4-5-20251001")
    assert ai_config.get_runtime_overrides() == {
        "provider": "anthropic", "model": "claude-haiku-4-5-20251001"}
    ai_config.clear_runtime_overrides()
    assert ai_config.get_runtime_overrides() == {}


def test_blank_override_clears_field():
    ai_config.set_runtime_overrides(model="claude-haiku-4-5-20251001")
    ai_config.set_runtime_overrides(model="")          # blank clears the field
    assert "model" not in ai_config.get_runtime_overrides()


def test_provider_defaults_and_clamps_unknown():
    assert ai_config.resolve_provider() == ai_config.DEFAULT_PROVIDER
    assert ai_config.resolve_provider("anthropic") == "anthropic"
    assert ai_config.resolve_provider("openai") == ai_config.DEFAULT_PROVIDER


def test_status_line_and_safe_dict_are_key_free(monkeypatch):
    monkeypatch.setenv(ai_config.API_KEY_ENV, SECRET)
    ai_config.set_runtime_overrides(model="claude-haiku-4-5-20251001")
    cfg = ai_config.resolve_config()
    assert cfg.model == "claude-haiku-4-5-20251001"
    line = cfg.status_line()
    assert ("enabled" in line) or ("disabled" in line)
    assert SECRET not in line
    d = cfg.to_safe_dict()
    assert d["key_present"] is True
    assert d["role"] == ai_config.AI_ROLE_LINE
    assert SECRET not in str(d)
