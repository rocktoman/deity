"""Scriptable execution backend for deterministic lifecycle and fault tests."""

from deity.core.contracts.execution import (
    ArtifactEntry,
    ArtifactManifest,
    BackendCapabilities,
    BackendKind,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    SandboxCleanupReport,
    SandboxHandle,
)


class FakeExecutionBackend:
    """Record every stage and optionally fail at an exact lifecycle point."""

    def __init__(
        self,
        *,
        fail_at: str | None = None,
        available: bool = True,
        result_status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
        manifest_entries: tuple[ArtifactEntry, ...] = (),
    ) -> None:
        self.fail_at = fail_at
        self.available = available
        self.result_status = result_status
        self.manifest_entries = manifest_entries
        self.calls: list[str] = []

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_at == name:
            raise RuntimeError(f"fake {name} failed")

    async def capabilities(self) -> BackendCapabilities:
        self._step("capabilities")
        return BackendCapabilities(
            BackendKind.FAKE,
            self.available,
            True,
            True,
            True,
            True,
            True,
            True,
        )

    async def preflight(self, request: ExecutionRequest) -> bool:
        self._step("preflight")
        return request.spec.backend == BackendKind.FAKE

    async def prepare(self, request: ExecutionRequest) -> SandboxHandle:
        self._step("prepare")
        return SandboxHandle(request.execution_id, BackendKind.FAKE, "fake-container", None)

    async def execute(self, handle: SandboxHandle, request: ExecutionRequest) -> ExecutionResult:
        self._step("execute")
        return ExecutionResult(
            request.execution_id,
            self.result_status,
            0 if self.result_status == ExecutionStatus.SUCCEEDED else None,
            "",
            1,
        )

    async def collect(self, handle: SandboxHandle, result: ExecutionResult) -> ArtifactManifest:
        self._step("collect")
        return ArtifactManifest(handle.execution_id, self.manifest_entries, "collected")

    async def cleanup(self, handle: SandboxHandle) -> SandboxCleanupReport:
        self._step("cleanup")
        return SandboxCleanupReport(handle.execution_id, "cleaned", True, True)
