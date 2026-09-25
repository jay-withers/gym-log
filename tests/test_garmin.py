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

    activities, days = garmin.sync_garmin(today=date(2026, 9, 30))

    assert captured["start"] == "2026-09-23"
    assert captured["end"] == "2026-09-30"
    assert len(days) == garmin.GARMIN_SYNC_DAYS + 1
    assert days[0].date == "2026-09-23"
    assert days[-1].date == "2026-09-30"
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

    activities, _days = garmin.sync_garmin(today=date(2026, 9, 15))

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

    activities, _days = garmin.sync_garmin(today=date(2026, 9, 15))

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

    _activities, days = garmin.sync_garmin(today=date(2026, 9, 15))

    assert all(d.steps == 5000 for d in days)
    assert all(d.resting_hr == 0 for d in days)
    assert all(d.sleep_seconds == 0 for d in days)


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
