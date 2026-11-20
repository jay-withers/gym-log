"""The rules a training log must keep, checked wherever it is read or written.

`Log.from_json` is deliberately forgiving: every field defaults, so a document
from an older version costs at most what it is missing. That same forgiveness
means a parse proves almost nothing. A renamed field reads back as `0`, a NaN
typed into a weight box parses, and a free-text date sorts into the history
wherever it lands. This module is what notices.

It runs in three places, through jay-withers/appkit's `appkit.integrity`:

- `store.update` refuses a change that adds a problem, before anything is sent.
- `selfcheck` runs the rules and appkit's round trip against the live log after
  every deploy, so a release that would drop a stored value is rolled back
  before it writes.
- the nightly backup judges them after every copy is in R2.

**Only the impossible is a rule here**: negative reps or load, a load that isn't
a finite number, a date that doesn't parse, two of one thing. What is merely
implausible (120 reps, 600 kg) is refused at the form instead (`api/routes.py`),
so an old typo in the log can't block every deploy.

**A problem names where, never what.** A session is named by its date and day,
which is its key (`Log.with_entry`); an entry and a set by their position in it.
Never an exercise name, a note, or a weight: these lines reach Log Analytics.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from datetime import date
from typing import Any

from .model import Log

__all__ = ["Parsed", "check", "check_parsed", "dump", "parse"]

# What `parse` hands appkit: the log, and the schema stamp the stored bytes carry.
Parsed = tuple[Log, Any]


def parse(raw: bytes) -> Parsed:
    """The log, and the stored `schema` stamp for `dump` to put back.

    `Log.to_json` always writes the current `SCHEMA_VERSION`. A release that bumps
    it is a deliberate migration, not data lost, so the round trip compares the
    data with the stored stamp kept.

    Only the stamp: a release whose reader reshapes stored fields (renames
    `weight`, say) still has them reported as dropped, and needs its first
    write to happen before a release that relies on the new shape.

    The plain model parse: never the backup's checking wrapper, which calls this.
    """
    stamp = json.loads(raw).get("schema")
    return Log.from_json(raw.decode("utf-8")), stamp


def dump(parsed: Parsed) -> str:
    log, stamp = parsed
    payload = json.loads(log.to_json())
    if stamp is not None:
        payload["schema"] = stamp
    return json.dumps(payload)


def check_parsed(parsed: Parsed) -> list[str]:
    return check(parsed[0])


def check(log: Log) -> list[str]:
    """One line per broken rule; empty when the log is consistent."""
    problems: list[str] = []

    for name, ids in (
        ("blocks", (b.id for b in log.blocks)),
        ("achievements", (a.id for a in log.achievements)),
        ("conditions", (c.id for c in log.conditions)),
        ("goals", (g.id for g in log.goals)),
        ("insights", (i.id for i in log.insights)),
        ("garmin_activities", (a.id for a in log.garmin_activities)),
    ):
        problems += _duplicates(name, "id", ids)
    problems += _duplicates("garmin_days", "date", (d.date for d in log.garmin_days))
    problems += _duplicates("garmin_fitness", "date", (f.date for f in log.garmin_fitness))

    keys: Counter[tuple[str, str]] = Counter()
    for position, session in enumerate(log.sessions):
        if _is_date(session.date):
            keys[(session.date, session.day)] += 1
            where = f"sessions (date {session.date}, day {session.day})"
        else:
            # The date itself is what's wrong and may be free text, so a position.
            # Its sets are still checked: skipped, they would surface as "new"
            # problems the moment the date was fixed, and refuse that fix.
            where = f"sessions[{position}]"
            problems.append(f"{where}: date is not YYYY-MM-DD")

        exercises = Counter(e.exercise for e in session.entries)
        repeated = sum(1 for n in exercises.values() if n > 1)
        if repeated:
            problems.append(f"{where}: {repeated} exercise(s) recorded twice")

        for e, entry in enumerate(session.entries):
            if entry.seconds < 0:
                problems.append(f"{where} entry {e}: negative time")
            for s, logged in enumerate(entry.sets):
                if logged.reps < 0:
                    problems.append(f"{where} entry {e} set {s}: negative reps")
                if not math.isfinite(logged.weight):
                    problems.append(f"{where} entry {e} set {s}: load is not a number")
                elif logged.weight < 0:
                    problems.append(f"{where} entry {e} set {s}: negative load")

    for (when, day), count in sorted(keys.items()):
        if count > 1:
            problems.append(f"sessions (date {when}, day {day}): recorded {count} times")

    return problems


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_date(text: str) -> bool:
    """`YYYY-MM-DD` and a real day. The pattern first: `fromisoformat` also takes
    ISO week dates (`2024-W01-1`), which sort wrongly and match no lookup."""
    if not _DATE.fullmatch(text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _duplicates(name: str, key: str, values: Iterable[str]) -> list[str]:
    counts = Counter(values)
    return [f"{name}: {key} {v} is used {n} times" for v, n in sorted(counts.items()) if n > 1]
