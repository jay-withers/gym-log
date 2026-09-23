"""Weekly AI insight generation: the idempotency guard and the prompt shape.

`_complete` is always mocked here — the suite must never reach
api.deepseek.com, the same reasoning conftest.py's `fake_secrets` applies to
Key Vault and blob storage.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from gymlog.insights import MIN_DAYS_BETWEEN, _prompt, generate_insight
from gymlog.model import Log

from .factories import achievement, condition, entry, goal, insight, session


def test_generates_a_new_insight_when_none_exists(monkeypatch):
    monkeypatch.setattr("gymlog.insights._complete", lambda prompt: "Solid week.")

    result = generate_insight(Log(), today=date(2026, 9, 15))

    assert result is not None
    assert result.summary == "Solid week."
    assert result.week_of == "2026-09-15"
    assert result.generated_at == "2026-09-15"


def test_declines_when_the_last_insight_is_too_recent(monkeypatch):
    monkeypatch.setattr(
        "gymlog.insights._complete", lambda prompt: pytest.fail("should not call the API")
    )
    log = Log(insights=(insight(generated_at="2026-09-12"),))

    assert generate_insight(log, today=date(2026, 9, 15)) is None


def test_generates_again_once_enough_days_have_passed(monkeypatch):
    monkeypatch.setattr("gymlog.insights._complete", lambda prompt: "Another week done.")
    log = Log(insights=(insight(generated_at="2026-09-01"),))
    today = date(2026, 9, 1) + timedelta(days=MIN_DAYS_BETWEEN)

    result = generate_insight(log, today=today)

    assert result is not None
    assert result.summary == "Another week done."


def test_prompt_includes_recent_sessions_and_excludes_old_ones():
    log = Log(
        sessions=(
            session("2026-09-14", "b", "A", entry("Cable Flyes", "chest", (10, 7.5))),
            session("2026-08-01", "b", "A", entry("Old Move", "chest", (10, 5.0))),
        )
    )
    prompt = _prompt(log, date(2026, 9, 15))
    assert "Cable Flyes" in prompt
    assert "Old Move" not in prompt


def test_prompt_includes_active_conditions_but_not_resolved_ones():
    log = Log(
        conditions=(
            condition(id="c1", body_part="lower back", status="active", note="from deadlifts"),
            condition(id="c2", body_part="knee", status="resolved"),
        )
    )
    prompt = _prompt(log, date(2026, 9, 15))
    assert "lower back" in prompt
    assert "from deadlifts" in prompt
    assert "knee" not in prompt


def test_prompt_includes_active_goals_but_not_achieved_ones():
    log = Log(
        goals=(
            goal(id="g1", title="Bench 100kg", status="active"),
            goal(id="g2", title="Squat 120kg", status="achieved"),
        )
    )
    prompt = _prompt(log, date(2026, 9, 15))
    assert "Bench 100kg" in prompt
    assert "Squat 120kg" not in prompt


def test_prompt_includes_achievements_logged_that_week():
    log = Log(achievements=(achievement(date="2026-09-14", title="Ran a 5k"),))
    prompt = _prompt(log, date(2026, 9, 15))
    assert "Ran a 5k" in prompt
