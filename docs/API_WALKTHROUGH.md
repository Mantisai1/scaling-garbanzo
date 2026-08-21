# API walkthrough (the same sequence `demo/run_demo.py` runs)

All calls: header `X-API-Key: mk_…`. Org-scoped (admin) keys pass `project_id` in the query or body; project-scoped keys never need it and cannot escape their project.

| Step | Role | Call |
|---|---|---|
| Bootstrap org (once) | bootstrap token | `POST /v1/admin/bootstrap` `{org_name}` + header `X-Bootstrap-Token` |
| Create project | org_admin | `POST /v1/admin/projects` `{name, redaction_rules:{deny_fields:[…]}, store_payloads}` |
| Create keys | admin | `POST /v1/admin/keys` `{name, role, project_id}` |
| Send traces | ingest | `POST /v1/traces` (OTLP protobuf or JSON) |
| Browse | viewer | `GET /v1/traces?session_id=&user_ref=&model=&status=&release=` · `GET /v1/traces/{id}` · `GET /v1/analytics/breakdown?by=model` |
| Export (audited) | engineer | `GET /v1/traces/{id}/export` |
| Dataset | engineer | `POST /v1/datasets` `{name, items:[{id,input,expected,assertions,budgets}]}` · `POST /v1/datasets/{id}/import` (file) |
| Agent config | engineer | `POST /v1/agent-configs` `{name, version, model, prompt_version, invoke:{kind:"http",url}|{kind:"builtin_demo"}}` |
| Evaluate | engineer | `POST /v1/eval-runs` `{dataset_version_id, agent_config_id, scorers:[{type:"contains"},{type:"tool_call_assert"},{type:"no_pii_leak"},{type:"llm_judge",rubric}]}` |
| Compare | viewer | `GET /v1/eval-runs/{cand}/compare/{baseline}` |
| Queue review | engineer | `POST /v1/annotations/queue` `{eval_run_id, only_failures_below:1.0}` or `{trace_ids:[…]}` |
| Annotate | reviewer | `POST /v1/annotations/{id}/annotate` `{signal_type, label}` |
| Approve → signal | data_approver | `POST /v1/annotations/{id}/approve` |
| Training dataset | data_approver | `POST /v1/training-datasets` `{name}` → immutable version with lineage |
| Train → candidate | engineer | `POST /v1/training-jobs` `{training_dataset_version_id, base_agent_config_id, adapter, method}` |
| Policy (versioned) | release_approver | `POST /v1/promotion-policies` `{rules:{required_metrics:[{name,min,min_delta_vs_baseline}], critical_checks:[…], required_approver_role}}` |
| Gate | engineer | `POST /v1/candidates/{id}/gate` `{eval_run_id, baseline_eval_run_id}` |
| Promote | release_approver | `POST /v1/candidates/{id}/promote` `{environment_id}` (409 unless gate passed) |
| Rollback | release_approver | `POST /v1/releases/{id}/rollback` |
| Lineage | viewer | `GET /v1/releases/{id}/lineage` |
| Audit | viewer (own project) / admin | `GET /v1/admin/audit?entity_id=` |

Interactive docs: `http://localhost:8080/docs`.
