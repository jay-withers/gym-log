"""Application Insights, which is off in every test and most of the time in life.

Copied code — the module is shared with repo-agent and market-agent — so these
guard the copy, and in particular the two narrowings that exist to live inside a
`daily_quota_gb = 0.15` shared with every other tenant on the platform.

Nothing here touches the network: `configure_azure_monitor` is faked, because
what is worth asserting is *what it is asked for*, not that the SDK works.
"""

from __future__ import annotations

import os
import types

import azure.monitor.opentelemetry
import pytest

from gymlog import telemetry


@pytest.fixture(autouse=True)
def unconfigured(monkeypatch):
    """`_configured` is module state, and a test that set it would leak into the next."""
    monkeypatch.setattr(telemetry, "_configured", False)


@pytest.fixture
def configured(monkeypatch):
    """A connection string and a fake exporter; yields the calls it received."""
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=00000000")
    from gymlog import settings as settings_module

    settings_module.settings.cache_clear()

    calls: list[dict] = []
    monkeypatch.setattr(
        azure.monitor.opentelemetry,
        "configure_azure_monitor",
        lambda **kwargs: calls.append(kwargs),
    )
    return calls


def test_no_connection_string_disables_telemetry_entirely(monkeypatch):
    """Which is what keeps the tests and a local `make run` offline and cheap to start."""
    imported = []
    monkeypatch.setattr(
        azure.monitor.opentelemetry,
        "configure_azure_monitor",
        lambda **kwargs: imported.append(kwargs),
    )
    assert telemetry.configure("serve") is False
    assert imported == []


def test_only_the_gymlog_logger_is_exported(configured):
    """The distro attaches to the root logger, which would ship every third-party line.

    Container stdout already reaches Log Analytics, so that would be the same
    line paid for twice against a shared cap.
    """
    assert telemetry.configure("serve") is True
    assert configured[0]["logger_name"] == "gymlog"


def test_the_periodic_emissions_are_off(configured):
    """Both are emitted on a schedule by an app that is scaled to zero most of the week."""
    telemetry.configure("serve")
    assert configured[0]["enable_live_metrics"] is False
    assert configured[0]["enable_performance_counters"] is False


def test_a_trace_names_the_build_that_produced_it(configured, monkeypatch):
    """`IMAGE_TAG` is the immutable tag `make deploy` pushed, not a moving one."""
    monkeypatch.setenv("IMAGE_TAG", "abc1234")
    telemetry.configure("serve")

    attributes = configured[0]["resource"].attributes
    assert attributes["service.name"] == "gymlog-serve"
    assert attributes["service.version"] == "abc1234"
    assert attributes["deployment.environment"] == "dev"


def test_an_unbuilt_image_says_so_rather_than_looking_current(configured, monkeypatch):
    monkeypatch.delenv("IMAGE_TAG", raising=False)
    telemetry.configure("serve")
    assert configured[0]["resource"].attributes["service.version"] == "unknown"


def test_the_probes_are_excluded_from_tracing(configured, monkeypatch):
    """At one span each they would be nearly every span in the workspace."""
    monkeypatch.delenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", raising=False)
    telemetry.configure("serve")
    assert os.environ["OTEL_PYTHON_FASTAPI_EXCLUDED_URLS"] == "healthz,readyz"


def test_an_exclusion_list_set_by_the_operator_is_not_overwritten(configured, monkeypatch):
    """`setdefault`, so this can be widened from the container app without a release."""
    monkeypatch.setenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "healthz,readyz,static")
    telemetry.configure("serve")
    assert os.environ["OTEL_PYTHON_FASTAPI_EXCLUDED_URLS"] == "healthz,readyz,static"


def test_configuring_twice_configures_once(configured):
    """The instrumentation patches FastAPI.__init__; doing it twice is not free."""
    assert telemetry.configure("serve") is True
    assert telemetry.configure("serve") is True
    assert len(configured) == 1


# --- flushing ----------------------------------------------------------------


class _Provider:
    def __init__(self, raises: Exception | None = None) -> None:
        self.flushed: list[int] = []
        self.raises = raises

    def force_flush(self, timeout_millis: int) -> None:
        self.flushed.append(timeout_millis)
        if self.raises is not None:
            raise self.raises


def _providers(monkeypatch, *, trace_provider, logs_provider, meter_provider) -> None:
    from opentelemetry import _logs, metrics, trace

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: trace_provider)
    monkeypatch.setattr(_logs, "get_logger_provider", lambda: logs_provider)
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: meter_provider)


def test_flushing_unconfigured_telemetry_does_nothing(monkeypatch):
    """Every `finally` in the CLI calls this, including the runs with no exporter."""
    providers = (_Provider(), _Provider(), _Provider())
    _providers(
        monkeypatch,
        trace_provider=providers[0],
        logs_provider=providers[1],
        meter_provider=providers[2],
    )
    telemetry.flush()
    assert [p.flushed for p in providers] == [[], [], []]


def test_every_provider_is_flushed_before_exit(monkeypatch):
    """A scale-to-zero replica is stopped, not wound down.

    Without this it exits with its spans still buffered, which looks exactly
    like telemetry that was never configured in the first place.
    """
    monkeypatch.setattr(telemetry, "_configured", True)
    providers = (_Provider(), _Provider(), _Provider())
    _providers(
        monkeypatch,
        trace_provider=providers[0],
        logs_provider=providers[1],
        meter_provider=providers[2],
    )

    telemetry.flush(timeout_millis=500)
    assert [p.flushed for p in providers] == [[500], [500], [500]]


def test_a_provider_that_cannot_flush_is_skipped(monkeypatch):
    """The no-op providers the SDK installs when nothing is configured have no `force_flush`."""
    monkeypatch.setattr(telemetry, "_configured", True)
    real = _Provider()
    _providers(
        monkeypatch,
        trace_provider=types.SimpleNamespace(),
        logs_provider=real,
        meter_provider=types.SimpleNamespace(),
    )

    telemetry.flush()
    assert real.flushed == [10_000]


def test_a_failed_flush_does_not_become_the_error_the_process_reports(monkeypatch, caplog):
    """Losing telemetry must not turn a successful run into a failed one.

    Nor mask the exception the process may already be exiting on — this runs in
    a `finally`.
    """
    monkeypatch.setattr(telemetry, "_configured", True)
    _providers(
        monkeypatch,
        trace_provider=_Provider(raises=RuntimeError("exporter is down")),
        logs_provider=_Provider(),
        meter_provider=_Provider(),
    )

    telemetry.flush()
    assert "exporter is down" in caplog.text
