"""integrity.py: the rules a stored log keeps, one broken log per rule.

`check` runs in `store.update`, the selfcheck and the backup, so a rule that is
wrong refuses a good session or rolls back a good release. Each rule has a test
that it fires; the seed and every API test show it doesn't fire on real shapes.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from appkit import integrity as kit

from health import integrity, seed
from health.model import Entry, GarminActivity, GarminDay, Log, SetLog

from .factories import achievement, block, entry, session


def _log(*sessions: Any, **more: Any) -> Log:
    return Log(blocks=(block("2026-09-01"),), sessions=sessions, **more)


def _one_set(reps: int, weight: float) -> Log:
    return _log(
        session("2026-09-15", "2026-09-01", "A", entry("Cable Flyes", "chest", (reps, weight)))
    )


def test_the_seed_sample_keeps_every_rule():
    assert integrity.check(seed.sample_log(date(2026, 9, 20))) == []


def test_an_empty_log_keeps_every_rule():
    assert integrity.check(Log()) == []


def test_a_bodyweight_set_and_a_zero_rep_set_are_fine():
    assert integrity.check(_one_set(0, 0.0)) == []


def test_negative_reps():
    assert integrity.check(_one_set(-1, 7.5)) == [
        "sessions (date 2026-09-15, day A) entry 0 set 0: negative reps"
    ]


def test_negative_load():
    assert integrity.check(_one_set(10, -2.5)) == [
        "sessions (date 2026-09-15, day A) entry 0 set 0: negative load"
    ]


@pytest.mark.parametrize("weight", [float("nan"), float("inf")])
def test_a_load_that_is_not_a_number(weight):
    assert integrity.check(_one_set(10, weight)) == [
        "sessions (date 2026-09-15, day A) entry 0 set 0: load is not a number"
    ]


def test_an_implausible_but_possible_set_is_not_a_stored_violation():
    """120 reps is refused at the form; an old typo must not block every deploy."""
    assert integrity.check(_one_set(120, 600.0)) == []


def test_a_negative_time():
    timed = Entry(slot="finisher", exercise="Sled Push", seconds=-5)
    assert integrity.check(_log(session("2026-09-15", "2026-09-01", "A", timed))) == [
        "sessions (date 2026-09-15, day A) entry 0: negative time"
    ]


@pytest.mark.parametrize("when", ["yesterday", "2026-9-15", "2026-02-30", "", "2026-W38-2"])
def test_a_session_date_that_does_not_parse_names_its_position_not_its_text(when):
    found = integrity.check(_log(session(when, "2026-09-01", "A")))
    assert found == ["sessions[0]: date is not YYYY-MM-DD"]


def test_one_session_recorded_twice():
    one = session("2026-09-15", "2026-09-01", "A")
    assert integrity.check(_log(one, one)) == [
        "sessions (date 2026-09-15, day A): recorded 2 times"
    ]


def test_the_same_day_on_two_dates_is_two_sessions():
    assert integrity.check(_log(session("2026-09-15"), session("2026-09-18"))) == []


def test_one_exercise_recorded_twice_in_a_session():
    twice = session("2026-09-15", "2026-09-01", "A", entry("Row"), entry("Row"))
    assert integrity.check(_log(twice)) == [
        "sessions (date 2026-09-15, day A): 1 exercise(s) recorded twice"
    ]


def test_a_duplicate_id():
    found = integrity.check(_log(achievements=(achievement("a1"), achievement("a1"))))
    assert found == ["achievements: id a1 is used 2 times"]


def test_a_duplicate_garmin_activity_and_day():
    activity = GarminActivity(id="g1", date="2026-09-15", activity_type="running")
    day = GarminDay(date="2026-09-15")
    found = integrity.check(Log(garmin_activities=(activity, activity), garmin_days=(day, day)))
    assert found == [
        "garmin_activities: id g1 is used 2 times",
        "garmin_days: date 2026-09-15 is used 2 times",
    ]


def test_a_problem_never_names_an_exercise_or_a_load():
    found = integrity.check(_one_set(-1, -42.5))
    assert not any("Cable" in p or "42.5" in p for p in found)


# --- the round trip ----------------------------------------------------------------


def test_the_seed_log_round_trips_with_nothing_dropped():
    raw = seed.sample_log(date(2026, 9, 20)).to_json().encode()
    assert kit.roundtrip(raw, integrity.parse, integrity.dump) == []


def test_an_older_schema_stamp_is_a_migration_not_a_loss():
    """Otherwise a release that bumps SCHEMA_VERSION would roll itself back."""
    payload = json.loads(_one_set(10, 7.5).to_json())
    payload["schema"] = 0
    assert kit.roundtrip(json.dumps(payload).encode(), integrity.parse, integrity.dump) == []


def test_a_stored_field_this_release_does_not_know_is_reported():
    payload = json.loads(_one_set(10, 7.5).to_json())
    payload["sessions"][0]["entries"][0]["sets"][0]["rpe"] = 8
    found = kit.roundtrip(json.dumps(payload).encode(), integrity.parse, integrity.dump)
    assert found == ["$.sessions[0].entries[0].sets[0].rpe: dropped"]


def test_a_set_whose_reps_were_renamed_away_is_reported():
    """The bug the forgiving parser invites: `reps` missing reads back as 0."""
    payload = json.loads(_one_set(10, 7.5).to_json())
    payload["sessions"][0]["entries"][0]["sets"][0] = {"count": 10, "weight": 7.5}
    found = kit.roundtrip(json.dumps(payload).encode(), integrity.parse, integrity.dump)
    assert "$.sessions[0].entries[0].sets[0].count: dropped" in found


def test_check_parsed_checks_the_log_half():
    parsed = integrity.parse(_one_set(-1, 7.5).to_json().encode())
    assert integrity.check_parsed(parsed) == integrity.check(parsed[0])
    assert isinstance(parsed[0].sessions[0].entries[0].sets[0], SetLog)


def test_a_bad_date_sessions_sets_are_still_checked():
    """Skipped, they'd look new once the date was fixed, and refuse that fix."""
    bad = session("yesterday", "2026-09-01", "A", entry("Row", "back", (-1, 50.0)))
    assert integrity.check(_log(bad)) == [
        "sessions[0]: date is not YYYY-MM-DD",
        "sessions[0] entry 0 set 0: negative reps",
    ]
