"""Importing the real Gym_3.xlsx.

The workbook is the actual one this project replaces, so these assertions are
against real data rather than a fixture built to agree with the parser.
"""

from __future__ import annotations

import pathlib
from datetime import date

import pytest

from gymlog.importer import _rep_range, _rest_seconds, import_workbook

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
    assert (chest.rep_low, chest.rep_high) == (10, 12)
    assert chest.seed_weight == 7.5
    # "60-70secs" -> the midpoint.
    assert chest.rest_seconds == 65


def test_the_finisher_is_untracked(block):
    finisher = block.days["A"].exercises[-1]
    assert finisher.name == "Sled Push"
    assert not finisher.tracked


def test_no_session_is_fabricated_from_the_achieved_reps(block):
    """The sheet's achieved-reps cells carry no date. They must not become history.

    They are read only as `seed_weight`; inventing a session date would put a lie
    at the head of the log this application exists to keep honest.
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
    ("raw", "expected"),
    [("10-12", (10, 12)), ("8-10", (8, 10)), ("12", (12, 12)), ("", (0, 0)), ("AMRAP", (0, 0))],
)
def test_rep_ranges(raw, expected):
    assert _rep_range(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("90secs", 90), ("60-70secs", 65), ("60secs", 60), ("", 0)],
)
def test_rest_seconds(raw, expected):
    assert _rest_seconds(raw) == expected
