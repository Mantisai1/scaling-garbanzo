"""Invokes the agent under evaluation and returns a normalized result linked to a trace.

invoke kinds:
  {"kind":"http","url":"https://.../run","headers":{...}}  -> POST {"input":..., "trace_id":...}; expects
        {"output": str, "tool_calls": [{"name":..,"arguments":..}], "trace_id": optional}
        The customer's endpoint is expected to be instrumented with the SDK so the trace arrives via OTLP.
  {"kind":"builtin_demo","behavior":"v1"|"v2"}              -> in-process demo agent that writes its own spans.
"""
import json
import time
import uuid

import httpx
from sqlalchemy.orm import Session

from ..models import Span

# A tiny "order support" agent used for demos. v1 forgets to call the lookup tool and sometimes echoes PII.
# v2 (the "trained" candidate) calls the tool and answers from its result.
ORDERS = {"A100": "shipped, arriving Friday", "A200": "processing", "A300": "delivered Tuesday"}


def _demo(item: dict, behavior: str, project_id: str, env_id: str | None, db: Session) -> dict:
    q = str(item["input"].get("question") if isinstance(item["input"], dict) else item["input"])
    order = next((o for o in ORDERS if o in q), None)
    trace_id, t0 = uuid.uuid4().hex, time.time_ns()
    root = uuid.uuid4().hex[:16]
    tool_calls, spans = [], []
    if behavior == "v2" and order:
        tool_calls.append({"name": "lookup_order", "arguments": {"order_id": order}})
        spans.append(Span(project_id=project_id, environment_id=env_id, trace_id=trace_id, span_id=uuid.uuid4().hex[:16], parent_span_id=root,
                          name="tool lookup_order", kind="tool_call", start_ns=t0 + 1_000_000, end_ns=t0 + 6_000_000,
                          attributes={"gen_ai.tool.name": "lookup_order", "gen_ai.tool.call.arguments": json.dumps({"order_id": order}),
                                      "gen_ai.tool.call.result": ORDERS[order]}))
        output = f"Order {order} is {ORDERS[order]}."
    elif behavior == "v2":
        output = "I can help with that — could you share your order number?"
    else:
        output = (f"I think order {order} is probably shipped. Contact us at support@example.com with your card number for details."
                  if order else "Sorry, I don't know.")
    it, ot = 120 + len(q), 20 + len(output) // 4
    spans.append(Span(project_id=project_id, environment_id=env_id, trace_id=trace_id, span_id=uuid.uuid4().hex[:16], parent_span_id=root,
                      name="chat demo-model", kind="llm_generation", start_ns=t0 + 7_000_000, end_ns=t0 + (900_000_000 if behavior == "v1" else 400_000_000),
                      provider="demo", model=f"demo-model-{behavior}", input_tokens=it, output_tokens=ot, cost_usd=round((it * 0.15 + ot * 0.6) / 1e6, 6),
                      attributes={"gen_ai.system": "demo", "gen_ai.request.model": f"demo-model-{behavior}", "gen_ai.prompt": q, "gen_ai.completion": output}))
    end = max(s.end_ns for s in spans) + 1_000_000
    spans.append(Span(project_id=project_id, environment_id=env_id, trace_id=trace_id, span_id=root, parent_span_id=None,
                      name="support_agent.run", kind="planner_step", start_ns=t0, end_ns=end, attributes={"mantis.eval": True}))
    db.add_all(spans); db.flush()
    return {"output": output, "tool_calls": tool_calls, "trace_id": trace_id,
            "latency_ms": round((end - t0) / 1e6, 3), "cost_usd": sum(s.cost_usd or 0 for s in spans)}


def invoke(agent_invoke: dict, item: dict, project_id: str, env_id: str | None, db: Session) -> dict:
    kind = agent_invoke.get("kind")
    if kind == "builtin_demo":
        return _demo(item, agent_invoke.get("behavior", "v1"), project_id, env_id, db)
    if kind == "http":
        trace_id = uuid.uuid4().hex
        t0 = time.perf_counter()
        r = httpx.post(agent_invoke["url"], json={"input": item["input"], "trace_id": trace_id, "task_id": item.get("id")},
                       headers=agent_invoke.get("headers", {}), timeout=agent_invoke.get("timeout_s", 60))
        r.raise_for_status()
        data = r.json()
        return {"output": data.get("output", ""), "tool_calls": data.get("tool_calls", []),
                "trace_id": data.get("trace_id", trace_id), "latency_ms": round((time.perf_counter() - t0) * 1000, 3),
                "cost_usd": data.get("cost_usd")}
    raise ValueError(f"unknown invoke kind {kind}")
