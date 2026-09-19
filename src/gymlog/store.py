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


def _blob() -> Any:
    """A client for the log document, or None when no container is configured.

    Imported lazily so the model, the progression rule and their tests never need
    the Azure SDK present — the same reason `settings.py` defers its imports.
    """
    url = settings().state_container_url
    if not url:
        return None

    from azure.storage.blob import BlobClient

    return BlobClient.from_blob_url(f"{url.rstrip('/')}/{BLOB_NAME}", credential=credential())


def local_path() -> pathlib.Path:
    return pathlib.Path(settings().local_state_path)


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
