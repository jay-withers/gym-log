"""The sample log used for local work.

The reason these assertions are worth having: the whole claim of `seed` is that
what a local run reads is shaped exactly like what the blob holds. A field the
sample forgets to set is a field the deployed app would find missing, and the
local run has to find it missing too — otherwise local work is done against a
document the real one never produces.
"""

from __future__ import annotations

from datetime import date

import pytest

from gymlog import store
from gymlog.cli import main
from gymlog.model import Log
from gymlog.progression import suggest
from gymlog.seed import sample_log

TODAY = date(2026, 9, 19)


@pytest.fixture
def log():
    return sample_log(TODAY)


def test_it_survives_the_round_trip_the_blob_puts_it_through(log):
    """Written and read back through the same conversion the blob uses."""
    assert Log.from_json(log.to_json()) == log


def test_the_block_is_part_way_through_rather_than_due(log):
    block = log.current_block
    assert block is not None
    assert block.week_of(TODAY) == 4
    assert not block.due(TODAY)


def test_both_days_have_every_slot(log):
    block = log.current_block
    assert sorted(block.days) == ["A", "B"]
    for day in block.days.values():
        slots = [e.slot for e in day.exercises]
        assert slots == list(Log().slots)


def test_the_days_have_different_amounts_of_history(log):
    """`_next_day` alternates, and equal history would never show that working."""
    counts = {key: len([s for s in log.sessions if s.day == key]) for key in ("A", "B")}
    assert counts["A"] != counts["B"]


def test_it_puts_the_session_screen_into_every_state(log):
    """One exercise per state the card has, so none of them needs imagining."""
    day = log.current_block.days["A"]
    by_slot = {e.slot: e for e in day.exercises}

    def suggestion(slot):
        exercise = by_slot[slot]
        previous = log.last_entry(exercise.name)
        last, when = previous if previous else (None, "")
        return suggest(exercise, last, when)

    # Climbing inside the range.
    chest = suggestion("chest")
    assert chest is not None and not chest.stalled

    # At the top of the range on every set, so the weight goes up.
    back = suggestion("back")
    assert back is not None
    assert back.weight > max(s.weight for s in log.last_entry("Wide Seated Machine Row")[0].sets)

    # Fell short, so the weight holds and the card goes amber.
    legs = suggestion("legs")
    assert legs is not None and legs.stalled

    # No history at all, so the seed weight carries it.
    triceps = suggestion("triceps")
    assert triceps is not None
    assert triceps.last is None
    assert triceps.weight == by_slot["triceps"].seed_weight


def test_the_finisher_is_timed_sometimes_and_not_others(log):
    """The time is optional, which is only visible if the sample varies it."""
    times = [
        entry.seconds
        for session in log.sessions
        for entry in session.entries
        if entry.slot == "finisher"
    ]
    assert 0 in times
    assert any(t > 0 for t in times)


def test_it_puts_goals_and_conditions_into_more_than_one_state(log):
    """Empty is the one state local work has the least reason to look at."""
    assert any(g.status == "active" for g in log.goals)
    assert any(g.status == "achieved" for g in log.goals)
    assert any(c.status == "active" for c in log.conditions)
    assert any(c.status == "resolved" for c in log.conditions)


def test_it_has_an_achievement_and_a_weekly_insight(log):
    assert log.achievements
    assert log.insights


def test_per_set_targets_are_present_and_not_uniform(log):
    """Both shapes, so `· per set` is exercised as well as the plain range."""
    exercises = [e for day in log.current_block.days.values() for e in day.exercises]
    assert any(len(set(e.rep_targets)) > 1 for e in exercises)
    assert any(len(set(e.rep_targets)) == 1 for e in exercises)


# --- the guards --------------------------------------------------------------


def test_it_refuses_to_run_against_a_real_container(monkeypatch, caplog):
    """The one thing this must never do is replace a real log with invented sessions."""
    monkeypatch.setenv("STATE_CONTAINER_URL", "https://example.blob.core.windows.net/state")
    from gymlog import settings as settings_module

    settings_module.settings.cache_clear()

    assert main(["seed"]) == 1
    assert "refusing to seed" in caplog.text
    assert "STATE_CONTAINER_URL" in caplog.text


def test_it_refuses_to_replace_an_existing_local_log_without_force(caplog):
    assert main(["seed"]) == 0
    assert main(["seed"]) == 1
    assert "--force" in caplog.text
    assert main(["seed", "--force"]) == 0


def test_a_seeded_log_loads_back_through_the_store():
    """End to end: written by the CLI, read by the same code the app uses.

    Compared against `sample_log(date.today())` rather than a fixed date,
    because the sample dates itself relative to now so a local run always has a
    block part-way through rather than one that expired months ago.
    """
    assert main(["seed"]) == 0
    loaded, etag = store.load()
    assert loaded == sample_log(date.today())
    # A local file has no ETag; that is the signal `store.save` keys off.
    assert etag is None
