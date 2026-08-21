"""Auto-instrumentation for the official `openai` Python client (chat.completions.create).
Built against the public client API only. Records GenAI semconv attributes; prompt/completion content is
captured only when MANTIS_CAPTURE_CONTENT=1 (default on) and is subject to client + server redaction."""
import json
import os

import openai
from opentelemetry.trace import Status, StatusCode

from ..client import get_tracer

_patched = False
CAPTURE = os.getenv("MANTIS_CAPTURE_CONTENT", "1") == "1"


def patch():
    global _patched
    if _patched:
        return
    from openai.resources.chat import completions as comp
    original = comp.Completions.create

    def traced_create(self, *args, **kwargs):
        model = kwargs.get("model", "unknown")
        with get_tracer().start_as_current_span(f"chat {model}") as sp:
            sp.set_attribute("mantis.span.kind", "llm_generation")
            sp.set_attribute("gen_ai.system", "openai")
            sp.set_attribute("gen_ai.operation.name", "chat")
            sp.set_attribute("gen_ai.request.model", model)
            for k in ("temperature", "max_tokens", "top_p"):
                if k in kwargs: sp.set_attribute(f"gen_ai.request.{k}", kwargs[k])
            if CAPTURE:
                sp.set_attribute("gen_ai.prompt", json.dumps(kwargs.get("messages", []), default=str)[:8000])
            try:
                resp = original(self, *args, **kwargs)
            except Exception as e:  # noqa: BLE001
                sp.record_exception(e); sp.set_status(Status(StatusCode.ERROR, str(e))); raise
            if getattr(resp, "model", None): sp.set_attribute("gen_ai.response.model", resp.model)
            if getattr(resp, "usage", None):
                sp.set_attribute("gen_ai.usage.input_tokens", resp.usage.prompt_tokens)
                sp.set_attribute("gen_ai.usage.output_tokens", resp.usage.completion_tokens)
            if CAPTURE and getattr(resp, "choices", None):
                c = resp.choices[0]
                sp.set_attribute("gen_ai.completion", (c.message.content or "")[:8000])
                sp.set_attribute("gen_ai.response.finish_reasons", [c.finish_reason or ""])
                if c.message.tool_calls:
                    sp.set_attribute("gen_ai.response.tool_calls", json.dumps([t.model_dump() for t in c.message.tool_calls])[:8000])
            return resp

    comp.Completions.create = traced_create
    _patched = True
