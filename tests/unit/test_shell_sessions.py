"""ADR-019 B1：持久 shell 会话——增量输出、stdin、跨调用存活与回收。"""

import asyncio
import os
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
async def test_subprocess_env_keeps_platform_essentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归（F2 CI macOS/Windows 失败根因）：最小环境必须保留平台必需变量。

    Windows 缺 SYSTEMROOT 时 Winsock 报 WinError 10106、联网子进程全灭；
    同时秘密类变量不得透传。一并覆盖一次性命令与持久会话两条路径。
    """
    essential = "SYSTEMROOT" if os.name == "nt" else "HOME"
    if essential not in os.environ:
        pytest.skip(f"{essential} not set in this environment")
    monkeypatch.setenv("DEITY_TEST_SECRET", "must-not-leak")
    probe = (
        "import os\n"
        f"print('essential', {essential!r} in os.environ)\n"
        "print('secret', 'DEITY_TEST_SECRET' in os.environ)\n"
    )
    dispatcher = _dispatcher()
    one_shot = await _shell(dispatcher, tmp_path, "e1", argv=[sys.executable, "-c", probe])
    assert "essential True" in one_shot.output
    assert "secret False" in one_shot.output
    started = await _shell(
        dispatcher,
        tmp_path,
        "e2",
        argv=[sys.executable, "-c", probe],
        session="envprobe",
        yield_ms=3000,
    )
    assert "essential True" in started.output
    assert "secret False" in started.output


@pytest.mark.asyncio
async def test_dev_server_end_to_end(tmp_path: Path) -> None:
    """验收：会话里跑 dev server，另一个一次性命令能读到它服务的内容。"""
    (tmp_path / "hello.txt").write_text("hello from session server", encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    dispatcher = _dispatcher()
    # 不用 python -m http.server：HTTPServer.server_bind 会调用
    # socket.getfqdn()，在 macOS CI runner 上该调用可阻塞数十秒
    # （主机名不可解析），导致 socket 已绑定却迟迟不 listen、
    # 取数全部超时。改用 TCPServer（其 server_bind 不做 fqdn）
    # + SimpleHTTPRequestHandler 的等价内联服务脚本。
    server_script = (
        "import functools, http.server, socketserver, sys\n"
        "handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=sys.argv[2])\n"
        "socketserver.ThreadingTCPServer.allow_reuse_address = True\n"
        "server = socketserver.ThreadingTCPServer(('127.0.0.1', int(sys.argv[1])), handler)\n"
        "server.serve_forever()\n"
    )
    started = await _shell(
        dispatcher,
        tmp_path,
        "c1",
        argv=[sys.executable, "-c", server_script, str(port), str(tmp_path)],
        session="web",
        yield_ms=800,
    )
    assert "running" in started.output
    fetch = (
        "import urllib.request\n"
        f"print(urllib.request.urlopen('http://127.0.0.1:{port}/hello.txt', timeout=10).read().decode())\n"
    )
    # 轮询等待式断言：慢 runner 上解释器启动 + bind 可能数秒，45 秒内
    # 持续重试真实 HTTP 取数；会话若已退出则立即失败并带出其输出。
    body = ""
    last_output = ""
    deadline = time.monotonic() + 45
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        result = await _shell(
            dispatcher,
            tmp_path,
            f"f{attempt}",
            argv=[sys.executable, "-c", fetch],
        )
        last_output = result.output
        if result.status == "ok":
            body = result.output
            break
        poll = await _shell(dispatcher, tmp_path, f"p{attempt}", session="web")
        if "exited" in poll.output or "no session" in poll.output:
            raise AssertionError(f"dev server session died early: {poll.output}")
        await asyncio.sleep(0.3)
    if "hello from session server" not in body:
        final_poll = await _shell(dispatcher, tmp_path, "p-final", session="web")
        raise AssertionError(
            f"fetch never succeeded: {last_output}; session state: {final_poll.output}"
        )
    closed = await _shell(dispatcher, tmp_path, "c9", session="web", close=True)
    assert "closed" in closed.output
