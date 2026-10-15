"""Security headers on every response.

Copied verbatim between career, finances and gym-log. Re-copy rather than
diverge, as for `settings.py`. The nightly passive DAST scan in
jay-withers/security-testing reports each of these when it is missing.

**Scripts are nonce-gated, styles are not.** Every inline `<script>` carries
`nonce="{{ request.state.csp_nonce }}"`, a fresh value per response, so an
injected script tag cannot run. A nonce cannot cover an inline `on*=`
attribute, so templates must have none; behaviour that needs one belongs in a
nonced script in `base.html`, keyed off a `data-*` attribute. Styles keep
`'unsafe-inline'` because the templates carry dozens of `style=""`
attributes, and an injected style is a far smaller risk than an injected
script.

**HSTS is set here, not at the edge.** Container Apps ingress terminates TLS
but adds no `Strict-Transport-Security` of its own. Over plain http (a local
`make run`) browsers ignore the header, so it costs nothing there.

**`CSP_FORM_ACTION` adds origins a form POST may redirect to**, space
separated. `form-action` applies to every hop of a redirect, so behind
Cloudflare Access an expired session bounces a submit to the Access team
domain and the browser blocks it. Read from the environment rather than
`settings.py` so this file stays identical across repos.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)

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


def content_security_policy(nonce: str, form_action: tuple[str, ...] = ()) -> str:
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self' 'unsafe-inline'",
            "object-src 'none'",
            "base-uri 'none'",
            " ".join(("form-action 'self'", *form_action)),
            "frame-ancestors 'none'",
        )
    )


def install(app: FastAPI) -> None:
    """Add the headers to every response, including errors and static files."""
    form_action = tuple(os.environ.get("CSP_FORM_ACTION", "").split())

    @app.middleware("http")
    async def _security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        try:
            response = await call_next(request)
        except Exception:
            # Starlette's ServerErrorMiddleware sits outside this one, so the
            # 500 it would build carries none of these headers. Build it here.
            logger.exception("unhandled error on %s %s", request.method, request.url.path)
            response = PlainTextResponse("Internal Server Error", status_code=500)
        response.headers.update(STATIC_HEADERS)
        response.headers["Content-Security-Policy"] = content_security_policy(nonce, form_action)
        return response
