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
class Session:
    """One training session, as performed. Never modified once written."""

    date: str
    block: str
    day: str
    entries: tuple[Entry, ...] = ()

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
        """Append a session. The only mutation this model allows."""
        return replace(self, sessions=(*self.sessions, session))

    def with_block(self, block: Block) -> Log:
        """Add or replace a block by id."""
        others = tuple(b for b in self.blocks if b.id != block.id)
        return replace(self, blocks=(*others, block))

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "slots": list(self.slots),
                "blocks": [b.to_json() for b in sorted(self.blocks, key=lambda b: b.started)],
                "sessions": [s.to_json() for s in sorted(self.sessions, key=lambda s: s.date)],
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
        )
