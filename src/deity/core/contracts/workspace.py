"""Workspace, Git guard and change-set contracts."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


@dataclass(frozen=True)
class WorkspaceFileState:
    path: str
    status: str
    sha256: str | None = None


@dataclass(frozen=True)
class WorkspaceSnapshot:
    snapshot_id: str
    workspace_root: str
    git_head: str | None
    files: tuple[WorkspaceFileState, ...]
    digest: str
    backend_version: str
    created_at: str


class PatchApplyStatus(StrEnum):
    APPLIED = "applied"
    CONFLICT = "conflict"
    DENIED = "denied"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PatchApplyResult:
    status: PatchApplyStatus
    path: str
    before_sha256: str | None
    after_sha256: str | None
    evidence_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class FileChange:
    path: str
    operation: Literal["create", "modify", "delete"]
    before_sha256: str | None
    after_sha256: str | None
    source_call_id: str
    status: str = "completed"


@dataclass(frozen=True)
class ChangeSet:
    change_set_id: str
    run_id: str
    baseline_snapshot_id: str
    status: Literal["active", "completed", "unknown"]
    changes: tuple[FileChange, ...] = ()


@dataclass(frozen=True)
class GitCheckpoint:
    snapshot_id: str
    change_set_id: str
    evidence_id: str | None


class WorkspaceSnapshotPort(Protocol):
    async def capture(self) -> WorkspaceSnapshot: ...
    async def compare(self, snapshot: WorkspaceSnapshot) -> tuple[str, ...]: ...


class ChangeSetRepository(Protocol):
    async def start_change_set(self, change_set: ChangeSet) -> None: ...
    async def record_file_change(self, change_set_id: str, change: FileChange) -> None: ...
    async def finalize_change_set(self, change_set_id: str, status: str) -> ChangeSet: ...
