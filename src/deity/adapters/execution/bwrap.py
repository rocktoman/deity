"""Linux bubblewrap 执行后端（ADR-020 B2）：真沙箱的一档，不冒充配额隔离。

分级语义参照 openai/codex 的 SandboxPolicy（Apache-2.0，
`codex-rs/protocol/src/protocol.rs`），argv 组装与生命周期用
Python 重新实现，声明见 `docs/06-legal/third-party-notices.md`。

边界（诚实口径）：bwrap 提供用户/进程/IPC/UTS/网络命名空间与
只读根文件系统绑定，但不强制 CPU/内存/PID 配额——capabilities
如实报告 False。工作区先复制到 staging（WorkspaceStager，与
Docker 同款：拒 symlink/特殊文件/秘密文件），沙箱内只见
staging；产物由宿主侧哈希清单回收，绝不信任沙箱内自报结果。
仅 Linux + 系统 bwrap 存在时可用；full-access 不是沙箱，本
后端一律拒绝；网络恒为 none（--unshare-net），spec.network
非 none 时 preflight 失败。
"""

import asyncio
import os
import re
import shutil
import sys
import time
from pathlib import Path

from deity.adapters.execution.local import run_local_process
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
    SandboxPolicy,
)


class BwrapExecutionBackend:
    """以 bwrap 运行一条命令：staging 绑定为 /workspace，其余只读。"""

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
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.staging_root = staging_root
        self.excluded_paths = excluded_paths
        self.excluded_patterns = excluded_patterns
        self.max_workspace_files = max_workspace_files
        self.max_workspace_bytes = max_workspace_bytes
        self.max_artifact_files = max_artifact_files
        self.max_artifact_bytes = max_artifact_bytes
        self._baselines: dict[str, dict[str, tuple[str, int]]] = {}

    @staticmethod
    def create_argv(request: ExecutionRequest, stage: Path) -> tuple[str, ...]:
        """组装 bwrap argv（纯函数，供测试逐项核对安全标志）。"""
        spec = request.spec
        if spec.policy == SandboxPolicy.FULL_ACCESS:
            raise ValueError("full-access is not a sandbox policy")
        if spec.network != "none":
            raise ValueError("bwrap backend requires network=none")
        if not request.argv or any(not item or "\0" in item for item in request.argv):
            raise ValueError("invalid argv")
        if Path(request.cwd).is_absolute() or ".." in Path(request.cwd).parts:
            raise ValueError("invalid cwd")
        if any(re.fullmatch(r"[A-Z][A-Z0-9_]*", name) is None for name in spec.env_names):
            raise ValueError("invalid environment allowlist")
        if not stage.is_absolute() or stage.is_symlink():
            raise ValueError("invalid staging path")
        bind_flag = "--ro-bind" if spec.policy == SandboxPolicy.READ_ONLY else "--bind"
        env_args: list[str] = []
        for name in ("PATH", *spec.env_names):
            value = os.environ.get(name)
            if value is None and name == "PATH":
                value = "/usr/bin:/bin"
            if value is not None:
                env_args.extend(("--setenv", name, value))
        return (
            "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            bind_flag,
            str(stage),
            "/workspace",
            "--chdir",
            str(Path("/workspace") / request.cwd),
            "--clearenv",
            *env_args,
            "--",
            *request.argv,
        )

    async def capabilities(self) -> BackendCapabilities:
        available = sys.platform.startswith("linux") and shutil.which("bwrap") is not None
        return BackendCapabilities(
            BackendKind.BWRAP,
            available,
            True,
            True,
            True,
            False,
            False,
            False,
            () if available else ("bwrap unavailable: requires Linux and the bwrap binary",),
        )

    async def preflight(self, request: ExecutionRequest) -> bool:
        """副作用前校验后端、policy 与 argv；任一不满足即拒绝。"""
        if request.spec.backend != BackendKind.BWRAP:
            return False
        if any(name not in os.environ for name in request.spec.env_names):
            return False
        try:
            self.create_argv(request, self.staging_root.resolve() / request.execution_id)
        except ValueError:
            return False
        return (await self.capabilities()).available

    async def prepare(self, request: ExecutionRequest) -> SandboxHandle:
        if not (await self.capabilities()).available:
            raise ValueError("bwrap sandbox unavailable")
        stage = self.staging_root / request.execution_id
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
        except BaseException:
            if stage.exists():
                shutil.rmtree(stage)
            self._baselines.pop(request.execution_id, None)
            raise
        return SandboxHandle(request.execution_id, BackendKind.BWRAP, "bwrap", str(stage))

    async def execute(self, handle: SandboxHandle, request: ExecutionRequest) -> ExecutionResult:
        if handle.staging_root is None:
            raise ValueError("missing staging root")
        started = time.monotonic()
        argv = self.create_argv(request, Path(handle.staging_root))
        try:
            output, code, truncated = await run_local_process(
                argv,
                Path(handle.staging_root),
                request.spec.budget.timeout_seconds,
                request.spec.budget.output_chars,
            )
        except TimeoutError:
            return ExecutionResult(
                request.execution_id,
                ExecutionStatus.TIMED_OUT,
                None,
                "",
                int((time.monotonic() - started) * 1000),
                error_code="timeout",
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
            )
        for name in request.spec.env_names:
            value = os.environ.get(name)
            if value:
                output = output.replace(value, "[redacted]")
        return ExecutionResult(
            request.execution_id,
            ExecutionStatus.SUCCEEDED if code == 0 else ExecutionStatus.FAILED,
            code,
            output,
            int((time.monotonic() - started) * 1000),
            truncated,
        )

    async def collect(self, handle: SandboxHandle, result: ExecutionResult) -> ArtifactManifest:
        if handle.staging_root is None:
            raise ValueError("missing staging root")
        if handle.execution_id not in self._baselines:
            raise ValueError("missing staging baseline")
        after = file_hashes(
            Path(handle.staging_root),
            max_files=self.max_workspace_files + self.max_artifact_files,
            max_bytes=self.max_workspace_bytes + self.max_artifact_bytes,
        )
        manifest = collect_manifest(
            handle.execution_id, self._baselines[handle.execution_id], after
        )
        if (
            len(manifest.entries) > self.max_artifact_files
            or sum(entry.size or 0 for entry in manifest.entries) > self.max_artifact_bytes
        ):
            raise ValueError("artifact collection limit exceeded")
        return manifest

    async def cleanup(self, handle: SandboxHandle) -> SandboxCleanupReport:
        removed = True
        if handle.staging_root is not None:
            shutil.rmtree(handle.staging_root, ignore_errors=True)
            removed = not Path(handle.staging_root).exists()
        self._baselines.pop(handle.execution_id, None)
        return SandboxCleanupReport(
            handle.execution_id, "cleaned" if removed else "failed", True, removed
        )


__all__ = ["BwrapExecutionBackend"]
