"""Coordinate one approved sandbox execution with durable audit facts."""

from collections.abc import Awaitable, Callable

from deity.core.contracts.execution import (
    ArtifactManifest,
    BackendKind,
    ExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    SandboxCleanupReport,
    SandboxHandle,
)


class SandboxUnavailable(ValueError):
    """A required backend cannot safely start an execution."""


class SandboxService:
    """Record intent before effects and cleanup outcome after every created handle."""

    def __init__(
        self,
        backend: ExecutionBackend,
        repository: object,
        reviewer: Callable[[SandboxHandle, ArtifactManifest], Awaitable[ArtifactManifest]]
        | None = None,
    ) -> None:
        self.backend = backend
        self.repository = repository
        self.reviewer = reviewer

    async def run(self, request: ExecutionRequest) -> tuple[ExecutionResult, ArtifactManifest]:
        capabilities = await self.backend.capabilities()
        if (
            request.spec.backend not in {BackendKind.DOCKER, BackendKind.FAKE}
            or request.spec.backend != capabilities.kind
            or not capabilities.available
        ):
            raise SandboxUnavailable("sandbox unavailable")
        if not all(
            (
                capabilities.process_isolation,
                capabilities.read_only_rootfs,
                capabilities.network_none,
                capabilities.cpu_limit,
                capabilities.memory_limit,
                capabilities.pids_limit,
            )
        ):
            raise SandboxUnavailable("sandbox capability missing")
        if not await self.backend.preflight(request):
            raise SandboxUnavailable("sandbox preflight failed")
        repo = self.repository
        await repo.prepare_sandbox_execution(request)  # type: ignore[attr-defined]
        handle: SandboxHandle | None = None
        result: ExecutionResult | None = None
        manifest: ArtifactManifest | None = None
        try:
            handle = await self.backend.prepare(request)
            await repo.record_sandbox_container(  # type: ignore[attr-defined]
                request.run_id, request.execution_id, handle.opaque_id
            )
            result = await self.backend.execute(handle, request)
            await repo.record_sandbox_result(request.run_id, result)  # type: ignore[attr-defined]
            if result.status in {
                ExecutionStatus.UNKNOWN,
                ExecutionStatus.TIMED_OUT,
                ExecutionStatus.CANCELLED,
            }:
                manifest = ArtifactManifest(request.execution_id, (), "unknown")
                await repo.record_sandbox_manifest(request.run_id, manifest)  # type: ignore[attr-defined]
            else:
                manifest = await self.backend.collect(handle, result)
                await repo.record_sandbox_manifest(request.run_id, manifest)  # type: ignore[attr-defined]
                if (
                    result.status == ExecutionStatus.SUCCEEDED
                    and self.reviewer is not None
                    and manifest.entries
                ):
                    manifest = await self.reviewer(handle, manifest)
        except BaseException:
            if result is None:
                unknown = ExecutionResult(
                    request.execution_id,
                    ExecutionStatus.UNKNOWN,
                    None,
                    "",
                    0,
                    error_code="result_unknown",
                )
                await repo.record_sandbox_result(request.run_id, unknown)  # type: ignore[attr-defined]
            elif manifest is None:
                await repo.record_sandbox_manifest(  # type: ignore[attr-defined]
                    request.run_id, ArtifactManifest(request.execution_id, (), "unknown")
                )
            elif self.reviewer is not None and manifest.entries:
                await repo.mark_sandbox_manifest_unknown(  # type: ignore[attr-defined]
                    request.run_id, request.execution_id
                )
            raise
        finally:
            if handle is not None:
                try:
                    cleanup = await self.backend.cleanup(handle)
                except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                    cleanup = SandboxCleanupReport(
                        request.execution_id,
                        "failed",
                        False,
                        False,
                        (type(exc).__name__,),
                    )
            else:
                cleanup = SandboxCleanupReport(request.execution_id, "not_found", False, True)
            await repo.record_sandbox_cleanup(request.run_id, cleanup)  # type: ignore[attr-defined]
        if cleanup.status != "cleaned":
            raise RuntimeError("sandbox cleanup failed")
        assert result is not None and manifest is not None
        return result, manifest
