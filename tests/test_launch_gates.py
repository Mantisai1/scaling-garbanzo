"""The eight non-negotiable launch gates, as one continuous end-to-end test on a fresh tenant.

1. Instrumented agent sends a trace via OTLP.
2. Sensitive fields are redacted before storage.
3. Authorized member finds/exports the trace; unauthorized cannot.
4. Trace produces an evaluation record.
5. Approved annotations generate a versioned training dataset.
6. Training job produces a tracked candidate.
7. Candidate is blocked/promoted only by versioned policy.
8. Every action is attributable to an identity in the audit log.
"""
import os
import sys
import time

import pytest

os.environ["DATABASE_URL"] = "sqlite:///./test_gates.db"
os.environ["MANTIS_BOOTSTRAP_TOKEN"] = "test-token"
for f in ("test_gates.db",):
    if os.path.exists(f): os.remove(f)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "platform"))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))

from fastapi.testclient import TestClient  # noqa: E402
from mantis_platform.main import app  # noqa: E402

client = TestClient(app)
S = {}  # shared state across ordered tests


def h(key): return {"X-API-Key": key}


def test_0_bootstrap_and_tenancy():
    r = client.post("/v1/admin/bootstrap", json={"org_name": "Acme"}, headers={"X-Bootstrap-Token": "test-token"})
    assert r.status_code == 200, r.text
    S["admin"] = r.json()["api_key"]
    assert client.post("/v1/admin/bootstrap", json={"org_name": "x"}, headers={"X-Bootstrap-Token": "test-token"}).status_code == 409
    r = client.post("/v1/admin/projects", json={"name": "support-agent", "redaction_rules": {"deny_fields": ["user.internal_note"]}}, headers=h(S["admin"]))
    S["project"] = r.json()["id"]; S["envs"] = {e["name"]: e["id"] for e in r.json()["environments"]}
    r2 = client.post("/v1/admin/projects", json={"name": "other-project"}, headers=h(S["admin"]))
    S["other_project"] = r2.json()["id"]
    for role in ("ingest", "engineer", "reviewer", "data_approver", "release_approver", "viewer"):
        r = client.post("/v1/admin/keys", json={"name": f"{role}-key", "role": role, "project_id": S["project"]}, headers=h(S["admin"]))
        assert r.status_code == 200, r.text
        S[role] = r.json()["api_key"]
    r = client.post("/v1/admin/keys", json={"name": "outsider", "role": "engineer", "project_id": S["other_project"]}, headers=h(S["admin"]))
    S["outsider"] = r.json()["api_key"]


def test_1_sdk_sends_trace_via_otlp():
    """Gate 1: real SDK -> real OTLP/protobuf -> ingest endpoint (through the ASGI test transport)."""
    import httpx
    import mantis_sdk
    from opentelemetry.exporter.otlp.proto.http import trace_exporter as te

    # Route the exporter's HTTP session through the in-process app.
    class _Session(httpx.Client):
        def post(self, url, data=None, headers=None, timeout=None, **kw):
            hdrs = {**dict(self.headers), **(headers or {})}
            r = client.post("/v1/traces", content=data, headers=hdrs)
            r.ok = r.status_code < 400; r.reason = r.text; return r
    orig = te.requests.Session
    te.requests.Session = _Session
    try:
        mantis_sdk.init(endpoint="http://testserver", api_key=S["ingest"], service_name="support-agent", release="1.0.0",
                        sync_export=True, instrument=(), redact_fields=["user.internal_note"])
        @mantis_sdk.tool
        def lookup_order(order_id: str): return {"status": "shipped"}
        with mantis_sdk.trace("handle_ticket", session_id="sess-1", user_ref="u-42") as sp:
            S["trace_id"] = format(sp.get_span_context().trace_id, "032x")
            with mantis_sdk.span("chat gpt-4o-mini", kind="llm_generation", **{
                "gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o-mini", "gen_ai.usage.input_tokens": 200, "gen_ai.usage.output_tokens": 40,
                "gen_ai.prompt": "Customer jane.doe@example.com asks about order A100; card 4111 1111 1111 1111",
                "gen_ai.completion": "Order A100 has shipped.", "user.internal_note": "VIP, do not escalate"}):
                lookup_order("A100")
        mantis_sdk.flush()
    finally:
        te.requests.Session = orig
    r = client.get(f"/v1/traces/{S['trace_id']}", headers=h(S["engineer"]))
    assert r.status_code == 200, r.text
    t = r.json()
    assert len(t["spans"]) == 3
    kinds = {s["kind"] for s in t["spans"]}
    assert kinds == {"planner_step", "llm_generation", "tool_call"}
    assert t["totals"]["input_tokens"] == 200 and t["totals"]["cost_usd"] > 0
    S["trace"] = t


