"""Phase 3：模式切换——普通/计划/自动接受（approval_mode 状态机）。"""

import builtins
import io
from pathlib import Path

import pytest
from rich.console import Console

from deity.application.config import ChatConfig, CodingConfig, PermissionConfig
from deity.core.contracts import AgentResult, RunStatus
from deity.interfaces.cli.chat_session import ChatSession


class FakeService:
    coding = CodingConfig(plan_build_enabled=True, git_guard_enabled=True)
    permission = PermissionConfig(shell="ask", workspace_write="ask")
    provider_id: str | None = None

    def __init__(self, tmp_path: Path) -> None:
        self.workspace = tmp_path
        self.db_path: Path | None = None

    async def run(
        self, task: str, *, model_name: str, session_id: str | None = None, **kwargs: object
    ) -> AgentResult:
        return AgentResult("run-1", RunStatus.COMPLETED, "reply")

    def skills_list(self) -> list[dict[str, str]]:
        return []

    def skills_inspect(self, name: str) -> dict[str, str]:
        return {"name": name, "source": "project", "description": "d", "path": "s.md"}

    def mcp_list(self) -> list[str]:
        return []


async def _run_session(service: FakeService, inputs: list[str]) -> tuple[str, ChatSession]:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=100)
    session = ChatSession(service, "fake", None, config=ChatConfig(), output=console)
    script = iter(inputs)
    old_input = builtins.input
    builtins.input = lambda prompt_text="": next(script)  # type: ignore[assignment]
    try:
        await session.run()
    finally:
        builtins.input = old_input
    return buffer.getvalue(), session


@pytest.mark.asyncio
async def test_auto_requires_explicit_confirmation(tmp_path: Path) -> None:
    output, session = await _run_session(FakeService(tmp_path), ["/auto", "no", "/exit"])
    assert "未进入自动接受模式" in output
    assert session.approval.mode == "ask"


@pytest.mark.asyncio
async def test_auto_enter_and_exit_via_normal(tmp_path: Path) -> None:
    output, session = await _run_session(
        FakeService(tmp_path),
        ["/auto", "yes", "/status", "/normal", "/exit"],
    )
    assert "已进入自动接受模式" in output
    assert "模式：自动接受" in output
    assert "已退出自动接受模式" in output
    assert "已返回普通模式" in output
    assert session.approval.mode == "ask"


@pytest.mark.asyncio
async def test_plan_and_auto_exit_together_on_normal(tmp_path: Path) -> None:
    output, session = await _run_session(
        FakeService(tmp_path),
        ["/plan", "/auto", "yes", "/normal", "/exit"],
    )
    assert "已进入计划模式" in output
    assert session.plan_mode is False
    assert session.approval.mode == "ask"


@pytest.mark.asyncio
async def test_auto_toggle_off_when_already_auto(tmp_path: Path) -> None:
    output, session = await _run_session(
        FakeService(tmp_path),
        ["/auto", "yes", "/auto", "/exit"],
    )
    assert output.count("已进入自动接受模式") == 1
    assert "已退出自动接受模式" in output
    assert session.approval.mode == "ask"
