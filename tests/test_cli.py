"""The single entrypoint the image's ENTRYPOINT resolves to.

Worth testing beyond `seed` (which tests/test_seed.py already drives end to end)
for one reason: Terraform sets **no** `command`, so `args = ["serve"]` is the
only thing standing between a revision and a crash-looping container. A
subcommand that stops dispatching is a full outage, and market-agent has already
taken one from exactly that.
"""

from __future__ import annotations

import logging
import signal
from datetime import date

import pytest

from gymlog import store, telemetry
from gymlog.cli import _configure_logging, _terminate, main
from gymlog.model import Log

from .factories import block, garmin_activity, garmin_day, insight


def test_serve_runs_the_app_with_the_access_log_off(monkeypatch):
    """Access logs are noise against a shared cap, and the probes would dominate them."""
    import uvicorn

    calls: list[tuple] = []
    monkeypatch.setattr(uvicorn, "run", lambda target, **kwargs: calls.append((target, kwargs)))

    assert main(["serve", "--port", "9000"]) == 0

    target, kwargs = calls[0]
    # The import string rather than an app object: `--reload` cannot work
    # against one already constructed.
    assert target == "gymlog.api.main:app"
    assert kwargs["port"] == 9000
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["access_log"] is False


def test_show_prints_the_document_as_the_blob_holds_it(capsys):
    """`gymlog show` is the operator's read of the real log, so it must be the real shape."""
    original = Log(blocks=(block(),))
    store.save(original)

    assert main(["show"]) == 0
    assert Log.from_json(capsys.readouterr().out) == original


def test_import_dates_the_block_today_by_default(monkeypatch):
    imported: list[dict] = []

    def fake_import(path, started, name):
        imported.append({"path": path, "started": started, "name": name})
        return block(started.isoformat())

    monkeypatch.setattr("gymlog.importer.import_workbook", fake_import)

    assert main(["import", "Gym_3.xlsx"]) == 0
    assert imported[0]["path"] == "Gym_3.xlsx"
    assert imported[0]["started"] == date.today()


def test_import_takes_a_start_date_and_a_name(monkeypatch):
    """A block is nearly always imported after its first session, not before it."""
    monkeypatch.setattr(
        "gymlog.importer.import_workbook",
        lambda path, started, name: block(started.isoformat()),
    )

    assert main(["import", "Gym_3.xlsx", "--started", "2026-09-01", "--name", "Block 3"]) == 0

    log, _etag = store.load()
    assert log.current_block is not None
    assert log.current_block.id == "2026-09-01"


def test_import_adds_a_block_rather_than_replacing_the_log(monkeypatch):
    """It goes through `store.update`, so an import cannot drop the sessions."""
    store.save(Log(blocks=(block("2026-07-01"),)))
    monkeypatch.setattr(
        "gymlog.importer.import_workbook",
        lambda path, started, name: block("2026-09-01"),
    )

    main(["import", "Gym_3.xlsx"])

    log, _etag = store.load()
    assert [b.id for b in log.blocks] == ["2026-07-01", "2026-09-01"]


def test_insight_records_what_generate_insight_returns(monkeypatch):
    generated = insight(id="i1", week_of="2026-09-15")
    monkeypatch.setattr("gymlog.insights.generate_insight", lambda log: generated)

    assert main(["insight"]) == 0

    log, _etag = store.load()
    assert log.insights == (generated,)


def test_garmin_sync_records_what_sync_garmin_returns(monkeypatch):
    activity = garmin_activity(id="a1")
    day = garmin_day(date="2026-09-15")
    monkeypatch.setattr("gymlog.garmin.sync_garmin", lambda days=None: ([activity], [day]))

    assert main(["garmin-sync"]) == 0

    log, _etag = store.load()
    assert log.garmin_activities == (activity,)
    assert log.garmin_days == (day,)


def test_garmin_sync_days_flag_is_passed_through_for_a_one_off_backfill(monkeypatch):
    seen_days: list[int | None] = []

    def fake_sync_garmin(days=None):
        seen_days.append(days)
        return [], []

    monkeypatch.setattr("gymlog.garmin.sync_garmin", fake_sync_garmin)

    assert main(["garmin-sync", "--days", "30"]) == 0

    assert seen_days == [30]


def test_a_missing_subcommand_is_refused(capsys):
    """`args` on the container app is what picks one; an empty one must not serve."""
    with pytest.raises(SystemExit):
        main([])


def test_telemetry_is_flushed_even_when_the_command_fails(monkeypatch):
    """It runs in a `finally` for this reason: a crash is when the spans matter most."""
    flushed: list[bool] = []
    monkeypatch.setattr(telemetry, "flush", lambda: flushed.append(True))
    monkeypatch.setattr(
        "gymlog.cli._show", lambda: (_ for _ in ()).throw(RuntimeError("blob down"))
    )

    with pytest.raises(RuntimeError):
        main(["show"])
    assert flushed == [True]


def test_telemetry_is_configured_before_the_app_is_imported(monkeypatch):
    """The instrumentation patches `FastAPI.__init__`, so ordering is the whole thing.

    Asserted by what `configure` is told: the role names the subcommand, which
    is what separates a `serve` trace from an `import` one in the workspace.
    """
    roles: list[str] = []
    monkeypatch.setattr(telemetry, "configure", lambda role: roles.append(role))
    monkeypatch.setattr(telemetry, "flush", lambda: None)

    main(["show"])
    assert roles == ["show"]


def test_sigterm_becomes_an_exception_so_finally_blocks_run(monkeypatch):
    """At `min_replicas = 0` this happens after every session, not rarely."""
    installed: list[tuple] = []
    monkeypatch.setattr(
        signal, "signal", lambda signum, handler: installed.append((signum, handler))
    )
    monkeypatch.setattr(telemetry, "flush", lambda: None)

    main(["show"])
    assert installed[0][0] == signal.SIGTERM

    with pytest.raises(SystemExit, match="terminated by signal"):
        _terminate(signal.SIGTERM, None)


def test_the_chatty_loggers_are_quietened(monkeypatch):
    """They say nothing about a session and are paid for twice — stdout and App Insights."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    from gymlog import settings as settings_module

    settings_module.settings.cache_clear()

    _configure_logging()
    assert logging.getLogger("azure.identity").level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING
