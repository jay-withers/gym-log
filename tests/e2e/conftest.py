"""Browser tests: the real app under uvicorn, driven by Chromium.

The rest of the suite tests routes through TestClient. These catch what only a
browser sees: a script the CSP blocks for a missing nonce, a template that
renders but throws in the page, the Secure session cookie not coming back.

One server for the session, on a free port, against a seeded local log in a
temp dir (never the blob: STATE_CONTAINER_URL and KEY_VAULT_URI are removed).
Skipped locally without Chromium (`make e2e` installs it); CI installs it and
fails rather than skips.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator

import pytest
from appkit import testing
from playwright.sync_api import BrowserType, Page

PASSCODE = "e2e-passcode"


@pytest.fixture(scope="session")
def browser_type(browser_type: BrowserType) -> BrowserType:
    """pytest-playwright's, checked before its `browser` fixture launches."""
    testing.require_browser(browser_type)
    return browser_type


@pytest.fixture(scope="session")
def base_url(browser_type: BrowserType, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """pytest-playwright's base URL: gymlog serving a freshly seeded log.

    Takes `browser_type` so a missing Chromium skips before anything starts.
    """
    env: dict[str, str | None] = {
        "APP_PASSCODE": PASSCODE,
        "REQUIRE_PASSCODE": "true",
        "LOCAL_STATE_PATH": str(tmp_path_factory.mktemp("e2e") / "log.json"),
        # The session server starts before the per-test `fake_secrets` clears
        # these, so it sets them itself: any one set would reach Azure. Empty
        # rather than removed, because Settings would then read a developer's
        # `.env` instead, and a variable that is set wins over `.env`.
        "STATE_CONTAINER_URL": "",
        "KEY_VAULT_URI": "",
        "AZURE_CLIENT_ID": "",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "",
        "CSP_FORM_ACTION": "",
    }
    gymlog = testing.console_script("gymlog")
    seeded = subprocess.run(
        [gymlog, "seed", "--force"],
        env={k: v for k, v in {**os.environ, **env}.items() if v is not None},
        capture_output=True,
        text=True,
    )
    if seeded.returncode != 0:
        pytest.fail(f"gymlog seed failed:\n{seeded.stdout}{seeded.stderr}", pytrace=False)
    with testing.live_server(
        [gymlog, "serve", "--host", "{host}", "--port", "{port}"], env=env
    ) as url:
        yield url


@pytest.fixture(autouse=True)
def console_errors(page: Page) -> Iterator[list[str]]:
    """Every journey ends with no console error: a CSP block is one."""
    errors = testing.watch_console(page)
    yield errors
    assert errors == [], errors


@pytest.fixture
def signed_in(page: Page) -> Page:
    testing.log_in(page, PASSCODE)
    return page
