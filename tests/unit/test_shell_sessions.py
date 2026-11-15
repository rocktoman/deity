"""ADR-019 B1：持久 shell 会话——增量输出、stdin、跨调用存活与回收。"""

import asyncio
import socket
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from deity.adapters.execution.session_process import ShellSessionManager
from deity.adapters.tools import LocalToolDispatcher, ShellArgs
from deity.core.contracts import AgentRequest, ToolCall

TICKER = (
    "import sys, time\n"
    "i = 0\n"
    "while True:\n"
    "    i += 1\n"
    "    print(f'tick {i}', flush=True)\n"
    "    time.sleep(0.05)\n"
)
ECHO = "import sys\nfor line in sys.stdin:\n    print(f'echo: {line.strip()}', flush=True)\n"


def _request(tmp_path: Path) -> AgentRequest:
    return AgentRequest("s", "t", str(tmp_path), "x", "fake")


def _dispatcher(**kwargs: object) -> LocalToolDispatcher:
    return LocalToolDispatcher(approve=lambda name, args: True, **kwargs)  # type: ignore[arg-type]


async def _shell(
    dispatcher: LocalToolDispatcher, tmp_path: Path, call_id: str, **args: object
) -> object:
    return await dispatcher.dispatch(ToolCall(call_id, "shell", dict(args)), _request(tmp_path))


def _write_script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return str(script)


def test_shell_args_shapes() -> None:
    assert ShellArgs(argv=["ls"]).session is None
    assert ShellArgs(session="s", stdin="x\n").argv == []
    with pytest.raises(ValidationError):
        ShellArgs()
    with pytest.raises(ValidationError):
        ShellArgs(argv=["ls"], stdin="x")
    with pytest.raises(ValidationError):
        ShellArgs(session="s", close=True, argv=["ls"])


@pytest.mark.asyncio
async def test_one_shot_shell_unchanged(tmp_path: Path) -> None:
    dispatcher = _dispatcher()
    result = await _shell(
        dispatcher,
        tmp_path,
        "c1",
        argv=[sys.executable, "-c", "print('hi')"],
    )
    assert result.status == "ok" and "hi" in result.output


@pytest.mark.asyncio
async def test_session_incremental_output_and_close(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "ticker.py", TICKER)
    dispatcher = _dispatcher()
    first = await _shell(
        dispatcher,
        tmp_path,
        "c1",
        argv=[sys.executable, "-u", script],
        session="ticker",
        yield_ms=400,
    )
    assert first.status == "ok"
    assert "started pid=" in first.output and "running" in first.output
    assert "tick 1" in first.output
    second = await _shell(
        dispatcher,
        tmp_path,
        "c2",
        session="ticker",
        yield_ms=300,
    )
    assert second.status == "ok"
    # 增量：第二段不重复第一段已返回的行。
    first_ticks = {line for line in first.output.splitlines() if line.startswith("tick")}
    second_ticks = {line for line in second.output.splitlines() if line.startswith("tick")}
    assert second_ticks and not (first_ticks & second_ticks)
    closed = await _shell(dispatcher, tmp_path, "c3", session="ticker", close=True)
    assert "closed" in closed.output
    gone = await _shell(dispatcher, tmp_path, "c4", session="ticker")
    assert gone.status == "error" and "not found" in gone.output


@pytest.mark.asyncio
async def test_session_stdin_roundtrip(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "echo.py", ECHO)
    dispatcher = _dispatcher()
    await _shell(
        dispatcher,
        tmp_path,
        "c1",
        argv=[sys.executable, "-u", script],
        session="repl",
        yield_ms=200,
    )
    result = await _shell(
        dispatcher,
        tmp_path,
        "c2",
        session="repl",
        stdin="hello\n",
        yield_ms=500,
    )
    assert "echo: hello" in result.output
    await _shell(dispatcher, tmp_path, "c3", session="repl", close=True)


