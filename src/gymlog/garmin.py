"""Garmin Connect sync: activities, per-activity heart rate zones, daily summaries.

Garmin has no public API for personal accounts, so this uses the unofficial
`garminconnect` library against Garmin Connect's own web endpoints. Mirrors
insights.py's shape: a pure-ish function that fetches and returns data,
never touching `store` itself for the log — the caller decides how it's
merged in. Session persistence is the one exception (see `_client` below),
since re-authenticating with a password on every run risks exactly the kind
of friction (MFA challenges, rate limiting) an unofficial API punishes.

Field names below (`activityId`, `startTimeLocal`, `activityType.typeKey`,
`averageHR`/`maxHR`, `totalSteps`, `restingHeartRate`,
`dailySleepDTO.sleepTimeSeconds`, `zoneNumber`/`secsInZone`/`zoneLowBoundary`)
are Garmin's own, confirmed against community-documented response shapes
rather than this library's (minimal) type hints — Garmin can change them
without notice, which is why every read here is defensive (`.get()` with a
fallback), never a bare index.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .model import GARMIN_ZONE_COUNT, GarminActivity, GarminDay
from .settings import secret

logger = logging.getLogger(__name__)

# How far back every sync looks, and — via Log.with_garmin_sync — how much is
# ever kept: a fixed rolling window rather than a growing archive. Re-scanning
# it every run means a missed day self-heals on the next one, with no sync
# cursor to track or get out of sync.
GARMIN_RETENTION_DAYS = 30


def sync_garmin(today: date | None = None) -> tuple[list[GarminActivity], list[GarminDay]]:
    """Fetch the trailing `GARMIN_RETENTION_DAYS` of activities and daily summaries.

    An auth failure (bad credentials, an MFA challenge the unofficial API
    can't complete) is not caught here — it propagates, the same way a missing
    secret does elsewhere in this app, so the caller sees it and the scheduled
    job exits non-zero rather than reporting a silent, empty sync.
    """
    today = today or datetime.now(UTC).date()
    start = today - timedelta(days=GARMIN_RETENTION_DAYS)

    client = _client()

    raw_activities = client.get_activities_by_date(start.isoformat(), today.isoformat())
    activities = [_activity(a, client) for a in raw_activities if isinstance(a, dict)]

    days: list[GarminDay] = []
    for offset in range(GARMIN_RETENTION_DAYS + 1):
        days.append(_day(client, (start + timedelta(days=offset)).isoformat()))

    return activities, days


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
    """One day's steps/resting-HR/sleep, tolerating any of the three failing on its own."""
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

    return GarminDay(date=cdate, steps=steps, resting_hr=resting_hr, sleep_seconds=sleep_seconds)
