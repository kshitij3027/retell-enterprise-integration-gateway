"""OpenTelemetry wiring (CR-13).

Startup calls `init_tracing(service_name, endpoint)` which:
  * registers a `TracerProvider` with `service.name=<service_name>`,
  * attaches a `BatchSpanProcessor(OTLPSpanExporter(endpoint))` so spans
    flush to Jaeger's OTLP gRPC port (default 4317),
  * installs the FastAPI + httpx + asyncpg instrumentors so every HTTP
    request and DB call gets a span for free.

Individual code paths then open **manual spans** at meaningful seams:
  * `webhook.received`      — in app/routes/webhooks.py
  * `signature.verified`    — in app/signature.py
  * `dedup.checked`         — in app/dedup.py
  * `pii.redacted`          — in app/pii.py
  * `oauth.refreshed`       — in adapters/salesforce.py
  * `adapter.upsert`        — in adapters/salesforce.py
  * `audit.written`         — in app/audit.py
  * `inbound.hydrate`       — in app/routes/inbound.py (C9)
  * `sfdc.lookup.by_phone`  — in adapters/salesforce.py (C9)

Each span carries attributes (retell.call_id, tenant.id, sfdc.lead_id,
pii.entities_removed, etc.) so a Jaeger search by call_id surfaces the
full tree.

Idempotency
-----------
`init_tracing` checks a module-level flag. Calling it twice is a no-op —
handy for tests that spin up the app multiple times per session.

Test harness
------------
`install_in_memory_tracer()` swaps the global provider for one that
writes to an `InMemorySpanExporter`. Tests call this in a fixture,
fire traffic through the app, and assert on exporter.get_finished_spans().
Not to be used outside tests.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.logging import get_logger

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

log = get_logger(__name__)

_INITIALIZED = False


def init_tracing(service_name: str, otlp_endpoint: str | None = None) -> None:
    """Register a TracerProvider with OTLP export to Jaeger.

    Args:
        service_name: e.g. "retell-integration-gateway". Landing in
                      Jaeger's service dropdown.
        otlp_endpoint: gRPC endpoint, typically `http://jaeger:4317`.
                       None skips OTLP registration (tests use the
                       in-memory exporter via `install_in_memory_tracer`).

    Side effects:
        * globally sets the TracerProvider via trace.set_tracer_provider.
        * instruments FastAPI + httpx + asyncpg if imported.

    Safe to call multiple times — only the first call wires everything.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        log.info(
            "tracing.init.otlp",
            service_name=service_name,
            endpoint=otlp_endpoint,
        )
    else:
        log.info("tracing.init.noop", service_name=service_name)

    trace.set_tracer_provider(provider)
    _INITIALIZED = True

    # Instrumentors — best-effort; failures here are logged, not raised.
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001 — optional instrumentation
        log.warning("tracing.httpx_instrument_failed", error=str(exc))

    try:
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor

        AsyncPGInstrumentor().instrument()  # type: ignore[no-untyped-call]
    except Exception as exc:  # noqa: BLE001 — optional instrumentation
        log.warning("tracing.asyncpg_instrument_failed", error=str(exc))


def instrument_fastapi(app: Any) -> None:
    """Attach the FastAPIInstrumentor to an already-built app instance.

    FastAPI must be instrumented AFTER the app is constructed (not at
    import time), so this helper is separate from init_tracing.
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:  # noqa: BLE001 — optional instrumentation
        log.warning("tracing.fastapi_instrument_failed", error=str(exc))


def get_tracer(name: str = "reig") -> Any:
    """Return a tracer handle. Production code uses `tracer.start_as_current_span`."""
    from opentelemetry import trace

    return trace.get_tracer(name)


def install_in_memory_tracer() -> InMemorySpanExporter:
    """Test-only — swap the global provider for one with an InMemorySpanExporter.

    Returns the exporter; callers call `.get_finished_spans()` to inspect.
    Resets `_INITIALIZED` so a subsequent `init_tracing` is still a no-op
    (tests don't want production init clobbering the in-memory setup).
    """
    global _INITIALIZED
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    resource = Resource.create({"service.name": "reig-test"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _INITIALIZED = True
    return exporter


def reset_for_tests() -> None:
    """Clear the initialisation flag. Test-only."""
    global _INITIALIZED
    _INITIALIZED = False