@pytest.mark.asyncio
async def test_session_exit_code_reported(tmp_path: Path) -> None:
    dispatcher = _dispatcher()
    result = await _shell(
        dispatcher,
        tmp_path,
        "c1",
        argv=[sys.executable, "-c", "print('bye'); raise SystemExit(3)"],
        session="once",
        yield_ms=3000,
    )
    assert result.status == "error" and result.exit_code == 3
    assert "bye" in result.output and "exited 3" in result.output


@pytest.mark.asyncio
async def test_session_start_duplicate_and_unknown(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "ticker.py", TICKER)
    dispatcher = _dispatcher()
    await _shell(
        dispatcher,
        tmp_path,
        "c1",
        argv=[sys.executable, "-u", script],
        session="dup",
        yield_ms=100,
    )
    again = await _shell(
        dispatcher,
        tmp_path,
        "c2",
        argv=[sys.executable, "-u", script],
        session="dup",
        yield_ms=100,
    )
    assert again.status == "error" and "already running" in again.output
    missing = await _shell(dispatcher, tmp_path, "c3", session="nope")
    assert missing.status == "error" and "not found" in missing.output
    await _shell(dispatcher, tmp_path, "c4", session="dup", close=True)


@pytest.mark.asyncio
async def test_poll_is_approval_exempt_but_start_and_stdin_are_not(
    tmp_path: Path,
) -> None:
    script = _write_script(tmp_path, "ticker.py", TICKER)
    manager = ShellSessionManager()
    owner = _dispatcher(session_manager=manager)
    await _shell(
        owner,
        tmp_path,
        "c1",
        argv=[sys.executable, "-u", script],
        session="shared",
        yield_ms=150,
    )
    stranger = LocalToolDispatcher(approve=None, session_manager=manager)
    polled = await _shell(stranger, tmp_path, "c2", session="shared", yield_ms=150)
    assert polled.status == "ok"
    started = await _shell(
        stranger,
        tmp_path,
        "c3",
        argv=[sys.executable, "-c", "pass"],
        session="other",
    )
    assert started.status == "denied"
    written = await _shell(stranger, tmp_path, "c4", session="shared", stdin="x\n")
    assert written.status == "denied"
    await manager.close_all()


@pytest.mark.asyncio
async def test_aclose_reaps_sessions(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "ticker.py", TICKER)
    manager = ShellSessionManager()
    dispatcher = _dispatcher(session_manager=manager)
    await _shell(
        dispatcher,
        tmp_path,
        "c1",
        argv=[sys.executable, "-u", script],
        session="t",
        yield_ms=100,
    )
    session = manager.get("t")
    assert session is not None and session.running
    await dispatcher.aclose()
    assert manager.names() == ()
    assert session.process.returncode is not None


@pytest.mark.asyncio
async def test_idle_reap_on_next_start(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "ticker.py", TICKER)
    manager = ShellSessionManager(idle_timeout_seconds=0.05)
    await manager.start("old", (sys.executable, "-u", script), tmp_path)
    await asyncio.sleep(0.1)
    await manager.start("new", (sys.executable, "-u", script), tmp_path)
    assert manager.names() == ("new",)
    await manager.close_all()


@pytest.mark.asyncio
async def test_dev_server_end_to_end(tmp_path: Path) -> None:
    """验收：会话里跑 dev server，另一个一次性命令能读到它服务的内容。"""
    (tmp_path / "hello.txt").write_text("hello from session server", encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    dispatcher = _dispatcher()
    started = await _shell(
        dispatcher,
        tmp_path,
        "c1",
        argv=[sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        session="web",
        yield_ms=800,
    )
    assert "running" in started.output
    fetch = (
        "import urllib.request\n"
        f"print(urllib.request.urlopen('http://127.0.0.1:{port}/hello.txt', timeout=5).read().decode())\n"
    )
    body = ""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = await _shell(
            dispatcher,
            tmp_path,
            f"f{int(time.monotonic() * 10)}",
            argv=[sys.executable, "-c", fetch],
        )
        if result.status == "ok":
            body = result.output
            break
        await asyncio.sleep(0.3)
    assert "hello from session server" in body
    closed = await _shell(dispatcher, tmp_path, "c9", session="web", close=True)
    assert "closed" in closed.output
