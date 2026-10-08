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
`dailySleepDTO.sleepTimeSeconds`/`.sleepScores.overall.value`,
`metricDescriptors[].key`/`activityDetailMetrics[].metrics` (`directTimestamp`,
`directHeartRate`), `maxHeartRateUsed`, `hrvSummary.lastNightAvg`/
`.status`, `generic.vo2MaxPreciseValue`/`.vo2MaxValue`,
`speed_and_heart_rate.speed`/`.heartRate`, `overallStressLevel`) are
Garmin's own, confirmed against community-documented response shapes
rather than this library's (minimal) type hints — Garmin can change them
without notice, which is why every read here is defensive (`.get()` with a
fallback), never a bare index.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any

from .model import GARMIN_ZONE_COUNT, GarminActivity, GarminDay, GarminFitness
from .settings import secret

logger = logging.getLogger(__name__)

# The trailing window each sync actually asks Garmin Connect for. Short on
# purpose: a day already synced doesn't change, so re-fetching it every run
# spends four API calls per day (summary, sleep — which sleep score rides
# along on for free — HRV, and stress) plus one per activity for zones on
# data that hasn't moved. A missed day still self-heals
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

# Where zones 1-5 start: a fraction of heart rate reserve for the HRR zones,
# of max heart rate for the plain HR ones. The same usual five-zone split.
ZONE_FLOORS = (0.50, 0.60, 0.70, 0.80, 0.90)

# Heart rate samples asked for per activity: a ceiling, not a target, so
# Garmin sends every sample it has (about one a second). Asked for fewer, it
# thins them out, and each sample is weighted by the time to the next anyway.
HR_SAMPLES = 100_000

