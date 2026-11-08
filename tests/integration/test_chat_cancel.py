"""Phase 2：运行态两阶段 Ctrl-C（子进程 + SIGINT，真信号验证）。

第一次 Ctrl-C 取消本轮并回到提示符（cancel() 被调用）；
取消清理窗口内第二次 Ctrl-C 退出 CLI，退出码 130。
仅 POSIX：Windows 上 SIGINT 进程语义不同，见 skip 原因。
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

CHILD = Path(__file__).parents[1] / "fixtures" / "chat_cancel_child.py"
ROOT = Path(__file__).parents[2]


def _spawn() -> tuple[subprocess.Popen[str], list[str], threading.Thread]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["TERM"] = "xterm"
    proc = subprocess.Popen(
        [sys.executable, str(CHILD)],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    output: list[str] = []

    def _read() -> None:
        # 逐字符读：流式文本没有换行，按行读会一直等不到它。
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            output.append(chunk)

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    return proc, output, thread


def _wait_for(output: list[str], needle: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in "".join(output):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT 进程测试仅在 POSIX 上验证")
def test_first_ctrl_c_cancels_run_and_returns_to_prompt() -> None:
    proc, output, _thread = _spawn()
    try:
        assert proc.stdin is not None
        proc.stdin.write("慢任务\n")
        proc.stdin.flush()
        assert _wait_for(output, "slow-start"), "".join(output)
        os.kill(proc.pid, signal.SIGINT)
        proc.stdin.close()
        code = proc.wait(timeout=20)
        text = "".join(output)
        assert code == 0, text
        assert "已取消" in text, text
        assert "slow-start" in text
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT 进程测试仅在 POSIX 上验证")
def test_second_ctrl_c_during_cancel_exits_130() -> None:
    proc, output, _thread = _spawn()
    try:
        assert proc.stdin is not None
        proc.stdin.write("慢任务\n")
        proc.stdin.flush()
        assert _wait_for(output, "slow-start"), "".join(output)
        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.5)
        assert proc.poll() is None, "".join(output)
        os.kill(proc.pid, signal.SIGINT)
        code = proc.wait(timeout=20)
        assert code == 130, "".join(output)
    finally:
        if proc.poll() is None:
            proc.kill()
