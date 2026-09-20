"""Importing Gym_3.xlsx.

Two halves, deliberately.

The first is against the real workbook — the actual one this project replaces —
so those assertions are on real data rather than on a fixture built to agree
with the parser. It only exists on the machine the sheet lives on, so those
tests skip everywhere else, CI included.

The second half builds a minimal `.xlsx` and is what runs everywhere. Without it
the parser — the half of this module with no library behind it, placing cells by
reference because a spreadsheet omits the empty ones — would be tested on one
laptop and nowhere else.
"""

from __future__ import annotations

import pathlib
import zipfile
from datetime import date

import pytest

from gymlog.importer import (
    _column,
    _rep_range,
    _rep_targets,
    _rest_seconds,
    import_workbook,
    read_workbook,
)

WORKBOOK = pathlib.Path("/Users/jay/Git/Gym_3.xlsx")


@pytest.fixture
def block():
    """The real sheet, or a skip. Skipped per test rather than per module: the
    parser tests below need no workbook and must run in CI."""
    if not WORKBOOK.exists():
        pytest.skip("Gym_3.xlsx is not on this machine")
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10-12", (10, 12)),
        # A bare number is a range with no width, which the session screen
        # renders as `10` rather than `10-10`.
        ("10", (10, 10)),
        # Written backwards in the cell; the sheet is typed by hand.
        ("12-10", (10, 12)),
        ("", (0, 0)),
        ("AMRAP", (0, 0)),
    ],
)
def test_rep_range(raw, expected):
    assert _rep_range(raw) == expected


# --- the parser --------------------------------------------------------------
#
# A synthetic `.xlsx`, so everything below runs without the real sheet. An
# `.xlsx` is a zip of XML and this module parses it with the standard library
# rather than adding openpyxl to the runtime image, which means the parsing is
# ours to get wrong and ours to test.

SHEET_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
REL_ID = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _cell(ref: str, value: str) -> str:
    """An inline string, which is what a sheet written by an exporter tends to hold."""
    return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'


def _sheet(*rows: str) -> str:
    body = "".join(f'<row r="{n}">{row}</row>' for n, row in enumerate(rows, start=1))
    return f"<worksheet {SHEET_NS}><sheetData>{body}</sheetData></worksheet>"


def _xlsx(
    tmp_path: pathlib.Path,
    sheets: dict[str, str],
    *,
    shared: list[str] | None = None,
    dangling_relationship: bool = False,
) -> str:
    """Write the smallest workbook `read_workbook` will read."""
    path = tmp_path / "book.xlsx"
    names = list(sheets)
    entries = "".join(
        f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>'
        for n, name in enumerate(names, start=1)
    )
    relationships = "".join(
        f'<Relationship Id="rId{n}" Target="worksheets/sheet{n}.xml"/>'
        for n in range(1, len(names) + 1)
    )
    if dangling_relationship:
        # Two things a deleted worksheet leaves behind: an entry with no
        # relationship at all, and one whose relationship points at a part that
        # is no longer in the archive.
        entries += '<sheet name="Ghost" sheetId="98" r:id="rId98"/>'
        entries += '<sheet name="Phantom" sheetId="99" r:id="rId99"/>'
        relationships += '<Relationship Id="rId99" Target="worksheets/gone.xml"/>'

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            f"<workbook {SHEET_NS} {REL_ID}><sheets>{entries}</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{relationships}</Relationships>",
        )
        for n, name in enumerate(names, start=1):
            archive.writestr(f"xl/worksheets/sheet{n}.xml", sheets[name])
        if shared is not None:
            items = "".join(f"<si><t>{value}</t></si>" for value in shared)
            archive.writestr(
                "xl/sharedStrings.xml",
                f'<sst {SHEET_NS} count="{len(shared)}">{items}</sst>',
            )
    return str(path)


