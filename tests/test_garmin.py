"""Garmin sync: the fixed sync window, defensive parsing, and session reuse.

The `Garmin` client itself is always faked here — the suite must never reach
Garmin Connect, the same reasoning `test_insights.py` gives for mocking
`_complete` rather than the DeepSeek HTTP call.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from gymlog import garmin, store


class _FakeInnerClient:
    def __init__(self, dumped: str = '{"di_token": "refreshed"}') -> None:
        self._dumped = dumped

    def dumps(self) -> str:
        return self._dumped


class _FakeGarmin:
    """Stands in for `garminconnect.Garmin`. `calls` records what was asked for."""

    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.client = _FakeInnerClient()
        self.logged_in_with: str | None = "unset"
        self.activities: list[dict[str, Any]] = []
        self.details_by_activity: dict[str, Any] = {}
        self.zone_settings: Any = [{"sport": "DEFAULT", "maxHeartRateUsed": 200}]
        self.summaries: dict[str, dict[str, Any]] = {}
        self.sleep: dict[str, dict[str, Any]] = {}
        self.hrv: dict[str, dict[str, Any]] = {}
        self.max_metrics: dict[str, Any] = {}
        self.lactate_threshold: dict[str, Any] = {}
        self.stress: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def login(self, tokenstore: str | None = None) -> tuple[None, None]:
        self.logged_in_with = tokenstore
        return None, None

    def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
        self.calls.append(f"activities:{start}:{end}")
        return self.activities

    def get_activity_details(self, activity_id: str, maxchart: int, maxpoly: int) -> Any:
        self.calls.append(f"details:{activity_id}")
        result = self.details_by_activity.get(activity_id, {})
        if isinstance(result, Exception):
            raise result
        return result

    def get_heart_rate_zones(self) -> Any:
        self.calls.append("heart_rate_zones")
        if isinstance(self.zone_settings, Exception):
            raise self.zone_settings
        return self.zone_settings

    def get_user_summary(self, cdate: str) -> dict[str, Any]:
        self.calls.append(f"summary:{cdate}")
        return self.summaries.get(cdate, {})

    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        self.calls.append(f"sleep:{cdate}")
        return self.sleep.get(cdate, {})

    def get_hrv_data(self, cdate: str) -> dict[str, Any]:
        self.calls.append(f"hrv:{cdate}")
        return self.hrv.get(cdate, {})

    def get_max_metrics(self, cdate: str) -> dict[str, Any]:
        self.calls.append(f"max_metrics:{cdate}")
        return self.max_metrics

    def get_lactate_threshold(self) -> dict[str, Any]:
        self.calls.append("lactate_threshold")
        return self.lactate_threshold

    def get_stress_data(self, cdate: str) -> dict[str, Any]:
        self.calls.append(f"stress:{cdate}")
        return self.stress.get(cdate, {})


ACTIVITY_PAYLOAD = {
    "activityId": 123,
    "startTimeLocal": "2026-09-15 07:00:00",
    "activityType": {"typeKey": "running"},
    "duration": 1800.0,
    "averageHR": 140,
    "maxHR": 165,
    "distance": 5023.7,
}


def details(*segments: tuple[int, int | None], every: int = 5) -> dict[str, Any]:
    """An activity details response: a sample every `every` seconds through
    each (seconds, bpm) segment, then one closing sample."""
    samples: list[tuple[int, int | None]] = []
    clock = 0
    for seconds, bpm in segments:
        for _ in range(seconds // every):
            samples.append((clock, bpm))
            clock += every
    samples.append((clock, None))
    return {
        "metricDescriptors": [
            {"metricsIndex": 0, "key": "directSpeed"},
            {"metricsIndex": 1, "key": "directTimestamp"},
            {"metricsIndex": 2, "key": "directHeartRate"},
        ],
        "activityDetailMetrics": [
            {"metrics": [2.5, 1_789_000_000_000 + at * 1000, bpm]} for at, bpm in samples
        ],
    }


# With max 200 and resting 50, zones start at 125, 140, 155, 170 and 185 bpm.
# A minute below zone 1, then 5, 15, 8, 1 and 1 minutes in zones 1-5.
DETAILS_PAYLOAD = details((60, 110), (300, 130), (900, 145), (480, 160), (60, 175), (60, 190))
DAY_PAYLOAD = {"restingHeartRate": 50}


def test_sync_fetches_the_trailing_sync_window_not_the_full_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sync only re-asks Garmin about the last `GARMIN_SYNC_DAYS` — far
    short of the 90 days `GARMIN_RETENTION_DAYS` keeps in the log, since
    that older history comes from previous syncs merging, not this one
    re-fetching it."""
    captured: dict[str, Any] = {}

    class _Recording(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            captured["start"], captured["end"] = start, end
            return []

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    monkeypatch.setattr("garminconnect.Garmin", _Recording)

    activities, days, _fitness = garmin.sync_garmin(today=date(2026, 9, 30))

    assert captured["start"] == "2026-09-23"
    assert captured["end"] == "2026-09-30"
    assert len(days) == garmin.GARMIN_SYNC_DAYS + 1
    assert days[0].date == "2026-09-23"
    assert days[-1].date == "2026-09-30"
    assert activities == []


def test_sync_days_can_be_widened_for_a_one_off_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field added to `GarminActivity` after activities were already stored
    (e.g. `distance_meters`) leaves those records stuck at that field's
    default until something re-fetches their date — the daily job's narrow
    `GARMIN_SYNC_DAYS` window never reaches back far enough on its own, so a
    wider one-off `days` override is how that gets fixed."""
    captured: dict[str, Any] = {}

    class _Recording(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            captured["start"], captured["end"] = start, end
            return []

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    monkeypatch.setattr("garminconnect.Garmin", _Recording)

    activities, days, _fitness = garmin.sync_garmin(today=date(2026, 9, 30), days=30)

    assert captured["start"] == "2026-08-31"
    assert len(days) == 31
    assert activities == []


def _with_activities(*payloads: dict[str, Any], **overrides: Any) -> type[_FakeGarmin]:
    class _WithActivities(_FakeGarmin):
        def __init__(self, email: str, password: str) -> None:
            super().__init__(email, password)
            self.summaries = {"2026-09-15": DAY_PAYLOAD}
            self.details_by_activity = {"123": DETAILS_PAYLOAD, "456": DETAILS_PAYLOAD}
            for name, value in overrides.items():
                setattr(self, name, value)

        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return list(payloads)

    return _WithActivities


def _sync(monkeypatch: pytest.MonkeyPatch, client: type[_FakeGarmin]) -> list[Any]:
    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    monkeypatch.setattr("garminconnect.Garmin", client)
    activities, _days, _fitness = garmin.sync_garmin(today=date(2026, 9, 15))
    return activities


def test_an_activity_is_mapped_with_its_heart_rate_zones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activities = _sync(monkeypatch, _with_activities(ACTIVITY_PAYLOAD))

    assert len(activities) == 1
    activity = activities[0]
    assert activity.id == "123"
    assert activity.date == "2026-09-15"
    assert activity.activity_type == "running"
    assert activity.duration_seconds == 1800
    assert activity.avg_hr == 140
    assert activity.max_hr == 165
    assert activity.distance_meters == 5023
    # Counted from the samples against HRR zones, not taken from Garmin: the
    # minute below zone 1 counts nowhere.
    assert activity.zone_low_bpm == (125, 140, 155, 170, 185)
    assert activity.zone_seconds == (300, 900, 480, 60, 60)
    # The same samples against % of max: zones start at 100, 120, ... 180, so
    # the minute at 110 counts this time, and 160 and 175 share zone 4.
    assert activity.max_zone_low_bpm == (100, 120, 140, 160, 180)
    assert activity.max_zone_seconds == (60, 300, 900, 540, 60)


def test_hrr_boundaries_are_karvonen():
    """resting + floor x (max - resting): 41 + 0.5 x 162 = 122."""
    assert garmin.hrr_boundaries(203, 41) == (122, 138, 154, 171, 187)
    assert garmin.hrr_boundaries(202, 55) == (129, 143, 158, 173, 187)  # 128.5 rounds up
    assert garmin.hrr_boundaries(0, 41) == ()
    assert garmin.hrr_boundaries(203, 0) == ()


def test_max_boundaries_are_a_share_of_max():
    assert garmin.max_boundaries(203) == (102, 122, 142, 162, 183)  # Garmin's own, for 203
    assert garmin.max_boundaries(0) == ()


def test_no_resting_heart_rate_still_counts_the_hr_zones(monkeypatch: pytest.MonkeyPatch) -> None:
    """% of max needs no resting heart rate, so it shouldn't wait for one."""
    activities = _sync(monkeypatch, _with_activities(ACTIVITY_PAYLOAD, summaries={}))
    assert activities[0].zone_seconds == ()
    assert activities[0].max_zone_seconds == (60, 300, 900, 540, 60)


def test_a_pause_between_samples_is_not_time_in_a_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ten minutes stopped at a crossing is not ten minutes in zone 1."""
    paused = details((120, 130))
    for row in paused["activityDetailMetrics"][12:]:  # from 60s on, ten minutes later
        row["metrics"][1] += 600_000
    activities = _sync(
        monkeypatch, _with_activities(ACTIVITY_PAYLOAD, details_by_activity={"123": paused})
    )
    # 120s moving; the ten minutes stopped, and the 5s of the sample before
    # it, are dropped.
    assert activities[0].zone_seconds == (115, 0, 0, 0, 0)


def test_an_activity_on_a_day_with_no_resting_heart_rate_takes_the_window_average(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Today's resting heart rate is often not settled yet when the sync runs."""
    activities = _sync(
        monkeypatch,
        _with_activities(
            ACTIVITY_PAYLOAD,
            summaries={
                "2026-09-13": {"restingHeartRate": 48},
                "2026-09-14": DAY_PAYLOAD | {"restingHeartRate": 52},
            },
        ),
    )
    assert activities[0].zone_low_bpm == (125, 140, 155, 170, 185)


def test_no_max_heart_rate_leaves_zones_empty_without_aborting_the_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activities = _sync(
        monkeypatch,
        _with_activities(ACTIVITY_PAYLOAD, zone_settings=RuntimeError("Garmin said no")),
    )
    assert len(activities) == 1
    assert activities[0].zone_seconds == ()
    assert activities[0].zone_low_bpm == ()
    assert activities[0].max_zone_seconds == ()


def test_a_failed_heart_rate_fetch_degrades_to_empty_without_aborting_the_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activities = _sync(
        monkeypatch,
        _with_activities(
            ACTIVITY_PAYLOAD,
            {**ACTIVITY_PAYLOAD, "activityId": 456},
            details_by_activity={"123": RuntimeError("Garmin said no"), "456": DETAILS_PAYLOAD},
        ),
    )

    assert len(activities) == 2
    failed, ok = activities
    assert failed.zone_seconds == ()
    assert failed.zone_low_bpm == ()
    assert ok.zone_seconds == (300, 900, 480, 60, 60)


def test_an_activity_with_no_heart_rate_has_no_zones(monkeypatch: pytest.MonkeyPatch) -> None:
    no_hr = details((120, None))
    activities = _sync(
        monkeypatch, _with_activities(ACTIVITY_PAYLOAD, details_by_activity={"123": no_hr})
    )
    assert activities[0].zone_seconds == ()


def test_a_day_summary_tolerates_a_missing_field(monkeypatch: pytest.MonkeyPatch) -> None:
    class _SparseDay(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

        def get_user_summary(self, cdate: str) -> dict[str, Any]:
            return {"totalSteps": 5000}  # no restingHeartRate

        def get_sleep_data(self, cdate: str) -> dict[str, Any]:
            return {}  # no dailySleepDTO at all

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    monkeypatch.setattr("garminconnect.Garmin", _SparseDay)

    _activities, days, _fitness = garmin.sync_garmin(today=date(2026, 9, 15))

    assert all(d.steps == 5000 for d in days)
    assert all(d.resting_hr == 0 for d in days)
    assert all(d.sleep_seconds == 0 for d in days)
    assert all(d.sleep_score == 0 for d in days)
    assert all(d.hrv_ms == 0 for d in days)
    assert all(d.stress_avg == 0 for d in days)


def test_a_day_carries_its_overnight_hrv_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    class _WithHrv(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    instance = _WithHrv("me@example.com", "hunter2")
    instance.hrv = {"2026-09-15": {"hrvSummary": {"lastNightAvg": 62, "status": "BALANCED"}}}
    monkeypatch.setattr("garminconnect.Garmin", lambda email, password: instance)

    _activities, days, _fitness = garmin.sync_garmin(today=date(2026, 9, 15), days=0)

    assert days[0].hrv_ms == 62
    assert days[0].hrv_status == "BALANCED"


def test_a_failed_hrv_fetch_degrades_to_zero_without_aborting_the_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HrvFails(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

        def get_hrv_data(self, cdate: str) -> dict[str, Any]:
            raise RuntimeError("Garmin said no")

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    monkeypatch.setattr("garminconnect.Garmin", _HrvFails)

    _activities, days, _fitness = garmin.sync_garmin(today=date(2026, 9, 15), days=0)

    assert days[0].hrv_ms == 0
    assert days[0].hrv_status == ""


def test_a_day_carries_its_sleep_score_from_the_same_sleep_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overall sleep score rides along on the same `get_sleep_data`
    response as sleep_seconds — no separate API call for it."""

    class _WithSleepScore(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    instance = _WithSleepScore("me@example.com", "hunter2")
    instance.sleep = {
        "2026-09-15": {
            "dailySleepDTO": {
                "sleepTimeSeconds": 27000,
                "sleepScores": {"overall": {"value": 85, "qualifierKey": "GOOD"}},
            }
        }
    }
    monkeypatch.setattr("garminconnect.Garmin", lambda email, password: instance)

    _activities, days, _fitness = garmin.sync_garmin(today=date(2026, 9, 15), days=0)

    assert days[0].sleep_seconds == 27000
    assert days[0].sleep_score == 85


def test_a_day_carries_its_average_stress_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    class _WithStress(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    instance = _WithStress("me@example.com", "hunter2")
    instance.stress = {"2026-09-15": {"overallStressLevel": 31}}
    monkeypatch.setattr("garminconnect.Garmin", lambda email, password: instance)

    _activities, days, _fitness = garmin.sync_garmin(today=date(2026, 9, 15), days=0)

    assert days[0].stress_avg == 31


def test_a_negative_stress_reading_is_clamped_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garmin uses -1/-2 for "not enough data" rather than omitting the field."""

    class _NotEnoughData(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    instance = _NotEnoughData("me@example.com", "hunter2")
    instance.stress = {"2026-09-15": {"overallStressLevel": -1}}
    monkeypatch.setattr("garminconnect.Garmin", lambda email, password: instance)

    _activities, days, _fitness = garmin.sync_garmin(today=date(2026, 9, 15), days=0)

    assert days[0].stress_avg == 0


def test_a_failed_stress_fetch_degrades_to_zero_without_aborting_the_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StressFails(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

        def get_stress_data(self, cdate: str) -> dict[str, Any]:
            raise RuntimeError("Garmin said no")

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    monkeypatch.setattr("garminconnect.Garmin", _StressFails)

    _activities, days, _fitness = garmin.sync_garmin(today=date(2026, 9, 15), days=0)

    assert days[0].stress_avg == 0


def test_fitness_reads_vo2max_and_lactate_threshold_for_today_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both endpoints only ever answer for "today", so this must be fetched
    once per sync — not looped across the whole trailing window the way
    `_day` is — and stamped with the sync's own `today`."""
    calls: list[str] = []

    class _WithFitness(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

        def get_max_metrics(self, cdate: str) -> dict[str, Any]:
            calls.append(f"max_metrics:{cdate}")
            return {"generic": {"vo2MaxPreciseValue": 52.3, "vo2MaxValue": 52}}

        def get_lactate_threshold(self) -> dict[str, Any]:
            calls.append("lactate_threshold")
            # 0.3876, not 3.876: Garmin's own `speed` field here is off by a
            # factor of 10 from true m/s (see `_fitness`'s comment).
            return {
                "speed_and_heart_rate": {"heartRate": 165, "speed": 0.3876},
                "power": {},
            }

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    monkeypatch.setattr("garminconnect.Garmin", _WithFitness)

    _activities, days, fitness = garmin.sync_garmin(today=date(2026, 9, 15), days=3)

    assert calls.count("max_metrics:2026-09-15") == 1
    assert calls.count("lactate_threshold") == 1
    assert len(days) == 4  # the fetch above is not part of the per-day loop
    assert len(fitness) == 1
    reading = fitness[0]
    assert reading.date == "2026-09-15"
    assert reading.vo2max == 52.3
    assert reading.lactate_threshold_bpm == 165
    assert reading.lactate_threshold_pace_seconds_per_km == round(1000 / 3.876)


def test_fitness_corrects_garmins_lactate_threshold_speed_being_off_by_10x(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: Garmin's own `speed` field on this endpoint is documented
    to read 10x too slow (e.g. a real ~3.9 m/s threshold comes back as
    ~0.39) — a quirk of this specific endpoint (garmin-grafana#59,
    garmin_mcp#281), not something this app should pass straight through."""

    class _WithRealisticSpeed(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

        def get_lactate_threshold(self) -> dict[str, Any]:
            return {"speed_and_heart_rate": {"heartRate": 165, "speed": 0.3888878}}

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    monkeypatch.setattr("garminconnect.Garmin", _WithRealisticSpeed)

    _activities, _days, fitness = garmin.sync_garmin(today=date(2026, 9, 15), days=0)

    # ~3.888878 m/s -> ~4:17/km (257s), not the ~42:57/km a straight
    # 1000/0.3888878 would give.
    assert fitness[0].lactate_threshold_pace_seconds_per_km == 257


def test_fitness_falls_back_to_the_imprecise_vo2max_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WithoutPrecise(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

        def get_max_metrics(self, cdate: str) -> dict[str, Any]:
            return {"generic": {"vo2MaxValue": 48}}

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    monkeypatch.setattr("garminconnect.Garmin", _WithoutPrecise)

    _activities, _days, fitness = garmin.sync_garmin(today=date(2026, 9, 15), days=0)

    assert fitness[0].vo2max == 48


def test_fitness_is_none_when_neither_reading_comes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoFitness(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    monkeypatch.setattr("garminconnect.Garmin", _NoFitness)

    _activities, _days, fitness = garmin.sync_garmin(today=date(2026, 9, 15), days=0)

    assert fitness == []


def test_a_failed_max_metrics_fetch_still_returns_the_lactate_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MaxMetricsFails(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

        def get_max_metrics(self, cdate: str) -> dict[str, Any]:
            raise RuntimeError("Garmin said no")

        def get_lactate_threshold(self) -> dict[str, Any]:
            return {"speed_and_heart_rate": {"heartRate": 160, "speed": 0.35}}

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    monkeypatch.setattr("garminconnect.Garmin", _MaxMetricsFails)

    _activities, _days, fitness = garmin.sync_garmin(today=date(2026, 9, 15), days=0)

    assert fitness[0].vo2max == 0
    assert fitness[0].lactate_threshold_bpm == 160


def test_the_cached_session_is_passed_to_login_and_resaved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    store.save_garmin_session('{"di_token": "cached"}')

    instances: list[_FakeGarmin] = []

    class _Recording(_FakeGarmin):
        def __init__(self, email: str, password: str) -> None:
            super().__init__(email, password)
            instances.append(self)

        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr("garminconnect.Garmin", _Recording)

    garmin.sync_garmin(today=date(2026, 9, 15))

    assert instances[0].logged_in_with == '{"di_token": "cached"}'
    assert store.load_garmin_session() == '{"di_token": "refreshed"}'


def test_a_first_run_with_no_cached_session_logs_in_with_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")

    instances: list[_FakeGarmin] = []

    class _Recording(_FakeGarmin):
        def __init__(self, email: str, password: str) -> None:
            super().__init__(email, password)
            instances.append(self)

        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr("garminconnect.Garmin", _Recording)

    garmin.sync_garmin(today=date(2026, 9, 15))

    assert instances[0].logged_in_with is None
