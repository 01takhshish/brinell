"""Request and response models.

These are the API's contract, kept separate from the ORM on purpose: a response model that is just
`Model.from_orm` leaks every column you add later, including the ones you did not mean to publish.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from crucible.domain.cost import format_usd


class Out(BaseModel):
    """Base for every response model.

    `from_attributes` is what lets a handler return a SQLAlchemy `Row` directly. The queries in the
    routers are hand-written SQL that select exactly the columns a response needs, so the row and
    the model line up by construction and there is no lazy-loading behind the serialisation.
    """

    model_config = ConfigDict(from_attributes=True)


class SignupRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(Out):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(Out):
    id: UUID
    email: str
    name: str
    is_admin: bool


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")


class ProjectOut(Out):
    id: UUID
    name: str
    slug: str
    role: str | None = None
    created_at: datetime


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ApiKeyOut(Out):
    id: UUID
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreated(ApiKeyOut):
    """The one response that carries the secret. It is not retrievable afterwards."""

    key: str


class TargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["prompt", "agent"] = "prompt"
    description: str | None = None


class TargetOut(Out):
    id: UUID
    name: str
    kind: str
    description: str | None
    latest_version: str | None = None
    created_at: datetime


class TargetVersionCreate(BaseModel):
    config: dict[str, Any]
    semver: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")
    notes: str | None = None


class TargetVersionOut(Out):
    id: UUID
    target_id: UUID
    semver: str
    content_hash: str
    config: dict[str, Any]
    notes: str | None = None
    created_at: datetime
    reused: bool = False


class CaseIn(BaseModel):
    external_id: str = Field(min_length=1, max_length=120)
    input: dict[str, Any]
    expected: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)
    weight: float = Field(default=1.0, gt=0, le=100)
    tool_fixtures: dict[str, Any] | None = None


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None


class DatasetOut(Out):
    id: UUID
    name: str
    description: str | None
    latest_version: int | None = None
    case_count: int = 0
    created_at: datetime


class DatasetVersionCreate(BaseModel):
    cases: list[CaseIn] = Field(min_length=1)
    notes: str | None = None


class DatasetVersionOut(Out):
    id: UUID
    dataset_id: UUID
    version: int
    case_count: int
    content_hash: str
    created_at: datetime
    reused: bool = False


class SuiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    dataset_id: UUID
    assertions: list[dict[str, Any]] = Field(min_length=1)
    judge_model: str | None = None
    pass_threshold: float = Field(default=1.0, ge=0, le=1)
    description: str | None = None


class SuiteOut(Out):
    id: UUID
    name: str
    dataset_id: UUID
    assertions: list[dict[str, Any]]
    judge_model: str | None
    pass_threshold: float
    created_at: datetime


class RunCreate(BaseModel):
    target_version_id: UUID
    suite_id: UUID
    dataset_version_id: UUID | None = None
    baseline_run_id: UUID | None = None
    concurrency: int | None = Field(default=None, ge=1, le=64)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    trigger: Literal["api", "cli", "ci", "ui"] = "api"
    git_sha: str | None = Field(default=None, max_length=40)
    git_branch: str | None = Field(default=None, max_length=120)
    label: str | None = Field(default=None, max_length=160)
    idempotency_key: str | None = Field(default=None, max_length=120)


class RunOut(Out):
    id: UUID
    status: str
    label: str | None = None
    trigger: str
    git_sha: str | None = None
    git_branch: str | None = None
    target_version_id: UUID
    suite_id: UUID
    dataset_version_id: UUID
    baseline_run_id: UUID | None = None
    total_cases: int
    completed_cases: int
    passed_cases: int
    errored_cases: int
    pass_rate: float | None = None
    mean_score: float | None = None
    total_cost_micros: int
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    cache_hits: int = 0
    p50_latency_ms: int | None = None
    p95_latency_ms: int | None = None
    p99_latency_ms: int | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime

    @property
    def cost_usd(self) -> str:
        return format_usd(self.total_cost_micros)


class RunCreated(Out):
    id: UUID
    total_cases: int
    reused: bool
    baseline_run_id: UUID | None = None


class RunCaseOut(Out):
    id: UUID
    external_id: str
    status: str
    score: float | None = None
    attempt: int
    latency_ms: int | None = None
    cost_micros: int | None = None
    cache_hit: bool = False
    error: str | None = None
    finished_at: datetime | None = None


class RunCaseDetail(RunCaseOut):
    input: dict[str, Any]
    expected: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    assertion_results: list[dict[str, Any]] = Field(default_factory=list)
    spans: list[dict[str, Any]] = Field(default_factory=list)


class Page[T](Out):
    items: list[T]
    total: int
    limit: int
    offset: int
