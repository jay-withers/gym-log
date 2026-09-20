"""Small builders, so a test names only what it is actually about."""

from __future__ import annotations

from gymlog.model import Block, Day, Entry, Exercise, Session, SetLog


def exercise(
    slot: str = "chest",
    name: str = "Cable Flyes",
    sets: int = 3,
    rep_low: int = 10,
    rep_high: int = 12,
    increment: float = 2.5,
    seed_weight: float | None = None,
    rest_seconds: int = 60,
    rep_targets: tuple[int, ...] = (),
) -> Exercise:
    return Exercise(
        slot=slot,
        name=name,
        sets=sets,
        rep_low=rep_low,
        rep_high=rep_high,
        rep_targets=rep_targets,
        rest_seconds=rest_seconds,
        increment=increment,
        seed_weight=seed_weight,
    )


def entry(
    name: str = "Cable Flyes",
    slot: str = "chest",
    *reps_weights: tuple[int, float],
) -> Entry:
    return Entry(
        slot=slot,
        exercise=name,
        sets=tuple(SetLog(reps=r, weight=w) for r, w in reps_weights),
    )


def block(block_id: str = "2026-09-01", *exercises: Exercise) -> Block:
    return Block(
        id=block_id,
        name="Test block",
        started=block_id,
        days={"A": Day(label="Tues", exercises=exercises or (exercise(),))},
    )


def session(
    date: str = "2026-09-15",
    block_id: str = "2026-09-01",
    day: str = "A",
    *entries: Entry,
) -> Session:
    return Session(date=date, block=block_id, day=day, entries=entries)
