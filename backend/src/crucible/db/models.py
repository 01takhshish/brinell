"""Schema.

Shape of the domain: a **project** owns **targets** (a prompt or agent) whose immutable
**target versions** are content-hashed; **datasets** own immutable **dataset versions** of
**cases**; a **suite** binds a dataset to a set of assertions; a **run** executes one target
version against one suite and fans out into **run_cases**, each of which records **spans**
(every LLM/tool/judge call, including retries).

Two rules the rest of the codebase depends on:

* Everything tenant-visible carries `project_id`, and every query filters on it.
* `run_cases` is the work queue. Workers claim rows with `FOR UPDATE SKIP LOCKED`, so claiming
  a case and writing its result happen in one transaction (docs/adr/0001).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from crucible.config import settings
from crucible.db.base import Base, created_at_col, uuid_pk

# ─── Identity & tenancy ──────────────────────────────────────────────────────

ROLES = ("owner", "admin", "member")


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id"),
        CheckConstraint("role IN ('owner','admin','member')", name="role_valid"),
    )

    id: Mapped[UUID] = uuid_pk()
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member", server_default=text("'member'"))
    created_at: Mapped[datetime] = created_at_col()


class ApiKey(Base):
    """CI credentials. Only the SHA-256 of the key is stored; `prefix` is shown in the UI so a
    user can tell two keys apart without us ever holding the secret."""

    __tablename__ = "api_keys"

    id: Mapped[UUID] = uuid_pk()
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


# ─── Registry: targets and versions ──────────────────────────────────────────


class Target(Base):
    __tablename__ = "targets"
    __table_args__ = (
        UniqueConstraint("project_id", "name"),
        CheckConstraint("kind IN ('prompt','agent')", name="kind_valid"),
    )

    id: Mapped[UUID] = uuid_pk()
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="prompt", server_default=text("'prompt'"))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()

    versions: Mapped[list["TargetVersion"]] = relationship(back_populates="target", cascade="all, delete-orphan")


class TargetVersion(Base):
    """Immutable. `content_hash` is the sha256 of the canonicalised config, so two versions with
    identical behaviour collapse to one row and every run is pinned to exactly what executed."""

    __tablename__ = "target_versions"
    __table_args__ = (
        UniqueConstraint("target_id", "semver"),
        UniqueConstraint("target_id", "content_hash"),
        Index("ix_target_versions_target_created", "target_id", "created_at"),
    )

    id: Mapped[UUID] = uuid_pk()
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), index=True)
    semver: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    git_sha: Mapped[str | None] = mapped_column(String(40))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at_col()

    target: Mapped[Target] = relationship(back_populates="versions")


# ─── Datasets ────────────────────────────────────────────────────────────────


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[UUID] = uuid_pk()
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version"),
        UniqueConstraint("dataset_id", "content_hash"),
    )

    id: Mapped[UUID] = uuid_pk()
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = created_at_col()


class Case(Base):
    """A golden test case. `external_id` is the stable identity *across* dataset versions — it is
    what lets a comparison say "case refund-07 broke" rather than "some case broke"."""

    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("dataset_version_id", "external_id"),
        Index("ix_cases_tags", "tags", postgresql_using="gin"),
    )

    id: Mapped[UUID] = uuid_pk()
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    dataset_version_id: Mapped[UUID] = mapped_column(ForeignKey("dataset_versions.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0, server_default=text("1.0"))


class Suite(Base):
    """Dataset + assertion spec + thresholds. Suites are mutable (they are configuration, not
    evidence); a run snapshots the assertions it executed so history stays truthful."""

    __tablename__ = "suites"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[UUID] = uuid_pk()
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey("datasets.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    assertions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    judge_model: Mapped[str | None] = mapped_column(String(80))
    pass_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=1.0, server_default=text("1.0"))
    created_at: Mapped[datetime] = created_at_col()


# ─── Runs ────────────────────────────────────────────────────────────────────

RUN_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
CASE_STATUSES = ("pending", "claimed", "passed", "failed", "error", "skipped")


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled')", name="status_valid"),
        Index("ix_runs_project_created", "project_id", "created_at"),
        Index("ix_runs_suite_target", "suite_id", "target_version_id", "created_at"),
        # CI retries the same POST /runs after a network blip. The key makes that return the
        # original run instead of paying for the suite twice.
        Index(
            "uq_runs_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    target_version_id: Mapped[UUID] = mapped_column(ForeignKey("target_versions.id", ondelete="RESTRICT"), index=True)
    suite_id: Mapped[UUID] = mapped_column(ForeignKey("suites.id", ondelete="RESTRICT"), index=True)
    dataset_version_id: Mapped[UUID] = mapped_column(ForeignKey("dataset_versions.id", ondelete="RESTRICT"))
    baseline_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", server_default=text("'queued'"))
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="api", server_default=text("'api'"))
    git_sha: Mapped[str | None] = mapped_column(String(40))
    git_branch: Mapped[str | None] = mapped_column(String(120))
    label: Mapped[str | None] = mapped_column(String(160))

    # Snapshot of what actually executed, so editing a suite later cannot rewrite history.
    assertions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=8, server_default=text("8"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default=text("3"))

    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    completed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    passed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    errored_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    pass_rate: Mapped[float | None] = mapped_column(Float)
    mean_score: Mapped[float | None] = mapped_column(Float)
    total_cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    total_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    total_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    cache_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    p50_latency_ms: Mapped[int | None] = mapped_column(Integer)
    p95_latency_ms: Mapped[int | None] = mapped_column(Integer)
    p99_latency_ms: Mapped[int | None] = mapped_column(Integer)

    # Set by the client (CLI/CI) so a retried POST /runs returns the original run instead of a duplicate.
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at_col()


class RunCase(Base):
    """One case inside one run — and one row of the work queue.

    `UNIQUE (run_id, case_id)` is the idempotency key: a worker that dies after writing its result
    but before committing its status cannot produce a second result row, and a re-claimed lease
    overwrites the same row rather than appending.
    """

    __tablename__ = "run_cases"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id"),
        CheckConstraint("status IN ('pending','claimed','passed','failed','error','skipped')", name="status_valid"),
        # The claim query: pending work, oldest first, ignoring rows another worker holds a live lease on.
        Index(
            "ix_run_cases_claimable",
            "run_id",
            "lease_expires_at",
            postgresql_where=text("status IN ('pending','claimed')"),
        ),
        Index("ix_run_cases_run_status", "run_id", "status"),
    )

    id: Mapped[UUID] = uuid_pk()
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default=text("'pending'"))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    claimed_by: Mapped[str | None] = mapped_column(String(80))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    score: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    assertion_results: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))

    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Span(Base):
    """One unit of work inside a case: an LLM call, a tool call, a judge call, or a retry.

    Spans carry `attempt` so a re-executed case keeps its history instead of silently losing it;
    the UI shows the winning attempt and lets you expand the earlier ones.
    """

    __tablename__ = "spans"
    __table_args__ = (
        CheckConstraint("kind IN ('llm','tool','judge','assertion','agent_step')", name="kind_valid"),
        Index("ix_spans_run_case_seq", "run_case_id", "attempt", "seq"),
        Index("ix_spans_run_started", "run_id", "started_at"),
    )

    id: Mapped[UUID] = uuid_pk()
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    run_case_id: Mapped[UUID] = mapped_column(ForeignKey("run_cases.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[UUID | None] = mapped_column(ForeignKey("spans.id", ondelete="CASCADE"))

    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(80))

    request: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = created_at_col()


# ─── Failure analysis ────────────────────────────────────────────────────────


class CaseEmbedding(Base):
    """Embedding of a failing case, used to group failures and to answer "show me cases like
    this one". Dimension is fixed by `settings.embedding_dim`; see docs/adr/0004."""

    __tablename__ = "case_embeddings"

    run_case_id: Mapped[UUID] = mapped_column(ForeignKey("run_cases.id", ondelete="CASCADE"), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    embedder: Mapped[str] = mapped_column(String(40), nullable=False)
    embedding: Mapped[Any] = mapped_column(Vector(settings.embedding_dim), nullable=False)


class FailureCluster(Base):
    __tablename__ = "failure_clusters"
    __table_args__ = (Index("ix_failure_clusters_run_size", "run_id", "size"),)

    id: Mapped[UUID] = uuid_pk()
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    centroid: Mapped[Any] = mapped_column(Vector(settings.embedding_dim), nullable=False)
    top_terms: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    created_at: Mapped[datetime] = created_at_col()


class ClusterMember(Base):
    __tablename__ = "cluster_members"
    __table_args__ = (UniqueConstraint("cluster_id", "run_case_id"),)

    id: Mapped[UUID] = uuid_pk()
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("failure_clusters.id", ondelete="CASCADE"), index=True)
    run_case_id: Mapped[UUID] = mapped_column(ForeignKey("run_cases.id", ondelete="CASCADE"), index=True)
    distance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default=text("0.0"))


class Outbox(Base):
    """Events written in the same transaction as the change they describe, dispatched after
    commit. Keeps webhooks/notifications from firing for work that rolled back."""

    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_undispatched", "created_at", postgresql_where=text("dispatched_at IS NULL")),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    topic: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
