"""Garmin Connect sync: activities, per-activity heart rate zones, daily summaries.

Garmin has no public API for personal accounts, so this uses the unofficial
`garminconnect` library against Garmin Connect's own web endpoints. Mirrors
insights.py's shape: a pure-ish function that fetches and returns data,
never touching `store` itself for the log — the caller decides how it's
merged in. Session persistence is the one exception (see `_client` below),
since re-authenticating with a password on every run risks exactly the kind
of friction (MFA challenges, rate limiting) an unofficial API punishes.

Field names below (`activityId`, `startTimeLocal`, `activityType.typeKey`,
`averageHR`/`maxHR`, `distance`, `totalSteps`, `restingHeartRate`,
`dailySleepDTO.sleepTimeSeconds`, `zoneNumber`/`secsInZone`/`zoneLowBoundary`,
`hrvSummary.lastNightAvg`/`.status`, `generic.vo2MaxPreciseValue`/
`.vo2MaxValue`, `speed_and_heart_rate.speed`/`.heartRate`) are Garmin's own,
confirmed against community-documented response shapes rather than this
library's (minimal) type hints — Garmin can change them without notice,
which is why every read here is defensive (`.get()` with a fallback), never
a bare index.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .model import GARMIN_ZONE_COUNT, GarminActivity, GarminDay, GarminFitness
from .settings import secret

logger = logging.getLogger(__name__)

# The trailing window each sync actually asks Garmin Connect for. Short on
# purpose: a day already synced doesn't change, so re-fetching it every run
# spends an API call per day (three, counting sleep and HRV) plus one per
# activity for zones on data that hasn't moved. A missed day still self-heals
# within a week, and Log.with_garmin_sync merges each run's window on top of
# what is already there rather than replacing it, so history outside this
# window survives untouched as long as it stays within GARMIN_RETENTION_DAYS
# below.
GARMIN_SYNC_DAYS = 7

# How far back the log itself keeps Garmin data, enforced at write time by
# Log.with_garmin_sync rather than anything here — a fixed rolling window
# rather than a growing archive. Deliberately wider than GARMIN_SYNC_DAYS:
# retention is how much history the app is willing to hold onto, fetching is
# how much of it any one sync bothers re-asking Garmin about, and conflating
# the two would mean either re-fetching 90 days on every run or only ever
# retaining 7.
GARMIN_RETENTION_DAYS = 90


def sync_garmin(
    today: date | None = None, days: int | None = None
) -> tuple[list[GarminActivity], list[GarminDay], list[GarminFitness]]:
    """Fetch the trailing `days` (default `GARMIN_SYNC_DAYS`) of activities and daily summaries.

    An auth failure (bad credentials, an MFA challenge the unofficial API
    can't complete) is not caught here — it propagates, the same way a missing
    secret does elsewhere in this app, so the caller sees it and the scheduled
    job exits non-zero rather than reporting a silent, empty sync.

    `days` exists for a one-off wider backfill (`gymlog garmin-sync --days N`),
    not for the daily job to vary its own window: a field added to
    `GarminActivity` after activities were already stored (e.g.
    `distance_meters`) leaves those older records stuck at that field's
    default forever, since `with_garmin_sync` only overwrites an activity when
    this function re-fetches its date. The daily job's narrow window never
    reaches back far enough to self-heal that; a wider one-off run does,
    because the merge is keyed by activity id and simply replaces the stale
    record.

    The fitness snapshot (see `_fitness`) is fetched once for `today` only,
    never per day in the window — unlike everything else here, its source
    endpoints don't actually answer for a requested date.
    """
    today = today or datetime.now(UTC).date()
    start = today - timedelta(days=days if days is not None else GARMIN_SYNC_DAYS)
    span_days = (today - start).days

    client = _client()

    raw_activities = client.get_activities_by_date(start.isoformat(), today.isoformat())
    activities = [_activity(a, client) for a in raw_activities if isinstance(a, dict)]

    days_out: list[GarminDay] = []
    for offset in range(span_days + 1):
        days_out.append(_day(client, (start + timedelta(days=offset)).isoformat()))

    fitness_today = _fitness(client, today.isoformat())
    fitness = [fitness_today] if fitness_today else []

    return activities, days_out, fitness


def _client() -> Any:
    """A logged-in Garmin client, reusing a cached session where possible.

    `login(tokenstore=...)` accepts the cached session inline (it detects a
    JSON string rather than a path) and transparently falls back to
    username/password if that's missing, expired, or rejected — this
    function never has to implement that fallback itself. `dumps()` returns
    the current session as a string regardless of whether this run reused
    the cache or logged in fresh, so it's always saved back; the write is a
    single small blob and doing it unconditionally is simpler than tracking
    whether anything actually changed.
    """
    from garminconnect import Garmin

    from . import store

    client = Garmin(secret("GARMIN-EMAIL"), secret("GARMIN-PASSWORD"))
    client.login(tokenstore=store.load_garmin_session())
    store.save_garmin_session(client.client.dumps())
    return client


def _activity(payload: dict[str, Any], client: Any) -> GarminActivity:
    activity_id = str(payload.get("activityId", ""))
    activity_type = payload.get("activityType") or {}
    return GarminActivity(
        id=activity_id,
        date=str(payload.get("startTimeLocal", ""))[:10],
        activity_type=str(activity_type.get("typeKey", "")),
        duration_seconds=int(payload.get("duration") or 0),
        avg_hr=int(payload.get("averageHR") or 0),
        max_hr=int(payload.get("maxHR") or 0),
        distance_meters=int(payload.get("distance") or 0),
        **_zones(activity_id, client),
    )


def _zones(activity_id: str, client: Any) -> dict[str, tuple[int, ...]]:
    """Time in, and the bpm each of the 5 zones starts at, or `()` for both
    if the detail call failed.

    Wrapped per-activity rather than once for the whole sync: one activity
    with no zone detail (an indoor strength session Garmin doesn't compute
    zones for, say) should not cost the rest of the sync. Returned together
    since both come off the same response and either fails or succeeds as a
    pair — no scenario needs one without the other.
    """
    if not activity_id:
        return {"zone_seconds": (), "zone_low_bpm": ()}
    try:
        raw_zones = client.get_activity_hr_in_timezones(activity_id)
    except Exception:
        logger.warning(
            "could not fetch heart rate zones for activity %s", activity_id, exc_info=True
        )
        return {"zone_seconds": (), "zone_low_bpm": ()}

    seconds_by_zone = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    low_bpm_by_zone = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    for entry in raw_zones if isinstance(raw_zones, list) else ():
        if not isinstance(entry, dict):
            continue
        zone_number = int(entry.get("zoneNumber") or 0)
        if 1 <= zone_number <= GARMIN_ZONE_COUNT:
            seconds_by_zone[zone_number - 1] = int(entry.get("secsInZone") or 0)
            low_bpm_by_zone[zone_number - 1] = int(entry.get("zoneLowBoundary") or 0)

    if not any(seconds_by_zone.values()):
        return {"zone_seconds": (), "zone_low_bpm": ()}
    return {
        "zone_seconds": tuple(seconds_by_zone[zone] for zone in range(GARMIN_ZONE_COUNT)),
        "zone_low_bpm": tuple(low_bpm_by_zone[zone] for zone in range(GARMIN_ZONE_COUNT))
        if all(low_bpm_by_zone.values())
        else (),
    }


def _day(client: Any, cdate: str) -> GarminDay:
    """One day's steps/resting-HR/sleep/HRV, tolerating any of the four failing on its own."""
    steps = 0
    resting_hr = 0
    try:
        summary = client.get_user_summary(cdate)
        steps = int(summary.get("totalSteps") or 0)
        resting_hr = int(summary.get("restingHeartRate") or 0)
    except Exception:
        logger.warning("could not fetch daily summary for %s", cdate, exc_info=True)

    sleep_seconds = 0
    try:
        sleep = client.get_sleep_data(cdate)
        daily_sleep = (sleep or {}).get("dailySleepDTO") or {}
        sleep_seconds = int(daily_sleep.get("sleepTimeSeconds") or 0)
    except Exception:
        logger.warning("could not fetch sleep data for %s", cdate, exc_info=True)

    hrv_ms = 0
    hrv_status = ""
    try:
        hrv = client.get_hrv_data(cdate)
        hrv_summary = (hrv or {}).get("hrvSummary") or {}
        hrv_ms = int(hrv_summary.get("lastNightAvg") or 0)
        hrv_status = str(hrv_summary.get("status") or "")
    except Exception:
        logger.warning("could not fetch HRV data for %s", cdate, exc_info=True)

    return GarminDay(
        date=cdate,
        steps=steps,
        resting_hr=resting_hr,
        sleep_seconds=sleep_seconds,
        hrv_ms=hrv_ms,
        hrv_status=hrv_status,
    )


def _fitness(client: Any, cdate: str) -> GarminFitness | None:
    """VO2max and lactate threshold as Garmin reports them right now, stamped with `cdate`.

    Both `get_max_metrics` and `get_lactate_threshold` answer with today's
    current reading no matter what date is asked for — a documented quirk of
    Garmin's own endpoints (`metrics-service`'s maxmet range and
    `biometric-service`'s latestLactateThreshold), not a request built wrong
    here. Calling this once per sync, for `today`, and stamping the result
    with that date is how a real trend still builds up over the app's own
    history of daily syncs, rather than either re-stamping the same "current"
    number onto every day in the trailing window or claiming a history
    Garmin doesn't actually have.

    Returns None rather than a zeroed `GarminFitness` when neither reading
    came back, so a wholly failed fetch leaves no trace in the log instead of
    looking like a real "0 vo2max" data point.
    """
    vo2max = 0.0
    try:
        raw = client.get_max_metrics(cdate)
        entry = raw[0] if isinstance(raw, list) and raw else raw if isinstance(raw, dict) else {}
        generic = (entry or {}).get("generic") or {}
        vo2max = float(generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue") or 0)
    except Exception:
        logger.warning("could not fetch max metrics for %s", cdate, exc_info=True)

    lactate_threshold_bpm = 0
    lactate_threshold_pace_seconds_per_km = 0
    try:
        lactate_threshold = client.get_lactate_threshold()
        speed_and_heart_rate = (lactate_threshold or {}).get("speed_and_heart_rate") or {}
        lactate_threshold_bpm = int(speed_and_heart_rate.get("heartRate") or 0)
        # Garmin's own `speed` field here is documented to be off by a factor
        # of 10 from true m/s (e.g. a real ~3.9 m/s threshold comes back as
        # ~0.39) — a quirk of this specific endpoint, confirmed against other
        # tools that hit the same one (garmin-grafana#59, garmin_mcp#281),
        # not a unit this app is misreading.
        speed_ms = (speed_and_heart_rate.get("speed") or 0) * 10
        if speed_ms:
            lactate_threshold_pace_seconds_per_km = round(1000 / speed_ms)
    except Exception:
        logger.warning("could not fetch lactate threshold for %s", cdate, exc_info=True)

    if not vo2max and not lactate_threshold_bpm:
        return None
    return GarminFitness(
        date=cdate,
        vo2max=vo2max,
        lactate_threshold_bpm=lactate_threshold_bpm,
        lactate_threshold_pace_seconds_per_km=lactate_threshold_pace_seconds_per_km,
    )
