"""`health selfcheck`, which `make smoke` runs in every job after a deploy.

It has to fail on exactly what would break a real run, and do nothing else: a
false failure rolls back a good release, and a side effect would fire on every
deploy.
"""

from __future__ import annotations

import logging

import pytest

from health import selfcheck, store
from health.cli import main


@pytest.fixture
def secrets_set(monkeypatch):
    from pyrage import x25519

    for name in (*selfcheck.SECRETS, *selfcheck.BACKUP_SECRETS):
        monkeypatch.setenv(name.replace("-", "_"), f"value-of-{name}")
    # What the backup job's own settings check parses.
    monkeypatch.setenv("BACKUP_AGE_DRILL_KEY", str(x25519.Identity.generate()))
    monkeypatch.setenv("BACKUP_R2_ENDPOINT", "https://acc.eu.r2.cloudflarestorage.com")
    monkeypatch.setenv("BACKUP_AGE_RECIPIENT", str(x25519.Identity.generate().to_public()))


@pytest.fixture
def storage_ok(monkeypatch):
    monkeypatch.setattr(store, "load", lambda: (None, None))


def test_passes_when_every_secret_and_the_storage_resolve(secrets_set, storage_ok):
    assert selfcheck.run() == 0


def test_fails_on_a_missing_secret_and_names_it(secrets_set, storage_ok, monkeypatch, caplog):
    monkeypatch.delenv("GARMIN_PASSWORD")
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL secret GARMIN-PASSWORD" in caplog.text


def test_fails_when_storage_cannot_be_read(secrets_set, monkeypatch, caplog):
    def broken():
        raise RuntimeError("blob down")

    monkeypatch.setattr(store, "load", broken)
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL storage" in caplog.text


def test_never_logs_a_secret_value(secrets_set, storage_ok, caplog):
    with caplog.at_level(logging.DEBUG):
        selfcheck.run()
    assert "value-of-" not in caplog.text


def test_is_dispatched_from_the_cli(monkeypatch):
    monkeypatch.setattr(selfcheck, "run", lambda: 0)
    assert main(["selfcheck"]) == 0


def test_the_backup_job_also_checks_its_bucket(secrets_set, storage_ok, monkeypatch, caplog):
    from health import backup

    calls = []
    monkeypatch.setenv("BACKUP_BUCKET", "test-backups")
    monkeypatch.setattr(backup.JOB, "check_bucket", lambda: calls.append("listed"))
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 0
    assert calls == ["listed"]
    assert "ok backup bucket" in caplog.text
    assert "ok backup settings and keys" in caplog.text


def test_the_backup_job_fails_on_an_unset_endpoint(secrets_set, storage_ok, monkeypatch, caplog):
    from health import backup

    monkeypatch.setenv("BACKUP_BUCKET", "test-backups")
    monkeypatch.delenv("BACKUP_R2_ENDPOINT")
    monkeypatch.setattr(backup.JOB, "check_bucket", lambda: None)
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL backup settings and keys: ValueError: not configured, BACKUP_R2_ENDPOINT" in (
        caplog.text
    )


def test_settings_that_dont_load_are_reported_not_raised(
    secrets_set, storage_ok, monkeypatch, caplog
):
    from functools import cache

    from health import settings as settings_module

    # Cached like the real one, so the suite's teardown can clear it.
    @cache
    def broken() -> None:
        raise ValueError("bad setting")

    monkeypatch.setattr(settings_module, "settings", broken)
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL settings: ValueError: bad setting" in caplog.text


def test_the_backup_job_fails_without_its_r2_keys(secrets_set, storage_ok, monkeypatch, caplog):
    from health import backup

    monkeypatch.setenv("BACKUP_BUCKET", "test-backups")
    monkeypatch.setattr(backup.JOB, "check_bucket", lambda: None)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY")
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL secret R2-SECRET-ACCESS-KEY" in caplog.text


def test_other_jobs_need_neither_the_bucket_nor_the_backup_keys(
    secrets_set, storage_ok, monkeypatch, caplog
):
    for name in selfcheck.BACKUP_SECRETS:
        monkeypatch.delenv(name.replace("-", "_"))
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 0
    assert "backup bucket" not in caplog.text
    assert "R2-" not in caplog.text


# --- integrity -----------------------------------------------------------------


def _write(payload: str) -> None:
    store.local_path().write_text(payload, encoding="utf-8")


def test_a_stored_rule_violation_does_not_fail_the_deploy(secrets_set, storage_ok, caplog):
    """A deploy can't change the data; one old bad set mustn't block every
    release. The nightly backup reports it instead."""
    from health.model import Log

    from .factories import block, entry, session

    bad = Log(
        blocks=(block("2026-09-01"),),
        sessions=(session("2026-09-15", "2026-09-01", "A", entry("Row", "back", (-1, 50.0))),),
    )
    _write(bad.to_json())
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 0
    assert "selfcheck: ok integrity gymlog.json" in caplog.text


def test_a_stored_field_this_release_would_drop_fails(secrets_set, storage_ok, caplog):
    import json

    from health.model import Log

    payload = json.loads(Log().to_json())
    payload["bodyweight_kg"] = 82.5  # a field from some other release
    _write(json.dumps(payload))
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "$.bodyweight_kg: dropped" in caplog.text
    assert "82.5" not in caplog.text


def test_a_good_stored_log_passes(secrets_set, storage_ok, caplog):
    from datetime import date

    from health import seed

    _write(seed.sample_log(date(2026, 9, 20)).to_json())
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 0
    assert "selfcheck: ok integrity gymlog.json" in caplog.text
