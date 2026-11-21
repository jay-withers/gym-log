"""Fixtures shared by the suite.

Copied in spirit from jay-withers/repo-agent tests/conftest.py: the point of
`fake_secrets` is that no test can reach a real Key Vault or a real storage
account, however a developer's own environment happens to be configured.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, settings

from health import settings as settings_module

# Property tests (adapted from finances' conftest). `make test` locally runs
# Hypothesis' default 100 examples; CI (CI=true) runs 1000. No deadline: a
# shared runner's pauses would read as a flaky failure. The
# function-scoped-fixture check is off because `fake_secrets` is autouse, so
# every @given test would trip it; that is only safe while **a property test
# takes no fixtures**, since one instance would be shared by all its examples.
settings.register_profile(
    "dev", deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
# Built on Hypothesis' own `ci` profile rather than replacing it: that one is
# derandomized (the same examples every run, so a required check can't fail on
# one PR's draw and pass on its re-run) and prints a blob to reproduce a failure.
_builtin_ci = settings.get_profile("ci")
settings.register_profile(
    "ci",
    parent=_builtin_ci,
    max_examples=1000,
    suppress_health_check=[
        *_builtin_ci.suppress_health_check,
        HealthCheck.function_scoped_fixture,
    ],
)
settings.load_profile("ci" if os.environ.get("CI", "").lower() in {"true", "1"} else "dev")


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
