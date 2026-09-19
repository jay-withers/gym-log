"""Fixtures shared by the suite.

Copied in spirit from jay-withers/repo-agent tests/conftest.py: the point of
`fake_secrets` is that no test can reach a real Key Vault or a real storage
account, however a developer's own environment happens to be configured.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from gymlog import settings as settings_module


@pytest.fixture(autouse=True)
def fake_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    """Resolve every secret from the environment, never from Azure.

    `KEY_VAULT_URI` and `STATE_CONTAINER_URL` must stay empty: either one set
    would let a missing value fall through to a real network call, and the test
    would then pass or fail depending on whose laptop it ran on.
    """
    monkeypatch.setenv("APP_PASSCODE", "test-passcode")
    monkeypatch.delenv("KEY_VAULT_URI", raising=False)
    monkeypatch.delenv("STATE_CONTAINER_URL", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    # Same reasoning: a developer with this set would otherwise have the suite
    # configure a real exporter and ship spans.
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("LOCAL_STATE_PATH", str(tmp_path / "log.json"))

    _clear()
    yield
    _clear()


def _clear() -> None:
    settings_module.settings.cache_clear()
    settings_module.secret.cache_clear()
    settings_module.optional_secret.cache_clear()
    settings_module.dotenv.cache_clear()
    settings_module.credential.cache_clear()
