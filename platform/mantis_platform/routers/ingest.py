"""OTLP/HTTP trace intake. Accepts application/x-protobuf (what OpenTelemetry SDKs send) and application/json.

Pipeline per span: authenticate -> resolve tenant -> normalize to pinned GenAI semconv -> redact -> store.
Only keys with role 'ingest' (or higher) may write. Keys can never write to another project.
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..auth import Principal, get_principal
from ..db import get_db
from ..models import Project, Span
from ..redaction import Redactor

router = APIRouter(prefix="/v1", tags=["ingest"])

GENAI_TO_KIND = {"chat": "llm_generation", "text_completion": "llm_generation", "generate_content": "llm_generation",
                 "embeddings": "retrieval", "execute_tool": "tool_call", "invoke_agent": "planner_step"}

# Rough public list prices per 1M tokens (input, output). Only used when the SDK did not send cost.
PRICES = {"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.5, 10.0), "gpt-4.1": (2.0, 8.0),
          "claude-3-5-haiku": (0.8, 4.0), "claude-sonnet-4": (3.0, 15.0), "claude-opus-4": (15.0, 75.0)}


def _anyvalue(v: dict):
    if "stringValue" in v: return v["stringValue"]
    if "intValue" in v: return int(v["intValue"])
    if "doubleValue" in v: return v["doubleValue"]
    if "boolValue" in v: return v["boolValue"]
    if "arrayValue" in v: return [_anyvalue(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v: return {kv["key"]: _anyvalue(kv["value"]) for kv in v["kvlistValue"].get("values", [])}
    return None


def _attrs(lst) -> dict:
    return {kv["key"]: _anyvalue(kv.get("value", {})) for kv in (lst or [])}


def _proto_to_json(body: bytes) -> dict:
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    req = ExportTraceServiceRequest(); req.ParseFromString(body)
    return MessageToDict(req, preserving_proto_field_name=False)


def _hex(v: str, is_proto: bool, width: int) -> str:
    if is_proto:  # MessageToDict base64-encodes bytes fields
        import base64
        return base64.b64decode(v).hex()
    return v.lower().rjust(width, "0")


def estimate_cost(model: str | None, inp: int | None, out: int | None) -> float | None:
    if not model or inp is None:
        return None
    for k, (pi, po) in PRICES.items():
        if model.startswith(k):
            return round((inp * pi + (out or 0) * po) / 1_000_000, 6)
    return None


@router.post("/traces")
async def ingest_traces(request: Request, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    if p.role != "ingest":
        p.require("engineer")  # write-only ingest keys, or engineers and above
    if not p.project_id:
        raise HTTPException(400, "ingest requires a project-scoped key")
    project = db.get(Project, p.project_id)
    redactor = Redactor(project.redaction_rules)
    body = await request.body()
    ctype = request.headers.get("content-type", "")
    is_proto = "protobuf" in ctype
    payload = _proto_to_json(body) if is_proto else json.loads(body or b"{}")

    n_spans, n_redactions = 0, 0
    for rs in payload.get("resourceSpans", []):
        res_attrs = _attrs(rs.get("resource", {}).get("attributes"))
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                a = {**res_attrs, **_attrs(s.get("attributes"))}
                a, k = redactor.apply(a, project.store_payloads)
                events = [{"name": e.get("name"), "time_ns": int(e.get("timeUnixNano", 0)),
                           "attributes": redactor.apply(_attrs(e.get("attributes")), project.store_payloads)[0]}
                          for e in s.get("events", [])]
                kind = a.get("mantis.span.kind") or GENAI_TO_KIND.get(a.get("gen_ai.operation.name"), "generic")
                if a.get("gen_ai.tool.name"): kind = "tool_call"
                model = a.get("gen_ai.response.model") or a.get("gen_ai.request.model")
                it, ot = a.get("gen_ai.usage.input_tokens"), a.get("gen_ai.usage.output_tokens")
                status_code = (s.get("status") or {}).get("code", 0)
                db.add(Span(
                    project_id=p.project_id, environment_id=p.environment_id or a.get("mantis.environment_id"),
                    trace_id=_hex(s["traceId"], is_proto, 32), span_id=_hex(s["spanId"], is_proto, 16),
                    parent_span_id=_hex(s["parentSpanId"], is_proto, 16) if s.get("parentSpanId") else None,
                    name=s.get("name", ""), kind=kind,
                    start_ns=int(s.get("startTimeUnixNano", 0)), end_ns=int(s.get("endTimeUnixNano", 0)),
                    status="ERROR" if status_code in (2, "STATUS_CODE_ERROR") else "OK",
                    session_id=a.get("session.id") or a.get("mantis.session_id"),
                    user_ref=a.get("user.id") or a.get("mantis.user_ref"),
                    release=a.get("service.version") or a.get("mantis.release"),
                    provider=a.get("gen_ai.system") or a.get("gen_ai.provider.name"), model=model,
                    input_tokens=it, output_tokens=ot,
                    cost_usd=a.get("mantis.cost_usd") or estimate_cost(model, it, ot),
                    attributes=a, events=events, redactions_applied=k,
                ))
                n_spans += 1; n_redactions += k
    db.commit()
    return {"accepted_spans": n_spans, "redactions_applied": n_redactions, "partialSuccess": {}}