# A gap between samples longer than this is a pause, not time in a zone.
HR_SAMPLE_MAX_GAP_SECONDS = 30


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

    Heart rate zones are counted here, not taken from Garmin (see `_zones`),
    and take one extra call per sync for the max heart rate they are built on.

    The fitness snapshot (see `_fitness`) is fetched once for `today` only,
    never per day in the window — unlike everything else here, its source
    endpoints don't actually answer for a requested date.
    """
    today = today or datetime.now(UTC).date()
    start = today - timedelta(days=days if days is not None else GARMIN_SYNC_DAYS)
    span_days = (today - start).days

    client = _client()

    days_out: list[GarminDay] = []
    for offset in range(span_days + 1):
        days_out.append(_day(client, (start + timedelta(days=offset)).isoformat()))

    # Days first: each activity's zones need that day's resting heart rate.
    resting_by_date = {d.date: d.resting_hr for d in days_out if d.resting_hr}
    max_hr = _max_hr(client)

    raw_activities = client.get_activities_by_date(start.isoformat(), today.isoformat())
    activities = [
        _activity(a, client, max_hr, resting_by_date) for a in raw_activities if isinstance(a, dict)
    ]

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


def _activity(
    payload: dict[str, Any], client: Any, max_hr: int, resting_by_date: dict[str, int]
) -> GarminActivity:
    activity_id = str(payload.get("activityId", ""))
    activity_type = payload.get("activityType") or {}
    activity_date = str(payload.get("startTimeLocal", ""))[:10]
    hrr = hrr_boundaries(max_hr, _resting(activity_date, resting_by_date))
    return GarminActivity(
        id=activity_id,
        date=activity_date,
        activity_type=str(activity_type.get("typeKey", "")),
        duration_seconds=int(payload.get("duration") or 0),
        avg_hr=int(payload.get("averageHR") or 0),
        max_hr=int(payload.get("maxHR") or 0),
        distance_meters=int(payload.get("distance") or 0),
        **_zones(activity_id, client, hrr, max_boundaries(max_hr)),
    )


def _max_hr(client: Any) -> int:
    """Max heart rate from Garmin Connect's default zone settings, or 0.

    0 leaves this sync's activities without zones rather than failing the
    sync; the next one inside the window fills them in.
    """
    try:
        zone_settings = client.get_heart_rate_zones()
    except Exception:
        logger.warning("could not fetch heart rate zone settings", exc_info=True)
        return 0
    for entry in zone_settings if isinstance(zone_settings, list) else ():
        if isinstance(entry, dict) and entry.get("sport") == "DEFAULT":
            return int(entry.get("maxHeartRateUsed") or 0)
    return 0


def _resting(activity_date: str, resting_by_date: dict[str, int]) -> int:
    """That day's resting heart rate, else the window's average, else 0.

    Today's is often missing: Garmin settles it later in the day.
    """
    if resting_by_date.get(activity_date):
        return resting_by_date[activity_date]
    if not resting_by_date:
        return 0
    return round(sum(resting_by_date.values()) / len(resting_by_date))


def hrr_boundaries(max_hr: int, resting_hr: int) -> tuple[int, ...]:
    """The bpm each zone starts at by heart rate reserve (Karvonen), or `()`.

    resting + floor x (max - resting), rounded half up: 128.5 is 129.
    """
    if not max_hr or not resting_hr or max_hr <= resting_hr:
        return ()
    return tuple(int(resting_hr + f * (max_hr - resting_hr) + 0.5) for f in ZONE_FLOORS)


def max_boundaries(max_hr: int) -> tuple[int, ...]:
    """The bpm each zone starts at as a share of max heart rate, or `()`."""
    if not max_hr:
        return ()
    return tuple(int(f * max_hr + 0.5) for f in ZONE_FLOORS)


def _zones(
    activity_id: str, client: Any, hrr: tuple[int, ...], of_max: tuple[int, ...]
) -> dict[str, tuple[int, ...]]:
    """Time in each zone, counted here from the activity's heart rate samples,
    twice: against the HRR boundaries and against the % of max ones.

    Not Garmin's `secsInZone`: that is bucketed on the watch against whatever
    zones it had when it recorded, and the watch and Garmin Connect have
    disagreed about those. Counting the samples here makes each set what it
    says whatever the watch was set to. Checked against Garmin's own figures
    using Garmin's own boundaries: within seconds.

    A set is `()` (both its fields) without boundaries; everything is `()`
    when the fetch fails or there is no heart rate — per activity, so one bad
    one doesn't cost the rest of the sync.
    """
    empty: dict[str, tuple[int, ...]] = {
        "zone_seconds": (),
        "zone_low_bpm": (),
        "max_zone_seconds": (),
        "max_zone_low_bpm": (),
    }
    if not activity_id or not (hrr or of_max):
        return empty
    try:
        details = client.get_activity_details(activity_id, maxchart=HR_SAMPLES, maxpoly=0)
    except Exception:
        logger.warning("could not fetch heart rate for activity %s", activity_id, exc_info=True)
        return empty

    samples = _heart_rate(details)
    hrr_seconds = _count(samples, hrr)
    max_seconds = _count(samples, of_max)
    return {
        "zone_seconds": hrr_seconds,
        "zone_low_bpm": hrr if hrr_seconds else (),
        "max_zone_seconds": max_seconds,
        "max_zone_low_bpm": of_max if max_seconds else (),
    }


def _heart_rate(details: Any) -> list[tuple[float, Any]]:
    """(timestamp in ms, bpm or None) per sample of an activity details response."""
    details = details if isinstance(details, dict) else {}
    index = {
        m.get("key"): m.get("metricsIndex")
        for m in details.get("metricDescriptors") or ()
        if isinstance(m, dict)
    }
    at, hr = index.get("directTimestamp"), index.get("directHeartRate")
    if not isinstance(at, int) or not isinstance(hr, int):
        return []
    return [
        (metrics[at], metrics[hr])
        for row in details.get("activityDetailMetrics") or ()
        if isinstance(row, dict)
        and isinstance(metrics := row.get("metrics"), list)
        and len(metrics) > max(at, hr)
        and metrics[at] is not None
    ]


def _count(samples: list[tuple[float, Any]], boundaries: tuple[int, ...]) -> tuple[int, ...]:
    """Seconds in each zone, each sample weighted by the gap to the next, or `()`.

    Time below zone 1 counts nowhere, as on the watch.
    """
    if len(boundaries) != GARMIN_ZONE_COUNT:
        return ()
    seconds = [0.0] * GARMIN_ZONE_COUNT
    for (when, bpm), (after, _bpm) in pairwise(samples):
        gap = (after - when) / 1000
        if not bpm or not 0 < gap <= HR_SAMPLE_MAX_GAP_SECONDS:
            continue
        zone = sum(1 for low in boundaries if bpm >= low) - 1
        if zone >= 0:
            seconds[zone] += gap
    return tuple(round(s) for s in seconds) if any(seconds) else ()


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
    sleep_score = 0
    try:
        sleep = client.get_sleep_data(cdate)
        daily_sleep = (sleep or {}).get("dailySleepDTO") or {}
        sleep_seconds = int(daily_sleep.get("sleepTimeSeconds") or 0)
        # Same response as sleep_seconds above, not a separate call: the
        # overall sleep score sits alongside sleepTimeSeconds on this same
        # dailySleepDTO.
        overall_score = (daily_sleep.get("sleepScores") or {}).get("overall") or {}
        sleep_score = int(overall_score.get("value") or 0)
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

    stress_avg = 0
    try:
        stress = client.get_stress_data(cdate)
        # Garmin uses -1/-2 for "not enough data" rather than omitting the
        # field, so a negative reading is clamped to 0 rather than kept as a
        # nonsensical negative stress level.
        stress_avg = max(0, int((stress or {}).get("overallStressLevel") or 0))
    except Exception:
        logger.warning("could not fetch stress data for %s", cdate, exc_info=True)

    return GarminDay(
        date=cdate,
        steps=steps,
        resting_hr=resting_hr,
        sleep_seconds=sleep_seconds,
        hrv_ms=hrv_ms,
        hrv_status=hrv_status,
        sleep_score=sleep_score,
        stress_avg=stress_avg,
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
