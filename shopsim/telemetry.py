"""OpenTelemetry tracing (manual instrumentation, OTLP/HTTP export).

Spans are created by hand rather than by auto-instrumentation packages so the
span names and attributes are stable across library upgrades and exactly what
the Perftelion test plan expects:

* SERVER  ``GET /api/products/{productId}`` (route template, not raw path)
* CLIENT  ``POST /stock/reservations`` with ``peer.service=inventory``
* CLIENT  ``UPDATE stock (reserve)`` with ``db.system=sqlite``, ``peer.service=inventory-db``
* INTERNAL ``db.pool.acquire`` carrying the time spent waiting for a connection
"""
from __future__ import annotations

import socket

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_provider: TracerProvider | None = None


def setup_tracing(*, service: str, version: str, env: str, instance: str, endpoint: str | None) -> trace.Tracer:
    global _provider
    resource = Resource.create(
        {
            "service.name": service,
            "service.namespace": "shopsim",
            "service.version": version,
            "service.instance.id": instance,
            "deployment.environment.name": env,
            "host.name": socket.gethostname(),
        }
    )
    _provider = TracerProvider(resource=resource)
    if endpoint:
        import requests
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        # The collector is on loopback: never route through an environment or system (registry) proxy.
        session = requests.Session()
        session.trust_env = False
        _provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, timeout=5, session=session),
                max_queue_size=8192,
                max_export_batch_size=512,
                schedule_delay_millis=2000,
            )
        )
    trace.set_tracer_provider(_provider)
    return trace.get_tracer("shopsim", version)


def shutdown_tracing() -> None:
    if _provider is not None:
        _provider.shutdown()


def tracer() -> trace.Tracer:
    return trace.get_tracer("shopsim")


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
