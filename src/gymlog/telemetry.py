"""Application Insights, configured at start-up and flushed before exit.

Copied from jay-withers/repo-agent src/repoagent/telemetry.py, which took it from
market-agent. Re-copy rather than diverge; fix bugs in all three. Only the logger
and service names differ, plus the FastAPI URL exclusion, which repo-agent
dropped because it serves nothing and which comes back here.

Terraform injects `APPLICATIONINSIGHTS_CONNECTION_STRING` from the shared
platform's Application Insights, but nothing reads it unless this module runs:
Container Apps has no codeless agent, so the variable on its own populates
precisely nothing. An absent or empty string disables telemetry entirely, which
keeps the tests and a local `make run` offline and free of the SDK's start-up
cost.

Two things are deliberately narrower than the distro's defaults, both because the
shared workspace runs on `daily_quota_gb = 0.15` split across every tenant:

- **Only the `gymlog` logger is exported.** The distro attaches its handler to
  the root logger, which would ship every third-party line to Application
  Insights *as well as* to Log Analytics, where container stdout already lands.
- **Performance counters and live metrics are off.** Both are periodic emissions
  from an app that is scaled to zero most of the week, and Container Apps already
  reports CPU and memory as free platform metrics.

**The probes are excluded from tracing.** `/healthz` and `/readyz` are polled on a
schedule for as long as a replica lives; at one span each they would be nearly
every span in the workspace and would say nothing about training.
"""

from __future__ import annotations

import logging
import os

from .settings import settings

logger = logging.getLogger(__name__)

_configured = False


def configure(role: str) -> bool:
    """Set up tracing, metrics and log export. Returns whether it was enabled.

    **Must run before the FastAPI application is imported.** The instrumentation
    patches `FastAPI.__init__`, so an app constructed first is never traced — and
    the failure is silent, which is the worst kind.
    """
    global _configured

    if _configured:
        return True

    connection_string = settings().applicationinsights_connection_string
    if not connection_string:
        return False

    os.environ.setdefault("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "healthz,readyz")

    # Imported here rather than at module scope so the model, the progression
    # rule and the tests need neither the SDK nor a connection string — the same
    # reason settings.py defers its Azure imports.
    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry.sdk.resources import Resource

    configure_azure_monitor(
        connection_string=connection_string,
        logger_name="gymlog",
        enable_live_metrics=False,
        enable_performance_counters=False,
        resource=Resource.create(
            {
                "service.name": f"gymlog-{role}",
                # The immutable image tag, so a trace names the build that
                # produced it.
                "service.version": os.environ.get("IMAGE_TAG", "unknown"),
                "deployment.environment": settings().environment,
            }
        ),
    )

    _configured = True
    logger.info("application insights enabled for %s", role)
    return True


def flush(timeout_millis: int = 10_000) -> None:
    """Push buffered telemetry before the process exits.

    The exporters batch, and a scale-to-zero replica is stopped rather than
    wound down gracefully. Without this it exits with its spans and logs still in
    the buffer, which looks exactly like telemetry that was never configured.

    Never raises: losing telemetry must not turn a successful run into a failed
    one, nor mask the exception the process is already exiting on.
    """
    if not _configured:
        return

    from opentelemetry import _logs, metrics, trace

    for provider in (
        trace.get_tracer_provider(),
        _logs.get_logger_provider(),
        metrics.get_meter_provider(),
    ):
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is None:
            continue
        try:
            force_flush(timeout_millis)
        except Exception as exc:
            logger.warning("flushing telemetry failed: %s", exc)
