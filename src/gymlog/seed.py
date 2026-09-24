"""A realistic local log, for working on the app without Azure.

**The point is that the shape is identical.** This builds a `Log` and writes it
through `store.save`, so what a local run reads back has been through the same
`to_json`/`from_json` round trip as the blob — a field this forgets to set is a
field the deployed app would find missing, and the local run finds it missing
too rather than quietly rendering something the real document never contains.

The block mirrors Gym_3.xlsx in structure rather than copying it: two days,
seven slots, a rep range beside a per-set target, a finisher with no load. The
sessions are invented, and are chosen to put the session screen into every state
it has rather than to look like anyone's actual training:

- **chest** climbs inside the range, so the suggestion reads "go for one more".
- **back** hit the top of the range on every set, so the weight goes up.
- **legs** fell short, so the weight holds and the target line goes amber.
- **shoulders** has one session, the least history that is still history.
- **triceps** and **biceps** have none, so they show their seed weight.
- the **finisher** carries a time on some sessions and not others, because it is
  optional and that is worth being able to see.

Achievements, goals, injuries/conditions and the insight get the same
treatment: a handful of invented entries chosen to put those screens into more
than one state (active and achieved, active and resolved) rather than left
empty, since an empty section is the one state local work has the least
reason to look at.
"""

from __future__ import annotations

from datetime import date, timedelta

from .model import (
    Achievement,
    Block,
    Condition,
    Day,
    Entry,
    Exercise,
    Goal,
    Insight,
    Log,
    Session,
    SetLog,
)

# Deliberately not the real numbers off the sheet. Close enough in shape that
# the screen looks right, far enough that nobody mistakes this for the log.
_DAY_A: tuple[Exercise, ...] = (
    Exercise("chest", "Low-to-High Cable Flyes", 3, 10, 12, (12, 12, 12), 65, 2.5, 7.5),
    Exercise("back", "Wide Seated Machine Row", 3, 10, 12, (12, 12, 12), 90, 5.0, 55.0),
    Exercise("legs", "DB Step Ups", 3, 8, 10, (10, 10, 10), 90, 2.0, 24.0),
    Exercise("shoulders", "Lean Cable Lat Raises", 3, 12, 15, (15, 15, 15), 60, 1.0, 5.0),
    Exercise("triceps", "Overhead Cable Tricep Ext", 3, 10, 12, (10, 12, 12), 60, 2.5, 15.0),
    Exercise("biceps", "EZ-Bar Curls", 3, 10, 12, (10, 10, 12), 60, 1.25, 23.0),
    Exercise("finisher", "Sled Push"),
)

_DAY_B: tuple[Exercise, ...] = (
    Exercise("chest", "High-to-Low Cable Flyes", 3, 10, 12, (12, 12, 12), 90, 2.5, 12.5),
    Exercise("back", "Wide Lat Pulldown", 3, 8, 10, (10, 10, 10), 90, 5.0, 60.0),
    Exercise("legs", "Lying Leg Curls", 3, 8, 10, (10, 10, 10), 90, 2.5, 35.0),
    Exercise("shoulders", "DB Rear Delt Flyes", 3, 12, 15, (12, 12, 15), 60, 1.0, 8.0),
    Exercise("triceps", "Rope Pushdowns", 3, 10, 12, (10, 10, 12), 60, 2.5, 22.5),
    Exercise("biceps", "Incline DB Bicep Curls", 3, 10, 12, (10, 12, 12), 60, 1.0, 12.0),
    Exercise("finisher", "Sandbag Lunges"),
)


def sample_log(today: date | None = None) -> Log:
    """A block part-way through, with enough history to exercise every screen."""
    now = today or date.today()
    # Four weeks in: far enough to have history, not so far the block is due to
    # rotate, which is a banner worth having to ask for rather than always on.
    started = now - timedelta(days=21)
    block = Block(
        id=started.isoformat(),
        name="Sample block",
        started=started.isoformat(),
        days={"A": Day("Tues", _DAY_A), "B": Day("Thur", _DAY_B)},
    )
    return Log(
        blocks=(block,),
        sessions=_sessions(block, started),
        achievements=_achievements(now),
        conditions=_conditions(now),
        goals=_goals(now),
        insights=_insights(now),
    )


