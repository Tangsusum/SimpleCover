"""
OpenTelemetry setup for the SimpleCover backend.

Exports traces over OTLP/HTTP to the local Datadog Agent collector
(localhost:4318 by default — Agent OTLP ingest / DDOT).

Extracts both W3C `traceparent` and Datadog `x-datadog-*` headers so RUM
sessions (simple_cover_browser) continue as APM traces (simple_cover_backend).
"""
from __future__ import annotations

import os
from typing import Optional

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.textmap import (
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    set_span_in_context,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "simple_cover_backend")
SERVICE_VERSION = os.environ.get("OTEL_SERVICE_VERSION", "1.0")
ENV = os.environ.get("DD_ENV") or os.environ.get("OTEL_ENV", "dev")
OTLP_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318"
).rstrip("/")

_INITIALIZED = False


def _header(getter: Getter, carrier, key: str) -> Optional[str]:
    vals = getter.get(carrier, key)
    if not vals:
        return None
    return vals[0]


class DatadogPropagator(TextMapPropagator):
    """Minimal Datadog header propagator (x-datadog-*) for RUM ↔ APM."""

    TRACE_ID = "x-datadog-trace-id"
    PARENT_ID = "x-datadog-parent-id"
    SAMPLING = "x-datadog-sampling-priority"
    ORIGIN = "x-datadog-origin"

    def extract(self, carrier, context: Optional[Context] = None, getter: Getter = default_getter):
        if context is None:
            context = Context()
        trace_id_s = _header(getter, carrier, self.TRACE_ID)
        parent_id_s = _header(getter, carrier, self.PARENT_ID)
        if not trace_id_s or not parent_id_s:
            return context
        try:
            trace_id = int(trace_id_s, 10)
            span_id = int(parent_id_s, 10)
        except ValueError:
            return context
        if trace_id <= 0 or span_id <= 0:
            return context
        sampled_s = _header(getter, carrier, self.SAMPLING) or "1"
        try:
            sampled = int(sampled_s) >= 1
        except ValueError:
            sampled = True
        span_context = SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED if sampled else 0),
        )
        return set_span_in_context(NonRecordingSpan(span_context), context)

    def inject(self, carrier, context: Optional[Context] = None, setter: Setter = default_setter):
        span = trace.get_current_span(context)
        span_context = span.get_span_context()
        if not span_context.is_valid:
            return
        # Datadog uses the lower 64 bits of a 128-bit OTel trace id.
        setter.set(carrier, self.TRACE_ID, str(span_context.trace_id & 0xFFFFFFFFFFFFFFFF))
        setter.set(carrier, self.PARENT_ID, str(span_context.span_id))
        sampled = bool(span_context.trace_flags & TraceFlags.SAMPLED)
        setter.set(carrier, self.SAMPLING, "1" if sampled else "0")
        setter.set(carrier, self.ORIGIN, "rum")

    @property
    def fields(self):
        return {self.TRACE_ID, self.PARENT_ID, self.SAMPLING, self.ORIGIN}


def _use_local_otel() -> bool:
    """Local Datadog Agent OTLP (:4318). Cloud Run uses ddtrace + agentless LLM Obs."""
    if os.environ.get("OTEL_SDK_DISABLED", "").lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("K_SERVICE"):
        return False
    return True


def setup_telemetry(app) -> None:
    """Configure the tracer provider and instrument FastAPI + botocore. Idempotent."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    if not _use_local_otel():
        _INITIALIZED = True
        print(
            "[otel] skipped (Cloud Run / OTEL_SDK_DISABLED); "
            "ddtrace + LLM Obs agentless handle traces",
            flush=True,
        )
        return

    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": SERVICE_VERSION,
            "deployment.environment": ENV,
            "deployment.environment.name": ENV,
        }
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f"{OTLP_ENDPOINT}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Datadog first so W3C (128-bit padded) wins when both header sets are present.
    set_global_textmap(
        CompositePropagator([DatadogPropagator(), TraceContextTextMapPropagator()])
    )

    BotocoreInstrumentor().instrument()
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="health,/api/health",
        tracer_provider=provider,
        http_capture_headers_server_request=[
            "traceparent",
            "x-datadog-origin",
            "x-datadog-trace-id",
            "x-datadog-parent-id",
        ],
    )
    _INITIALIZED = True
    print(
        f"[otel] exporting traces to {OTLP_ENDPOINT} "
        f"service={SERVICE_NAME} env={ENV} version={SERVICE_VERSION}",
        flush=True,
    )


def tracer():
    return trace.get_tracer("simple_cover.backend", SERVICE_VERSION)
