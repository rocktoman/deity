"""Shared bounded host process runner and explicitly non-isolated local backend."""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

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


def _terminate_process_group(process: asyncio.subprocess.Process, *, force: bool) -> None:
    """终止进程组：POSIX 用 killpg（先 TERM 后 KILL）；Windows 无进程组
    概念（也无 SIGKILL），退化为终止进程本体。进程已退出时静默。"""
    if sys.platform == "win32":
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


#: 子进程最小环境必须保留的平台必需变量。Windows 缺 SYSTEMROOT 时 Winsock
#: 无法加载服务提供者（WinError 10106），任何联网子进程都会在 socket()
#: 处失败（F2 CI 在 macOS/Windows 上的 dev server 用例即因此失败）；
#: POSIX 工具链普遍依赖 HOME/TMPDIR。秘密类变量仍一律不透传。
_ESSENTIAL_ENV_KEYS: tuple[str, ...] = (
    ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT")
    if os.name == "nt"
    else ("PATH", "LANG", "LC_ALL", "HOME", "TMPDIR")
)


def minimal_subprocess_env() -> dict[str, str]:
    """父环境的子集：仅平台必需变量，不含任何凭据。"""
    return {key: os.environ[key] for key in _ESSENTIAL_ENV_KEYS if key in os.environ}


async def run_local_process(
    argv: tuple[str, ...], workspace: Path, timeout: float, output_cap: int
) -> tuple[str, int, bool]:
    """Execute argv with a minimal environment and bounded output and lifetime."""
    env = minimal_subprocess_env()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=workspace,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def read_limited(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
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
            asyncio.gather(
                read_limited(process.stdout), read_limited(process.stderr), process.wait()
            ),
            timeout,
        )
    except (TimeoutError, asyncio.CancelledError):
        _terminate_process_group(process, force=False)
        try:
            await asyncio.wait_for(process.wait(), 2.0)
        except TimeoutError:
            _terminate_process_group(process, force=True)
            await process.wait()
        raise
    stdout, stdout_clipped = stdout_result
    stderr, stderr_clipped = stderr_result
    combined = (stdout + stderr)[:output_cap]
    return (
        combined.decode("utf-8", errors="replace"),
        process.returncode or 0,
        stdout_clipped or stderr_clipped or len(stdout) + len(stderr) > output_cap,
    )


class LocalExecutionBackend:
    """Expose host execution through the same lifecycle without claiming isolation."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve(strict=True)

    async def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            BackendKind.LOCAL, True, False, False, False, False, False, False
        )

    async def preflight(self, request: ExecutionRequest) -> bool:
        return request.spec.backend == BackendKind.LOCAL

    async def prepare(self, request: ExecutionRequest) -> SandboxHandle:
        if request.spec.backend != BackendKind.LOCAL:
            raise ValueError("backend mismatch")
        return SandboxHandle(request.execution_id, BackendKind.LOCAL, "local", None)

    async def execute(self, handle: SandboxHandle, request: ExecutionRequest) -> ExecutionResult:
        if handle.execution_id != request.execution_id:
            raise ValueError("execution identity mismatch")
        cwd = (self.workspace / request.cwd).resolve(strict=True)
        if not cwd.is_relative_to(self.workspace) or not cwd.is_dir():
            raise ValueError("local cwd outside workspace")
        started = time.monotonic()
        try:
            output, code, truncated = await run_local_process(
                request.argv,
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
            )
        return ExecutionResult(
            request.execution_id,
            ExecutionStatus.SUCCEEDED if code == 0 else ExecutionStatus.FAILED,
            code,
            output,
            int((time.monotonic() - started) * 1000),
            truncated,
        )

    async def collect(self, handle: SandboxHandle, result: ExecutionResult) -> ArtifactManifest:
        return ArtifactManifest(handle.execution_id, (), "collected")

    async def cleanup(self, handle: SandboxHandle) -> SandboxCleanupReport:
        return SandboxCleanupReport(handle.execution_id, "cleaned", True, True)
