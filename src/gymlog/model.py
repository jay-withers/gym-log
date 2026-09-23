"""The training log: blocks, sessions and the sets actually performed.

The shape exists to fix the one thing Gym_3.xlsx could not do. That sheet held a
single achieved-reps and weight cell per exercise, overwritten every session, so
`Gym_1` and `Gym_2` left nothing behind. Here `sessions` is **append-only** and
nothing is ever overwritten.

Two ideas carry the design:

- A **slot** (chest, back, legs, ...) is stable across blocks; the *exercise*
  filling it is not. Keeping the slot on every entry is what lets the chest slot
  be compared across an exercise change, which is exactly what rotating every
  eight weeks otherwise destroys.
- A **block** is the eight-week rotation. It owns the prescription — sets, rep
  range, rest — and the sessions reference it, so changing next block's
  prescription cannot retroactively alter what a past session was asked to do.

Everything here is a frozen dataclass with explicit JSON conversion rather than
pydantic models. The document is written by this app and read by this app; what
is wanted is a shape that survives being hand-edited in the portal and round
tripped, which `from_json` tolerating missing fields gives.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

# Bumped when the shape changes incompatibly. A document from the future is left
# alone rather than overwritten — the alternative is a newer deployment silently
# discarding a training history it could not read.
SCHEMA_VERSION = 1

# The seven slots off the spreadsheet, in the order they are performed. `finisher`
# is deliberately last and deliberately untracked (see Exercise.tracked): Sled Push
# and Sandbag Lunges carried no sets, reps or weight in the sheet either.
DEFAULT_SLOTS: tuple[str, ...] = (
    "chest",
    "back",
    "legs",
    "shoulders",
    "triceps",
    "biceps",
    "finisher",
)


@dataclass(frozen=True)
class Exercise:
    """One prescribed movement within a block: what to do, not what was done."""

    slot: str
    name: str
    sets: int = 0
    # The range to work within — the sheet's `Reps` beside `Sets`, `8-10` for DB
    # Step Ups. This is what double progression climbs, and what the session
    # screen shows above the inputs as the thing to aim at.
    rep_low: int = 0
    rep_high: int = 0
    # The number to hit on each set in turn — the sheet's `Reps` beside `Weight`,
    # written `10/12/12` when the sets differ. Held per set rather than reduced
    # to a range because which set is which is the whole point of writing it
    # that way. Empty for a movement the sheet gave no per-set numbers.
    rep_targets: tuple[int, ...] = ()
    rest_seconds: int = 0
    # The smallest useful jump *for this movement*. Per-exercise rather than
    # global because the logged weights are mixed — 7.5, 55, 24 and 5 all came
    # off the same sheet, and a cable stack, a machine and a dumbbell rack do
    # not share a step.
    increment: float = 2.5
    # The weight recorded in the spreadsheet at import, used only until the
    # first real session supersedes it. Not a log entry: it carries no date, and
    # inventing one would put a lie at the head of the history.
    seed_weight: float | None = None

    @property
    def rep_range_label(self) -> str:
        """`8-10`, or a bare `10` when the range has no width. Empty if untracked."""
        if not self.rep_high:
            return ""
        if self.rep_low == self.rep_high:
            return str(self.rep_high)
        return f"{self.rep_low}\u2013{self.rep_high}"

    def target_for(self, position: int) -> int:
        """The rep target for set `position`, 0-based.

        Falls back to `rep_low` rather than raising for a position the sheet did
        not describe: a block edited by hand in the portal can carry fewer
        targets than sets, and a session form that renders is better than one
        that 500s over a placeholder.
        """
        if position < len(self.rep_targets):
            return self.rep_targets[position]
        return self.rep_low

    @property
    def tracked(self) -> bool:
        """Whether this movement takes a load and rep target at all.

        The finisher does not. It is performed and ticked off, and asking for
        three weighted sets of a sled push would be noise in every session.
        """
        return self.sets > 0 and self.rep_high > 0

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "slot": self.slot,
            "name": self.name,
            "sets": self.sets,
            "rep_low": self.rep_low,
            "rep_high": self.rep_high,
            "rest_seconds": self.rest_seconds,
            "increment": self.increment,
        }
        if self.rep_targets:
            payload["rep_targets"] = list(self.rep_targets)
        if self.seed_weight is not None:
            payload["seed_weight"] = self.seed_weight
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Exercise:
        return cls(
            slot=str(payload.get("slot", "")),
            name=str(payload.get("name", "")),
            sets=int(payload.get("sets", 0) or 0),
            rep_low=int(payload.get("rep_low", 0) or 0),
            rep_high=int(payload.get("rep_high", 0) or 0),
            rep_targets=tuple(int(r) for r in payload.get("rep_targets", []) or ()),
            rest_seconds=int(payload.get("rest_seconds", 0) or 0),
            increment=float(payload.get("increment", 2.5) or 2.5),
            seed_weight=(
                float(payload["seed_weight"]) if payload.get("seed_weight") is not None else None
            ),
        )


@dataclass(frozen=True)
class Day:
    """One of the two weekly sessions. `label` is what the sheet called it."""

    label: str
    exercises: tuple[Exercise, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"label": self.label, "exercises": [e.to_json() for e in self.exercises]}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Day:
        return cls(
            label=str(payload.get("label", "")),
            exercises=tuple(
                Exercise.from_json(e) for e in payload.get("exercises", []) if isinstance(e, dict)
            ),
        )


@dataclass(frozen=True)
class Block:
    """An eight-week rotation. `id` is the start date, which is unique enough."""

    id: str
    name: str
    started: str
    weeks: int = 8
    days: dict[str, Day] = field(default_factory=dict)

    def week_of(self, today: date) -> int:
        """Which week of the block `today` falls in, 1-based.

        Runs past `weeks` rather than clamping: being in week 10 of an eight-week
        block is the signal to rotate, and hiding it behind a clamp would be
        hiding the one thing this number is for.
        """
        try:
            started = date.fromisoformat(self.started)
        except ValueError:
            return 1
        return max(1, (today - started).days // 7 + 1)

    def due(self, today: date) -> bool:
        return self.week_of(today) > self.weeks

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "started": self.started,
            "weeks": self.weeks,
            "days": {k: v.to_json() for k, v in self.days.items()},
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Block:
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            started=str(payload.get("started", "")),
            weeks=int(payload.get("weeks", 8) or 8),
            days={
                str(k): Day.from_json(v)
                for k, v in (payload.get("days") or {}).items()
                if isinstance(v, dict)
            },
        )


@dataclass(frozen=True)
class SetLog:
    """One set, as performed."""

    reps: int
    weight: float

    def to_json(self) -> dict[str, Any]:
        return {"reps": self.reps, "weight": self.weight}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SetLog:
        return cls(reps=int(payload.get("reps", 0) or 0), weight=float(payload.get("weight", 0)))


@dataclass(frozen=True)
class Entry:
    """Every set performed on one exercise in one session.

    `exercise` is stored as a name rather than a reference into the block. The
    block's prescription can be edited afterwards; what was performed cannot, and
    a log that changes when you fix a typo in a rep range is not a log.
    """

    slot: str
    exercise: str
    sets: tuple[SetLog, ...] = ()
    note: str = ""
    # How long it took, for a movement timed rather than counted — the sled push
    # and the sandbag lunges. Optional even for those: the finisher is performed
    # whether or not anyone started a clock, so 0 means untimed, not zero
    # seconds, and `sets` stays empty because there is no load to record.
    seconds: int = 0

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "slot": self.slot,
            "exercise": self.exercise,
            "sets": [s.to_json() for s in self.sets],
        }
        if self.note:
            payload["note"] = self.note
        if self.seconds:
            payload["seconds"] = self.seconds
        return payload

    @property
    def time_label(self) -> str:
        """`90` -> `1:30`, `45` -> `0:45`. Empty when untimed."""
        if not self.seconds:
            return ""
        return f"{self.seconds // 60}:{self.seconds % 60:02d}"

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Entry:
        return cls(
            slot=str(payload.get("slot", "")),
            exercise=str(payload.get("exercise", "")),
            sets=tuple(SetLog.from_json(s) for s in payload.get("sets", []) if isinstance(s, dict)),
            note=str(payload.get("note", "")),
            seconds=int(payload.get("seconds", 0) or 0),
        )


@dataclass(frozen=True)
class Achievement:
    """A milestone logged by hand — not derived from lift data."""

    id: str
    date: str
    title: str
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "date": self.date, "title": self.title}
        if self.note:
            payload["note"] = self.note
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Achievement:
        return cls(
            id=str(payload.get("id", "")),
            date=str(payload.get("date", "")),
            title=str(payload.get("title", "")),
            note=str(payload.get("note", "")),
        )


@dataclass(frozen=True)
class Condition:
    """An injury or medical condition. Resolved by resubmitting the same `id`."""

    id: str
    body_part: str
    started: str
    status: str = "active"
    resolved: str = ""
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "body_part": self.body_part,
            "started": self.started,
            "status": self.status,
        }
        if self.resolved:
            payload["resolved"] = self.resolved
        if self.note:
            payload["note"] = self.note
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Condition:
        return cls(
            id=str(payload.get("id", "")),
            body_part=str(payload.get("body_part", "")),
            started=str(payload.get("started", "")),
            status=str(payload.get("status", "active") or "active"),
            resolved=str(payload.get("resolved", "")),
            note=str(payload.get("note", "")),
        )


@dataclass(frozen=True)
class Goal:
    """A free-form target. Achieved by resubmitting the same `id`."""

    id: str
    title: str
    target_date: str = ""
    status: str = "active"
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "title": self.title, "status": self.status}
        if self.target_date:
            payload["target_date"] = self.target_date
        if self.note:
            payload["note"] = self.note
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Goal:
        return cls(
            id=str(payload.get("id", "")),
            title=str(payload.get("title", "")),
            target_date=str(payload.get("target_date", "")),
            status=str(payload.get("status", "active") or "active"),
            note=str(payload.get("note", "")),
        )


@dataclass(frozen=True)
class Insight:
    """A weekly AI-generated summary, written only by the scheduled job."""

    id: str
    week_of: str
    summary: str
    generated_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "week_of": self.week_of,
            "summary": self.summary,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Insight:
        return cls(
            id=str(payload.get("id", "")),
            week_of=str(payload.get("week_of", "")),
            summary=str(payload.get("summary", "")),
            generated_at=str(payload.get("generated_at", "")),
        )


@dataclass(frozen=True)
class Session:
    """One training session, as performed.

    **Filled in as the session happens, then never touched again.** Each
    exercise is saved as it is finished, so a session grows an entry at a time
    over the hour it takes — losing the phone half way costs the exercise in
    progress rather than the whole morning.

    The append-only rule the log is built on is about *history*, and it still
    holds: `with_entry` only ever reaches the session for today's date and day.
    Yesterday's is as immutable as it ever was.
    """

    date: str
    block: str
    day: str
    entries: tuple[Entry, ...] = ()

    def with_entry(self, entry: Entry) -> Session:
        """Add `entry`, or replace the one already recorded for that exercise.

        Replacing rather than appending is what makes a correction possible: an
        11 typed where a 1 was meant is noticed on the next set, and the only
        way to fix it otherwise would be to leave both in the log.
        """
        others = tuple(e for e in self.entries if e.exercise != entry.exercise)
        if len(others) == len(self.entries):
            return replace(self, entries=(*self.entries, entry))
        # Rebuilt in place so re-saving an exercise does not move it to the end,
        # which would make the log disagree with the order they were performed.
        return replace(
            self,
            entries=tuple(entry if e.exercise == entry.exercise else e for e in self.entries),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "block": self.block,
            "day": self.day,
            "entries": [e.to_json() for e in self.entries],
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Session:
        return cls(
            date=str(payload.get("date", "")),
            block=str(payload.get("block", "")),
            day=str(payload.get("day", "")),
            entries=tuple(
                Entry.from_json(e) for e in payload.get("entries", []) if isinstance(e, dict)
            ),
        )


@dataclass(frozen=True)
class Log:
    """The whole document. One of these is the entire persisted state."""

    slots: tuple[str, ...] = DEFAULT_SLOTS
    blocks: tuple[Block, ...] = ()
    sessions: tuple[Session, ...] = ()
    achievements: tuple[Achievement, ...] = ()
    conditions: tuple[Condition, ...] = ()
    goals: tuple[Goal, ...] = ()
    insights: tuple[Insight, ...] = ()

    @property
    def current_block(self) -> Block | None:
        """The most recently started block, or None before the first import."""
        if not self.blocks:
            return None
        return max(self.blocks, key=lambda b: b.started)

    def block(self, block_id: str) -> Block | None:
        return next((b for b in self.blocks if b.id == block_id), None)

    def sessions_for(self, exercise: str) -> tuple[Session, ...]:
        """Every session containing `exercise`, most recent first."""
        return tuple(
            s
            for s in sorted(self.sessions, key=lambda s: s.date, reverse=True)
            if any(e.exercise == exercise for e in s.entries)
        )

    def last_entry(self, exercise: str) -> tuple[Entry, str] | None:
        """The most recent performance of `exercise`, with the date it happened."""
        for session in self.sessions_for(exercise):
            for entry in session.entries:
                # `or entry.seconds`: the finisher records a time and no sets,
                # and it is still the last thing that happened on that movement.
                if entry.exercise == exercise and (entry.sets or entry.seconds):
                    return entry, session.date
        return None

    def history(self, slot: str) -> tuple[tuple[str, str, SetLog], ...]:
        """Every logged set for a slot as (date, exercise, heaviest set), oldest first.

        Keyed on the slot rather than the exercise deliberately — that is the
        whole reason the slot is stored, and it is what makes a rotation
        comparable rather than a reset.
        """
        out: list[tuple[str, str, SetLog]] = []
        for session in sorted(self.sessions, key=lambda s: s.date):
            for entry in session.entries:
                if entry.slot == slot and entry.sets:
                    heaviest = max(entry.sets, key=lambda s: s.weight)
                    out.append((session.date, entry.exercise, heaviest))
        return tuple(out)

    def with_session(self, session: Session) -> Log:
        """Append a whole session."""
        return replace(self, sessions=(*self.sessions, session))

    def with_entry(self, when: str, block: str, day: str, entry: Entry) -> Log:
        """Record one exercise into today's session, starting it if need be.

        The session for a date and day is the unit, not the submit: six saves
        over an hour produce one row in the log, because one training session is
        what happened.
        """
        for index, session in enumerate(self.sessions):
            if session.date == when and session.day == day:
                updated = session.with_entry(entry)
                return replace(
                    self,
                    sessions=(*self.sessions[:index], updated, *self.sessions[index + 1 :]),
                )
        started = Session(date=when, block=block, day=day, entries=(entry,))
        return replace(self, sessions=(*self.sessions, started))

    def session_on(self, when: str, day: str) -> Session | None:
        """Today's session for `day`, if it has been started."""
        return next(
            (s for s in self.sessions if s.date == when and s.day == day),
            None,
        )

    def excluding(self, when: str, day: str) -> Log:
        """This log without one session.

        Used to work out what to suggest *for* a session while it is being
        filled in: with today's entries included, every suggestion would chase
        the set that had just been typed into it.
        """
        return replace(
            self,
            sessions=tuple(s for s in self.sessions if not (s.date == when and s.day == day)),
        )

    def with_block(self, block: Block) -> Log:
        """Add or replace a block by id."""
        others = tuple(b for b in self.blocks if b.id != block.id)
        return replace(self, blocks=(*others, block))

    def with_achievement(self, achievement: Achievement) -> Log:
        """Add or replace an achievement by id — resubmitting edits it."""
        others = tuple(a for a in self.achievements if a.id != achievement.id)
        return replace(self, achievements=(*others, achievement))

    def without_achievement(self, achievement_id: str) -> Log:
        return replace(
            self, achievements=tuple(a for a in self.achievements if a.id != achievement_id)
        )

    def with_condition(self, condition: Condition) -> Log:
        """Add or replace a condition by id — resubmitting edits or resolves it."""
        others = tuple(c for c in self.conditions if c.id != condition.id)
        return replace(self, conditions=(*others, condition))

    def without_condition(self, condition_id: str) -> Log:
        return replace(self, conditions=tuple(c for c in self.conditions if c.id != condition_id))

    def with_goal(self, goal: Goal) -> Log:
        """Add or replace a goal by id — resubmitting edits or achieves it."""
        others = tuple(g for g in self.goals if g.id != goal.id)
        return replace(self, goals=(*others, goal))

    def without_goal(self, goal_id: str) -> Log:
        return replace(self, goals=tuple(g for g in self.goals if g.id != goal_id))

    def with_insight(self, insight: Insight) -> Log:
        """Append a weekly insight."""
        return replace(self, insights=(*self.insights, insight))

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "slots": list(self.slots),
                "blocks": [b.to_json() for b in sorted(self.blocks, key=lambda b: b.started)],
                "sessions": [s.to_json() for s in sorted(self.sessions, key=lambda s: s.date)],
                "achievements": [
                    a.to_json() for a in sorted(self.achievements, key=lambda a: a.date)
                ],
                "conditions": [
                    c.to_json() for c in sorted(self.conditions, key=lambda c: c.started)
                ],
                "goals": [g.to_json() for g in sorted(self.goals, key=lambda g: g.id)],
                "insights": [i.to_json() for i in sorted(self.insights, key=lambda i: i.week_of)],
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> Log:
        """Parse a log document, tolerating anything but invalid JSON and a future schema.

        Every field is optional and defaulted: a document written by an older
        version, or edited by hand in the portal, should cost at most the fields
        it is missing.
        """
        payload: dict[str, Any] = json.loads(raw)
        version = int(payload.get("schema", SCHEMA_VERSION))
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"log document is schema {version}, this build understands "
                f"{SCHEMA_VERSION}. Refusing to read it rather than overwriting it."
            )
        slots = tuple(str(s) for s in payload.get("slots") or ()) or DEFAULT_SLOTS
        return cls(
            slots=slots,
            blocks=tuple(
                Block.from_json(b) for b in payload.get("blocks", []) if isinstance(b, dict)
            ),
            sessions=tuple(
                Session.from_json(s) for s in payload.get("sessions", []) if isinstance(s, dict)
            ),
            achievements=tuple(
                Achievement.from_json(a)
                for a in payload.get("achievements", []) or ()
                if isinstance(a, dict)
            ),
            conditions=tuple(
                Condition.from_json(c)
                for c in payload.get("conditions", []) or ()
                if isinstance(c, dict)
            ),
            goals=tuple(
                Goal.from_json(g) for g in payload.get("goals", []) or () if isinstance(g, dict)
            ),
            insights=tuple(
                Insight.from_json(i)
                for i in payload.get("insights", []) or ()
                if isinstance(i, dict)
            ),
        )
