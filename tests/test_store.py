"""Persistence, including the inversion that matters.

repo-agent's state degrades silently on every failure because it is commentary.
This document *is* the product, so the tests here are largely about it refusing
to lose anything.
"""

from __future__ import annotations

import json
import pathlib
import types
from collections.abc import Callable

import azure.storage.blob
import pytest
from azure.core import MatchConditions
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)

from gymlog import settings as settings_module
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


def test_a_missing_local_garmin_session_is_not_a_failure():
    """Unlike the log itself: a first sync (or one older than this feature) just re-logs in."""
    assert store.load_garmin_session() is None


def test_a_saved_local_garmin_session_reads_back():
    store.save_garmin_session('{"di_token": "abc"}')
    assert store.load_garmin_session() == '{"di_token": "abc"}'


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


# --- the blob ----------------------------------------------------------------
#
# Everything above runs against the local file, which is what `make run` uses.
# What follows is the path that runs in Azure, faked at the one seam the module
# has: `BlobClient.from_blob_url`. Worth faking rather than leaving uncovered
# because this is where the inversion lives — a failed read must raise, and a
# write that lost its race must be reported — and the local path exercises
# neither.


class _Stream:
    def __init__(self, body: bytes, etag: str) -> None:
        self._body = body
        self.properties = types.SimpleNamespace(etag=etag)

    def readall(self) -> bytes:
        return self._body


class _FakeBlob:
    """One blob in a fake container: the `BlobClient` calls `store` actually makes.

    Deliberately stateful and long-lived, so a `save` followed by a `load` round
    trips through it the way the real one does. `store` builds a client per call.
    """

    def __init__(self) -> None:
        self.url = ""
        self.credential: object = None
        self.content: bytes | None = None
        self.etag = '"read"'
        # Armed by a test to make the next call fail. A list, because the retry
        # in `update` is only visible if the first attempt fails and the second
        # does not.
        self.download_raises: Exception | None = None
        self.upload_raises: list[Exception] = []
        # Called when an upload is refused: the other writer landing its own
        # document, which is what makes the retry read something different.
        self.on_conflict: Callable[[], None] | None = None
        self.uploads: list[dict] = []

    def download_blob(self) -> _Stream:
        if self.download_raises is not None:
            raise self.download_raises
        if self.content is None:
            raise ResourceNotFoundError("no such blob")
        return _Stream(self.content, self.etag)

    def upload_blob(self, body: bytes, **kwargs: object) -> dict[str, str]:
        self.uploads.append({"body": body, **kwargs})
        if self.upload_raises:
            if self.on_conflict is not None:
                self.on_conflict()
            raise self.upload_raises.pop(0)
        self.content = body
        return {"etag": '"written"'}


@pytest.fixture
def blob(monkeypatch):
    """Point the store at a fake container and hand back the one blob in it."""
    monkeypatch.setenv("STATE_CONTAINER_URL", "https://acct.blob.core.windows.net/state/")
    settings_module.settings.cache_clear()
    # A sentinel credential: resolving a real one would make the suite pass or
    # fail on whether the machine running it happens to be logged in to Azure.
    monkeypatch.setattr(store, "credential", lambda: "the-credential")

    fake = _FakeBlob()

    class _Client:
        @staticmethod
        def from_blob_url(url: str, credential: object) -> _FakeBlob:
            fake.url = url
            fake.credential = credential
            return fake

    monkeypatch.setattr(azure.storage.blob, "BlobClient", _Client)
    return fake


def test_the_document_is_named_under_the_container_url(blob):
    """A container URL with a trailing slash would otherwise give `state//gymlog.json`."""
    store.load()
    assert blob.url == "https://acct.blob.core.windows.net/state/gymlog.json"
    assert blob.credential == "the-credential"


def test_a_blob_that_has_never_existed_is_a_first_run(blob):
    """The one tolerated absence: no document yet is not a lost document."""
    log, etag = store.load()
    assert log == Log()
    assert etag is None


def test_an_unreadable_blob_raises_rather_than_reading_as_empty(blob):
    """The inversion from repo-agent, on the path where it matters.

    repo-agent swallows this because its state is commentary. Here the empty log
    a swallowed error returns would be written back over a real history by the
    next save.
    """
    blob.download_raises = HttpResponseError("storage is having a day")
    with pytest.raises(HttpResponseError):
        store.load()


