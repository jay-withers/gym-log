"""Where the training log lives: one JSON blob, read whole and written whole.

Adapted from jay-withers/repo-agent src/repoagent/state.py. The blob client and
the lazy-import discipline are the same; **the error handling is deliberately the
opposite, and that inversion is the point.**

repo-agent's state is commentary — it decides which findings are "new", so losing
it costs one week of deltas and every failure degrades silently to an empty
document. Here the document *is* the product. A session that fails to write is a
workout that did not happen as far as this app is concerned, and silently
returning an empty log would present a year of training as a blank slate and then
overwrite it. So: reads raise, writes raise, and the only tolerated absence is a
blob that has never existed.

**Concurrency.** One user and `max_replicas = 1`, so a lost update needs two
browser tabs to even be possible. It is still guarded, because the cost is one
header: every write carries an `If-Match` on the ETag read at the start, and
`update()` retries once against the newer document. That turns the rare case into
a retry rather than into a silently discarded session.

With no container configured the log falls back to a local file, which is what
makes `make run` work against nothing but a checkout.
"""

from __future__ import annotations

import logging
import pathlib
from collections.abc import Callable
from typing import Any

from .model import Log
from .settings import credential, settings

logger = logging.getLogger(__name__)

# The document's own name inside the container.
BLOB_NAME = "gymlog.json"

# A cached Garmin login session (garth's OAuth tokens), reused across syncs so
# the job does not have to re-authenticate with a password every run — see
# gymlog.garmin. A second, small blob in the same container; no ETag dance, no
# new IAM: the identity's container-scoped role already covers any blob name.
GARMIN_SESSION_BLOB_NAME = "garmin-session.json"

# The AI chat conversation — see gymlog.chat. Kept out of the log document on
# purpose: it is conversation, not training record, so it should neither grow
# the one document this app cannot afford to lose nor race it for its ETag.
CHAT_BLOB_NAME = "chat.json"


class ConflictError(RuntimeError):
    """The document changed between being read and being written."""


def load() -> tuple[Log, str | None]:
    """Read the log and the ETag to write it back against.

    A blob that has never existed yields an empty log and no ETag — that is a
    first run, not a failure. Every other error propagates.
    """
    blob = _blob()
    if blob is None:
        return _load_local()

    from azure.core.exceptions import ResourceNotFoundError

    try:
        stream = blob.download_blob()
        raw = stream.readall()
    except ResourceNotFoundError:
        logger.info("no log document yet; starting an empty one")
        return Log(), None

    etag = stream.properties.etag
    return Log.from_json(raw.decode("utf-8")), etag


def save(log: Log, etag: str | None = None) -> str | None:
    """Write the log, refusing to clobber a document that moved underneath us.

    `etag` is the value from the `load()` that produced this log. None means
    "this must be a create", which is what stops two first-runs racing and one
    of them winning silently.
    """
    blob = _blob()
    if blob is None:
        return _save_local(log)

    from azure.core import MatchConditions
    from azure.core.exceptions import ResourceExistsError, ResourceModifiedError

    body = log.to_json().encode("utf-8")
    try:
        if etag is None:
            result = blob.upload_blob(body, overwrite=False)
        else:
            result = blob.upload_blob(
                body,
                overwrite=True,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
    except (ResourceModifiedError, ResourceExistsError) as exc:
        raise ConflictError("the log changed while this session was being written") from exc

    return result.get("etag") if isinstance(result, dict) else None


def update(change: Callable[[Log], Log]) -> Log:
    """Read, apply `change`, write — retrying once if the document moved.

    `change` must be pure and cheap: on a conflict it is called a second time
    against the newer document, so anything with a side effect would happen
    twice.
    """
    for attempt in (1, 2):
        log, etag = load()
        updated = change(log)
        try:
            save(updated, etag)
        except ConflictError:
            if attempt == 2:
                raise
            logger.warning("log changed under us, retrying against the newer document")
            continue
        return updated
    raise AssertionError("unreachable")


def _blob(name: str = BLOB_NAME) -> Any:
    """A client for `name` in the state container, or None when none is configured.

    Imported lazily so the model, the progression rule and their tests never need
    the Azure SDK present — the same reason `settings.py` defers its imports.
    """
    url = settings().state_container_url
    if not url:
        return None

    from azure.storage.blob import BlobClient

    return BlobClient.from_blob_url(f"{url.rstrip('/')}/{name}", credential=credential())


def local_path() -> pathlib.Path:
    return pathlib.Path(settings().local_state_path)


def load_garmin_session() -> str | None:
    """The cached Garmin login session, or None if one has never been saved.

    Unlike the log itself, absence here is never a failure worth raising over —
    a first sync (or one that ran before this existed) just falls back to a
    fresh username/password login, same as `login()` does internally.
    """
    return _load_side(GARMIN_SESSION_BLOB_NAME, ".garmin-session.json")


def save_garmin_session(token_json: str) -> None:
    """Persist a refreshed Garmin login session, overwriting any previous one."""
    _save_side(GARMIN_SESSION_BLOB_NAME, ".garmin-session.json", token_json)


def load_chat() -> str | None:
    """The saved chat conversation as raw JSON, or None if there has never been one."""
    return _load_side(CHAT_BLOB_NAME, ".chat.json")


def save_chat(conversation_json: str) -> None:
    """Persist the chat conversation, overwriting the previous one.

    No ETag: one person, one conversation, and the worst a lost race costs is
    one exchange of chat — not a logged set.
    """
    _save_side(CHAT_BLOB_NAME, ".chat.json", conversation_json)


def _load_side(name: str, local_suffix: str) -> str | None:
    """A small blob beside the log, or None if it has never been written.

    Only absence is tolerated; any other storage error propagates, as it does
    for the log itself.
    """
    blob = _blob(name)
    if blob is None:
        path = local_path().with_suffix(local_suffix)
        return path.read_text(encoding="utf-8") if path.exists() else None

    from azure.core.exceptions import ResourceNotFoundError

    try:
        return blob.download_blob().readall().decode("utf-8")
    except ResourceNotFoundError:
        return None


def _save_side(name: str, local_suffix: str, text: str) -> None:
    blob = _blob(name)
    if blob is None:
        path = local_path().with_suffix(local_suffix)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return

    blob.upload_blob(text.encode("utf-8"), overwrite=True)


def _load_local() -> tuple[Log, str | None]:
    path = local_path()
    if not path.exists():
        logger.info("no local log at %s; starting an empty one", path)
        return Log(), None
    return Log.from_json(path.read_text(encoding="utf-8")), None


def _save_local(log: Log) -> None:
    """Write via a temporary file and rename.

    An interrupted write that truncates the file in place would lose the whole
    history; a rename is atomic on every filesystem this runs on.
    """
    path = local_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(log.to_json(), encoding="utf-8")
    tmp.replace(path)
    return None
