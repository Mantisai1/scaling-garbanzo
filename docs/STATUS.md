# What is real, what is a stand-in, and what comes next

## Real and tested (tests/test_launch_gates.py)
- Tenant model: org → project → environment; every row carries project_id; every query filters on the caller's key scope.
- API keys: hashed at rest, role-scoped, project-scoped, revocable. Write-only ingest keys.
- OTLP/HTTP intake in protobuf (what OpenTelemetry SDKs send) and JSON. Normalization to pinned GenAI semconv 1.37.
- Redaction: client-side SpanProcessor in the SDK **and** server-side on every span (patterns + per-project field deny-list + payload-storage policy). Redaction counts are stored per span.
- Trace reconstruction, filters, cost/latency/error breakdowns derived from raw spans, audited export, admin-only delete.
- Datasets: immutable versioned snapshots with content hash; JSON/JSONL/CSV import; per-record `source_trace_ids`.
- Evaluation runs pin dataset hash, agent config (model + prompt version + checkpoint), evaluator version, environment image. Scorers: exact/contains/json/tool-call assertions/latency & cost budgets/PII leak/LLM judge. Candidate-vs-baseline comparison refuses mismatched dataset versions.
- Annotation queue → reviewer annotates → **data_approver** approves (not self) → training signal → versioned training dataset with lineage per record.
- Training job → checkpoint (hash) → candidate. Candidates cannot serve traffic; they are gated by a **versioned** policy (metric minimums, delta vs baseline, critical checks, required approver role) and promoted only by a release_approver. One-action rollback. `GET /v1/releases/{id}/lineage` walks back to source traces.
- Append-only audit log with actor identity on every mutating action; project-scoped visibility.

## Stand-ins (deliberate, each is an interface swap)
| Area | Now | Blueprint target | Swap |
|---|---|---|---|
| Telemetry store | Postgres `spans` table | ClickHouse + object storage | `routers/ingest.py` write + `routers/traces.py` reads; add a `TelemetryStore` interface |
| LLM judge without a key | heuristic overlap, labelled `llm_judge:heuristic@1` | approved provider judges | set `OPENAI_API_KEY`; Anthropic judge is a 20-line addition in `services/scorers.py` |
| Training | `local_stub` adapter emits a checkpoint hash; `openai_finetune` submits a real SFT job | managed + customer-GPU + open-model adapters; preference/RL | implement `services/adapters.py` contract per provider |
| Eval execution | synchronous in the API process | sandboxed runners, queue, worker pools | move `routers/evals.run_eval` body behind a job queue |
| Agent under test | `http` endpoint or `builtin_demo` | same, plus container task environments | extend `services/agent_runner.py` |
| Identity | API keys with roles | OIDC/SAML SSO, users, groups | add identity provider → issue short-lived keys/JWTs mapping to the same `Principal` |
| Online learning | not implemented | shadow / fractional traffic to candidates | router in front of the agent, reading the release registry |
| Retention / delete | config + per-trace delete | scheduled enforcement per data class, bulk export/delete | a worker over `received_at` |
| Self-hosted | docker compose | Helm chart, same control-plane contract | packaging only |

## Build order from here (matches the blueprint's tranches)
1. Data foundation: ClickHouse store, async ingestion queue, retention worker, TypeScript SDK.
2. Evaluation foundation: job queue + sandboxed runners, Anthropic judge, human-scored eval metrics in gate.
3. Learning foundation: second real adapter (customer K8s job), preference-pair export, protected-field enforcement at export.
4. Release control: shadow-traffic router, release webhooks (signed), environment-specific policies.
5. Enterprise hardening: SSO, user identities, scoped key rotation with grace window, bulk export/delete, Helm.
