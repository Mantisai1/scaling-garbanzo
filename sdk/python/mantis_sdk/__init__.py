"""Mantis SDK — a thin OpenTelemetry client.

    import mantis_sdk
    mantis_sdk.init(endpoint="http://localhost:8080", api_key="mk_...", service_name="support-agent", release="1.4.0")

    with mantis_sdk.trace("handle_ticket", session_id="s-1", user_ref="u-42") as span:
        ...
    @mantis_sdk.tool
    def lookup_order(order_id): ...

Everything is standard OTel under the hood: pass `extra_exporters=[...]` or set OTEL_EXPORTER_OTLP_ENDPOINT to
dual-write to your own collector. Attribute names follow the OpenTelemetry GenAI semantic conventions; product
specific fields live under the `mantis.*` namespace.
"""
from .client import init, trace, span, tool, set_attributes, flush, shutdown, get_tracer
from .redaction import ClientRedactor

__all__ = ["init", "trace", "span", "tool", "set_attributes", "flush", "shutdown", "get_tracer", "ClientRedactor"]
__version__ = "0.1.0"
