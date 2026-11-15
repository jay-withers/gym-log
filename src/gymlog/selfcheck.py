"""`gymlog selfcheck`: prove a job's container could do a real run, and do nothing.

After every deploy, `make smoke` (scripts/smoke.sh) starts one run of each job
with its args swapped for `selfcheck`. That run gets everything a real one
does — the image, the managed identity, the env and the Key Vault references —
and checks what a real run needs before it does anything: the settings load,
every secret the jobs read resolves to a value, and the log in storage reads.
It sends no email, calls no LLM, logs in to nothing and writes nothing.

Exits 1 if any check fails, naming each failure but never a secret's value. A
failure fails the smoke test, which rolls the deploy back.

The secrets are the ones the jobs read with `secret()`, which raises on a
missing value, so a run without one would fail anyway. Add a secret here when a
job starts needing one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial

logger = logging.getLogger(__name__)

# insights.py, mailer.py (the insight job) and garmin.py (the garmin job).
SECRETS = (
    "DEEPSEEK-API-KEY",
    "RESEND-API-KEY",
    "INSIGHT-EMAIL-TO",
    "GARMIN-EMAIL",
    "GARMIN-PASSWORD",
)

# backup.py's. Checked only in the job given a bucket, so a missing R2 key
# fails that job's selfcheck without it reading as a fault in the others.
BACKUP_SECRETS = (
    "R2-ACCESS-KEY-ID",
    "R2-SECRET-ACCESS-KEY",
    "BACKUP-AGE-DRILL-KEY",
)


def checks() -> list[tuple[str, Callable[[], object]]]:
    from . import store
    from .settings import secret, settings

    def present(name: str) -> None:
        if not secret(name):
            raise ValueError("resolved to an empty value")

    def secret_checks(names: tuple[str, ...]) -> list[tuple[str, Callable[[], object]]]:
        return [(f"secret {name}", partial(present, name)) for name in names]

    found: list[tuple[str, Callable[[], object]]] = [
        ("settings", settings),
        *secret_checks(SECRETS),
        ("storage", store.load),
    ]
    # Only the backup job is given a bucket, so only its selfcheck lists these.
    if settings().backup_bucket:
        from . import backup

        found += secret_checks(BACKUP_SECRETS)
        found.append(("backup bucket", backup.check_bucket))
    return found


def run() -> int:
    failed = 0
    for label, check in checks():
        try:
            check()
        except Exception as exc:
            failed += 1
            logger.error("selfcheck: FAIL %s: %s: %s", label, type(exc).__name__, str(exc)[:300])
        else:
            logger.info("selfcheck: ok %s", label)
    if failed:
        logger.error("selfcheck: %d check(s) failed", failed)
        return 1
    logger.info("selfcheck: all checks passed")
    return 0