def test_a_read_hands_back_the_etag_to_write_against(blob):
    original = Log(blocks=(block(),))
    blob.content = original.to_json().encode("utf-8")

    log, etag = store.load()
    assert log == original
    assert etag == '"read"'


def test_a_first_write_refuses_to_overwrite(blob):
    """No ETag means "this must be a create", which is what stops two first runs racing."""
    store.save(Log())
    assert blob.uploads[0]["overwrite"] is False
    assert "etag" not in blob.uploads[0]


def test_a_later_write_carries_the_etag_it_read(blob):
    store.save(Log(), etag='"read"')
    upload = blob.uploads[0]
    assert upload["overwrite"] is True
    assert upload["etag"] == '"read"'
    assert upload["match_condition"] is MatchConditions.IfNotModified


@pytest.mark.parametrize(
    ("etag", "error"),
    [
        # Two first runs racing: the create lost.
        (None, ResourceExistsError("already there")),
        # The ordinary case: the document moved between the read and the write.
        ('"read"', ResourceModifiedError("moved")),
    ],
)
def test_a_write_that_lost_its_race_is_a_conflict(blob, etag, error):
    """Both SDK errors mean the same thing here, so both surface as one type.

    A caller that had to know which is which would eventually catch only one of
    them, and the other would reach the browser as a 500 rather than as the
    warning that a session was not recorded.
    """
    blob.upload_raises = [error]
    with pytest.raises(store.ConflictError):
        store.save(Log(), etag=etag)


def test_update_retries_once_against_the_newer_document(blob):
    """A lost race costs a retry, not a session."""
    blob.content = Log(blocks=(block(),)).to_json().encode("utf-8")
    blob.upload_raises = [ResourceModifiedError("moved")]

    updated = store.update(lambda log: log.with_session(session("2026-09-15", "2026-09-01", "A")))

    assert len(updated.sessions) == 1
    assert len(blob.uploads) == 2
    # The second attempt read again first, so it wrote against the newer ETag
    # rather than re-sending the stale one.
    assert blob.uploads[1]["etag"] == '"read"'


def test_update_gives_up_after_the_second_conflict(blob):
    """Two in a row is not contention any more, and retrying forever would hide it."""
    blob.content = Log(blocks=(block(),)).to_json().encode("utf-8")
    blob.upload_raises = [ResourceModifiedError("moved"), ResourceModifiedError("again")]

    with pytest.raises(store.ConflictError):
        store.update(lambda log: log.with_session(session("2026-09-15", "2026-09-01", "A")))
    assert len(blob.uploads) == 2


def test_the_retry_applies_the_change_to_what_the_other_writer_left(blob):
    """`update` re-runs `change` against the newer log, which is why it must be pure.

    Asserted on the body actually uploaded: a retry that re-sent the first
    attempt's document would silently drop the session the other writer added,
    which is the whole failure the ETag exists to catch.
    """
    first = Log(blocks=(block(),))
    blob.content = first.to_json().encode("utf-8")
    blob.upload_raises = [ResourceModifiedError("moved")]
    blob.on_conflict = lambda: setattr(
        blob,
        "content",
        first.with_session(session("2026-09-13", "2026-09-01", "B")).to_json().encode("utf-8"),
    )

    store.update(lambda log: log.with_session(session("2026-09-15", "2026-09-01", "A")))

    written = Log.from_json(blob.uploads[1]["body"].decode("utf-8"))
    assert [s.date for s in written.sessions] == ["2026-09-13", "2026-09-15"]


# --- the Garmin session blob --------------------------------------------------
#
# A second, small blob in the same container, with none of the log's ETag
# dance — the sync job is the only writer, so there is no race to guard
# against.


def test_the_garmin_session_is_named_under_the_container_url(blob):
    store.load_garmin_session()
    assert blob.url == "https://acct.blob.core.windows.net/state/garmin-session.json"


def test_a_garmin_session_blob_that_has_never_existed_is_none(blob):
    assert store.load_garmin_session() is None


def test_a_saved_garmin_session_blob_reads_back(blob):
    store.save_garmin_session('{"di_token": "abc"}')
    assert store.load_garmin_session() == '{"di_token": "abc"}'
