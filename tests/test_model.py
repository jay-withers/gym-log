"""The log document: round-tripping, and the queries the pages depend on."""

from __future__ import annotations

from datetime import date

import pytest

from gymlog.model import DEFAULT_SLOTS, Log, SetLog

from .factories import (
    achievement,
    block,
    condition,
    entry,
    exercise,
    garmin_activity,
    garmin_day,
    goal,
    insight,
    session,
)


def test_round_trips_through_json():
    log = Log(
        blocks=(block(),),
        sessions=(session("2026-09-15", "2026-09-01", "A", entry()),),
        achievements=(achievement(),),
        conditions=(condition(),),
        goals=(goal(),),
        insights=(insight(),),
        garmin_activities=(garmin_activity(),),
        garmin_days=(garmin_day(),),
        garmin_synced_at="2026-09-15T07:00:00+00:00",
    )
    assert Log.from_json(log.to_json()) == log


def test_an_empty_document_reads_as_an_empty_log():
    assert Log.from_json('{"schema": 1}') == Log(slots=DEFAULT_SLOTS)


def test_a_future_schema_is_refused_rather_than_overwritten():
    """A newer deployment must not silently discard a history it cannot read."""
    with pytest.raises(ValueError, match="schema 99"):
        Log.from_json('{"schema": 99}')


def test_missing_fields_cost_only_themselves():
    """The document can be hand-edited in the portal; it should survive that."""
    log = Log.from_json('{"blocks": [{"id": "x"}], "sessions": [{"date": "2026-01-01"}]}')
    assert log.blocks[0].id == "x"
    assert log.blocks[0].weeks == 8
    assert log.sessions[0].entries == ()


def test_current_block_is_the_most_recently_started():
    log = Log(blocks=(block("2026-09-01"), block("2026-10-27"), block("2026-06-02")))
    assert log.current_block is not None
    assert log.current_block.id == "2026-10-27"


def test_last_entry_finds_the_most_recent_performance():
    log = Log(
        sessions=(
            session("2026-09-01", "b", "A", entry("Flyes", "chest", (10, 5.0))),
            session("2026-09-15", "b", "A", entry("Flyes", "chest", (12, 7.5))),
            session("2026-09-08", "b", "A", entry("Flyes", "chest", (11, 6.0))),
        )
    )
    found = log.last_entry("Flyes")
    assert found is not None
    found_entry, when = found
    assert when == "2026-09-15"
    assert found_entry.sets[0].weight == 7.5


def test_last_entry_skips_entries_with_no_sets():
    """A ticked-off finisher is a record of attendance, not a load to progress."""
    log = Log(
        sessions=(
            session("2026-09-01", "b", "A", entry("Flyes", "chest", (10, 5.0))),
            session("2026-09-15", "b", "A", entry("Flyes", "chest")),
        )
    )
    found = log.last_entry("Flyes")
    assert found is not None
    assert found[1] == "2026-09-01"


def test_history_is_keyed_on_the_slot_not_the_exercise():
    """The whole reason the slot is stored: a rotation must not reset the record."""
    log = Log(
        sessions=(
            session("2026-09-15", "b1", "A", entry("Cable Flyes", "chest", (12, 7.5))),
            session("2026-10-28", "b2", "A", entry("Incline DB Press", "chest", (11, 20.0))),
        )
    )
    rows = log.history("chest")
    assert [r[1] for r in rows] == ["Cable Flyes", "Incline DB Press"]
    # Oldest first, which is the order a chart wants.
    assert [r[0] for r in rows] == ["2026-09-15", "2026-10-28"]


def test_history_reports_the_heaviest_set():
    log = Log(
        sessions=(session("2026-09-15", "b", "A", entry("x", "chest", (12, 20.0), (8, 25.0))),)
    )
    assert log.history("chest")[0][2] == SetLog(reps=8, weight=25.0)


def test_with_session_appends_and_never_replaces():
    """Append-only is the one property the spreadsheet did not have."""
    log = Log(sessions=(session("2026-09-15"),))
    grown = log.with_session(session("2026-09-15"))
    assert len(grown.sessions) == 2
    assert len(log.sessions) == 1  # frozen; the original is untouched


