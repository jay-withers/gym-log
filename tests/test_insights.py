"""AI insight generation: the prompt shape.

`_complete` is always mocked here — the suite must never reach
api.deepseek.com, the same reasoning conftest.py's `fake_secrets` applies to
Key Vault and blob storage.
"""

from __future__ import annotations

from datetime import date

from gymlog.insights import BLOCK_ENDING_SOON_WEEKS, _prompt, generate_insight
from gymlog.model import Log

from .factories import (
    achievement,
    block,
    condition,
    entry,
    garmin_activity,
    garmin_day,
    goal,
    insight,
    session,
)


def test_generates_a_new_insight_when_none_exists(monkeypatch):
    monkeypatch.setattr("gymlog.insights._complete", lambda prompt: "Solid week.")

    result = generate_insight(Log(), today=date(2026, 9, 15))

    assert result.summary == "Solid week."
    assert result.week_of == "2026-09-15"
    assert result.generated_at == "2026-09-15"


def test_generates_even_when_the_last_insight_was_just_generated(monkeypatch):
    """No cooldown: running the job (or the CLI) again right away still produces one."""
    monkeypatch.setattr("gymlog.insights._complete", lambda prompt: "Another one.")
    log = Log(insights=(insight(generated_at="2026-09-15"),))

    result = generate_insight(log, today=date(2026, 9, 15))

    assert result.summary == "Another one."


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


def test_prompt_says_no_block_when_none_started():
    prompt = _prompt(Log(), date(2026, 9, 15))
    assert "Current training block:\n- none" in prompt


def test_prompt_includes_current_block_week_when_not_ending_soon():
    log = Log(blocks=(block("2026-08-01", weeks=8),))
    prompt = _prompt(log, date(2026, 8, 15))
    assert "Test block, week 3 of 8" in prompt
    assert "(due for rotation)" not in prompt
    assert "ending soon" not in prompt


def test_prompt_does_not_flag_ending_soon_with_weeks_of_headroom():
    log = Log(
        blocks=(block("2026-07-01", weeks=8),),
        sessions=(
            session("2026-08-10", "2026-07-01", "A", entry("Bench Press", "chest", (8, 60.0))),
        ),
    )
    prompt = _prompt(log, date(2026, 8, 5))
    assert "ending soon" not in prompt
    assert "chest:" not in prompt


def test_prompt_flags_block_ending_soon_with_recent_slot_progression():
    assert BLOCK_ENDING_SOON_WEEKS == 1
    log = Log(
        blocks=(block("2026-07-01", weeks=8),),
        sessions=(
            session("2026-08-10", "2026-07-01", "A", entry("Bench Press", "chest", (8, 60.0))),
        ),
    )
    prompt = _prompt(log, date(2026, 8, 19))
    assert "week 8 of 8" in prompt
    assert "ending soon" in prompt
    assert "chest: 2026-08-10 Bench Press (8x60.0kg)" in prompt


def test_prompt_flags_an_overdue_block_as_due_and_ending_soon():
    log = Log(blocks=(block("2026-06-01", weeks=8),))
    prompt = _prompt(log, date(2026, 9, 15))
    assert "(due for rotation)" in prompt
    assert "ending soon" in prompt


def test_prompt_omits_the_garmin_section_when_nothing_has_ever_synced():
    prompt = _prompt(Log(), date(2026, 9, 15))
    assert "Garmin" not in prompt


def test_prompt_includes_recent_garmin_activity_with_its_zone_breakdown():
    log = Log(
        garmin_activities=(
            garmin_activity(
                date="2026-09-14",
                activity_type="running",
                duration_seconds=1800,
                avg_hr=140,
                max_hr=165,
                zone_seconds=(60, 300, 900, 480, 60),
            ),
            garmin_activity(id="old", date="2026-08-01", activity_type="cycling"),
        )
    )
    prompt = _prompt(log, date(2026, 9, 15))
    assert "2026-09-14 running: 30 min, avg 140 bpm, max 165 bpm" in prompt
    assert "Z2 5m" in prompt
    assert "2026-08-01" not in prompt  # outside the 7-day window


def test_prompt_includes_recovery_averages_from_recent_garmin_days():
    log = Log(
        garmin_days=(
            garmin_day(date="2026-09-13", steps=8000, resting_hr=54, sleep_seconds=27000),
            garmin_day(date="2026-09-14", steps=10000, resting_hr=56, sleep_seconds=25200),
        )
    )
    prompt = _prompt(log, date(2026, 9, 15))
    assert "average 9000 steps/day" in prompt
    assert "average resting heart rate 55 bpm" in prompt
    assert "average sleep 435m/night" in prompt


def test_prompt_notes_no_activities_when_only_days_are_synced():
    log = Log(garmin_days=(garmin_day(date="2026-09-14"),))
    prompt = _prompt(log, date(2026, 9, 15))
    assert "no activities logged" in prompt
