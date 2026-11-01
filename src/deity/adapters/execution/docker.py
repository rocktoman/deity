"""Bounded local Docker CLI execution; the daemon remains a trusted component."""

import asyncio
import os
import re
import shutil
import time
from pathlib import Path

from deity.adapters.execution.workspace import WorkspaceStager, collect_manifest, file_hashes
from deity.core.contracts.execution import (
    ArtifactManifest,
    BackendCapabilities,
    BackendKind,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    SandboxCleanupReport,
    SandboxHandle,
)


class DockerExecutionBackend:
    """Execute one command in a locally managed, constrained container."""

    def __init__(
        self,
        workspace: Path,
        staging_root: Path,
        *,
        excluded_paths: tuple[Path, ...] = (),
        excluded_patterns: tuple[str, ...] = (),
        max_workspace_files: int = 50_000,
        max_workspace_bytes: int = 1_073_741_824,
        max_artifact_files: int = 200,
        max_artifact_bytes: int = 52_428_800,
        cleanup_timeout_seconds: int = 15,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.staging_root = staging_root
        self.excluded_paths = excluded_paths
        self.excluded_patterns = excluded_patterns
        self.max_workspace_files = max_workspace_files
        self.max_workspace_bytes = max_workspace_bytes
        self.max_artifact_files = max_artifact_files
        self.max_artifact_bytes = max_artifact_bytes
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        self._baselines: dict[str, dict[str, tuple[str, int]]] = {}

    @staticmethod
    def create_argv(request: ExecutionRequest, stage: Path, name: str) -> tuple[str, ...]:
        budget = request.spec.budget
        image = request.spec.image
        if image is None or re.fullmatch(r"(?:[\w./-]+@)?sha256:[a-f0-9]{64}", image) is None:
            raise ValueError("image must be pinned by digest")
        if request.spec.network != "none" and (
            not request.spec.network_approved
            or request.spec.network in {"host", "bridge", "default"}
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", request.spec.network) is None
        ):
            raise ValueError("network not explicitly approved")
        if any(re.fullmatch(r"[A-Z][A-Z0-9_]*", name) is None for name in request.spec.env_names):
            raise ValueError("invalid environment allowlist")
        if not request.argv or any(not item or "\0" in item for item in request.argv):
            raise ValueError("invalid argv")
        if Path(request.cwd).is_absolute() or ".." in Path(request.cwd).parts:
            raise ValueError("invalid cwd")
        if not stage.is_absolute() or stage.is_symlink():
            raise ValueError("invalid staging path")
        return (
            "docker",
            "create",
            "--pull=never",
            "--name",
            name,
            "--label",
            f"deity.execution={request.execution_id}",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--network={request.spec.network}",
            f"--cpus={budget.cpu_count}",
            f"--memory={budget.memory_bytes}",
            f"--pids-limit={budget.pids_limit}",
            f"--tmpfs=/tmp:rw,noexec,nosuid,size={budget.tmpfs_bytes}",
            "--mount",
            f"type=bind,src={stage},dst=/workspace",
            "--workdir",
            str(Path("/workspace") / request.cwd),
            *(item for name in request.spec.env_names for item in ("--env", name)),
            image,
            *request.argv,
        )

    async def capabilities(self) -> BackendCapabilities:
        if os.name != "posix" or shutil.which("docker") is None or os.environ.get("DOCKER_HOST"):
            return BackendCapabilities(
                BackendKind.DOCKER,
                False,
                True,
                True,
                True,
                True,
                True,
                True,
                ("docker unavailable or remote host selected",),
            )
        try:
            context_code, endpoint, _ = await self._docker(
                "docker",
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
                timeout=5,
            )
            if context_code != 0 or not endpoint.strip().startswith("unix://"):
                return BackendCapabilities(
                    BackendKind.DOCKER,
                    False,
                    True,
                    True,
                    True,
                    True,
                    True,
                    True,
                    ("non-local Docker context",),
                )
            code, server_os, _ = await self._docker(
                "docker", "info", "--format", "{{.OSType}}", timeout=5
            )
        except (OSError, TimeoutError):
            code = 1
            server_os = ""
        supported = code == 0 and server_os.strip() == "linux"
        return BackendCapabilities(
            BackendKind.DOCKER,
            supported,
            True,
            True,
            True,
            True,
            True,
            True,
            () if supported else ("Linux Docker daemon unavailable",),
        )

    async def image_available(self, image: str) -> bool:
        """Check a pinned local image without pulling or printing registry credentials."""
        if re.fullmatch(r"(?:[\w./-]+@)?sha256:[a-f0-9]{64}", image) is None:
            return False
        try:
            code, image_id, _ = await self._docker(
                "docker", "image", "inspect", image, "--format", "{{.Id}}", timeout=5
            )
        except (OSError, TimeoutError):
            return False
        return code == 0 and (not image.startswith("sha256:") or image_id.strip() == image)

    async def preflight(self, request: ExecutionRequest) -> bool:
        """Reject image, argv and unsupported policy before any durable intent or side effect."""
        if request.spec.backend != BackendKind.DOCKER or request.spec.image is None:
            return False
        if any(name not in os.environ for name in request.spec.env_names):
            return False
        try:
            self.create_argv(request, self.staging_root.resolve() / request.execution_id, "probe")
        except ValueError:
            return False
        return await self.image_available(request.spec.image)

    async def prepare(self, request: ExecutionRequest) -> SandboxHandle:
        if not (await self.capabilities()).available:
            raise ValueError("sandbox unavailable")
        if request.spec.image is None or not await self.image_available(request.spec.image):
            raise ValueError("sandbox image unavailable or changed")
        stage = self.staging_root / request.execution_id
        name = f"deity-{request.execution_id}"
        create_attempted = False
        try:
            WorkspaceStager(
                self.workspace,
                stage,
                max_files=self.max_workspace_files,
                max_bytes=self.max_workspace_bytes,
                excluded_paths=self.excluded_paths,
                excluded_patterns=self.excluded_patterns,
            ).copy()
            self._baselines[request.execution_id] = file_hashes(
                stage, max_files=self.max_workspace_files, max_bytes=self.max_workspace_bytes
            )
            create_attempted = True
            code, output, _ = await self._docker(
                *self.create_argv(request, stage, name),
                timeout=20,
                env_names=request.spec.env_names,
            )
            if code != 0:
                raise ValueError("container create failed")
            container_id = output.strip()
            if not re.fullmatch(r"[a-f0-9]{64}", container_id):
                raise ValueError("invalid container ID")
            return SandboxHandle(request.execution_id, BackendKind.DOCKER, container_id, str(stage))
        except BaseException:
            # Docker may have created the named container before the CLI response was lost.
            if create_attempted:
                try:
                    await self._docker("docker", "rm", "--force", name, timeout=5)
                except (OSError, TimeoutError):
                    pass
            if stage.exists():
                shutil.rmtree(stage)
            self._baselines.pop(request.execution_id, None)
            raise

    async def execute(self, handle: SandboxHandle, request: ExecutionRequest) -> ExecutionResult:
        started = time.monotonic()
        try:
            code, output, clipped = await self._docker(
                "docker",
                "start",
                "--attach",
                handle.opaque_id,
                timeout=request.spec.budget.timeout_seconds,
                output_cap=request.spec.budget.output_chars,
            )
        except TimeoutError:
            return ExecutionResult(
                request.execution_id,
                ExecutionStatus.TIMED_OUT,
                None,
                "",
                int((time.monotonic() - started) * 1000),
                error_code="timeout",
                container_id=handle.opaque_id,
            )
        except asyncio.CancelledError:
            raise
        except OSError:
            return ExecutionResult(
                request.execution_id,
                ExecutionStatus.UNKNOWN,
                None,
                "",
                int((time.monotonic() - started) * 1000),
                error_code="result_unknown",
                container_id=handle.opaque_id,
            )
        inspect_code, state, _ = await self._docker(
            "docker",
            "inspect",
            "--format",
            "{{.State.ExitCode}} {{.State.OOMKilled}} {{.Image}}",
            handle.opaque_id,
            timeout=5,
        )
        for name in request.spec.env_names:
            value = os.environ.get(name)
            if value:
                output = output.replace(value, "[redacted]")
        fields = state.strip().split()
        if inspect_code != 0 or len(fields) != 3:
            return ExecutionResult(
                request.execution_id,
                ExecutionStatus.UNKNOWN,
                None,
                output,
                int((time.monotonic() - started) * 1000),
                clipped,
                error_code="container_inspect_failed",
                container_id=handle.opaque_id,
            )
        try:
            exit_code = int(fields[0])
        except ValueError:
            return ExecutionResult(
                request.execution_id,
                ExecutionStatus.UNKNOWN,
                None,
                output,
                int((time.monotonic() - started) * 1000),
                clipped,
                error_code="invalid_container_exit",
                container_id=handle.opaque_id,
            )
        status = (
            ExecutionStatus.SUCCEEDED
            if exit_code == 0 and code == 0 and fields[1] != "true"
            else ExecutionStatus.FAILED
        )
        return ExecutionResult(
            request.execution_id,
            status,
            exit_code,
            output[: request.spec.budget.output_chars],
            int((time.monotonic() - started) * 1000),
            clipped,
            error_code="container_oom" if fields[1] == "true" else None,
            container_id=handle.opaque_id,
            image_digest=fields[2],
        )

    async def collect(self, handle: SandboxHandle, result: ExecutionResult) -> ArtifactManifest:
        if handle.staging_root is None:
            raise ValueError("missing staging root")
        after = file_hashes(
            Path(handle.staging_root),
            max_files=self.max_workspace_files + self.max_artifact_files,
            max_bytes=self.max_workspace_bytes + self.max_artifact_bytes,
        )
        manifest = collect_manifest(
            handle.execution_id,
            self._baselines[handle.execution_id],
            after,
        )
        if (
            len(manifest.entries) > self.max_artifact_files
            or sum(entry.size or 0 for entry in manifest.entries) > self.max_artifact_bytes
        ):
            raise ValueError("artifact collection limit exceeded")
        return manifest

    async def cleanup(self, handle: SandboxHandle) -> SandboxCleanupReport:
        try:
            code, _, _ = await self._docker(
                "docker", "rm", "--force", handle.opaque_id, timeout=self.cleanup_timeout_seconds
            )
            removed = code == 0
        except (OSError, TimeoutError):
            removed = False
        stage_removed = False
        if handle.staging_root is not None:
            shutil.rmtree(handle.staging_root)
            stage_removed = not Path(handle.staging_root).exists()
        self._baselines.pop(handle.execution_id, None)
        return SandboxCleanupReport(
            handle.execution_id,
            "cleaned" if removed and stage_removed else "failed",
            removed,
            stage_removed,
        )

    @staticmethod
    async def _docker(
        *argv: str,
        timeout: float,
        output_cap: int = 1_000_000,
        env_names: tuple[str, ...] = (),
    ) -> tuple[int, str, bool]:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={key: os.environ[key] for key in ("PATH", "HOME", *env_names) if key in os.environ},
            start_new_session=True,
        )

        async def drain(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
            chunks: list[bytes] = []
            kept = 0
            clipped = False
            while chunk := await stream.read(4096):
                remaining = max(0, output_cap - kept)
                if remaining:
                    chunks.append(chunk[:remaining])
                    kept += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    clipped = True
            return b"".join(chunks), clipped

        try:
            assert process.stdout is not None and process.stderr is not None
            stdout_result, stderr_result, _ = await asyncio.wait_for(
                asyncio.gather(drain(process.stdout), drain(process.stderr), process.wait()),
                timeout,
            )
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        stdout, stdout_clipped = stdout_result
        stderr, stderr_clipped = stderr_result
        return (
            process.returncode or 0,
            (stdout + stderr)[:output_cap].decode("utf-8", errors="replace"),
            stdout_clipped or stderr_clipped or len(stdout) + len(stderr) > output_cap,
        )
