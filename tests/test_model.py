"""The log document: round-tripping, and the queries the pages depend on."""

from __future__ import annotations

from datetime import date

import pytest

from gymlog.model import DEFAULT_SLOTS, Log, SetLog

from .factories import block, entry, exercise, session


def test_round_trips_through_json():
    log = Log(blocks=(block(),), sessions=(session("2026-09-15", "2026-09-01", "A", entry()),))
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
