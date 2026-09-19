"""The passcode gate.

This is reachable from the public internet the moment ingress is external, so the
gate defaults **on** and a missing passcode fails closed rather than open.

It is a shared passcode rather than Entra sign-in, and that is a considered
trade rather than laziness: Container Apps' built-in auth is not exposed by
`azurerm` and would need the `azapi` provider, which no repo in this estate uses,
in exchange for an Entra round trip on a phone between sets. The data behind this
gate is a list of how much someone lifted.

The session cookie is signed rather than encrypted — it carries no secret, only
an issue time, and the signature is what makes it unforgeable. Its key is derived
from the passcode when `COOKIE_SECRET` is unset, which has the useful property
that rotating the passcode invalidates every outstanding session for free.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

from fastapi import Cookie, HTTPException, status

from ..settings import secret, settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "gymlog_session"


def _signing_key() -> bytes:
    """The key the session cookie is signed with.

    Derived from the passcode rather than generated when `COOKIE_SECRET` is
    unset: a generated key would live in process memory, so every deploy — and
    every scale-from-zero — would silently log the phone out again.
    """
    configured = settings().cookie_secret
    if configured:
        return configured.encode("utf-8")
    return hashlib.sha256(secret("APP-PASSCODE").encode("utf-8")).digest()


def issue() -> str:
    """A signed session token for a browser that has presented the passcode."""
    issued = str(int(time.time()))
    signature = hmac.new(_signing_key(), issued.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{issued}.{signature}"


def valid(token: str | None) -> bool:
    """Whether `token` is one this process issued and has not expired."""
    if not token or "." not in token:
        return False
    issued, _, signature = token.partition(".")
    if not issued.isdigit():
        return False

    expected = hmac.new(_signing_key(), issued.encode("ascii"), hashlib.sha256).hexdigest()
    # Constant-time: a plain != leaks the matching prefix length to anyone
    # willing to time the responses.
    if not hmac.compare_digest(signature, expected):
        return False

    return time.time() - int(issued) < settings().cookie_max_age_seconds


def check_passcode(presented: str) -> bool:
    """Whether `presented` is the configured passcode."""
    expected = secret("APP-PASSCODE")
    return bool(expected) and hmac.compare_digest(presented, expected)


def require_session(gymlog_session: str | None = Cookie(default=None)) -> None:
    """Gate a page behind the passcode, redirecting to the login form if it is not met.

    A 303 rather than a 401: every consumer of this is an HTML page opened
    directly in a browser, and a browser shown a 401 renders the error rather
    than the login form.
    """
    if not settings().require_passcode:
        return
    if valid(gymlog_session):
        return
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        detail="passcode required",
        headers={"Location": "/login"},
    )
