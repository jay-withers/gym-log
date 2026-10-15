"""Security headers (api/headers.py) on the pages a browser actually renders."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import gymlog
from gymlog.api import headers
from gymlog.api.main import create_app


@pytest.fixture
def anonymous():
    with TestClient(create_app(), base_url="https://testserver", follow_redirects=False) as entered:
        yield entered


@pytest.fixture
def client(anonymous):
    # https and a real login for the reasons in test_api.py's fixture.
    assert anonymous.post("/login", data={"passcode": "test-passcode"}).status_code in (200, 303)
    return anonymous


def _nonce(response) -> str:
    policy = response.headers["content-security-policy"]
    match = re.search(r"script-src 'self' 'nonce-([^']+)'", policy)
    assert match, policy
    return match.group(1)


def test_a_page_carries_every_security_header(client):
    response = client.get("/")

    assert response.status_code == 200
    for name, value in headers.STATIC_HEADERS.items():
        assert response.headers[name] == value
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_every_inline_script_carries_this_responses_nonce(client):
    """A script without the nonce is silently blocked in the browser, and
    nothing server-side would notice."""
    response = client.get("/")
    nonce = _nonce(response)

    scripts = re.findall(r"<script\b[^>]*>", response.text)
    assert scripts == [s for s in scripts if f'nonce="{nonce}"' in s]


def test_no_inline_event_handlers(client):
    """The CSP blocks `on*=` attributes outright; a nonce cannot cover them."""
    assert not re.search(r"<[^>]+\son[a-z]+=", client.get("/").text)


def test_the_nonce_is_fresh_per_response(client):
    assert _nonce(client.get("/")) != _nonce(client.get("/"))


def test_the_login_page_carries_the_headers_too(anonymous):
    """The one page ZAP can reach without the passcode."""
    response = anonymous.get("/login")

    assert "content-security-policy" in response.headers
    hsts = headers.STATIC_HEADERS["Strict-Transport-Security"]
    assert response.headers["strict-transport-security"] == hsts


def test_an_unhandled_error_still_carries_the_headers():
    """ServerErrorMiddleware would otherwise send a bare 500."""
    app = FastAPI()
    headers.install(app)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 500
    assert response.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in response.headers


def test_csp_form_action_adds_redirect_origins(monkeypatch):
    """Behind Cloudflare Access a POST can bounce to the team domain."""
    monkeypatch.setenv("CSP_FORM_ACTION", "https://team.cloudflareaccess.com")
    app = FastAPI()
    headers.install(app)
    app.get("/")(lambda: {})

    policy = TestClient(app).get("/").headers["content-security-policy"]

    assert "form-action 'self' https://team.cloudflareaccess.com;" in policy


def test_form_action_defaults_to_self(client):
    assert "form-action 'self';" in client.get("/").headers["content-security-policy"]


TEMPLATES = sorted(
    p
    for p in (Path(gymlog.__file__).parent / "templates").rglob("*.html")
    if "email" not in p.parts
)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_template_has_an_inline_event_handler(template):
    """Checked in the source, not a rendered page: which page renders which
    partial depends on the data, and a test against `/` alone passed with the
    handlers this replaced still in place."""
    assert not re.search(r"<[^>]+\son[a-z]+\s*=", template.read_text(), re.IGNORECASE)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_every_template_script_carries_the_nonce(template):
    for tag in re.findall(r"<script\b[^>]*>", template.read_text()):
        if "src=" not in tag:
            assert 'nonce="{{ request.state.csp_nonce }}"' in tag, tag