def test_with_block_replaces_by_id():
    log = Log(blocks=(block("2026-09-01", exercise(name="Old")),))
    updated = log.with_block(block("2026-09-01", exercise(name="New")))
    assert len(updated.blocks) == 1
    assert updated.blocks[0].days["A"].exercises[0].name == "New"


def test_ensuring_slot_adds_an_unseen_slot():
    log = Log()
    grown = log.ensuring_slot("mobility")
    assert "mobility" in grown.slots
    assert "mobility" not in log.slots  # frozen; the original is untouched


def test_ensuring_slot_does_not_duplicate_a_known_slot():
    log = Log()
    assert log.ensuring_slot("chest").slots.count("chest") == 1


def test_ensuring_slot_ignores_a_blank_slot():
    log = Log()
    assert log.ensuring_slot("").slots == log.slots


def test_with_achievement_appends_a_new_one():
    log = Log(achievements=(achievement(id="a1"),))
    grown = log.with_achievement(achievement(id="a2"))
    assert len(grown.achievements) == 2
    assert len(log.achievements) == 1  # frozen; the original is untouched


def test_with_achievement_replaces_by_id():
    """Editing an achievement is resubmitting the same id with new fields."""
    log = Log(achievements=(achievement(id="a1", title="Old title"),))
    edited = log.with_achievement(achievement(id="a1", title="New title"))
    assert len(edited.achievements) == 1
    assert edited.achievements[0].title == "New title"


def test_without_achievement_removes_by_id():
    log = Log(achievements=(achievement(id="a1"), achievement(id="a2")))
    shrunk = log.without_achievement("a1")
    assert [a.id for a in shrunk.achievements] == ["a2"]


def test_with_condition_replaces_by_id():
    """Resolving a condition is resubmitting the same id with a new status."""
    log = Log(conditions=(condition(id="c1", status="active"),))
    resolved = log.with_condition(condition(id="c1", status="resolved", resolved="2026-09-20"))
    assert len(resolved.conditions) == 1
    assert resolved.conditions[0].status == "resolved"
    assert resolved.conditions[0].resolved == "2026-09-20"


def test_without_condition_removes_by_id():
    log = Log(conditions=(condition(id="c1"), condition(id="c2")))
    shrunk = log.without_condition("c1")
    assert [c.id for c in shrunk.conditions] == ["c2"]


def test_with_goal_replaces_by_id():
    """Achieving a goal is resubmitting the same id with a new status."""
    log = Log(goals=(goal(id="g1", status="active"),))
    achieved = log.with_goal(goal(id="g1", status="achieved"))
    assert len(achieved.goals) == 1
    assert achieved.goals[0].status == "achieved"


def test_without_goal_removes_by_id():
    log = Log(goals=(goal(id="g1"), goal(id="g2")))
    shrunk = log.without_goal("g1")
    assert [g.id for g in shrunk.goals] == ["g2"]


def test_with_insight_appends():
    log = Log(insights=(insight(id="i1"),))
    grown = log.with_insight(insight(id="i2"))
    assert len(grown.insights) == 2
    assert len(log.insights) == 1  # frozen; the original is untouched


def test_without_insight_removes_by_id():
    log = Log(insights=(insight(id="i1"), insight(id="i2")))
    shrunk = log.without_insight("i1")
    assert [i.id for i in shrunk.insights] == ["i2"]


def test_with_garmin_sync_merges_new_records_and_dedupes_existing_ones():
    log = Log(
        garmin_activities=(garmin_activity(id="a1", date="2026-09-10"),),
        garmin_days=(garmin_day(date="2026-09-10", steps=1000),),
    )
    updated = log.with_garmin_sync(
        activities=[
            garmin_activity(id="a1", date="2026-09-10", avg_hr=150),
            garmin_activity(id="a2", date="2026-09-12"),
        ],
        days=[garmin_day(date="2026-09-12", steps=2000)],
        keep_since="2026-08-01",
    )
    assert [a.id for a in updated.garmin_activities] == ["a1", "a2"]
    assert updated.garmin_activities[0].avg_hr == 150  # replaced, not duplicated
    assert [d.date for d in updated.garmin_days] == ["2026-09-10", "2026-09-12"]


