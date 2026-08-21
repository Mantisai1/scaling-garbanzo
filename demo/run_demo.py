"""Walks the whole loop against a running platform and prints the keys you need for the console.

    python demo/run_demo.py            # platform at http://localhost:8080

Steps: bootstrap -> project + keys -> instrumented agent sends traces -> golden dataset -> baseline eval ->
queue failures -> annotate -> approve -> training dataset -> training job -> candidate eval -> gate (blocked, then passed)
-> promote to prod -> lineage.
"""
import json
import os
import subprocess
import sys

import httpx

BASE = os.getenv("MANTIS_ENDPOINT", "http://localhost:8080")
TOKEN = os.getenv("MANTIS_BOOTSTRAP_TOKEN", "change-me-bootstrap")
STATE = os.path.join(os.path.dirname(__file__), ".demo_state.json")


def call(method, path, key=None, **kw):
    r = httpx.request(method, BASE + path, headers={"X-API-Key": key} if key else kw.pop("headers", {}), timeout=120, **kw)
    if r.status_code >= 400:
        raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text}")
    return r.json()


def step(n, msg): print(f"\n\033[1m[{n}] {msg}\033[0m")


def main():
    if os.path.exists(STATE):
        st = json.load(open(STATE)); print("re-using existing tenant from", STATE)
    else:
        step(0, "Bootstrap organization and create project + role-scoped keys")
        boot = call("POST", "/v1/admin/bootstrap", json={"org_name": "Demo Customer"}, headers={"X-Bootstrap-Token": TOKEN})
        admin = boot["api_key"]
        proj = call("POST", "/v1/admin/projects", admin, json={"name": "support-agent", "redaction_rules": {"deny_fields": ["customer.email"]}})
        st = {"admin": admin, "project": proj["id"], "envs": {e["name"]: e["id"] for e in proj["environments"]}}
        for role in ("ingest", "engineer", "reviewer", "data_approver", "release_approver", "viewer"):
            st[role] = call("POST", "/v1/admin/keys", admin, json={"name": f"demo-{role}", "role": role, "project_id": proj["id"]})["api_key"]
        json.dump(st, open(STATE, "w"), indent=1)
    k = st

    step(1, "Instrumented agent sends production traces over OTLP")
    env = {**os.environ, "MANTIS_API_KEY": k["ingest"], "MANTIS_ENDPOINT": BASE}
    subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "demo_agent.py"), "12"], env=env, check=True)
    traces = call("GET", "/v1/traces?limit=5", k["viewer"])
    t = call("GET", f"/v1/traces/{traces[0]['trace_id']}", k["viewer"])
    red = sum(s["redactions_applied"] for s in t["spans"])
    print(f"    {len(call('GET', '/v1/traces?limit=100', k['viewer']))} traces stored; latest has {len(t['spans'])} spans, {red} redactions applied before storage")

    step(2, "Golden task set -> baseline evaluation of agent v1.0.0")
    items = [{"id": "t1", "input": {"question": "Where is order A100?"}, "expected": "Order A100 is shipped, arriving Friday.", "assertions": {"must_call": ["lookup_order"]}},
             {"id": "t2", "input": {"question": "Status of A200 please"}, "expected": "Order A200 is processing.", "assertions": {"must_call": ["lookup_order"]}},
             {"id": "t3", "input": {"question": "Did A300 arrive?"}, "expected": "Order A300 is delivered Tuesday.", "assertions": {"must_call": ["lookup_order"]}},
             {"id": "t4", "input": {"question": "Hi, I have a question about my order"}, "expected": "could you share your order number"}]
    ds = call("POST", "/v1/datasets", k["engineer"], json={"name": "support-golden", "items": items})
    scorers = [{"type": "contains"}, {"type": "tool_call_assert"}, {"type": "no_pii_leak"}, {"type": "latency_budget_ms", "max_ms": 800},
               {"type": "llm_judge", "rubric": "Does the answer correctly state the order status without exposing personal data?"}]
    base_cfg = call("POST", "/v1/agent-configs", k["engineer"], json={"name": "support-agent", "version": "1.0.0", "model": "demo-model-v1", "prompt_version": "p1", "invoke": {"kind": "builtin_demo", "behavior": "v1"}})["id"]
    base_run = call("POST", "/v1/eval-runs", k["engineer"], json={"dataset_version_id": ds["version_id"], "agent_config_id": base_cfg, "scorers": scorers})
    print("    baseline:", base_run["summary"])

    step(3, "Queue failures for human review; reviewer annotates; data approver approves -> training signals")
    q = call("POST", "/v1/annotations/queue", k["engineer"], json={"eval_run_id": base_run["id"], "only_failures_below": 1.0})
    for a in call("GET", "/v1/annotations?status=queued", k["reviewer"]):
        call("POST", f"/v1/annotations/{a['id']}/annotate", k["reviewer"], json={"signal_type": "corrected_output", "label": {"correct": False, "corrected_output": a["input"].get("expected"), "notes": "must call lookup_order; never echo PII"}})
    for a in call("GET", "/v1/annotations?status=annotated", k["data_approver"]):
        call("POST", f"/v1/annotations/{a['id']}/approve", k["data_approver"])
    tds = call("POST", "/v1/training-datasets", k["data_approver"], json={"name": "support-sft"})
    print(f"    {len(q['queued'])} queued -> approved -> training dataset v{tds['version']} ({tds['records']} records, hash {tds['hash'][:12]})")

    step(4, "Training job -> checkpoint -> registered candidate")
    job = call("POST", "/v1/training-jobs", k["engineer"], json={"training_dataset_version_id": tds["version_id"], "base_agent_config_id": base_cfg, "adapter": "local_stub", "method": "sft"})
    print("    ", job["status"], "| candidate", job["candidate_id"][:8], "| checkpoint", job["checkpoint_id"][:8])

    step(5, "Evaluate candidate on the SAME dataset version; gate against policy")
    cand_run = call("POST", "/v1/eval-runs", k["engineer"], json={"dataset_version_id": ds["version_id"], "agent_config_id": job["candidate_agent_config_id"], "scorers": scorers})
    print("    candidate:", cand_run["summary"])
    call("POST", "/v1/promotion-policies", k["release_approver"], json={"rules": {"required_metrics": [{"name": "llm_judge", "min": 1.01}], "critical_checks": ["no_pii_leak"]}})
    g1 = call("POST", f"/v1/candidates/{job['candidate_id']}/gate", k["engineer"], json={"eval_run_id": cand_run["id"], "baseline_eval_run_id": base_run["id"]})
    print("    policy v1 (unreachable bar):", "PASS" if g1["passed"] else "BLOCKED")
    r = httpx.post(f"{BASE}/v1/candidates/{job['candidate_id']}/promote", headers={"X-API-Key": k["release_approver"]}, json={"environment_id": k["envs"]["prod"]})
    print("    promote attempt while blocked ->", r.status_code, r.json()["detail"])
    call("POST", "/v1/promotion-policies", k["release_approver"], json={"rules": {"required_metrics": [{"name": "contains", "min": 0.75, "min_delta_vs_baseline": 0.1}, {"name": "tool_call_assert", "min": 1.0}], "critical_checks": ["no_pii_leak"], "required_approver_role": "release_approver"}})
    g2 = call("POST", f"/v1/candidates/{job['candidate_id']}/gate", k["engineer"], json={"eval_run_id": cand_run["id"], "baseline_eval_run_id": base_run["id"]})
    print("    policy v2:", "PASS" if g2["passed"] else "BLOCKED")

    step(6, "Release approver promotes to prod; lineage walks back to source traces")
    rel = call("POST", f"/v1/candidates/{job['candidate_id']}/promote", k["release_approver"], json={"environment_id": k["envs"]["prod"]})
    lin = call("GET", f"/v1/releases/{rel['release_id']}/lineage", k["viewer"])
    print(f"    release {rel['release_id'][:8]} -> candidate -> checkpoint {lin['checkpoint']['hash'][:10]} -> job -> dataset v{lin['training_dataset_version']['version']} -> {len(lin['training_signals'])} signals -> {len(lin['annotations'])} annotations -> {len(lin['source_trace_ids'])} source traces")

    step(7, "Audit trail")
    log = call("GET", "/v1/admin/audit?limit=1000", k["admin"])
    print(f"    {len(log)} audit events, {len({e['actor_id'] for e in log})} distinct identities, actions: {sorted({e['action'] for e in log})}")

    print(f"\n\033[1mOpen {BASE}/  and paste this viewer key:\033[0m  {k['viewer']}")
    print(f"Admin key (all actions):  {k['admin']}\nAll keys saved in {STATE}")


if __name__ == "__main__":
    main()
