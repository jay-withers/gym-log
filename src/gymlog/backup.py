"""`gymlog backup`: copy the log off Azure, encrypted, and prove the copy restores.

The job itself is jay-withers/appkit's `appkit.backup`, shared with finances
and career. This module declares what gym-log backs up and how each document is
checked to restore; the reasoning below is why the job is shaped as it is.

The log lives in one LRS storage account in one subscription. Versioning, soft
delete and the delete lock cover a bad write or a slip of the hand; they do
nothing if the subscription itself is disabled or lost. So once a day this job
reads each document *exactly as stored*, encrypts it with age, and puts it in
Cloudflare R2 (the `cloudflare` repo's `jw-app-backups` bucket) under
`daily/gym-log/<UTC timestamp>/`. The first run in a month whose daily copy
verifies, and that finds no `monthly/gym-log/<yyyy-mm>/` copy, writes one
there too: a run that fails on the 1st doesn't leave the month without one,
and a bad copy never takes the month's slot. R2's lifecycle rules are the
only retention; nothing here deletes.

**Nothing is ever overwritten.** Every key carries the moment it was taken
(and a monthly copy is only written where none exists), so a rerun or a
retry, even one reading a document that went bad since, adds a copy instead
of replacing a good one.

**Two age recipients.** Every copy is encrypted to the owner's public key
(`BACKUP_AGE_RECIPIENT`), whose private half stays offline, and to a separate
*drill* key whose private half is the Key Vault secret
`BACKUP-AGE-DRILL-KEY`. Either can decrypt. The drill key is what lets this job
prove, on every run, that what it uploaded comes back: it downloads each object
again, decrypts it, checks it is byte-for-byte what it read, and parses it the
way the app does. A backup that has never been restored is a hope, not a
backup. Whoever can read the drill key can already read the blob itself, so it
widens nothing.

**Copy first, judge second.** A document that fails to parse is still uploaded
and then fails the run, so the job-failure alert fires while the last good
copies are still in R2. A failure with one document or key is logged and
counted, and the rest are still attempted.

The Garmin session blob is deliberately not copied: it is a login token, which
a backup would only leak, and the next sync makes a new one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from appkit import integrity as kit
from appkit.backup import Backup, Document

from . import integrity, store
from .model import Log
from .settings import secret, settings

# The key prefix under daily/ and monthly/, shared by every app writing to the
# bucket. The repository's name, so a restore knows where to look.
APP = "gym-log"


def _log(raw: bytes) -> None:
    Log.from_json(raw.decode("utf-8"))


def _chat(raw: bytes) -> None:
    from .chat import parse

    parse(raw.decode("utf-8"))


def _load(name: str) -> Callable[[], bytes | None]:
    # Looked up on every call, so a test that patches store.load_raw sees it.
    return lambda: store.load_raw(name)


# A missing chat (none started yet) is skipped; a missing log fails.
JOB = Backup(
    APP,
    [
        Document(store.BLOB_NAME, _load(store.BLOB_NAME), _log, required=True),
        Document(store.CHAT_BLOB_NAME, _load(store.CHAT_BLOB_NAME), _chat, required=False),
    ],
    settings=settings,
    secret=secret,
)

logger = logging.getLogger(__name__)


def run(client: Any | None = None, now: datetime | None = None) -> int:
    """Back up, then check the log's rules and round trip: 1 if either fails.

    Judged after the daily and any monthly copy are in R2 (copy first, judge
    second), so a log that went bad fires the job-failure alert with every copy
    safe, and never costs the month its monthly copy. This is the daily
    integrity check; the selfcheck is the per-deploy one.
    """
    failed = JOB.run(client=client, now=now)
    try:
        raw = store.load_raw(store.BLOB_NAME)
    except Exception as exc:
        logger.error("integrity: not checked, the log did not read: %s", type(exc).__name__)
        return 1
    if raw is None:
        return failed
    found = kit.problems(raw, integrity.parse, integrity.dump, integrity.check_parsed)
    if found:
        logger.error("integrity: FAIL %s", kit.IntegrityError(found))
        return 1
    logger.info("integrity: ok %s", store.BLOB_NAME)
    return failed


keys = JOB.keys
check_bucket = JOB.check_bucket
r2_client = JOB.r2_client
daily_key = JOB.daily_key
monthly_key = JOB.monthly_key
