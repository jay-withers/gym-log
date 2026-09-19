"""Configuration and secret resolution.

Copied from jay-withers/repo-agent src/repoagent/settings.py, which took it from
market-agent. The secret-resolution machinery below — `secret`, `optional_secret`,
`_from_environment`, `StaticTokenCredential`, `credential` and `dotenv` — is
unchanged. Re-copy rather than diverge; fix bugs in both.

**This is the third consumer.** repo-agent's CLAUDE.md sets the tripwire as "a
third consumer appears, *or* the same bug gets fixed twice". It has now fired. The
decision recorded in CLAUDE.md is to copy once more and extract a library the next
time a bug is fixed twice, rather than to pretend the rule was not tripped.

Every secret is read from an environment variable first, then `.env`, and only
then from Key Vault. That ordering is what makes `make run` work locally with no
Azure involved, and it is why Terraform manages no Container Apps Key Vault
reference: a revision carrying one hard-fails if the secret is absent, whereas
this resolves at runtime and reports what is missing.

The name mapping is mechanical: `secret("APP-PASSCODE")` reads `$APP_PASSCODE`,
then `.env`, then the Key Vault secret named `APP-PASSCODE`. Key Vault forbids
underscores in names, environment variables conventionally forbid hyphens, so one
of the two has to be rewritten.
"""

from __future__ import annotations

import os
import time
from functools import cache, lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Non-secret configuration. Anything in this class is safe in a log line."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "dev"

    key_vault_uri: str = ""
    # The managed identity's client id. Empty locally, which is the signal to
    # fall back to DefaultAzureCredential.
    azure_client_id: str = ""
    # A pre-fetched Key Vault access token, for a container that has no `az`.
    azure_keyvault_token: str = ""

    applicationinsights_connection_string: str = ""

    # Blob container holding the training log.
    #
    # Unlike repo-agent, where an empty value degrades to "everything is new",
    # empty here means the log lives in a local file instead — see store.py.
    # There is no mode in which this app runs without somewhere to write, because
    # the log *is* the product rather than commentary on it.
    #
    # Not a secret: it is a URL, and reaching it still needs the managed
    # identity's RBAC grant on the container.
    state_container_url: str = ""

    # Where the log goes when no container is configured. Only ever used locally.
    local_state_path: str = ".gymlog.json"

    # The passcode gate is on unless explicitly disabled. It defaults *on* so
    # that forgetting to configure it fails closed — this is reachable from the
    # public internet the moment ingress is external.
    require_passcode: bool = True

    # Signs the session cookie. Absent means the key is derived from the passcode
    # instead — see `deps._signing_key`, which explains why: a per-process key
    # would log the phone out on every deploy and every scale-from-zero, and at
    # `min_replicas = 0` that is after every session. Setting this decouples the
    # two, so rotating the passcode no longer invalidates outstanding sessions.
    cookie_secret: str = ""

    # Thirty days. Long on purpose: this is opened on a phone in a gym, and a
    # login prompt between sets is the kind of friction that sends someone back
    # to the spreadsheet.
    cookie_max_age_seconds: int = 60 * 60 * 24 * 30

    log_level: str = Field(default="INFO")


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def dotenv() -> dict[str, str]:
    """`.env` as a plain dict, or empty when there is no such file.

    Read with the same parser `Settings` uses, so a value quoted for one is
    quoted for the other.

    Resolved relative to the working directory rather than to this file: `.env`
    belongs to whoever is running the command, and in the image there is none.
    """
    from dotenv import dotenv_values

    return {key: value for key, value in dotenv_values(".env").items() if value is not None}


@cache
def secret(name: str) -> str:
    """Resolve a secret by its hyphenated Key Vault name."""
    from_env = _from_environment(name)
    if from_env:
        return from_env

    uri = settings().key_vault_uri
    if not uri:
        raise RuntimeError(
            f"{name} is not set and no KEY_VAULT_URI is configured. "
            f"Set ${name.replace('-', '_').upper()} locally, or "
            f"`az keyvault secret set --name {name}` for a deployed environment."
        )

    # Imported here rather than at module scope so the model, the progression
    # rule and the tests never need the Azure SDK installed or a credential.
    from azure.keyvault.secrets import SecretClient

    client = SecretClient(vault_url=uri, credential=credential())
    return client.get_secret(name).value or ""