def _sessions(block: Block, started: date) -> tuple[Session, ...]:
    """Three weeks of day A, plus two of day B. Day A is deliberately ahead.

    Uneven on purpose: `_next_day` picks whichever was not done last, and two
    days with identical history would never show that working.
    """
    out: list[Session] = []
    for week in range(3):
        when = started + timedelta(days=week * 7)
        out.append(
            Session(
                date=when.isoformat(),
                block=block.id,
                day="A",
                entries=(
                    # Climbing inside the range: 10, then 11, then 12.
                    _even("chest", "Low-to-High Cable Flyes", 10 + week, 7.5),
                    # At the top every time, so the next suggestion adds weight.
                    _even("back", "Wide Seated Machine Row", 12, 55.0 + week * 5),
                    # Short of the bottom of 8-10 on the last one: stalled.
                    _even("legs", "DB Step Ups", 10 if week < 2 else 6, 24.0),
                    *((_even("shoulders", "Lean Cable Lat Raises", 13, 5.0),) if week == 2 else ()),
                    # Timed twice, untimed once: the field is optional.
                    Entry(
                        slot="finisher",
                        exercise="Sled Push",
                        note="done",
                        seconds=(0, 95, 88)[week],
                    ),
                ),
            )
        )

    for week in range(2):
        when = started + timedelta(days=week * 7 + 2)
        out.append(
            Session(
                date=when.isoformat(),
                block=block.id,
                day="B",
                entries=(
                    _even("chest", "High-to-Low Cable Flyes", 11, 12.5),
                    _even("back", "Wide Lat Pulldown", 9 + week, 60.0),
                    Entry(slot="finisher", exercise="Sandbag Lunges", note="done", seconds=120),
                ),
            )
        )
    return tuple(sorted(out, key=lambda s: s.date))


def _achievements(now: date) -> tuple[Achievement, ...]:
    return (
        Achievement(
            id="sample-5k",
            date=(now - timedelta(days=10)).isoformat(),
            title="Ran a 5k",
            note="Personal best, 24:10",
        ),
        Achievement(
            id="sample-bodyweight-row",
            date=(now - timedelta(days=3)).isoformat(),
            title="First strict bodyweight row",
        ),
    )


def _conditions(now: date) -> tuple[Condition, ...]:
    # `Log.to_json` writes these sorted by `started`, and a real document is
    # therefore always in that order once it has been saved once — built that
    # way here too, rather than insertion order, so the round trip in
    # test_seed.py holds the same way it would for a document that came from
    # the blob.
    return (
        Condition(
            id="sample-shoulder",
            body_part="left shoulder",
            started=(now - timedelta(days=60)).isoformat(),
            status="resolved",
            resolved=(now - timedelta(days=30)).isoformat(),
            note="Impingement from overhead pressing, cleared up with rehab band work",
        ),
        Condition(
            id="sample-lower-back",
            body_part="lower back",
            started=(now - timedelta(days=14)).isoformat(),
            status="active",
            note="Tight after deadlifts — mobility work before legs day",
        ),
    )


def _goals(now: date) -> tuple[Goal, ...]:
    # Sorted by id, for the same reason `_conditions` is sorted by `started`.
    return (
        Goal(
            id="sample-5k-under-25",
            title="Run a 5k under 25 minutes",
            status="achieved",
            note="Hit 24:10 in September",
        ),
        Goal(
            id="sample-bench-100",
            title="Bench press 100kg",
            target_date=(now + timedelta(days=90)).isoformat(),
            status="active",
            note="Currently at 85kg for 3x8",
        ),
        Goal(id="sample-pullups", title="10 strict pull-ups"),
    )


def _insights(now: date) -> tuple[Insight, ...]:
    return (
        Insight(
            id="sample-insight",
            week_of=now.isoformat(),
            summary=(
                "Solid week: chest and back both climbed inside their rep ranges, "
                "with back ready for a weight increase next session. Legs fell just "
                "short of the bottom of the range on the last set, so that weight "
                "holds rather than drops — nothing to worry about after three good "
                "weeks in a row. The lower back is still listed as an active "
                "condition, so keep the mobility work in before leg day."
            ),
            generated_at=now.isoformat(),
        ),
    )


def _even(slot: str, name: str, reps: int, weight: float) -> Entry:
    """Three sets at the same reps and weight.

    Even sets rather than a tapering 12/11/9, because `suggest` keys off the
    weakest set — sample data that tapered would make every suggestion look like
    it was ignoring the first two sets.
    """
    return Entry(
        slot=slot,
        exercise=name,
        sets=tuple(SetLog(reps=reps, weight=weight) for _ in range(3)),
    )
