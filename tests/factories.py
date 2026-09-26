"""Small builders, so a test names only what it is actually about."""

from __future__ import annotations

from gymlog.model import (
    Achievement,
    Block,
    Condition,
    Day,
    Entry,
    Exercise,
    GarminActivity,
    GarminDay,
    GarminFitness,
    Goal,
    Insight,
    Session,
    SetLog,
)


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


def block(
    block_id: str = "2026-09-01",
    *exercises: Exercise,
    name: str = "Test block",
    started: str | None = None,
    weeks: int = 8,
) -> Block:
    return Block(
        id=block_id,
        name=name,
        started=started if started is not None else block_id,
        weeks=weeks,
        days={"A": Day(label="Tues", exercises=exercises or (exercise(),))},
    )


def session(
    date: str = "2026-09-15",
    block_id: str = "2026-09-01",
    day: str = "A",
    *entries: Entry,
) -> Session:
    return Session(date=date, block=block_id, day=day, entries=entries)


def achievement(
    id: str = "ach1", date: str = "2026-09-15", title: str = "Ran a 5k", note: str = ""
) -> Achievement:
    return Achievement(id=id, date=date, title=title, note=note)


def condition(
    id: str = "cond1",
    body_part: str = "lower back",
    started: str = "2026-09-01",
    status: str = "active",
    resolved: str = "",
    note: str = "",
) -> Condition:
    return Condition(
        id=id, body_part=body_part, started=started, status=status, resolved=resolved, note=note
    )


def goal(
    id: str = "goal1",
    title: str = "Bench press 100kg",
    target_date: str = "",
    status: str = "active",
    note: str = "",
) -> Goal:
    return Goal(id=id, title=title, target_date=target_date, status=status, note=note)


def insight(
    id: str = "ins1",
    week_of: str = "2026-09-15",
    summary: str = "Good week.",
    generated_at: str = "2026-09-15",
) -> Insight:
    return Insight(id=id, week_of=week_of, summary=summary, generated_at=generated_at)


def garmin_activity(
    id: str = "act1",
    date: str = "2026-09-15",
    activity_type: str = "running",
    duration_seconds: int = 1800,
    avg_hr: int = 140,
    max_hr: int = 165,
    distance_meters: int = 5000,
    zone_seconds: tuple[int, ...] = (60, 300, 900, 480, 60),
    zone_low_bpm: tuple[int, ...] = (96, 114, 132, 150, 161),
) -> GarminActivity:
    return GarminActivity(
        id=id,
        date=date,
        activity_type=activity_type,
        duration_seconds=duration_seconds,
        avg_hr=avg_hr,
        max_hr=max_hr,
        distance_meters=distance_meters,
        zone_seconds=zone_seconds,
        zone_low_bpm=zone_low_bpm,
    )


def garmin_day(
    date: str = "2026-09-15",
    steps: int = 8000,
    resting_hr: int = 55,
    sleep_seconds: int = 27000,
    hrv_ms: int = 0,
    hrv_status: str = "",
) -> GarminDay:
    return GarminDay(
        date=date,
        steps=steps,
        resting_hr=resting_hr,
        sleep_seconds=sleep_seconds,
        hrv_ms=hrv_ms,
        hrv_status=hrv_status,
    )


def garmin_fitness(
    date: str = "2026-09-15",
    vo2max: float = 52.0,
    lactate_threshold_bpm: int = 165,
    lactate_threshold_pace_seconds_per_km: int = 258,
) -> GarminFitness:
    return GarminFitness(
        date=date,
        vo2max=vo2max,
        lactate_threshold_bpm=lactate_threshold_bpm,
        lactate_threshold_pace_seconds_per_km=lactate_threshold_pace_seconds_per_km,
    )
