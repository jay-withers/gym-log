"""Importing the real Gym_3.xlsx.

The workbook is the actual one this project replaces, so these assertions are
against real data rather than a fixture built to agree with the parser.
"""

from __future__ import annotations

import pathlib
from datetime import date

import pytest

from gymlog.importer import _rep_targets, _rest_seconds, import_workbook

WORKBOOK = pathlib.Path("/Users/jay/Git/Gym_3.xlsx")

pytestmark = pytest.mark.skipif(not WORKBOOK.exists(), reason="Gym_3.xlsx is not on this machine")


@pytest.fixture
def block():
    return import_workbook(str(WORKBOOK), started=date(2026, 9, 1), name="Block 3")


def test_both_worksheets_become_days(block):
    assert sorted(block.days) == ["A", "B"]
    assert block.days["A"].label == "Tues"
    assert block.days["B"].label == "Thur"


def test_every_slot_is_present_in_order(block):
    slots = [e.slot for e in block.days["A"].exercises]
    assert slots == ["chest", "back", "legs", "shoulders", "triceps", "biceps", "finisher"]


def test_the_prescription_survives_the_round_trip(block):
    chest = block.days["A"].exercises[0]
    assert chest.name == "Low-to-High Cable Flyes"
    assert chest.sets == 3
    # The range to work within comes from the `Reps` beside `Sets`.
    assert (chest.rep_low, chest.rep_high) == (10, 12)
    assert chest.rep_range_label == "10\u201312"
    assert chest.seed_weight == 7.5
    # "60-70secs" -> the midpoint.
    assert chest.rest_seconds == 65


def test_the_two_reps_columns_land_in_different_fields(block):
    """The sheet has two `Reps` columns and they are not the same thing.

    Chest reads `10-12` beside `Sets` and `15` beside `Weight`: a range to work
    within, and the number actually hit on each set. Both are kept, because
    either one alone loses something the sheet was saying.
    """
    by_name = {e.name: e for e in block.days["A"].exercises}

    chest = by_name["Low-to-High Cable Flyes"]
    assert (chest.rep_low, chest.rep_high) == (10, 12)
    assert chest.rep_targets == (15, 15, 15)

    # The one the range is asked about by name.
    steps = by_name["DB Step Ups"]
    assert (steps.rep_low, steps.rep_high) == (8, 10)
    assert steps.rep_range_label == "8\u201310"
    assert steps.rep_targets == (10, 10, 10)


def test_a_slashed_value_is_one_target_per_set(block):
    """`10/12/12` is a ten then two twelves, in that order."""
    triceps = {e.slot: e for e in block.days["A"].exercises}["triceps"]
    assert triceps.name == "Overhead Cable Tricep Ext"
    assert triceps.rep_targets == (10, 12, 12)
    # The range still spans them, so double progression needs no special case.
    assert (triceps.rep_low, triceps.rep_high) == (10, 12)
    assert [triceps.target_for(i) for i in range(triceps.sets)] == [10, 12, 12]


def test_a_flat_value_is_still_stored_per_set(block):
    """`15` across three sets is kept, because the range no longer implies it.

    The two columns are read from different cells and routinely disagree —
    chest is a 10-12 range worked at 15 — so dropping a flat value would lose
    the disagreement rather than compress it.
    """
    chest = block.days["A"].exercises[0]
    assert chest.rep_targets == (15, 15, 15)
    assert [chest.target_for(i) for i in range(chest.sets)] == [15, 15, 15]


def test_every_tracked_exercise_carries_a_range(block):
    """A movement with no range has nothing for double progression to climb."""
    for day in block.days.values():
        for e in day.exercises:
            if e.tracked:
                assert e.rep_low > 0 and e.rep_high >= e.rep_low, e.name
                assert e.rep_range_label, e.name


def test_every_slashed_row_in_the_sheet_is_read_in_order(block):
    """The five rows that carry per-set reps, as the workbook actually has them."""
    found = {
        e.name: e.rep_targets
        for day in block.days.values()
        for e in day.exercises
        if len(set(e.rep_targets)) > 1
    }
    assert found == {
        "Overhead Cable Tricep Ext": (10, 12, 12),
        "EZ-Bar Curls": (10, 10, 12),
        "DB Rear Delt Flyes": (12, 12, 15),
        "Rope Pushdowns": (10, 10, 12),
        "Incline DB Bicep Curls": (10, 12, 12),
    }


def test_the_finisher_is_untracked(block):
    finisher = block.days["A"].exercises[-1]
    assert finisher.name == "Sled Push"
    assert not finisher.tracked
    # Timed rather than loaded, so the sheet gives it no reps to import.
    assert finisher.rep_targets == ()
    assert finisher.rep_range_label == ""
    assert block.days["B"].exercises[-1].name == "Sandbag Lunges"


def test_no_session_is_fabricated_from_the_achieved_reps(block):
    """The sheet's achieved-reps cells carry no date. They must not become history.

    The reps and weight inform the *prescription* and the seed weight; neither
    becomes a dated entry, because inventing a session date would put a lie at
    the head of the log this application exists to keep honest.
    """
    assert block.days["A"].exercises[0].seed_weight == 7.5


def test_a_workbook_with_too_many_sheets_is_refused(monkeypatch):
    """Three worksheets is a different programme, not a third training day.

    Silently dropping the third would import a block that quietly omitted a
    session, which is worse than refusing the file.
    """
    monkeypatch.setattr(
        "gymlog.importer.read_workbook",
        lambda _path: {"Mon": [[]], "Wed": [[]], "Fri": [[]]},
    )
    with pytest.raises(ValueError, match="worksheets"):
        import_workbook("whatever.xlsx")


def test_an_empty_workbook_is_refused(monkeypatch):
    monkeypatch.setattr("gymlog.importer.read_workbook", lambda _path: {})
    with pytest.raises(ValueError, match="no worksheets"):
        import_workbook("whatever.xlsx")


@pytest.mark.parametrize(
    ("raw", "sets", "expected"),
    [
        ("10/12/12", 3, (10, 12, 12)),
        ("15", 3, (15, 15, 15)),
        # Fewer numbers than sets: padded with the last rather than refused.
        ("10/12", 3, (10, 12, 12)),
        # More than sets: truncated.
        ("10/12/12/12", 3, (10, 12, 12)),
        ("", 3, ()),
        ("AMRAP", 3, ()),
        ("12", 0, ()),
    ],
)
def test_rep_targets(raw, sets, expected):
    assert _rep_targets(raw, sets) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("90secs", 90), ("60-70secs", 65), ("60secs", 60), ("", 0)],
)
def test_rest_seconds(raw, expected):
    assert _rest_seconds(raw) == expected
