"""`health selfcheck`: prove a job's container could do a real run, and do nothing.

After every deploy, `make smoke` (jay-withers/workflows' scripts/smoke.sh)
starts one run of each job with its args swapped for `selfcheck`. That run
gets everything a real one does — the image, the managed identity, the env
and the Key Vault references — and checks what a real run needs before it
does anything: the settings load, every secret the jobs read resolves to a
value, and the log in storage reads.
It sends no email, calls no LLM, logs in to nothing and writes nothing.

Exits 1 if any check fails, naming each failure but never a secret's value. A
failure fails the smoke test, which rolls the deploy back.

The secrets are the ones the jobs read with `secret()`, which raises on a
missing value, so a run without one would fail anyway. Add a secret here when a
job starts needing one.
"""

from __future__ import annotations

from appkit import selfcheck as kit

# backup.py's (appkit's R2 keys and drill key). Checked only in the job given a
# bucket, so a missing R2 key fails that job's selfcheck without it reading as a
# fault in the others.
from appkit.backup import SECRETS as BACKUP_SECRETS

# insights.py, mailer.py (the insight job) and garmin.py (the garmin job).
SECRETS = (
    "DEEPSEEK-API-KEY",
    "RESEND-API-KEY",
    "INSIGHT-EMAIL-TO",
    "GARMIN-EMAIL",
    "GARMIN-PASSWORD",
)

__all__ = ["BACKUP_SECRETS", "SECRETS", "checks", "run"]


def checks() -> list[kit.Check]:
    from . import backup, integrity, store
    from .settings import secret, settings

    return [
        ("settings", settings),
        *kit.secret_checks(SECRETS, secret),
        ("storage", store.load),
        # The round trip only: will this release write back every stored value?
        # That is what a deploy can break. The rules are about the data, which a
        # deploy can't change: the store refuses new violations, and the backup
        # reports stored ones nightly. Gating here would let one old bad set
        # block every release, security fixes included.
        *kit.integrity_checks(
            store.BLOB_NAME,
            lambda: store.load_raw(store.BLOB_NAME),
            integrity.parse,
            integrity.dump,
        ),
        *kit.backup_checks(backup.JOB),
    ]


def run() -> int:
    return kit.run(checks())
