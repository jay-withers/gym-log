"""One entrypoint — `gymlog serve|import|show`.

`serve` is the workload; `import` and `show` are operator commands that run
against the real blob, so they need `STATE_CONTAINER_URL` and a credential with
`Storage Blob Data Contributor` on the container — which whoever applied the
Terraform already has.

Note Terraform deliberately sets **no** `command`: the Dockerfile's `ENTRYPOINT`
names this console script, and duplicating that name in Terraform creates a
second source of truth that is not versioned with the code defining it.
market-agent took a full outage from exactly that in September 2026, when an
`ignore_changes` command went stale against a renamed script and every workload
crash-looped on `executable file not found`. `args` picks the subcommand.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import date

from . import telemetry
from .settings import settings


def _configure_logging() -> None:
    """Plain logging to stdout.

    The Container Apps environment already ships stdout to Log Analytics, where
    `daily_quota_gb = 0.15` is shared with every other tenant on the platform.
    `telemetry.configure()` then exports this same `gymlog` logger to Application
    Insights when a connection string is present, so a line logged here is paid
    for twice — keep it that way round rather than logging more because one of
    the two destinations happens to be quiet.
    """
    logging.basicConfig(
        level=getattr(logging, settings().log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout,
    )
    # Chatty at INFO and say nothing useful about a session.
    for noisy in (
        "httpx",
        "httpcore",
        "azure.core.pipeline.policies.http_logging_policy",
        "azure.identity",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _terminate(signum: int, _frame: object) -> None:
    """Turn SIGTERM into an exception so `finally` blocks run.

    Container Apps sends SIGTERM before SIGKILL when a replica is scaled down —
    which, at `min_replicas = 0`, happens after every session. Without this the
    default disposition kills the process outright and buffered telemetry goes
    with it, because `telemetry.flush()` never runs.
    """
    raise SystemExit(f"terminated by signal {signum}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gymlog")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web application")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="reload on edit, for local work")

    load = sub.add_parser("import", help="import a block definition from an .xlsx")
    load.add_argument("path", help="path to the workbook, e.g. Gym_3.xlsx")
    load.add_argument("--name", default="", help="name for the block")
    load.add_argument(
        "--started",
        metavar="YYYY-MM-DD",
        help="the block's start date; defaults to today",
    )

    sub.add_parser("show", help="print the log as JSON")

    args = parser.parse_args(argv)

    _configure_logging()
    # Before the app is imported: the instrumentation patches FastAPI.__init__.
    telemetry.configure(args.command)
    signal.signal(signal.SIGTERM, _terminate)

    try:
        if args.command == "serve":
            return _serve(args)
        if args.command == "import":
            return _import(args)
        if args.command == "show":
            return _show()
    finally:
        telemetry.flush()

    return 1


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "gymlog.api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        # Access logs are noise against a tight shared ingestion cap, and the
        # probes alone would dominate them.
        access_log=False,
    )
    return 0


def _import(args: argparse.Namespace) -> int:
    from . import store
    from .importer import import_workbook

    started = date.fromisoformat(args.started) if args.started else date.today()
    block = import_workbook(args.path, started=started, name=args.name)

    log = store.update(lambda current: current.with_block(block))
    counts = ", ".join(f"{k}: {len(d.exercises)}" for k, d in sorted(block.days.items()))
    logging.getLogger("gymlog").info(
        "imported block %s (%s) — %s; %d block(s) now recorded",
        block.id,
        block.name,
        counts,
        len(log.blocks),
    )
    return 0


def _show() -> int:
    from . import store

    log, _etag = store.load()
    print(log.to_json())
    return 0
