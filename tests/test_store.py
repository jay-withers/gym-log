"""Persistence, including the inversion that matters.

repo-agent's state degrades silently on every failure because it is commentary.
This document *is* the product, so the tests here are largely about it refusing
to lose anything.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from gymlog import store
from gymlog.model import Log

from .factories import block, entry, session


def test_a_missing_file_is_a_first_run_not_a_failure():
    log, etag = store.load()
    assert log == Log()
    assert etag is None


def test_a_saved_log_reads_back():
    original = Log(blocks=(block(),), sessions=(session("2026-09-15", "2026-09-01", "A", entry()),))
    store.save(original)
    assert store.load()[0] == original


def test_update_applies_and_persists():
    store.save(Log(blocks=(block(),)))
    store.update(lambda log: log.with_session(session("2026-09-15", "2026-09-01", "A", entry())))
    assert len(store.load()[0].sessions) == 1


def test_a_corrupt_document_raises_rather_than_starting_fresh(monkeypatch, tmp_path):
    """The failure mode this exists to prevent.

    repo-agent returns an empty state on an unreadable document, which is right
    when the document is commentary. Here it would present a year of training as
    a blank slate and then overwrite it with the next session.
    """
    path = tmp_path / "broken.json"
    path.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv("LOCAL_STATE_PATH", str(path))
    from gymlog import settings as settings_module

    settings_module.settings.cache_clear()

    with pytest.raises(json.JSONDecodeError):
        store.load()


def test_a_local_write_is_atomic(monkeypatch, tmp_path):
    """Written via a temp file and renamed, so an interrupted write cannot truncate.

    Asserted by its observable consequence: no stray temp file survives a
    successful write.
    """
    path = tmp_path / "log.json"
    monkeypatch.setenv("LOCAL_STATE_PATH", str(path))
    from gymlog import settings as settings_module

    settings_module.settings.cache_clear()

    store.save(Log(blocks=(block(),)))
    assert path.exists()
    assert list(pathlib.Path(tmp_path).glob("*.tmp")) == []
