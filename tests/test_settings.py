"""Secret resolution, which is copied code — so these guard the copy."""

from __future__ import annotations

import pytest

from gymlog.settings import optional_secret, secret, settings


def test_a_secret_comes_from_the_environment_first(monkeypatch):
    monkeypatch.setenv("APP_PASSCODE", "from-env")
    secret.cache_clear()
    assert secret("APP-PASSCODE") == "from-env"


def test_hyphens_map_to_underscores(monkeypatch):
    """Key Vault forbids underscores, env vars conventionally forbid hyphens."""
    monkeypatch.setenv("SOME_OTHER_VALUE", "x")
    secret.cache_clear()
    assert secret("SOME-OTHER-VALUE") == "x"


def test_a_missing_secret_with_no_vault_says_what_to_do(monkeypatch):
    monkeypatch.delenv("APP_PASSCODE", raising=False)
    secret.cache_clear()
    with pytest.raises(RuntimeError, match="az keyvault secret set"):
        secret("APP-PASSCODE")


def test_an_optional_secret_is_allowed_to_be_absent(monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    optional_secret.cache_clear()
    assert optional_secret("NOT-SET-ANYWHERE") is None


def test_the_passcode_gate_defaults_on():
    """It fails closed: this is reachable from the internet the moment ingress is."""
    assert settings().require_passcode is True
