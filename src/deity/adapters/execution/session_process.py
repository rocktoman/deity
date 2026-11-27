"""持久 shell 会话（ADR-019 B1）：run 级存活的进程会话表。

参照 Codex unified_exec 的进程会话模型（id→进程、write_stdin、
分段取输出）用 Python/asyncio 重新实现。会话进程以最小环境、独立
进程组启动（与一次性执行同款）；stdout/stderr 由泵任务持续读入有
界滚动缓冲（超限丢最旧并计数），调用方按游标增量取新输出。会话是
run 级资源：运行内跨轮存活，运行结束由持有者 close_all 回收；另
有空闲回收（默认 900 秒无活动）在每次操作时惰性检查。终止走进程
组 TERM→KILL（与 local._terminate_process_group 同语义）。
"""

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from deity.adapters.execution.local import _terminate_process_group, minimal_subprocess_env

MAX_BUFFER_CHARS = 128_000
DEFAULT_IDLE_TIMEOUT_SECONDS = 900.0


@dataclass(frozen=True)
class SessionRead:
    """一次增量读取的结果：新输出 + 会话当前状态。"""

    text: str
    running: bool
    exit_code: int | None
    dropped_chars: int


class _Session:
    """单个持久进程 + 其输出缓冲与读游标。"""

    def __init__(self, name: str, process: asyncio.subprocess.Process) -> None:
        self.name = name
        self.process = process
        self.pid = process.pid
        self._text = ""
        self._base = 0  # _text[0] 的绝对偏移
        self._total = 0  # 累计追加字符数
        self._cursor = 0  # 已被读取的绝对偏移
        self.last_activity = time.monotonic()
        self.exit_code: int | None = None
        self._pumps: list[asyncio.Task[None]] = []

    @property
    def running(self) -> bool:
        return self.exit_code is None

    @property
    def dropped_chars(self) -> int:
        return self._base

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        self._text += chunk
        self._total += len(chunk)
        overflow = len(self._text) - MAX_BUFFER_CHARS
        if overflow > 0:
            self._text = self._text[overflow:]
            self._base += overflow

    def read_new(self) -> SessionRead:
        start = max(self._cursor, self._base)
        text = self._text[start - self._base :]
        self._cursor = self._total
        self.last_activity = time.monotonic()
        return SessionRead(text, self.running, self.exit_code, self._base)


async def _pump(stream: asyncio.StreamReader, session: _Session) -> None:
    try:
        while chunk := await stream.read(4096):
            session.append(chunk.decode("utf-8", errors="replace"))
    except (asyncio.CancelledError, OSError):
        pass


async def _watch_exit(session: _Session) -> None:
    session.exit_code = await session.process.wait()


class ShellSessionManager:
    """一张会话表：启动/写入/增量读/关闭，含空闲回收。"""

    def __init__(self, *, idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS) -> None:
        self._sessions: dict[str, _Session] = {}
        self._idle_timeout = idle_timeout_seconds

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._sessions))

    def list_sessions(self) -> tuple[dict[str, object], ...]:
        """返回所有会话的只读快照（名称、PID、存活状态、退出码）。"""
        return tuple(
            {
                "name": name,
                "pid": session.pid,
                "running": session.running,
                "exit_code": session.exit_code,
            }
            for name, session in sorted(self._sessions.items())
        )

    def get(self, name: str) -> _Session | None:
        return self._sessions.get(name)

    async def start(self, name: str, argv: tuple[str, ...], cwd: Path) -> _Session:
        """启动新会话；同名存活会话存在时报错，同名已退出会话被替换。"""
        await self.reap_idle()
        existing = self._sessions.get(name)
        if existing is not None:
            if existing.running:
                raise ValueError(f"session {name} already running")
            await self.close(name)
        env = minimal_subprocess_env()
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        session = _Session(name, process)
        assert process.stdout is not None and process.stderr is not None
        session._pumps = [
            asyncio.ensure_future(_pump(process.stdout, session)),
            asyncio.ensure_future(_pump(process.stderr, session)),
            asyncio.ensure_future(_watch_exit(session)),
        ]
        self._sessions[name] = session
        return session

    async def write(self, name: str, data: str) -> None:
        """向会话 stdin 写入；会话不存在或已退出时报错。"""
        session = self._sessions.get(name)
        if session is None:
            raise ValueError(f"session {name} not found")
        if not session.running or session.process.stdin is None:
            raise ValueError(f"session {name} already exited")
        session.process.stdin.write(data.encode("utf-8"))
        await session.process.stdin.drain()
        session.last_activity = time.monotonic()

    async def collect(self, name: str, yield_seconds: float) -> SessionRead:
        """等待至多 yield_seconds（进程退出则提前），返回增量输出。"""
        session = self._sessions.get(name)
        if session is None:
            raise ValueError(f"session {name} not found")
        deadline = time.monotonic() + max(0.0, yield_seconds)
        while session.running and time.monotonic() < deadline:
            await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        if not session.running:
            # 进程已退出：给泵任务一个短窗口把尾部输出读完。
            try:
                await asyncio.wait_for(
                    asyncio.gather(*session._pumps, return_exceptions=True), timeout=1.0
                )
            except TimeoutError:
                pass
        return session.read_new()

    async def close(self, name: str) -> _Session | None:
        """终止并移除会话（已退出则只移除），返回被关会话或 None。"""
        session = self._sessions.pop(name, None)
        if session is None:
            return None
        if session.running:
            _terminate_process_group(session.process, force=False)
            try:
                await asyncio.wait_for(session.process.wait(), timeout=2.0)
            except TimeoutError:
                _terminate_process_group(session.process, force=True)
                await session.process.wait()
            if session.exit_code is None:
                session.exit_code = session.process.returncode
        for task in session._pumps:
            task.cancel()
        return session

    async def close_all(self) -> None:
        """回收全部会话（运行结束时调用）。"""
        for name in list(self._sessions):
            await self.close(name)

    async def reap_idle(self) -> None:
        """惰性回收空闲超时会话（在每次 start 时检查）。"""
        now = time.monotonic()
        for name, session in list(self._sessions.items()):
            if now - session.last_activity > self._idle_timeout:
                await self.close(name)


__all__ = ["SessionRead", "ShellSessionManager"]
