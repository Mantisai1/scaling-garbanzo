# Metis-Class Platform — Business Logic & Step-by-Step Implementation

This translates the Day-One Blueprint into concrete business rules, data entities, and an ordered build plan. Each step has an entry condition, what gets built, the business rules that must hold, and an exit test.

---

## Part 1 — The core loop (the product in one sentence)

> A customer connects an agent → the platform records what it does → the customer measures it against real tasks → humans label failures → approved labels become training data → a new candidate is trained → the candidate is gated against the baseline → a human approves → it ships with a rollback path → recording continues.

Every entity and rule below exists to make one hop in that loop enforceable and auditable.

---

## Part 2 — Core entities (the nouns)

| Entity | What it is | Key rule |
|---|---|---|
| Organization | The paying customer | Hard tenant boundary. No query crosses it. |
| Project | A product/agent inside an org | Every API key, trace, dataset, and job belongs to exactly one project. |
| Environment | dev / staging / prod inside a project | Data never silently flows across environments. |
| Trace / Span | One agent run and its nested steps | Stored in the telemetry plane. Immutable after ingestion. |
| Session / User ref | Groups traces by conversation or end-user | Optional, customer-supplied, redaction-eligible. |
| Score | A number or label attached to a trace or span | Has a source: deterministic, LLM judge, or human. Versioned evaluator. |
| Dataset | A versioned snapshot of task records | Immutable once snapshotted. New edits = new version. |
| Task | One test case in a dataset | Has input, expected outcome, assertions, budgets. |
| Evaluation Run | Executing an agent config against a dataset version | Records model, prompt/policy version, evaluator version, dataset version, environment image. |
| Annotation | A human judgment on a trace or task result | Has reviewer identity, timestamp, and approval state. |
| Training Signal | An approved, exportable learning example derived from annotations/scores | Must link back to source trace(s). |
| Training Job | A fine-tune / preference / RL job | Launched through a provider adapter. Produces checkpoints. |
| Candidate | A trained checkpoint + agent configuration awaiting promotion | Cannot serve production traffic directly. |
| Release | A promoted candidate in the release registry | Has approval record, evaluation report, and rollback target. |
| Audit Event | Who did what, when, to which entity | Append-only. Every mutating action writes one. |

**The lineage invariant:** From any Release you must be able to walk backwards to: the Candidate → Training Job → Training Signals → Annotations → Traces. If any link is missing, the release cannot be promoted.

---

## Part 3 — Business rules by plane

### Plane A — Instrumentation & ingestion
- A1. SDKs emit standard OpenTelemetry spans over OTLP (HTTP/protobuf and gRPC). No proprietary wire protocol.
- A2. Shared attribute schema = pinned version of OpenTelemetry GenAI semantic conventions. Proprietary fields live only under a documented namespace (e.g. `yourco.*`).
- A3. Customers can dual-write to their own collector. The platform is never the only destination.
- A4. Redaction can run client-side (SDK config) and must run server-side (gateway) regardless.
- A5. Every inbound request is authenticated (API key or OIDC), resolved to org/project/environment, quota-checked, and sampled per project policy — before any storage.
- A6. Payloads (prompts, completions) are stored only if project policy allows, encrypted, with a retention class.

### Plane B — Observability
- B1. Span kinds are first-class: `llm_generation`, `tool_call`, `retrieval`, `planner_step`, `policy_check`, `generic`.
- B2. Raw timings are stored as received; all aggregates (cost, latency, tokens) are derived and reproducible from raw.
- B3. Searchable/filterable by: user, session, environment, release, tag, model, provider, score, error.
- B4. Trace export and deletion are project-scoped API operations and write audit events.
- B5. Alert rules are defined per project on aggregates, not raw spans.

### Plane C — Evaluation
- C1. A dataset version is immutable. Import from JSON/JSONL/CSV/API; schema-validated on import.
- C2. Every evaluation run pins: dataset version, agent config/prompt version, model, evaluator version, environment image hash.
- C3. Three scorer types: deterministic (assertions, tool-call checks, latency/cost budgets), LLM judge (approved provider + pinned prompt), human (annotation queue).
- C4. A comparison report always compares candidate vs. an explicit baseline run on the same dataset version.
- C5. Judge inputs/outputs are subject to the same redaction and retention policy as traces.

