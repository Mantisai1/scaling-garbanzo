# Mantis — agent learning & reliability platform

One loop: **observe → evaluate → label → improve → validate → deploy → observe again**, with tenant isolation,
role-scoped keys, server-side redaction, immutable dataset versions, a versioned promotion policy, and an
append-only audit log that can walk any release back to the production traces it was learned from.

```
Customer agent ──(Mantis SDK / any OpenTelemetry SDK, OTLP)──▶  /v1/traces  ──▶ auth → tenant → redact → store
                                                                                       │
        Console ◀── /v1/traces, /v1/eval-runs, /v1/annotations, /v1/candidates, /v1/releases, /v1/admin/audit
```

## Run it (15 minutes)

```bash
cp .env.example .env            # optionally add OPENAI_API_KEY for a real LLM judge + fine-tune adapter
make up                         # Postgres + platform on http://localhost:8080
make demo                       # walks the whole loop, prints console keys
open http://localhost:8080/     # paste the viewer (or admin) key
```

`make test` runs the eight launch-gate tests without Docker (SQLite).

## Instrument your own agent

```bash
pip install -e sdk/python[openai]      # or [anthropic]
```
```python
import mantis_sdk
mantis_sdk.init(endpoint="http://localhost:8080", api_key="mk_ing_…", service_name="my-agent", release="1.2.0",
                redact_fields=["customer.email"])          # OpenAI/Anthropic clients are auto-instrumented

@mantis_sdk.tool
def search(q: str): ...

with mantis_sdk.trace("answer", session_id=sid, user_ref=uid):
    search("…"); client.chat.completions.create(...)
```
Already on OpenTelemetry? Point your exporter at `POST /v1/traces` with header `X-API-Key`. Standard GenAI
semantic-convention attributes are understood; anything Mantis-specific is under `mantis.*`.

## Repository layout

| Path | What |
|---|---|
| `platform/mantis_platform/` | FastAPI control plane + telemetry plane (`routers/`: admin, ingest, traces, evals, feedback, learning; `services/`: scorers, agent runner, training adapters) |
| `sdk/python/mantis_sdk/` | OpenTelemetry-native SDK, client-side redaction processor, OpenAI/Anthropic auto-instrumentation |
| `console/` | Single-file web console (no build step) |
| `demo/` | `demo_agent.py` (instrumented agent) and `run_demo.py` (drives the full loop) |
| `tests/test_launch_gates.py` | The eight non-negotiable gates as one end-to-end test |
| `docs/` | Business rules, API walkthrough, what is real vs. stubbed, and the path to the full blueprint |

## Roles

`org_admin` › `project_admin` › `engineer` (datasets, eval runs, training jobs, gating) · `reviewer` (annotate) ·
`data_approver` (turn annotations into training signals — cannot approve own work) · `release_approver`
(promote / rollback) · `viewer` · `ingest` (write-only). Approver roles are separation-of-duty: only an exact
match or an admin satisfies them.

## Honest status

See `docs/STATUS.md`. Short version: the contracts, tenancy, redaction, lineage, gating, and audit are real and
tested; the hot-path store is Postgres (not ClickHouse) and the default training adapter is a stub that produces a
tracked checkpoint without training weights. Both are swap points, not rewrites.
