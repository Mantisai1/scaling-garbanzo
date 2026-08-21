import functools
import inspect
import json
import os
from contextlib import contextmanager

from opentelemetry import trace as ot
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Status, StatusCode

from .redaction import ClientRedactor

_provider: TracerProvider | None = None
_defaults: dict = {}


def init(endpoint: str | None = None, api_key: str | None = None, service_name: str = "agent",
         release: str | None = None, environment: str | None = None,
         redact_fields: list[str] | None = None, client_redaction: bool = True,
         extra_exporters: list[SpanExporter] | None = None, sync_export: bool = False,
         instrument: tuple[str, ...] = ("openai", "anthropic")) -> TracerProvider:
    """Configure the global tracer. Safe to call once per process."""
    global _provider
    endpoint = endpoint or os.getenv("MANTIS_ENDPOINT", "http://localhost:8080")
    api_key = api_key or os.getenv("MANTIS_API_KEY")
    if not api_key:
        raise ValueError("api_key required (or set MANTIS_API_KEY)")
    attrs = {"service.name": service_name, "telemetry.sdk.language": "python", "mantis.sdk.version": "0.1.0"}
    if release: attrs["service.version"] = release
    if environment: attrs["deployment.environment.name"] = environment
    _provider = TracerProvider(resource=Resource.create(attrs))
    if client_redaction:
        _provider.add_span_processor(ClientRedactor(deny_fields=redact_fields))  # must run before exporters
    exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces", headers={"X-API-Key": api_key})
    proc = SimpleSpanProcessor if sync_export else BatchSpanProcessor
    _provider.add_span_processor(proc(exporter))
    for ex in extra_exporters or []:
        _provider.add_span_processor(proc(ex))
    ot.set_tracer_provider(_provider)
    for name in instrument:
        try:
            __import__(f"mantis_sdk.integrations.{name}", fromlist=["patch"]).patch()
        except ImportError:
            pass  # provider library not installed; skip quietly
    return _provider


def get_tracer():
    return ot.get_tracer("mantis_sdk")


def set_attributes(**kwargs):
    sp = ot.get_current_span()
    for k, v in kwargs.items():
        sp.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else json.dumps(v))


@contextmanager
def span(name: str, kind: str = "generic", **attributes):
    """Generic nested span. kind: llm_generation | tool_call | retrieval | planner_step | policy_check | generic"""
    with get_tracer().start_as_current_span(name) as sp:
        sp.set_attribute("mantis.span.kind", kind)
        for k, v in attributes.items():
            sp.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else json.dumps(v))
        try:
            yield sp
        except Exception as e:  # noqa: BLE001
            sp.record_exception(e); sp.set_status(Status(StatusCode.ERROR, str(e))); raise


@contextmanager
def trace(name: str, session_id: str | None = None, user_ref: str | None = None, **attributes):
    """Root span for one agent run."""
    extra = {}
    if session_id: extra["session.id"] = session_id
    if user_ref: extra["user.id"] = user_ref
    with span(name, kind="planner_step", **extra, **attributes) as sp:
        yield sp


def tool(fn=None, *, name: str | None = None):
    """Decorator: records a tool_call span with JSON-serialized args/result (subject to redaction)."""
    def deco(f):
        tname = name or f.__name__
        if inspect.iscoroutinefunction(f):
            @functools.wraps(f)
            async def aw(*a, **kw):
                with span(f"tool {tname}", kind="tool_call", **{"gen_ai.tool.name": tname, "gen_ai.tool.call.arguments": json.dumps({"args": a, "kwargs": kw}, default=str)}) as sp:
                    r = await f(*a, **kw); sp.set_attribute("gen_ai.tool.call.result", json.dumps(r, default=str)[:4000]); return r
            return aw
        @functools.wraps(f)
        def w(*a, **kw):
            with span(f"tool {tname}", kind="tool_call", **{"gen_ai.tool.name": tname, "gen_ai.tool.call.arguments": json.dumps({"args": a, "kwargs": kw}, default=str)}) as sp:
                r = f(*a, **kw); sp.set_attribute("gen_ai.tool.call.result", json.dumps(r, default=str)[:4000]); return r
        return w
    return deco(fn) if fn else deco


def flush(timeout_ms: int = 10000):
    if _provider: _provider.force_flush(timeout_ms)


def shutdown():
    if _provider: _provider.shutdown()
