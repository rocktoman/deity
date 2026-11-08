"""Phase 2 取消测试的子进程入口：用慢速 FakeService 跑交互 CLI。

由 tests/integration/test_chat_cancel.py 以子进程启动：
- stdin 管道输入一条慢任务，随后父进程发 SIGINT；
- 第一次 SIGINT 应取消本轮并回到提示符（随后 EOF 退出 0）；
- 取消清理窗口为 2 秒，窗口内第二次 SIGINT 应退出 130。
强制流式（绕过 TTY 检测），只为验证取消语义。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from deity.application.config import CodingConfig, Config, ModelConfig
from deity.core.contracts import AgentResult, RunStatus
from deity.core.contracts.client import ClientEvent
from deity.interfaces.cli import main as cli
from deity.interfaces.cli.chat_session import ChatSession


class SlowService:
    """慢速假服务：run_stream 先吐一个增量，随后等取消信号。"""

    coding = CodingConfig()
    workspace = Path.cwd()

    def __init__(self) -> None:
        self.cancelled = asyncio.Event()
        self.cancel_calls: list[str] = []

    def cancel(self, run_id: str) -> bool:
        """记录取消调用并唤醒 run_stream。"""
        self.cancel_calls.append(run_id)
        self.cancelled.set()
        return True

    async def run(self, task: str, **kwargs: object) -> AgentResult:
        return AgentResult("r", RunStatus.COMPLETED, "ok")

    async def run_stream(self, task: str, **kwargs: object):  # type: ignore[no-untyped-def]
        yield ClientEvent(seq=1, run_id="r-slow", kind="text_delta", payload={"text": "slow-start"})
        try:
            await asyncio.wait_for(self.cancelled.wait(), timeout=30)
        except TimeoutError:
            pass
        # 取消清理窗口：给第二次 Ctrl-C 留出确定性的到达时间。
        await asyncio.sleep(2)
        yield ClientEvent(
            seq=2,
            run_id="r-slow",
            kind="run_status",
            payload={"status": "cancelled", "final_text": None},
        )


def main() -> None:
    config = Config(model=ModelConfig("openai_compatible", "fake", "FAKE_KEY"))
    cli.load_config = lambda path: config  # type: ignore[assignment]
    cli.RunService.from_config = staticmethod(lambda *args, **kwargs: SlowService())  # type: ignore[assignment]
    ChatSession._use_streaming = lambda self: True  # type: ignore[assignment]
    cli.app(["run"])


if __name__ == "__main__":
    main()