@cache
def optional_secret(name: str) -> str | None:
    """Resolve a secret that is allowed not to exist, returning None if it does not.

    **Absence is not the same as failure.** A missing env var, no vault
    configured, or a secret that is not in the vault all return None; an
    authentication or network error propagates, because "the credential is
    broken" must not look like "not configured".
    """
    from_env = _from_environment(name)
    if from_env:
        return from_env

    uri = settings().key_vault_uri
    if not uri:
        return None

    from azure.core.exceptions import ResourceNotFoundError
    from azure.keyvault.secrets import SecretClient

    client = SecretClient(vault_url=uri, credential=credential())
    try:
        return client.get_secret(name).value or None
    except ResourceNotFoundError:
        return None


def _from_environment(name: str) -> str | None:
    """A secret from the process environment, then `.env`.

    The real environment wins, so an inline `FOO=bar make run` overrides a
    checked-out `.env` rather than being silently ignored by it.
    """
    variable = name.replace("-", "_").upper()
    return os.environ.get(variable) or dotenv().get(variable)


# The scope a Key Vault data-plane token is issued for. `az` calls the same
# thing `--resource https://vault.azure.net`.
KEY_VAULT_SCOPE = "https://vault.azure.net/.default"


class StaticTokenCredential:
    """A credential wrapping one pre-fetched access token.

    Exists for the containerised local loop. The application image carries no
    `az`, so `DefaultAzureCredential` has nothing to fall back to. Passing a
    token in is strictly better than writing secrets to a `.env`: it expires in
    about an hour and is scoped to Key Vault alone.

    Never used in Azure, where the managed identity is available directly.
    """

    def __init__(self, token: str, scope: str = KEY_VAULT_SCOPE) -> None:
        self._token = token
        self._scope = scope
        # az does not report the expiry in a form worth parsing here, and the
        # SDK only uses this to decide whether to refresh — which this
        # credential cannot do. An hour matches the real lifetime; an expired
        # token then fails as a 401 from Key Vault, which is the honest outcome.
        self._expires_on = int(time.time()) + 3600

    def _check(self, scopes: tuple[str, ...]) -> None:
        """Refuse a scope this token was not issued for."""
        if scopes and self._scope not in scopes:
            raise ValueError(f"this token is scoped to {self._scope}, not {', '.join(scopes)}")

    def get_token(self, *scopes: str, **_kwargs: Any) -> Any:
        from azure.core.credentials import AccessToken

        self._check(scopes)
        return AccessToken(self._token, self._expires_on)

    def get_token_info(self, *scopes: str, **_kwargs: Any) -> Any:
        """The newer protocol; some SDK versions call this instead."""
        from azure.core.credentials import AccessTokenInfo

        self._check(scopes)
        return AccessTokenInfo(self._token, self._expires_on)


@lru_cache(maxsize=1)
def credential():
    """The credential for Key Vault and Blob Storage.

    Three cases, in priority order:

    1. A pre-fetched Key Vault token from the environment — the containerised
       local loop, where there is no `az` to fall back to.
    2. The user-assigned identity, named explicitly. In Azure this must be
       explicit: `DefaultAzureCredential` would find the same identity
       eventually, but a workload with more than one identity attached picks
       unpredictably, and the failure looks like a permissions problem rather
       than a wrong-identity one.
    3. `DefaultAzureCredential`, which picks up a developer's `az login`.
    """
    cfg = settings()

    if cfg.azure_keyvault_token:
        return StaticTokenCredential(cfg.azure_keyvault_token)

    if cfg.azure_client_id:
        from azure.identity import ManagedIdentityCredential

        return ManagedIdentityCredential(client_id=cfg.azure_client_id)

    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()
