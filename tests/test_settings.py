"""Secret resolution, which is copied code — so these guard the copy."""

from __future__ import annotations

import time
from typing import ClassVar

import azure.identity
import azure.keyvault.secrets
import pytest
from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError

from gymlog import settings as settings_module
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


# --- Key Vault ---------------------------------------------------------------
#
# The vault is faked at the SDK client, because the fall-through to it is the
# half of this module that a developer's machine never exercises: locally every
# secret is answered by the environment before the vault is ever consulted.


class _FakeSecret:
    def __init__(self, value: str | None) -> None:
        self.value = value


class _FakeVault:
    """The `SecretClient` surface `settings` uses, plus a record of how it was built.

    What the vault holds and what it raises are class attributes because the
    client is constructed inside `secret()` — a test has no instance to arm
    before the call it is testing. The fixture resets both.
    """

    built: ClassVar[list[_FakeVault]] = []
    secrets: ClassVar[dict[str, str | None]] = {}
    raises: ClassVar[Exception | None] = None

    def __init__(self, vault_url: str, credential: object) -> None:
        self.vault_url = vault_url
        self.credential = credential
        self.requested: list[str] = []
        _FakeVault.built.append(self)

    def get_secret(self, name: str) -> _FakeSecret:
        self.requested.append(name)
        if self.raises is not None:
            raise self.raises
        if name not in self.secrets:
            raise ResourceNotFoundError(f"no secret named {name}")
        return _FakeSecret(self.secrets[name])


@pytest.fixture
def vault(monkeypatch):
    """Configure a vault and hand back the clients built against it."""
    monkeypatch.setenv("KEY_VAULT_URI", "https://kv-gymlog-dev.vault.azure.net/")
    monkeypatch.delenv("APP_PASSCODE", raising=False)
    settings.cache_clear()
    secret.cache_clear()
    optional_secret.cache_clear()
    settings_module.credential.cache_clear()
    # A pre-fetched token, so resolving the credential stays offline: asking for
    # a real one would make the suite pass or fail on whether the machine
    # running it happens to be logged in to Azure.
    monkeypatch.setenv("AZURE_KEYVAULT_TOKEN", "a-token")
    monkeypatch.setattr(azure.keyvault.secrets, "SecretClient", _FakeVault)
    monkeypatch.setattr(_FakeVault, "built", [])
    monkeypatch.setattr(_FakeVault, "secrets", {"APP-PASSCODE": "from-the-vault"})
    monkeypatch.setattr(_FakeVault, "raises", None)
    return _FakeVault.built


def test_a_secret_falls_through_to_the_vault(vault):
    """The deployed path: nothing in the environment, so the vault answers."""
    assert secret("APP-PASSCODE") == "from-the-vault"
    assert vault[0].vault_url == "https://kv-gymlog-dev.vault.azure.net/"
    assert isinstance(vault[0].credential, settings_module.StaticTokenCredential)
    # The hyphenated name goes to the vault as it stands: only the environment
    # lookup rewrites it, because Key Vault forbids underscores in names.
    assert vault[0].requested == ["APP-PASSCODE"]


def test_the_environment_still_wins_over_a_configured_vault(vault, monkeypatch):
    """What makes `make run` and a one-off override work with a vault configured."""
    monkeypatch.setenv("APP_PASSCODE", "from-env")
    secret.cache_clear()
    assert secret("APP-PASSCODE") == "from-env"
    assert vault == []


def test_a_secret_is_resolved_once_and_cached(vault):
    """`lifespan` leans on this: the passcode is fetched at start-up, not per request."""
    assert secret("APP-PASSCODE") == secret("APP-PASSCODE")
    assert len(vault) == 1


def test_an_optional_secret_missing_from_the_vault_is_none(vault):
    assert optional_secret("NOT-IN-THE-VAULT") is None


