"""Provider-neutral planning contracts for V0.5."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


class ExecutionMode(StrEnum):
    """Capability mode fixed for one run."""

    PLAN = "plan"
    BUILD = "build"
    VERIFY = "verify"


@dataclass(frozen=True)
class PlanStep:
    ordinal: int
    title: str
    description: str
    files: tuple[str, ...]
    verification: tuple[str, ...]


@dataclass(frozen=True)
class PlanDraft:
    schema_version: int
    goal: str
    assumptions: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    risks: tuple[str, ...]
    verification: tuple[str, ...]


@dataclass(frozen=True)
class PlanArtifact:
    plan_id: str
    source_run_id: str
    session_id: str
    draft: PlanDraft
    sha256: str
    requirements_sha256: str
    workspace_snapshot_id: str
    status: Literal["draft", "approved", "stale", "rejected"]
    created_at: str


@dataclass(frozen=True)
class PlanApproval:
    plan_id: str
    build_run_id: str
    approved_at: str
    allow_dirty_paths: tuple[str, ...] = ()


class PlanRepository(Protocol):
    async def create_plan(self, artifact: PlanArtifact) -> None: ...
    async def get_plan(self, plan_id: str) -> PlanArtifact: ...
    async def list_plans(self, session_id: str | None = None) -> tuple[PlanArtifact, ...]: ...
    async def approve_plan(self, plan_id: str, approval: PlanApproval) -> None: ...
    async def mark_plan_stale(self, plan_id: str, reason: str) -> None: ...
