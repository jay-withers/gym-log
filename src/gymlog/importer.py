"""Read Gym_3.xlsx into a block definition.

A `.xlsx` is a zip of XML, so this parses it with `zipfile` and `xml.etree` from
the standard library rather than adding `openpyxl` to the runtime image for a
one-off import that runs once per rotation at most.

**It imports the prescription, not the performance.** The sheet's achieved-reps
and weight cells are read only as `seed_weight`, so the first suggestion is not
blind. They are deliberately *not* turned into a logged session: those cells carry
no date, and inventing one would put a fabricated entry at the head of the history
this application exists to keep honest.

The expected shape, which is what the sheet already has — one worksheet per
training day, a header row, then one row per exercise:

    Part | Exercises | Sets | Reps | Rest | Reps | Weight
    Chest | Low-to-High Cable Flyes | 3 | 10-12 | 60-70secs | 15 | 7.5

A trailing row with a name but no `Part` is the finisher (Sled Push, Sandbag
Lunges): performed and ticked off, carrying no load or rep target.
"""

from __future__ import annotations

import re
import zipfile
from datetime import date
from xml.etree import ElementTree

from .model import Block, Day, Exercise

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# Worksheet name -> the day key used throughout the app. Order matters only in
# that "A" is the first session of the week.
DAY_KEYS = ("A", "B")


def import_workbook(path: str, started: date | None = None, name: str = "") -> Block:
    """Build a block from an `.xlsx`, one day per worksheet."""
    sheets = read_workbook(path)
    if not sheets:
        raise ValueError(f"{path} contains no worksheets")
    if len(sheets) > len(DAY_KEYS):
        raise ValueError(
            f"{path} has {len(sheets)} worksheets; this expects at most "
            f"{len(DAY_KEYS)} (one per training day)"
        )

    start = started or date.today()
    days = {
        key: Day(label=label, exercises=tuple(_exercises(rows)))
        for key, (label, rows) in zip(DAY_KEYS, sheets.items(), strict=False)
    }
    return Block(
        id=start.isoformat(),
        name=name or f"Block from {path.rsplit('/', 1)[-1]}",
        started=start.isoformat(),
        days=days,
    )


def _exercises(rows: list[list[str]]) -> list[Exercise]:
    out: list[Exercise] = []
    for row in rows[1:]:  # row 0 is the header
        cells = [c.strip() for c in row] + [""] * 7
        part, name, sets, reps, rest, _achieved, weight = cells[:7]
        if not name:
            continue
        if not part:
            # The finisher: a movement with no load and no rep target.
            out.append(Exercise(slot="finisher", name=name))
            continue
        low, high = _rep_range(reps)
        out.append(
            Exercise(
                slot=part.lower(),
                name=name,
                sets=_int(sets),
                rep_low=low,
                rep_high=high,
                rest_seconds=_rest_seconds(rest),
                seed_weight=_float(weight),
            )
        )
    return out


def _rep_range(value: str) -> tuple[int, int]:
    """`10-12` -> (10, 12); a bare `10` -> (10, 10); anything else -> (0, 0)."""
    numbers = [int(n) for n in re.findall(r"\d+", value)]
    if not numbers:
        return 0, 0
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return min(numbers), max(numbers)


def _rest_seconds(value: str) -> int:
    """`90secs` -> 90; `60-70secs` -> 65.

    A range becomes its midpoint. The timer needs one number, and the sheet's
    ranges are narrow enough that either end would do — the midpoint just avoids
    a systematic bias in whichever direction the end was picked.
    """
    numbers = [int(n) for n in re.findall(r"\d+", value)]
    if not numbers:
        return 0
    return round(sum(numbers) / len(numbers))


def _int(value: str) -> int:
    try:
        return int(float(value))
    except ValueError:
        return 0


def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def read_workbook(path: str) -> dict[str, list[list[str]]]:
    """Every worksheet as `{name: rows}`, each row a list of cell strings.

    Rows and columns are placed by their cell references rather than by document
    order, because a spreadsheet omits empty cells entirely — reading them
    positionally silently shifts every value left of a blank.
    """
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        sheets: dict[str, list[list[str]]] = {}
        for name, target in _sheet_targets(archive).items():
            sheets[name] = _rows(archive.read(target), shared)
    return sheets


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(f"{NS}t")) for si in root.findall(f"{NS}si")]


def _sheet_targets(archive: zipfile.ZipFile) -> dict[str, str]:
    """Worksheet name -> path inside the archive, in the workbook's own order."""
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel.get("Target", "") for rel in rels}

    out: dict[str, str] = {}
    for sheet in workbook.iter(f"{NS}sheet"):
        target = targets.get(sheet.get(f"{REL_NS}id", ""), "")
        if not target:
            continue
        path = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
        if path in archive.namelist():
            out[sheet.get("name", "")] = path
    return out


def _rows(blob: bytes, shared: list[str]) -> list[list[str]]:
    root = ElementTree.fromstring(blob)
    rows: list[list[str]] = []
    for row in root.iter(f"{NS}row"):
        cells: dict[int, str] = {}
        for cell in row.findall(f"{NS}c"):
            index = _column(cell.get("r", "A1"))
            cells[index] = _value(cell, shared)
        if not cells:
            continue
        width = max(cells)
        values = [cells.get(i, "") for i in range(1, width + 1)]
        if any(v.strip() for v in values):
            rows.append(values)
    return rows


def _value(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    inline = cell.find(f"{NS}is")
    if inline is not None:
        return "".join(t.text or "" for t in inline.iter(f"{NS}t"))
    node = cell.find(f"{NS}v")
    if node is None or node.text is None:
        return ""
    if kind == "s":
        index = int(node.text)
        return shared[index] if 0 <= index < len(shared) else ""
    return node.text


def _column(reference: str) -> int:
    """`C7` -> 3. Spreadsheet columns are base-26 with no zero digit."""
    letters = re.match(r"([A-Z]+)", reference)
    if not letters:
        return 1
    index = 0
    for char in letters.group(1):
        index = index * 26 + (ord(char) - 64)
    return index
