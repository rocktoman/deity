"""Provider-neutral process execution contracts."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


class BackendKind(StrEnum):
    LOCAL = "local"
    DOCKER = "docker"
    BWRAP = "bwrap"
    SEATBELT = "seatbelt"
    FAKE = "fake"


class SandboxPolicy(StrEnum):
    """沙箱分级语义（ADR-020，参照 Codex SandboxPolicy 三档）。

    read-only：根文件系统与工作区都只读；workspace-write：仅工作区
    可写；full-access：无沙箱（只能由 local 后端表达，隔离后端拒绝）。
    """

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    FULL_ACCESS = "full-access"


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ResourceBudget:
    timeout_seconds: float
    cpu_count: float | None
    memory_bytes: int | None
    pids_limit: int | None
    output_chars: int
    tmpfs_bytes: int | None


@dataclass(frozen=True)
class BackendCapabilities:
    kind: BackendKind
    available: bool
    process_isolation: bool
    read_only_rootfs: bool
    network_none: bool
    cpu_limit: bool
    memory_limit: bool
    pids_limit: bool
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceMountPolicy:
    mode: Literal["copy"] = "copy"
    container_path: str = "/workspace"
    reject_symlinks: bool = True


@dataclass(frozen=True)
class SandboxSpec:
    backend: BackendKind
    image: str | None
    budget: ResourceBudget
    network: str = "none"
    network_approved: bool = False
    env_names: tuple[str, ...] = ()
    workspace: WorkspaceMountPolicy = WorkspaceMountPolicy()
    artifact_policy: Literal["discard", "review"] = "discard"
    policy: SandboxPolicy = SandboxPolicy.WORKSPACE_WRITE


@dataclass(frozen=True)
class ExecutionRequest:
    execution_id: str
    run_id: str
    call_id: str | None
    argv: tuple[str, ...]
    cwd: str
    spec: SandboxSpec


@dataclass(frozen=True)
class SandboxHandle:
    execution_id: str
    backend: BackendKind
    opaque_id: str
    staging_root: str | None


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    status: ExecutionStatus
    exit_code: int | None
    output: str
    duration_ms: int
    truncated: bool = False
    error_code: str | None = None
    container_id: str | None = None
    image_digest: str | None = None


@dataclass(frozen=True)
class ArtifactEntry:
    path: str
    operation: Literal["create", "modify", "delete"]
    before_sha256: str | None
    after_sha256: str | None
    size: int | None


@dataclass(frozen=True)
class ArtifactManifest:
    execution_id: str
    entries: tuple[ArtifactEntry, ...]
    status: Literal["collected", "rejected", "applied", "partial", "unknown"]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class SandboxCleanupReport:
    execution_id: str
    status: Literal["cleaned", "not_found", "failed", "unknown"]
    container_removed: bool
    staging_removed: bool
    diagnostics: tuple[str, ...] = ()


class ExecutionBackend(Protocol):
    async def capabilities(self) -> BackendCapabilities: ...
    async def preflight(self, request: ExecutionRequest) -> bool: ...
    async def prepare(self, request: ExecutionRequest) -> SandboxHandle: ...
    async def execute(
        self, handle: SandboxHandle, request: ExecutionRequest
    ) -> ExecutionResult: ...
    async def collect(self, handle: SandboxHandle, result: ExecutionResult) -> ArtifactManifest: ...
    async def cleanup(self, handle: SandboxHandle) -> SandboxCleanupReport: ...
