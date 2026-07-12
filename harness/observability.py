import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI, Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.types import ASGIApp, Receive, Scope, Send

from harness.config import Settings

RUNS_STARTED = Counter("harness_runs_started_total", "Runs created", ["channel"])
RUNS_COMPLETED = Counter("harness_runs_completed_total", "Runs completed", ["status"])
RUN_DURATION = Histogram("harness_run_duration_seconds", "Run duration", ["status"])
LLM_TOKENS = Counter("harness_llm_tokens_total", "LLM tokens", ["model", "kind"])
TOOL_CALLS = Counter("harness_tool_calls_total", "Tool calls", ["tool", "status"])
WAITING_APPROVALS = Gauge("harness_waiting_approvals", "Pending approval requests")
ACTIVE_RUNS = Gauge("harness_active_runs", "Runs currently non-terminal")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("run_id", "tenant_id", "step_no", "tool_name", "status"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = str(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(settings: Settings) -> None:
    if not settings.json_logs:
        logging.basicConfig(level=logging.INFO)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def configure_observability(app: FastAPI, settings: Settings) -> None:
    configure_logging(settings)
    if settings.otel_enabled:
        resource = Resource.create({"service.name": settings.otel_service_name})
        provider = TracerProvider(resource=resource)
        if settings.otel_exporter_otlp_endpoint:
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
            )
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)

    app.add_middleware(MetricsMiddleware)


class MetricsMiddleware:
    """Plain ASGI middleware — not BaseHTTPMiddleware.

    BaseHTTPMiddleware runs the downstream app in a separate task inside its
    own anyio task group and pipes the response through an in-memory stream.
    When a downstream handler runs its own nested task group (as the MCP
    client SDK's streamable-HTTP transport does) and that inner group gets
    cancelled — e.g. a connection to a non-MCP endpoint fails mid-handshake —
    the cancellation can cross task boundaries and violate anyio's cancel
    scope ownership, surfacing as a spurious 500 instead of the real error.
    Plain ASGI middleware doesn't add that extra task/stream layer.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)

        duration = time.perf_counter() - start
        # Use the matched route's path template (e.g. "/v1/runs/{run_id}"),
        # not the raw URL, or every distinct id ever requested becomes its
        # own Prometheus label combination and cardinality grows unbounded.
        route = scope.get("route")
        path = route.path if route is not None else scope["path"]
        REQUESTS.labels(method=scope["method"], path=path, status=status_code).inc()
        REQUEST_DURATION.labels(method=scope["method"], path=path).observe(duration)


REQUESTS = Counter("harness_http_requests_total", "HTTP requests", ["method", "path", "status"])
REQUEST_DURATION = Histogram(
    "harness_http_request_duration_seconds", "HTTP request duration", ["method", "path"]
)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def tracer(name: str = "harness"):
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    with tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, str(value))
        yield current
