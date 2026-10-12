"""The insight email: what is sent to Resend.

`urlopen` is always replaced here, so the suite never reaches api.resend.com.
"""

from __future__ import annotations

import json

import pytest

from gymlog.mailer import RESEND_URL, send_insight

from .factories import insight


@pytest.fixture
def sent(monkeypatch):
    """Requests passed to urlopen, captured instead of sent."""
    requests = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _urlopen(request, timeout):
        requests.append(request)
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("INSIGHT_EMAIL_TO", "me@example.com")
    monkeypatch.setenv("INSIGHT_EMAIL_FROM", "Gym log <insights@example.com>")
    return requests


def test_sends_the_summary_to_the_configured_address(sent):
    send_insight(insight(id="i1", week_of="2026-10-11", summary="Solid week."))

    (request,) = sent
    body = json.loads(request.data)
    assert request.full_url == RESEND_URL
    assert request.get_header("Authorization") == "Bearer re_test"
    assert body == {
        "from": "Gym log <insights@example.com>",
        "to": ["me@example.com"],
        "subject": "Training insight, week of 2026-10-11",
        "text": "Solid week.",
    }


def test_rerunning_the_same_insight_reuses_its_idempotency_key(sent):
    send_insight(insight(id="i1"))
    send_insight(insight(id="i1"))

    assert {r.get_header("Idempotency-key") for r in sent} == {"insight-i1"}


def test_sets_a_user_agent_cloudflare_will_let_through(sent):
    send_insight(insight())

    assert not sent[0].get_header("User-agent").startswith("Python-urllib")


def test_links_back_to_the_insights_page_when_public_url_is_set(sent, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://health.example.com/")

    send_insight(insight(summary="Solid week."))

    text = json.loads(sent[0].data)["text"]
    assert text == "Solid week.\n\nhttps://health.example.com/insights/ai"


@pytest.mark.parametrize("missing", ["INSIGHT_EMAIL_TO", "INSIGHT_EMAIL_FROM"])
def test_refuses_to_send_without_an_address(sent, monkeypatch, missing):
    monkeypatch.delenv(missing)

    with pytest.raises(RuntimeError, match=missing):
        send_insight(insight())
    assert sent == []