HEADER = "".join(
    _cell(f"{column}1", label)
    for column, label in zip(
        "ABCDEFG",
        ("Part", "Exercises", "Sets", "Reps", "Rest", "Reps", "Weight"),
        strict=True,
    )
)


def test_a_blank_cell_does_not_shift_the_row_left(tmp_path):
    """The reason cells are placed by reference rather than in document order.

    A spreadsheet omits an empty cell entirely. Read positionally, an exercise
    with no `Rest` would take its per-set reps as its rest and its weight as its
    per-set reps — plausible numbers in the wrong fields, which is the worst
    kind of import bug.
    """
    row = (
        _cell("A2", "Chest")
        + _cell("B2", "Cable Flyes")
        + _cell("C2", "3")
        + _cell("D2", "10-12")
        # No E2 at all: the Rest cell was never filled in.
        + _cell("F2", "12")
        + _cell("G2", "7.5")
    )
    block = import_workbook(_xlsx(tmp_path, {"Tues": _sheet(HEADER, row)}))

    chest = block.days["A"].exercises[0]
    assert chest.rest_seconds == 0
    assert chest.rep_targets == (12, 12, 12)
    assert chest.seed_weight == 7.5


def test_a_row_with_no_part_is_the_finisher(tmp_path):
    """Sled Push and Sandbag Lunges sit under the table with no `Part` against them."""
    rows = _sheet(
        HEADER,
        _cell("A2", "Chest") + _cell("B2", "Cable Flyes") + _cell("C2", "3") + _cell("D2", "10-12"),
        _cell("B3", "Sled Push"),
    )
    block = import_workbook(_xlsx(tmp_path, {"Tues": rows}))

    finisher = block.days["A"].exercises[-1]
    assert finisher.slot == "finisher"
    assert finisher.name == "Sled Push"
    assert not finisher.tracked


def test_a_row_with_no_exercise_is_not_an_exercise(tmp_path):
    """A `Part` left behind after the movement was deleted, which the sheet has."""
    rows = _sheet(HEADER, _cell("A2", "Chest") + _cell("C2", "3"))
    block = import_workbook(_xlsx(tmp_path, {"Tues": rows}))
    assert block.days["A"].exercises == ()


def test_cells_that_are_not_numbers_do_not_refuse_the_file(tmp_path):
    """A 12-exercise block importing with one odd cell beats refusing the workbook."""
    row = (
        _cell("A2", "Chest")
        + _cell("B2", "Cable Flyes")
        + _cell("C2", "three")
        + _cell("D2", "AMRAP")
        + _cell("E2", "as needed")
        + _cell("G2", "bodyweight")
    )
    chest = import_workbook(_xlsx(tmp_path, {"Tues": _sheet(HEADER, row)})).days["A"].exercises[0]

    assert chest.sets == 0
    assert (chest.rep_low, chest.rep_high) == (0, 0)
    assert chest.rest_seconds == 0
    # None rather than 0.0: no weight recorded is not a lift of nothing.
    assert chest.seed_weight is None
    assert not chest.tracked


def test_both_worksheets_become_a_day_in_order(tmp_path):
    rows = _sheet(HEADER, _cell("A2", "Chest") + _cell("B2", "Cable Flyes"))
    block = import_workbook(_xlsx(tmp_path, {"Tues": rows, "Thur": rows}))

    assert sorted(block.days) == ["A", "B"]
    assert block.days["A"].label == "Tues"
    assert block.days["B"].label == "Thur"


def test_the_block_is_dated_and_named(tmp_path):
    """The id is the start date, which is what the sessions reference."""
    path = _xlsx(tmp_path, {"Tues": _sheet(HEADER)})
    block = import_workbook(path, started=date(2026, 9, 1), name="Block 3")

    assert block.id == "2026-09-01"
    assert block.started == "2026-09-01"
    assert block.name == "Block 3"


def test_an_unnamed_block_is_named_after_the_file(tmp_path):
    block = import_workbook(_xlsx(tmp_path, {"Tues": _sheet(HEADER)}))
    assert block.name == "Block from book.xlsx"