def test_2_redaction_before_storage():
    """Gate 2: PII/secrets and denied fields never reach the store."""
    llm = next(s for s in S["trace"]["spans"] if s["kind"] == "llm_generation")
    prompt = llm["attributes"]["gen_ai.prompt"]
    assert "jane.doe@example.com" not in prompt and "[REDACTED:email]" in prompt
    assert "4111" not in prompt and "[REDACTED:card]" in prompt
    assert llm["attributes"]["user.internal_note"] == "[REDACTED:field]"
    # Server-side redaction is independent of the client: send raw JSON OTLP with a secret in it.
    body = {"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": [{
        "traceId": "a" * 32, "spanId": "b" * 16, "name": "raw", "startTimeUnixNano": "1", "endTimeUnixNano": "2",
        "attributes": [{"key": "gen_ai.prompt", "value": {"stringValue": "key is sk-live-ABCDEFGHIJKLMNOP call 555-123-4567"}}]}]}]}]}
    r = client.post("/v1/traces", json=body, headers=h(S["ingest"]))
    assert r.json()["accepted_spans"] == 1 and r.json()["redactions_applied"] >= 2
    raw = client.get("/v1/traces/" + "a" * 32, headers=h(S["engineer"])).json()["spans"][0]["attributes"]["gen_ai.prompt"]
    assert "sk-live" not in raw and "555-123" not in raw


def test_3_authorization_boundaries():
    """Gate 3: authorized export works and is audited; other-project and viewer keys cannot export; ingest keys cannot read."""
    assert client.get(f"/v1/traces/{S['trace_id']}/export", headers=h(S["engineer"])).status_code == 200
    assert client.get(f"/v1/traces/{S['trace_id']}", headers=h(S["outsider"])).status_code == 404
    assert client.get(f"/v1/traces/{S['trace_id']}/export", headers=h(S["viewer"])).status_code == 403
    assert client.get("/v1/traces", headers=h(S["ingest"])).status_code == 403
    assert client.get("/v1/traces", headers={"X-API-Key": "mk_bogus"}).status_code == 401
    assert client.get("/v1/traces", headers=h(S["outsider"])).json() == []  # tenant isolation: sees nothing of ours
    # ingest key cannot write to another project (it is bound to its own)
    assert client.post("/v1/traces", json={"resourceSpans": []}, headers=h(S["viewer"])).status_code == 403


def test_4_trace_produces_evaluation_record():
    """Gate 4: dataset -> agent config -> eval run; every result links to a trace with scores attached."""
    items = [{"id": "t1", "input": {"question": "Where is order A100?"}, "expected": "Order A100 is shipped, arriving Friday.", "assertions": {"must_call": ["lookup_order"]}},
             {"id": "t2", "input": {"question": "Status of A200 please"}, "expected": "Order A200 is processing."},
             {"id": "t3", "input": {"question": "Did A300 arrive?"}, "expected": "Order A300 is delivered Tuesday."},
             {"id": "t4", "input": {"question": "Hi, I have a question about my order"}, "expected": "could you share your order number"}]
    r = client.post("/v1/datasets", json={"name": "support-golden", "items": items}, headers=h(S["engineer"]))
    assert r.status_code == 200, r.text
    S["dv"] = r.json()["version_id"]
    # immutability: a re-import creates version 2, version 1 is untouched
    r = client.post(f"/v1/datasets/{r.json()['id']}/import", files={"file": ("more.jsonl", '{"id":"t5","input":{"question":"x"}}\n')}, headers=h(S["engineer"]))
    assert r.json()["version"] == 2
    assert client.get(f"/v1/datasets/versions/{S['dv']}", headers=h(S["viewer"])).json()["version"] == 1
    S["scorers"] = [{"type": "contains"}, {"type": "tool_call_assert"}, {"type": "no_pii_leak"}, {"type": "latency_budget_ms", "max_ms": 800},
                    {"type": "llm_judge", "rubric": "Does the answer correctly state the order status?"}]
    r = client.post("/v1/agent-configs", json={"name": "support-agent", "version": "1.0.0", "model": "demo-model-v1", "prompt_version": "p1",
                                               "invoke": {"kind": "builtin_demo", "behavior": "v1"}}, headers=h(S["engineer"]))
    S["base_cfg"] = r.json()["id"]
    r = client.post("/v1/eval-runs", json={"dataset_version_id": S["dv"], "agent_config_id": S["base_cfg"], "scorers": S["scorers"]}, headers=h(S["engineer"]))
    assert r.status_code == 200, r.text
    S["baseline_run"] = r.json()["id"]
    run = client.get(f"/v1/eval-runs/{S['baseline_run']}", headers=h(S["viewer"])).json()
    assert run["pinned"]["dataset_hash"] and run["pinned"]["evaluator_version"] and run["pinned"]["environment_image"]
    assert all(res["trace_id"] for res in run["results"])
    tr = client.get(f"/v1/traces/{run['results'][0]['trace_id']}", headers=h(S["viewer"])).json()
    assert len(tr["scores"]) == len(S["scorers"])
    assert run["summary"]["no_pii_leak"] < 1.0  # v1 leaks PII, so the gate will have something to block
    assert run["summary"]["tool_call_assert"] < 1.0


