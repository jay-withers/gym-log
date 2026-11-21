"""Configuration and secret resolution.

`Settings` is this app's own. Secret resolution comes from jay-withers/appkit
(`appkit.keyvault`), which replaced the copies of it in market-agent,
repo-agent, health, finances and career; fix a bug there, once. The wrappers
below bind it to these settings and cache it, so a test can clear them.

Every secret is read from an environment variable first, then `.env`, and only
then from Key Vault. That ordering is what makes `make run` work locally with no
Azure involved, and it is why Terraform manages no Container Apps Key Vault
reference: a revision carrying one hard-fails if the secret is absent, whereas
this resolves at runtime and reports what is missing.

The name mapping is mechanical: `secret("APP-PASSCODE")` reads `$APP_PASSCODE`,
then `.env`, then the Key Vault secret named `APP-PASSCODE`.
"""

from __future__ import annotations

from functools import cache, lru_cache
from typing import Any

from appkit import keyvault

# Re-exported: the tests (and conftest's cache clearing) reach them here.
from appkit.keyvault import KEY_VAULT_SCOPE as KEY_VAULT_SCOPE
from appkit.keyvault import StaticTokenCredential as StaticTokenCredential
from appkit.keyvault import dotenv as dotenv
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
    local_state_path: str = ".health.json"

    # The passcode gate is on unless explicitly disabled. It defaults *on* so
    # that forgetting to configure it fails closed — this is reachable from the
    # public internet the moment ingress is external.
    require_passcode: bool = True

    # Signs the session cookie. Absent means the key is derived from the passcode
    # instead (see `appkit.passcode`'s `signing_key`), because a per-process key
    # would log the phone out on every deploy and every scale-from-zero, and at
    # `min_replicas = 0` that is after every session. Setting this decouples the
    # two, so rotating the passcode no longer invalidates outstanding sessions.
    cookie_secret: str = ""

    # Thirty days. Long on purpose: this is opened on a phone in a gym, and a
    # login prompt between sets is the kind of friction that sends someone back
    # to the spreadsheet.
    cookie_max_age_seconds: int = 60 * 60 * 24 * 30

    # The weekly insight email (`health insight --email`, see mailer.py). FROM
    # must be on a domain verified in Resend; the recipient is the secret
    # INSIGHT-EMAIL-TO. PUBLIC_URL, when set, adds a link back to the
    # insights page.
    insight_email_from: str = ""
    public_url: str = ""

    # The nightly off-Azure backup (`health backup`, see backup.py). Only the
    # backup job is given these. The recipient is the owner's age public key,
    # whose private half never leaves their password manager; the R2 keys and
    # the drill key are secrets, resolved by name in backup.py.
    backup_bucket: str = ""
    backup_r2_endpoint: str = ""
    backup_age_recipient: str = ""
    log_level: str = Field(default="INFO")


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


@cache
def secret(name: str) -> str:
    """Resolve a secret by its hyphenated Key Vault name, raising if it is absent."""
    return keyvault.resolve(name, key_vault_uri=settings().key_vault_uri, credential=credential)


@cache
def optional_secret(name: str) -> str | None:
    """Resolve a secret that is allowed not to exist, returning None if it does not.

    An authentication or network error still raises: "the credential is broken"
    must not look like "not configured".
    """
    return keyvault.resolve_optional(
        name, key_vault_uri=settings().key_vault_uri, credential=credential
    )


@lru_cache(maxsize=1)
def credential() -> Any:
    """The credential for Key Vault and Blob Storage (`appkit.keyvault.make_credential`)."""
    cfg = settings()
    return keyvault.make_credential(
        keyvault_token=cfg.azure_keyvault_token, client_id=cfg.azure_client_id
    )