def test_a_broken_credential_is_not_reported_as_an_absent_secret(vault, monkeypatch):
    """The distinction the docstring draws, and the one worth a test.

    "Not configured" is a normal state and returns None. "The identity cannot
    authenticate" is a deployment fault, and swallowing it would present as a
    feature quietly not being on.
    """
    monkeypatch.setattr(_FakeVault, "raises", ClientAuthenticationError("no managed identity"))
    with pytest.raises(ClientAuthenticationError):
        optional_secret("APP-PASSCODE")


# --- the credential ----------------------------------------------------------


def test_a_pre_fetched_token_wins(monkeypatch):
    """The containerised local loop: the image has no `az` to fall back to."""
    monkeypatch.setenv("AZURE_KEYVAULT_TOKEN", "a-token")
    monkeypatch.setenv("AZURE_CLIENT_ID", "an-identity")
    settings.cache_clear()
    settings_module.credential.cache_clear()

    assert isinstance(settings_module.credential(), settings_module.StaticTokenCredential)


def test_the_identity_is_named_explicitly_when_there_is_one(monkeypatch):
    """`DefaultAzureCredential` picks unpredictably when several are attached.

    The failure then reads as a permissions problem rather than as the wrong
    identity, which is why this is never left to the default.
    """
    named: list[str] = []
    monkeypatch.setattr(
        azure.identity, "ManagedIdentityCredential", lambda client_id: named.append(client_id)
    )
    monkeypatch.setenv("AZURE_CLIENT_ID", "the-user-assigned-identity")
    settings.cache_clear()
    settings_module.credential.cache_clear()

    settings_module.credential()
    assert named == ["the-user-assigned-identity"]


def test_it_falls_back_to_the_developers_az_login(monkeypatch):
    built: list[str] = []
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda: built.append("default"))
    settings.cache_clear()
    settings_module.credential.cache_clear()

    settings_module.credential()
    assert built == ["default"]


def test_the_static_token_refuses_a_scope_it_was_not_issued_for():
    """It cannot refresh, so a token used against the wrong resource would 401 obscurely."""
    credential = settings_module.StaticTokenCredential("a-token")
    assert credential.get_token(settings_module.KEY_VAULT_SCOPE).token == "a-token"
    assert credential.get_token_info(settings_module.KEY_VAULT_SCOPE).token == "a-token"
    with pytest.raises(ValueError, match="scoped to"):
        credential.get_token("https://storage.azure.com/.default")


def test_the_static_token_carries_an_expiry_the_sdk_will_accept():
    """Zero would make every SDK call treat it as expired before sending it."""
    credential = settings_module.StaticTokenCredential("a-token")
    assert credential.get_token().expires_on > time.time()


# --- .env --------------------------------------------------------------------


def test_a_secret_comes_from_dotenv_when_the_environment_is_silent(monkeypatch, tmp_path):
    """`.env` belongs to whoever runs the command, so it is read from the cwd."""
    (tmp_path / ".env").write_text("APP_PASSCODE=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_PASSCODE", raising=False)
    settings_module.dotenv.cache_clear()
    secret.cache_clear()

    assert secret("APP-PASSCODE") == "from-dotenv"


def test_the_real_environment_overrides_a_checked_out_dotenv(monkeypatch, tmp_path):
    """`APP_PASSCODE=x make run` must not be silently ignored by a `.env` on disk."""
    (tmp_path / ".env").write_text("APP_PASSCODE=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_PASSCODE", "from-env")
    settings_module.dotenv.cache_clear()
    secret.cache_clear()

    assert secret("APP-PASSCODE") == "from-env"


def test_no_dotenv_is_not_an_error(monkeypatch, tmp_path):
    """There is none in the image, and that is the ordinary case rather than a fault."""
    monkeypatch.chdir(tmp_path)
    settings_module.dotenv.cache_clear()
    assert settings_module.dotenv() == {}


def test_an_optional_secret_present_in_the_environment_needs_no_vault():
    """The local loop: nothing optional should force a vault to be configured."""
    optional_secret.cache_clear()
    assert optional_secret("APP-PASSCODE") == "test-passcode"
