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
        self.zones_by_activity: dict[str, Any] = {}
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

    def get_activity_hr_in_timezones(self, activity_id: str) -> Any:
        self.calls.append(f"zones:{activity_id}")
        result = self.zones_by_activity.get(activity_id, [])
        if isinstance(result, Exception):
            raise result
        return result

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

ZONE_PAYLOAD = [
    {"zoneNumber": 1, "secsInZone": 60, "zoneLowBoundary": 96},
    {"zoneNumber": 2, "secsInZone": 300, "zoneLowBoundary": 114},
    {"zoneNumber": 3, "secsInZone": 900, "zoneLowBoundary": 132},
    {"zoneNumber": 4, "secsInZone": 480, "zoneLowBoundary": 150},
    {"zoneNumber": 5, "secsInZone": 60, "zoneLowBoundary": 161},
]


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


def test_an_activity_is_mapped_with_its_heart_rate_zones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WithOneActivity(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return [ACTIVITY_PAYLOAD]

        def get_activity_hr_in_timezones(self, activity_id: str) -> Any:
            assert activity_id == "123"
            return ZONE_PAYLOAD

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    monkeypatch.setattr("garminconnect.Garmin", _WithOneActivity)

    activities, _days, _fitness = garmin.sync_garmin(today=date(2026, 9, 15))

    assert len(activities) == 1
    activity = activities[0]
    assert activity.id == "123"
    assert activity.date == "2026-09-15"
    assert activity.activity_type == "running"
    assert activity.duration_seconds == 1800
    assert activity.avg_hr == 140
    assert activity.max_hr == 165
    assert activity.distance_meters == 5023
    assert activity.zone_seconds == (60, 300, 900, 480, 60)
    assert activity.zone_low_bpm == (96, 114, 132, 150, 161)


def test_a_failed_zone_fetch_degrades_to_empty_without_aborting_the_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ZonesFail(_FakeGarmin):
        def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
            return [ACTIVITY_PAYLOAD, {**ACTIVITY_PAYLOAD, "activityId": 456}]

        def get_activity_hr_in_timezones(self, activity_id: str) -> Any:
            if activity_id == "123":
                raise RuntimeError("Garmin said no")
            return ZONE_PAYLOAD

    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    monkeypatch.setattr("garminconnect.Garmin", _ZonesFail)

    activities, _days, _fitness = garmin.sync_garmin(today=date(2026, 9, 15))

    assert len(activities) == 2
    failed, ok = activities
    assert failed.zone_seconds == ()
    assert failed.zone_low_bpm == ()
    assert ok.zone_seconds == (60, 300, 900, 480, 60)
    assert ok.zone_low_bpm == (96, 114, 132, 150, 161)


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
