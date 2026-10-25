"""`gymlog selfcheck`, which `make smoke` runs in every job after a deploy.

It has to fail on exactly what would break a real run, and do nothing else: a
false failure rolls back a good release, and a side effect would fire on every
deploy.
"""

from __future__ import annotations

import logging

import pytest

from gymlog import selfcheck, store
from gymlog.cli import main


@pytest.fixture
def secrets_set(monkeypatch):
    for name in selfcheck.SECRETS:
        monkeypatch.setenv(name.replace("-", "_"), f"value-of-{name}")


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