def test_with_garmin_sync_prunes_anything_older_than_keep_since():
    log = Log(
        garmin_activities=(garmin_activity(id="old", date="2026-08-01"),),
        garmin_days=(garmin_day(date="2026-08-01"),),
    )
    updated = log.with_garmin_sync(activities=[], days=[], keep_since="2026-09-01")
    assert updated.garmin_activities == ()
    assert updated.garmin_days == ()


def test_with_garmin_sync_records_when_it_ran():
    log = Log()
    updated = log.with_garmin_sync(
        activities=[], days=[], keep_since="2026-08-01", synced_at="2026-09-15T07:00:00+00:00"
    )
    assert updated.garmin_synced_at == "2026-09-15T07:00:00+00:00"


def test_with_garmin_sync_keeps_the_previous_timestamp_if_none_is_given():
    log = Log(garmin_synced_at="2026-09-15T07:00:00+00:00")
    updated = log.with_garmin_sync(activities=[], days=[], keep_since="2026-08-01")
    assert updated.garmin_synced_at == "2026-09-15T07:00:00+00:00"


def test_garmin_zone_seconds_since_sums_across_activities_on_or_after_cutoff():
    log = Log(
        garmin_activities=(
            garmin_activity(date="2026-09-01", zone_seconds=(10, 20, 30, 40, 50)),
            garmin_activity(date="2026-09-10", zone_seconds=(1, 2, 3, 4, 5)),
        )
    )
    assert log.garmin_zone_seconds_since("2026-09-05") == (1, 2, 3, 4, 5)
    assert log.garmin_zone_seconds_since("2026-08-01") == (11, 22, 33, 44, 55)


def test_garmin_zone_seconds_since_ignores_a_failed_zone_fetch():
    """`zone_seconds=()` (the per-activity detail call failed) contributes nothing."""
    log = Log(garmin_activities=(garmin_activity(zone_seconds=()),))
    assert log.garmin_zone_seconds_since("2026-01-01") == (0, 0, 0, 0, 0)


def test_garmin_zone_seconds_since_only_counts_runs():
    """A strength session's heart rate tracks the rest between sets, not effort."""
    log = Log(
        garmin_activities=(
            garmin_activity(id="a1", activity_type="running", zone_seconds=(1, 2, 3, 4, 5)),
            garmin_activity(
                id="a2", activity_type="treadmill_running", zone_seconds=(1, 1, 1, 1, 1)
            ),
            garmin_activity(id="a3", activity_type="cycling", zone_seconds=(10, 20, 30, 40, 50)),
            garmin_activity(
                id="a4",
                activity_type="strength_training",
                zone_seconds=(10, 20, 30, 40, 50),
            ),
        )
    )
    assert log.garmin_zone_seconds_since("2026-01-01") == (2, 3, 4, 5, 6)


@pytest.mark.parametrize(
    ("today", "week", "due"),
    [
        (date(2026, 9, 1), 1, False),
        (date(2026, 9, 7), 1, False),
        (date(2026, 9, 8), 2, False),
        (date(2026, 10, 20), 8, False),
        (date(2026, 10, 27), 9, True),
    ],
)
def test_week_of_and_due(today, week, due):
    b = block("2026-09-01")
    assert b.week_of(today) == week
    assert b.due(today) is due


def test_week_of_survives_an_unparseable_start_date():
    """A hand-edited document should degrade, not raise, on a page load."""
    b = block("2026-09-01")
    b = type(b)(id=b.id, name=b.name, started="not-a-date", days=b.days)
    assert b.week_of(date(2026, 9, 19)) == 1


@pytest.mark.parametrize(
    ("low", "high", "label"),
    [
        (10, 12, "10\u201312"),
        # No width: the sheet writes a single number against some movements, and
        # `10-10` would read as a range that is not one.
        (10, 10, "10"),
        # The finisher, which takes no rep target at all.
        (0, 0, ""),
    ],
)
def test_the_rep_range_label(low, high, label):
    assert exercise(rep_low=low, rep_high=high).rep_range_label == label
