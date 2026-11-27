"""macOS Seatbelt (sandbox-exec) 执行后端（ADR-027）：轻量原生文件与网络隔离。

边界（诚实口径）：Seatbelt 通过 SBPL 规则强制禁网与写入路径白名单，但不
强制 CPU/内存/PID 配额——capabilities 如实报告 False。工作区同样先复制到
staging（WorkspaceStager），产物由宿主侧哈希清单回收。仅 macOS + 系统
`sandbox-exec` 存在时可用；full-access 与非 none 网络一律拒绝。
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


def _escape_sbpl(path: str) -> str:
    return path.replace("\\", "\\\\").replace('"', '\\"')


class SeatbeltExecutionBackend:
    """以 macOS sandbox-exec 运行一条受限命令（禁网 + 严格写路径白名单）。"""

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
    def build_profile(stage: Path, policy: SandboxPolicy) -> str:
        """生成确定性 SBPL 策略字符串（纯函数，便于跨平台单测审计）。"""
        if policy == SandboxPolicy.FULL_ACCESS:
            raise ValueError("full-access is not a sandbox policy")
        if not stage.is_absolute() or stage.is_symlink():
            raise ValueError("invalid staging path")
        stage_str = _escape_sbpl(str(stage))
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process-exec)",
            "(allow process-fork)",
            "(allow signal (target self))",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow file-read*)",
            "(deny network*)",
        ]
        if policy == SandboxPolicy.WORKSPACE_WRITE:
            lines.append(
                f'(allow file-write* (subpath "{stage_str}") '
                '(subpath "/private/tmp") (subpath "/tmp") (literal "/dev/null"))'
            )
        else:
            lines.append('(allow file-write* (literal "/dev/null"))')
        return "\n".join(lines)

    @classmethod
    def create_argv(cls, request: ExecutionRequest, stage: Path) -> tuple[str, ...]:
        """组装 sandbox-exec argv（纯函数，执行前做全量参数校验）。"""
        spec = request.spec
        if spec.policy == SandboxPolicy.FULL_ACCESS:
            raise ValueError("full-access is not a sandbox policy")
        if spec.network != "none":
            raise ValueError("seatbelt backend requires network=none")
        if not request.argv or any(not item or "\0" in item for item in request.argv):
            raise ValueError("invalid argv")
        if Path(request.cwd).is_absolute() or ".." in Path(request.cwd).parts:
            raise ValueError("invalid cwd")
        if any(re.fullmatch(r"[A-Z][A-Z0-9_]*", name) is None for name in spec.env_names):
            raise ValueError("invalid environment allowlist")
        profile = cls.build_profile(stage, spec.policy)
        return ("sandbox-exec", "-p", profile, *request.argv)

    async def capabilities(self) -> BackendCapabilities:
        available = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None
        return BackendCapabilities(
            BackendKind.SEATBELT,
            available,
            True,
            True,
            True,
            False,
            False,
            False,
            () if available else ("seatbelt unavailable: requires macOS and sandbox-exec",),
        )

    async def preflight(self, request: ExecutionRequest) -> bool:
        if request.spec.backend != BackendKind.SEATBELT:
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
            raise ValueError("seatbelt sandbox unavailable")
        stage = self.staging_root.resolve() / request.execution_id
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
        return SandboxHandle(request.execution_id, BackendKind.SEATBELT, "seatbelt", str(stage))

    async def execute(self, handle: SandboxHandle, request: ExecutionRequest) -> ExecutionResult:
        if handle.staging_root is None:
            raise ValueError("missing staging root")
        started = time.monotonic()
        stage = Path(handle.staging_root)
        argv = self.create_argv(request, stage)
        cwd = (stage / request.cwd).resolve()
        try:
            output, code, truncated = await run_local_process(
                argv,
                cwd,
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
        del result
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


__all__ = ["SeatbeltExecutionBackend"]