### Plane D — Feedback & training signals
- D1. Annotations are queued per project; reviewers are RBAC-assigned.
- D2. An annotation becomes a training signal only after approval by a user with the `data_approver` role.
- D3. Signal types: outcome label, preference pair, corrected output, tool-quality label, incident-derived example.
- D4. A training dataset is a versioned snapshot of approved signals. Source trace IDs are stored with each record.
- D5. Signals containing fields flagged `protected` cannot be exported to an external training provider unless the project policy explicitly allows it.

### Plane E — Learning & release control
- E1. Training jobs run only through provider adapters (managed fine-tune API, customer GPU cluster, open-model runtime). The platform owns lineage, not the trainer.
- E2. A job input is always a training dataset version; output is one or more checkpoints with immutable artifact hashes.
- E3. A candidate = checkpoint + agent configuration. Candidates are registered, never deployed directly.
- E4. Promotion policy is a versioned document per project: required metrics, thresholds, mandatory safety checks, required approver roles.
- E5. Gate evaluation: candidate passes only if all required metrics meet thresholds AND all critical checks pass against the baseline on the same dataset version.
- E6. Online learning: candidates can receive shadow or fractional traffic only under an explicit traffic policy; production behavior never changes without passing the gate and human approval.
- E7. Every release has a rollback target (the previous release). Rollback is a single action, audited.

### Plane F — Enterprise control
- F1. SSO via OIDC/SAML. RBAC roles at minimum: `org_admin`, `project_admin`, `engineer`, `reviewer`, `data_approver`, `release_approver`, `viewer`.
- F2. API keys are scoped (project + environment + permission set) and rotatable; old keys have a grace window.
- F3. Audit log is append-only and includes user/service identity, action, entity, before/after reference, timestamp.
- F4. Retention is configurable per data class (traces, payloads, datasets, judge outputs, audit). Audit retention cannot be shorter than the others.
- F5. Export and delete APIs exist for every customer-owned data class.
- F6. Self-hosted/single-tenant uses the same control-plane API contract as SaaS. No fork.

---

## Part 4 — Step-by-step build order

This follows the document's integration sequence. Each step ends with a test that must pass before the next begins.

### Step 0 — Legal and clean-room setup (week 0, before any code)
- Create the public-source register (every external doc/spec consulted, with URL and date).
- Write the independent requirements document from this blueprint — not from any competitor artifact.
- Set up the license ledger and SBOM tooling in CI.
- Write the repository admission rule: no competitor code, recovered artifacts, credentials, or confidential material. Enforce via code-review checklist.
- Get IP/open-source counsel to sign off on the process.
- **Exit:** Counsel-approved clean-room process document; CI blocks merges without license check.

### Step 1 — Platform contract (weeks 1–4)
- Define and ratify: org/project/environment model, identity model, API key format and scoping, audit event schema, OTLP envelope + attribute namespace, schema versioning policy, storage-lineage rules.
- Write the two technical design docs the blueprint calls out: (a) canonical event/lineage schema, (b) candidate-promotion policy format.
- Stand up PostgreSQL control DB with migrations, row-level tenant controls, and audit table.
- **Exit:** Schema docs reviewed and frozen at v1. A test can create an org → project → environment → API key and every step appears in the audit log.

### Step 2 — Data foundation (weeks 3–12, overlaps Step 1)
- Build the OTLP gateway: OpenTelemetry Collector distribution + your own tenant/policy layer (auth, routing, quota, sampling, redaction, retention classification).
- Stand up ClickHouse for spans/events; object storage for encrypted payloads.
- Build the Python SDK (OpenAI, Anthropic, LangChain, LlamaIndex, LiteLLM, DSPy, generic HTTP) and TypeScript SDK (OpenAI, Anthropic, Vercel AI SDK, LangChain.js, fetch).
- Build trace reconstruction and the project-scoped trace/run API.
- Build the console: trace search, span tree, timeline, filters, cost/latency/error breakdowns, saved queries, export, delete.
- **Exit (gates 1–3):** Instrumented agent sends a trace over OTLP → sensitive fields are redacted per policy before storage → authorized project member locates and exports it. Unauthorized member cannot.