def test_5_approved_annotations_become_versioned_training_dataset():
    """Gate 5: queue failures -> reviewer annotates -> approver approves -> signals -> versioned training dataset."""
    r = client.post("/v1/annotations/queue", json={"eval_run_id": S["baseline_run"], "only_failures_below": 1.0}, headers=h(S["engineer"]))
    queued = r.json()["queued"]; assert len(queued) >= 3
    # no signals exist before approval
    assert client.get("/v1/training-signals", headers=h(S["viewer"])).json() == []
    anns = client.get("/v1/annotations?status=queued", headers=h(S["reviewer"])).json()
    for a in anns:
        label = {"correct": False, "corrected_output": a["input"].get("expected") or "Please share your order number.", "notes": "did not call tool / leaked PII"}
        assert client.post(f"/v1/annotations/{a['id']}/annotate", json={"signal_type": "corrected_output", "label": label}, headers=h(S["reviewer"])).status_code == 200
    # reviewer cannot approve (role), viewer cannot approve
    assert client.post(f"/v1/annotations/{anns[0]['id']}/approve", headers=h(S["reviewer"])).status_code == 403
    assert client.post(f"/v1/annotations/{anns[0]['id']}/approve", headers=h(S["viewer"])).status_code == 403
    for a in anns:
        r = client.post(f"/v1/annotations/{a['id']}/approve", headers=h(S["data_approver"]))
        assert r.status_code == 200, r.text
    sigs = client.get("/v1/training-signals", headers=h(S["viewer"])).json()
    assert len(sigs) == len(anns) and all(s["source_trace_id"] for s in sigs)
    r = client.post("/v1/training-datasets", json={"name": "support-sft"}, headers=h(S["data_approver"]))
    assert r.status_code == 200, r.text
    S["train_dv"] = r.json()["version_id"]; assert r.json()["version"] == 1 and r.json()["records"] == len(sigs)
    dv = client.get(f"/v1/datasets/versions/{S['train_dv']}", headers=h(S["viewer"])).json()
    assert all(it["source_trace_ids"] for it in dv["items"])  # per-record lineage


def test_6_training_job_produces_tracked_candidate():
    r = client.post("/v1/training-jobs", json={"training_dataset_version_id": S["train_dv"], "base_agent_config_id": S["base_cfg"],
                                               "adapter": "local_stub", "method": "sft"}, headers=h(S["engineer"]))
    assert r.status_code == 200, r.text
    j = r.json(); assert j["status"] == "succeeded" and j["candidate_id"] and j["checkpoint_id"]
    S["cand"] = j["candidate_id"]; S["cand_cfg"] = j["candidate_agent_config_id"]
    cands = client.get("/v1/candidates", headers=h(S["viewer"])).json()
    assert cands[0]["id"] == S["cand"] and cands[0]["status"] == "registered"


