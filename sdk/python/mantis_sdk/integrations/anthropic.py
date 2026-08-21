"""Auto-instrumentation for the official `anthropic` Python client (messages.create). Public API only."""
import json
import os

import anthropic
from opentelemetry.trace import Status, StatusCode

from ..client import get_tracer

_patched = False
CAPTURE = os.getenv("MANTIS_CAPTURE_CONTENT", "1") == "1"


def patch():
    global _patched
    if _patched:
        return
    from anthropic.resources import messages as msgs
    original = msgs.Messages.create

    def traced_create(self, *args, **kwargs):
        model = kwargs.get("model", "unknown")
        with get_tracer().start_as_current_span(f"chat {model}") as sp:
            sp.set_attribute("mantis.span.kind", "llm_generation")
            sp.set_attribute("gen_ai.system", "anthropic")
            sp.set_attribute("gen_ai.operation.name", "chat")
            sp.set_attribute("gen_ai.request.model", model)
            if "max_tokens" in kwargs: sp.set_attribute("gen_ai.request.max_tokens", kwargs["max_tokens"])
            if CAPTURE:
                sp.set_attribute("gen_ai.prompt", json.dumps({"system": kwargs.get("system"), "messages": kwargs.get("messages", [])}, default=str)[:8000])
            try:
                resp = original(self, *args, **kwargs)
            except Exception as e:  # noqa: BLE001
                sp.record_exception(e); sp.set_status(Status(StatusCode.ERROR, str(e))); raise
            if getattr(resp, "model", None): sp.set_attribute("gen_ai.response.model", resp.model)
            if getattr(resp, "usage", None):
                sp.set_attribute("gen_ai.usage.input_tokens", resp.usage.input_tokens)
                sp.set_attribute("gen_ai.usage.output_tokens", resp.usage.output_tokens)
            if CAPTURE and getattr(resp, "content", None):
                text = "".join(getattr(b, "text", "") for b in resp.content)
                sp.set_attribute("gen_ai.completion", text[:8000])
                sp.set_attribute("gen_ai.response.finish_reasons", [resp.stop_reason or ""])
                tools = [b.model_dump() for b in resp.content if getattr(b, "type", "") == "tool_use"]
                if tools: sp.set_attribute("gen_ai.response.tool_calls", json.dumps(tools)[:8000])
            return resp

    msgs.Messages.create = traced_create
    _patched = True
