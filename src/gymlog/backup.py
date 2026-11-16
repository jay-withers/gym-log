"""`gymlog backup`: copy the log off Azure, encrypted, and prove the copy restores.

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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import store
from .model import Log

logger = logging.getLogger(__name__)

# The key prefix under daily/ and monthly/, shared by every app writing to the
# bucket. The repository's name, so a restore knows where to look.
APP = "gym-log"


def _log(raw: bytes) -> None:
    Log.from_json(raw.decode("utf-8"))


def _chat(raw: bytes) -> None:
    from .chat import parse

    parse(raw.decode("utf-8"))


@dataclass(frozen=True)
class Document:
    name: str
    # Parses the bytes or raises: what "restores" means for this document.
    validate: Callable[[bytes], None]
    # A missing optional document (no chat yet) is skipped; a missing log fails.
    required: bool


DOCUMENTS = (
    Document(store.BLOB_NAME, _log, required=True),
    Document(store.CHAT_BLOB_NAME, _chat, required=False),
)


def daily_key(name: str, now: datetime) -> str:
    return f"daily/{APP}/{now:%Y-%m-%dT%H%M%SZ}/{name}.age"


def monthly_key(name: str, now: datetime) -> str:
    return f"monthly/{APP}/{now:%Y-%m}/{name}.age"


def _exists(client: Any, bucket: str, key: str) -> bool:
    found = client.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
    return any(item.get("Key") == key for item in found.get("Contents", []))


def r2_client() -> Any:
    """An S3 client for the R2 bucket, from the settings and Key Vault."""
    import boto3
    from botocore.config import Config

    from .settings import secret, settings

    return boto3.client(
        "s3",
        endpoint_url=settings().backup_r2_endpoint,
        aws_access_key_id=secret("R2-ACCESS-KEY-ID"),
        aws_secret_access_key=secret("R2-SECRET-ACCESS-KEY"),
        region_name="auto",
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )


def check_bucket(client: Any | None = None) -> None:
    """Prove the R2 keys can list this app's prefix, writing nothing (selfcheck)."""
    from .settings import settings

    client = client or r2_client()
    client.list_objects_v2(Bucket=settings().backup_bucket, Prefix=f"daily/{APP}/", MaxKeys=1)


def keys() -> tuple[list[Any], Any]:
    """The recipients every copy is encrypted to, and the drill identity.

    Raises ValueError, naming what is wrong but never a key, when the backup
    settings are missing or a key doesn't parse. selfcheck calls it too, so a
    deploy that would fail every nightly run fails its smoke test instead.
    """
    from pyrage import x25519

    from .settings import secret, settings

    config = settings()
    missing = [
        name
        for name, value in (
            ("BACKUP_BUCKET", config.backup_bucket),
            ("BACKUP_R2_ENDPOINT", config.backup_r2_endpoint),
            ("BACKUP_AGE_RECIPIENT", config.backup_age_recipient),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"not configured, {', '.join(missing)} unset")
    try:
        recipient = x25519.Recipient.from_str(config.backup_age_recipient)
    except Exception:
        raise ValueError("BACKUP_AGE_RECIPIENT is not an age public key") from None
    try:
        drill = x25519.Identity.from_str(secret("BACKUP-AGE-DRILL-KEY").strip())
    except Exception:
        raise ValueError("BACKUP-AGE-DRILL-KEY is not an age secret key") from None
    return [recipient, drill.to_public()], drill


def run(client: Any | None = None, now: datetime | None = None) -> int:
    """Back up every document and verify each copy. 0 when all is well, else 1."""
    from .settings import settings

    try:
        recipients, drill = keys()
    except ValueError as exc:
        logger.error("backup: %s", exc)
        return 1
    config = settings()
    client = client or r2_client()
    now = now or datetime.now(UTC)

    failed = 0
    for doc in DOCUMENTS:
        try:
            failed += _back_up(client, config.backup_bucket, doc, recipients, drill, now)
        except Exception as exc:
            failed += 1
            logger.error("backup: FAIL %s: %s: %s", doc.name, type(exc).__name__, str(exc)[:300])

    if failed:
        logger.error("backup: %d problem(s)", failed)
        return 1
    logger.info("backup: all documents copied and verified")
    return 0


def _back_up(
    client: Any, bucket: str, doc: Document, recipients: list[Any], drill: Any, now: datetime
) -> int:
    """Copy one document daily (and monthly, if due) and verify each. Returns the failures."""
    import pyrage

    raw = store.load_raw(doc.name)
    if raw is None:
        if doc.required:
            logger.error("backup: FAIL %s: the document does not exist", doc.name)
            return 1
        logger.info("backup: skip %s: never written", doc.name)
        return 0

    sealed = pyrage.encrypt(raw, recipients)
    if not _copy(client, bucket, doc, daily_key(doc.name, now), sealed, raw, drill):
        return 1

    monthly = monthly_key(doc.name, now)
    try:
        if _exists(client, bucket, monthly):
            return 0
    except Exception as exc:
        logger.error(
            "backup: FAIL %s -> %s: %s: %s", doc.name, monthly, type(exc).__name__, str(exc)[:300]
        )
        return 1
    return 0 if _copy(client, bucket, doc, monthly, sealed, raw, drill) else 1


def _copy(
    client: Any, bucket: str, doc: Document, key: str, sealed: bytes, raw: bytes, drill: Any
) -> bool:
    """Upload one copy, then download, decrypt, compare and parse it. True if it restores."""
    import pyrage

    try:
        client.put_object(Bucket=bucket, Key=key, Body=sealed)
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        restored = pyrage.decrypt(body, [drill])
        if restored != raw:
            raise ValueError("the decrypted copy differs from the document")
        doc.validate(restored)
    except Exception as exc:
        logger.error(
            "backup: FAIL %s -> %s: %s: %s", doc.name, key, type(exc).__name__, str(exc)[:300]
        )
        return False
    logger.info("backup: ok %s -> %s (%d bytes, restored and parsed)", doc.name, key, len(raw))
    return True
