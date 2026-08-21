"""Control-plane and telemetry data model.

Tenancy rule: every customer-owned row carries project_id (and org_id where relevant).
Every query in the routers filters on the caller's project. Nothing crosses it.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # Policy: which payload fields are never stored, and whether raw prompts/completions are kept.
    store_payloads: Mapped[bool] = mapped_column(Boolean, default=True)
    redaction_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    allow_external_training_of_protected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Environment(Base):
    __tablename__ = "environments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(50))  # dev / staging / prod


ROLES = ("org_admin", "project_admin", "engineer", "ingest", "reviewer", "data_approver", "release_approver", "viewer")


class ApiKey(Base):
    """A principal. Keys are scoped to a project (or org-wide for org_admin) and a role."""
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    environment_id: Mapped[str | None] = mapped_column(ForeignKey("environments.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(40))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(12))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    """Append-only. No UPDATE/DELETE path exists in the application for this table."""
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    actor_id: Mapped[str] = mapped_column(String(32))
    actor_name: Mapped[str] = mapped_column(String(200))
    actor_role: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# ---------------- Telemetry plane ----------------

class Span(Base):
    """Raw span as received (post-redaction). Immutable. Aggregates are derived from these rows."""
    __tablename__ = "spans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    environment_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    span_id: Mapped[str] = mapped_column(String(16))
    parent_span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    name: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(30), index=True)  # llm_generation/tool_call/retrieval/planner_step/policy_check/generic
    start_ns: Mapped[int] = mapped_column(BigInteger)
    end_ns: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(10), default="OK")  # OK / ERROR
    session_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    user_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    release: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    provider: Mapped[str | None] = mapped_column(String(60), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    events: Mapped[list] = mapped_column(JSON, default=list)
    redactions_applied: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    __table_args__ = (Index("ix_spans_project_trace", "project_id", "trace_id"),)


class Score(Base):
    __tablename__ = "scores"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    eval_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(100))
    value: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20))  # deterministic / llm_judge / human
    evaluator_version: Mapped[str] = mapped_column(String(60))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------- Evaluation plane ----------------

class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default="eval")  # eval / training
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DatasetVersion(Base):
    """Immutable snapshot. Items are copied in at snapshot time; edits create a new version."""
    __tablename__ = "dataset_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    item_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    items: Mapped[list] = mapped_column(JSON)  # [{id,input,expected,assertions,budgets,protected_fields,split,source_trace_ids}]
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentConfig(Base):
    """What gets evaluated / promoted: a pointer to model + prompt/policy version + how to invoke it."""
    __tablename__ = "agent_configs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(60))
    invoke: Mapped[dict] = mapped_column(JSON)  # {"kind":"http","url":...} | {"kind":"builtin_demo","behavior":"v1"}
    checkpoint_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"))
    agent_config_id: Mapped[str] = mapped_column(ForeignKey("agent_configs.id"))
    scorers: Mapped[list] = mapped_column(JSON)  # [{"type":"exact_match",...}]
    evaluator_version: Mapped[str] = mapped_column(String(60))
    environment_image: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)  # {metric: mean}
    results: Mapped[list] = mapped_column(JSON, default=list)  # per-item results incl. trace_id
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------- Feedback / training-signal plane ----------------

class Annotation(Base):
    __tablename__ = "annotations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    eval_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued / annotated / approved / rejected
    signal_type: Mapped[str | None] = mapped_column(String(40), nullable=True)  # outcome_label/preference_pair/corrected_output/tool_quality/incident
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    label: Mapped[dict] = mapped_column(JSON, default=dict)
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewer_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approver_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrainingSignal(Base):
    """Created ONLY by approving an annotation. Carries source lineage."""
    __tablename__ = "training_signals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    annotation_id: Mapped[str] = mapped_column(ForeignKey("annotations.id"), unique=True)
    source_trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    signal_type: Mapped[str] = mapped_column(String(40))
    record: Mapped[dict] = mapped_column(JSON)  # {input, output/chosen/rejected, label}
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------- Learning / release plane ----------------

class TrainingJob(Base):
    __tablename__ = "training_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    training_dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"))
    base_agent_config_id: Mapped[str] = mapped_column(ForeignKey("agent_configs.id"))
    adapter: Mapped[str] = mapped_column(String(40))  # local_stub / openai_finetune
    method: Mapped[str] = mapped_column(String(20))  # sft / preference
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    provider_job_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    log: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Checkpoint(Base):
    __tablename__ = "checkpoints"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    training_job_id: Mapped[str] = mapped_column(ForeignKey("training_jobs.id"))
    artifact_ref: Mapped[str] = mapped_column(String(300))
    artifact_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Candidate(Base):
    """A checkpoint + agent configuration awaiting promotion. Never serves production directly."""
    __tablename__ = "candidates"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    agent_config_id: Mapped[str] = mapped_column(ForeignKey("agent_configs.id"))
    checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey("checkpoints.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="registered")  # registered/gated_pass/gated_fail/promoted/rejected
    gate_result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PromotionPolicy(Base):
    __tablename__ = "promotion_policies"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    # {"required_metrics":[{"name":"exact_match","min":0.8,"min_delta_vs_baseline":0.0}],
    #  "critical_checks":["no_pii_leak"], "required_approver_role":"release_approver"}
    rules: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Release(Base):
    __tablename__ = "releases"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    environment_id: Mapped[str] = mapped_column(String(32), index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"))
    policy_id: Mapped[str] = mapped_column(ForeignKey("promotion_policies.id"))
    eval_run_id: Mapped[str] = mapped_column(String(32))
    baseline_eval_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_by: Mapped[str] = mapped_column(String(32))
    rollback_target_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
