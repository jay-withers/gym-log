"""Security headers on every response.

Copied verbatim between career, finances and gym-log. Re-copy rather than
diverge, as for `settings.py`. The nightly passive DAST scan in
jay-withers/security-testing reports each of these when it is missing.

**Scripts are nonce-gated, styles are not.** Every inline `<script>` carries
`nonce="{{ request.state.csp_nonce }}"`, a fresh value per response, so an
injected script tag cannot run. A nonce cannot cover an inline `on*=`
attribute, so there are none: forms say `data-confirm` and the like, and a
nonced script in `base.html` handles them. Styles keep `'unsafe-inline'`
because the templates carry dozens of `style=""` attributes, and an injected
style is a far smaller risk than an injected script.

**HSTS is set here, not at the edge.** Container Apps ingress terminates TLS
but adds no `Strict-Transport-Security` of its own. Over plain http (a local
`make run`) browsers ignore the header, so it costs nothing there.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

STATIC_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    # Superseded by CSP `frame-ancestors`, but still the only clickjacking
    # defence an older browser understands.
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    # Everything is served from this one origin, so isolating it costs nothing.
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
}


def content_security_policy(nonce: str) -> str:
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self' 'unsafe-inline'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )
    )


def install(app: FastAPI) -> None:
    """Add the headers to every response, including errors and static files."""

    @app.middleware("http")
    async def _security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers.update(STATIC_HEADERS)
        response.headers["Content-Security-Policy"] = content_security_policy(nonce)
        return response