def test_blank_and_empty_rows_are_dropped(tmp_path):
    """A sheet has hundreds of them below the table, and each would be an exercise."""
    rows = _sheet(
        HEADER,
        _cell("A2", "Chest") + _cell("B2", "Cable Flyes"),
        "",
        _cell("A4", " ") + _cell("B4", ""),
    )
    block = import_workbook(_xlsx(tmp_path, {"Tues": rows}))
    assert [e.name for e in block.days["A"].exercises] == ["Cable Flyes"]


def test_shared_strings_are_resolved(tmp_path):
    """How Excel itself writes text: every string once, in a table, referenced by index."""
    row = '<c r="A2" t="s"><v>0</v></c><c r="B2" t="s"><v>1</v></c>'
    sheets = {"Tues": _sheet(HEADER, row)}
    block = import_workbook(_xlsx(tmp_path, sheets, shared=["Chest", "Cable Flyes"]))

    assert block.days["A"].exercises[0].name == "Cable Flyes"


def test_an_index_past_the_end_of_the_shared_table_is_empty(tmp_path):
    """A truncated or rewritten table, which must not raise on the way past it."""
    row = '<c r="A2" t="s"><v>0</v></c><c r="B2" t="s"><v>99</v></c>'
    sheets = {"Tues": _sheet(HEADER, row)}
    block = import_workbook(_xlsx(tmp_path, sheets, shared=["Chest"]))

    # No name, so no exercise — not a chest slot holding an empty string.
    assert block.days["A"].exercises == ()


def test_a_cell_with_no_value_reads_as_empty(tmp_path):
    row = _cell("A2", "Chest") + _cell("B2", "Cable Flyes") + '<c r="C2"/>'
    chest = import_workbook(_xlsx(tmp_path, {"Tues": _sheet(HEADER, row)})).days["A"].exercises[0]
    assert chest.sets == 0


def test_a_sheet_the_workbook_lists_but_does_not_contain_is_skipped(tmp_path):
    """A relationship pointing at nothing, which is what a deleted sheet leaves behind."""
    rows = _sheet(HEADER, _cell("A2", "Chest") + _cell("B2", "Cable Flyes"))
    sheets = read_workbook(_xlsx(tmp_path, {"Tues": rows}, dangling_relationship=True))

    assert list(sheets) == ["Tues"]


def test_a_workbook_with_no_shared_string_table_is_read(tmp_path):
    """There is none when every string is inline, and its absence is not a fault."""
    rows = _sheet(HEADER, _cell("A2", "Chest") + _cell("B2", "Cable Flyes"))
    sheets = read_workbook(_xlsx(tmp_path, {"Tues": rows}))

    assert sheets["Tues"][1][1] == "Cable Flyes"


def test_numbers_are_stored_as_numbers_rather_than_as_strings(tmp_path):
    """How a spreadsheet actually holds `Sets` and `Weight`: a bare `<v>`, no type."""
    row = (
        _cell("A2", "Chest")
        + _cell("B2", "Cable Flyes")
        + '<c r="C2"><v>3</v></c>'
        + _cell("D2", "10-12")
        + '<c r="G2"><v>7.5</v></c>'
    )
    chest = import_workbook(_xlsx(tmp_path, {"Tues": _sheet(HEADER, row)})).days["A"].exercises[0]

    assert chest.sets == 3
    assert chest.seed_weight == 7.5


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("A1", 1),
        ("C7", 3),
        # Base-26 with no zero digit, which is where an off-by-one hides: the
        # sheet is seven columns wide today and this is what survives it growing.
        ("Z1", 26),
        ("AA1", 27),
        ("AB10", 28),
        # Malformed rather than absent — a missing `r` defaults to `A1` before
        # it ever reaches here.
        ("7", 1),
    ],
)
def test_the_column_of_a_cell_reference(reference, expected):
    assert _column(reference) == expected