### Step 3 — Evaluation foundation (weeks 8–20)
- Build the dataset/task registry with versioned snapshots, schema validation, split metadata, protected-field flags.
- Build the evaluation runtime: sandboxed runners with pinned dependencies; deterministic scorers; LLM-judge executor with approved providers; artifact capture.
- Build human annotation queues and reviewer assignment.
- Build comparison reports linked back to traces.
- **Exit (gate 4):** Customer imports a task set, runs an agent in a reproducible environment, gets deterministic + judge scores, sees a report that links each score to its trace.

### Step 4 — Learning foundation (weeks 16–30)
- Build the training-signal approval workflow (annotation → approval → signal).
- Build training dataset versioning with source-trace lineage per record.
- Build the provider adapter interface and at least two adapters (one managed fine-tune API, one Kubernetes job runner for customer-hosted/open models).
- Build checkpoint and artifact registry with immutable hashes.
- **Exit (gates 5–6):** Approved annotations produce a versioned training dataset → an adapter launches a job → a tracked candidate with checkpoint lineage appears in the registry.

### Step 5 — Release control (weeks 26–36)
- Build the candidate registry and promotion-policy engine (versioned policies, metric thresholds, mandatory checks, approver roles).
- Build the release registry, approval workflow, and one-click rollback.
- Build shadow/fractional traffic policy for controlled online candidates.
- **Exit (gate 7):** Candidate runs the evaluation suite → is blocked when it fails policy → is promoted only when it passes and an approver signs → rollback restores the previous release. All with complete lineage.

### Step 6 — Enterprise hardening (weeks 30–40, overlaps)
- SSO (OIDC/SAML), full RBAC, scoped key rotation, webhook signing, region/deployment config.
- Retention enforcement, export/delete APIs for all data classes.
- Self-hosted deployment mode against the same control-plane contract.
- Threat model, pen test, operational monitoring, incident runbooks.
- **Exit (gate 8):** Every action in the system is attributable to a user or service identity in the audit log. Security and availability gates signed off.

### Step 7 — End-to-end launch verification
Run the eight non-negotiable gates as one continuous automated test on a fresh tenant:
1. Instrumented agent sends a trace via OTLP.
2. Sensitive fields redacted before storage.
3. Authorized member finds and exports the trace; unauthorized cannot.
4. Trace produces an evaluation record.
5. Approved annotations generate a versioned training dataset.
6. Training job produces a tracked candidate.
7. Candidate is blocked/promoted only by versioned policy.
8. Every action is in the audit log with an identity.

**Nothing is marketed as complete until this test passes green.**

---

## Part 5 — Team allocation to steps

| Role group | Headcount | Primary steps |
|---|---|---|
| Product/technical leadership | 2 | 0, 1, 7 + design partners throughout |
| SDK & integrations | 2–3 | 2 |
| Data/ingestion/platform | 3 | 1, 2, 6 |
| Application/frontend | 2 | 2, 3, 5 |
| ML/evaluation/learning | 3–4 | 3, 4, 5 |
| Security/reliability | 1–2 | 0, 1, 6, 7 |

---

## Part 6 — Decisions you still have to make

1. **Public launch sequencing.** The build order above has planes A/B/C/F working months before D/E. Decide now whether design partners get access at Step 3 (recommended) or only at Step 7 (the blueprint's stated intent).
2. **First two provider adapters.** Which managed fine-tune API and which open-model runtime? This determines the first customers you can serve.
3. **Hosting posture at launch.** SaaS-first, self-hosted-first, or both on day one? Both doubles the ops surface for Step 6.
4. **Promotion policy defaults.** What ships as the default gate? Too loose and the governance story is hollow; too strict and nobody ever promotes.
5. **Naming and visual system.** Must be original and in the clean-room inventory before the console is built (Step 2).
