"""Deterministic verification contracts."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class VerificationCommand:
    kind: Literal["compile", "test", "lint", "format", "typecheck", "build", "custom"]
    argv: tuple[str, ...]
    required: bool = True


@dataclass(frozen=True)
class VerificationPlan:
    commands: tuple[VerificationCommand, ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationEvidence:
    evidence_id: str
    command: VerificationCommand
    status: VerificationStatus
    exit_code: int | None
    duration_ms: int
    output: str
    truncated: bool = False
    reason: str | None = None
    execution_id: str | None = None
    backend: str | None = None
    image_digest: str | None = None
    cleanup_status: str | None = None


@dataclass(frozen=True)
class FinalReport:
    status: VerificationStatus
    run_id: str | None
    change_set_id: str | None
    checks: tuple[VerificationEvidence, ...]
    remaining_risks: tuple[str, ...] = ()


class Verifier(Protocol):
    async def detect(self) -> VerificationPlan: ...
    async def run(self, plan: VerificationPlan) -> tuple[VerificationEvidence, ...]: ...


class VerificationRepository(Protocol):
    async def create_verification(self, run_id: str | None, plan: VerificationPlan) -> str: ...
    async def record_verification_check(
        self, verification_id: str, evidence: VerificationEvidence
    ) -> None: ...
    async def complete_verification(
        self, verification_id: str, status: VerificationStatus
    ) -> None: ...
