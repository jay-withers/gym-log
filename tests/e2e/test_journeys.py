"""What a person does with health, in a real browser."""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

import pytest
from appkit import testing
from playwright.sync_api import Page, expect

from .conftest import PASSCODE

# Every screen reachable from the nav, with the seeded log behind it. A page
# that renders but trips the CSP, or throws in its script, fails the
# autouse console check.
PAGES = [
    "/",
    "/strength",
    "/session/A",
    "/session/B",
    "/session/manual",
    "/history/chest",
    "/block",
    "/block/edit",
    "/achievements",
    "/achievements/new",
    "/goals",
    "/goals/new",
    "/conditions",
    "/conditions/new",
    "/insights",
    "/insights/strength",
    "/insights/strength/e1rm",
    "/insights/strength/volume",
    "/insights/strength/stalled",
    "/insights/strength/records",
    "/insights/fitness",
    "/insights/running",
    "/insights/hr-zones",
    "/insights/ai",
    "/chat",
    "/garmin",
]


def test_a_page_without_a_session_goes_to_login(page: Page) -> None:
    page.goto("/strength")
    assert page.url.endswith("/login")


def test_the_wrong_passcode_is_refused_and_the_right_one_lets_you_in(
    page: Page, console_errors: list[str]
) -> None:
    page.goto("/login")
    page.fill('input[name="passcode"]', "not-it")
    # The refusal is a 401, which Chromium logs as a failed resource load.
    # Waited for: the message is delivered asynchronously, and one landing
    # after the clear below would fail the test in the console check.
    with page.expect_console_message(lambda message: "401" in message.text):
        page.click('form[action="/login"] [type="submit"]')
    expect(page.locator(".reason")).to_have_text("That is not it.")
    assert page.url.endswith("/login")
    assert all(
        error.startswith("Failed to load resource") and "401" in error for error in console_errors
    ), console_errors
    console_errors.clear()

    testing.log_in(page, PASSCODE)
    assert not page.url.endswith("/login")


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_cleanly(signed_in: Page, path: str) -> None:
    response = signed_in.goto(path)
    assert response is not None
    assert response.status == 200, path
    # goto follows redirects: a screen that bounced elsewhere would pass on
    # the page it landed on.
    assert urlparse(signed_in.url).path == path
    signed_in.wait_for_load_state("load")


def test_a_logged_set_is_there_after_a_reload(signed_in: Page) -> None:
    page = signed_in
    page.goto("/session/A")
    card = page.locator('form[action="/session/A/0"]')
    card.locator('input[name="reps_0_0"]').fill("9")
    card.locator('input[name="weight_0_0"]').fill("41.5")
    card.locator('[type="submit"]').click()
    page.wait_for_url("**/session/A?saved=0*")

    page.goto("/session/A")
    expect(page.locator('input[name="reps_0_0"]')).to_have_value("9")
    expect(page.locator('input[name="weight_0_0"]')).to_have_value("41.5")


def test_a_new_goal_is_listed_after_a_reload(signed_in: Page) -> None:
    page = signed_in
    title = f"Deadlift {uuid.uuid4().hex[:6]}"
    page.goto("/goals/new")
    page.fill('input[name="title"]', title)
    page.click('form[action="/goals"] [type="submit"]')
    page.wait_for_url("**/goals")

    page.reload()
    expect(page.get_by_text(title)).to_be_visible()


def test_back_leaves_a_page_rather_than_stepping_through_its_chips(signed_in: Page) -> None:
    """Tapping between a page's chips (movements, slots, spans) swaps the page
    in place, so the phone's Back goes to where you came from, not through
    every chip you tried. The history page's slots share the handler with
    the strength insights' movements."""
    page = signed_in
    page.goto("/strength")
    page.goto("/history/chest")
    chips = page.locator("[data-replace] a.chip")
    assert chips.count() >= 3
    for i in (1, 2):
        href = chips.nth(i).get_attribute("href")
        assert href is not None
        chips.nth(i).click()
        page.wait_for_url(f"**{href}")
        chips = page.locator("[data-replace] a.chip")

    page.go_back()
    page.wait_for_url("**/strength")
    assert urlparse(page.url).path == "/strength"