def test_7_gate_blocks_then_promotes_by_versioned_policy():
    # Candidate cannot be promoted before it is gated.
    prod = S["envs"]["prod"]
    assert client.post(f"/v1/candidates/{S['cand']}/promote", json={"environment_id": prod}, headers=h(S["release_approver"])).status_code == 409
    # Policy v1 is impossible to satisfy (judge must be perfect) -> blocked.
    r = client.post("/v1/promotion-policies", json={"rules": {"required_metrics": [{"name": "llm_judge", "min": 1.01}], "critical_checks": ["no_pii_leak"]}}, headers=h(S["release_approver"]))
    assert r.json()["version"] == 1
    r = client.post("/v1/eval-runs", json={"dataset_version_id": S["dv"], "agent_config_id": S["cand_cfg"], "scorers": S["scorers"]}, headers=h(S["engineer"]))
    S["cand_run"] = r.json()["id"]
    cmp = client.get(f"/v1/eval-runs/{S['cand_run']}/compare/{S['baseline_run']}", headers=h(S["viewer"])).json()
    assert all(m["delta"] >= 0 for m in cmp["metrics"])
    r = client.post(f"/v1/candidates/{S['cand']}/gate", json={"eval_run_id": S["cand_run"], "baseline_eval_run_id": S["baseline_run"]}, headers=h(S["engineer"]))
    assert r.json()["passed"] is False and r.json()["policy_version"] == 1
    assert client.post(f"/v1/candidates/{S['cand']}/promote", json={"environment_id": prod}, headers=h(S["release_approver"])).status_code == 409
    # Policy v2: realistic thresholds + must beat baseline + PII critical check -> passes.
    r = client.post("/v1/promotion-policies", json={"rules": {
        "required_metrics": [{"name": "contains", "min": 0.75, "min_delta_vs_baseline": 0.1}, {"name": "tool_call_assert", "min": 1.0}],
        "critical_checks": ["no_pii_leak"], "required_approver_role": "release_approver"}}, headers=h(S["release_approver"]))
    assert r.json()["version"] == 2
    r = client.post(f"/v1/candidates/{S['cand']}/gate", json={"eval_run_id": S["cand_run"], "baseline_eval_run_id": S["baseline_run"]}, headers=h(S["engineer"]))
    assert r.json()["passed"] is True, r.json()
    # Gate cannot be fooled with a run from a different agent config.
    assert client.post(f"/v1/candidates/{S['cand']}/gate", json={"eval_run_id": S["baseline_run"]}, headers=h(S["engineer"])).status_code == 400
    # Engineer cannot promote; release_approver can.
    assert client.post(f"/v1/candidates/{S['cand']}/promote", json={"environment_id": prod}, headers=h(S["engineer"])).status_code == 403
    r = client.post(f"/v1/candidates/{S['cand']}/promote", json={"environment_id": prod}, headers=h(S["release_approver"]))
    assert r.status_code == 200, r.text
    S["release"] = r.json()["release_id"]
    lin = client.get(f"/v1/releases/{S['release']}/lineage", headers=h(S["viewer"])).json()
    assert lin["checkpoint"]["hash"] and lin["training_dataset_version"]["records"] >= 3 and lin["source_trace_ids"]
    assert len(lin["annotations"]) == len(lin["training_signals"])
    # Rollback is one audited action.
    r = client.post(f"/v1/releases/{S['release']}/rollback", headers=h(S["release_approver"]))
    assert r.status_code == 200
    assert all(not x["active"] for x in client.get("/v1/releases", headers=h(S["viewer"])).json())


def test_8_every_action_is_in_the_audit_log():
    log = client.get("/v1/admin/audit?limit=1000", headers=h(S["admin"])).json()
    actions = {e["action"] for e in log}
    expected = {"org.bootstrap", "project.create", "key.create", "trace.export", "dataset.create", "dataset.snapshot", "agent_config.create",
                "eval_run.start", "eval_run.complete", "annotation.queue", "annotation.submit", "annotation.approve", "training_job.launch",
                "checkpoint.create", "candidate.register", "training_job.finish", "promotion_policy.create", "candidate.gate",
                "candidate.promote_blocked", "release.promote", "release.rollback"}
    missing = expected - actions
    assert not missing, f"missing audit actions: {missing}"
    assert all(e["actor_id"] and e["actor"] and e["role"] for e in log)
    # Project-scoped viewer sees only its project's audit trail; outsider sees none of it.
    ours = client.get("/v1/admin/audit?limit=1000", headers=h(S["viewer"])).json()
    assert ours and all(e["entity_type"] != "organization" for e in ours)
    theirs = client.get("/v1/admin/audit?limit=1000", headers=h(S["outsider"])).json()
    assert not any(e["action"] == "release.promote" for e in theirs)
    # Audit table has no update/delete endpoints.
    assert client.delete("/v1/admin/audit", headers=h(S["admin"])).status_code == 405
