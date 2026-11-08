"""Phase 3：新命令（/resume /sessions /compact /clear /cost /model /status /auto /doctor）
与 ! shell 直通的集成测试。"""

import builtins
import io
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from deity.application.config import ChatConfig, CodingConfig, PermissionConfig, RunConfig
from deity.application.run_service import RunService
from deity.core.contracts import AgentResult, ModelEvent, RunStatus
from deity.interfaces.cli.chat_session import ChatSession


class FakeService:
    """命令测试替身：只提供 ChatSession/ChatContext 触及的属性。"""

    def __init__(
        self,
        tmp_path: Path,
        *,
        coding_enabled: bool = False,
        db_path: Path | None = None,
        permission: PermissionConfig | None = None,
        limits: RunConfig | None = None,
    ) -> None:
        self.workspace = tmp_path
        self.db_path = tmp_path / "deity.db" if db_path is None else db_path
        self.coding = CodingConfig(
            plan_build_enabled=coding_enabled, git_guard_enabled=coding_enabled
        )
        self.permission = permission or PermissionConfig(shell="ask", workspace_write="ask")
        self.limits = limits or RunConfig(compaction_enabled=False)
        self.provider_id: str | None = None
        self.runs: list[str] = []

    async def run(
        self, task: str, *, model_name: str, session_id: str | None = None, **kwargs: Any
    ) -> AgentResult:
        self.runs.append(task)
        return AgentResult("run-1", RunStatus.COMPLETED, "reply")

    async def resume(self, run_id: str) -> AgentResult:
        return AgentResult(run_id, RunStatus.COMPLETED, "已恢复：继续执行")

    def skills_list(self) -> list[dict[str, str]]:
        return []

    def skills_inspect(self, name: str) -> dict[str, str]:
        return {"name": name, "source": "project", "description": "d", "path": "s.md"}

    def mcp_list(self) -> list[str]:
        return []


class FakeModel:
    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        yield ModelEvent("text_delta", text="hello")
        yield ModelEvent("usage", input_tokens=5, output_tokens=2)
        yield ModelEvent("message_completed")


def _real_service(tmp_path: Path, *, shell: str = "ask", approve=None) -> RunService:
    return RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=FakeModel(),  # type: ignore[arg-type]
        permission=PermissionConfig(shell=shell, workspace_write="ask"),
        approve=approve,
    )


async def _run_session(
    service: Any, inputs: list[str], tmp_path: Path, *, session_id: str | None = None
) -> tuple[str, ChatSession]:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=100)
    session = ChatSession(service, "fake", session_id, config=ChatConfig(), output=console)
    script = iter(inputs)
    old_input = builtins.input
    builtins.input = lambda prompt_text="": next(script)  # type: ignore[assignment]
    try:
        await session.run()
    finally:
        builtins.input = old_input
    return buffer.getvalue(), session


@pytest.mark.asyncio
async def test_cost_without_usage_says_not_connected(tmp_path: Path) -> None:
    output, _ = await _run_session(FakeService(tmp_path), ["/cost", "/exit"], tmp_path)
    assert "token 未接通" in output


@pytest.mark.asyncio
async def test_cost_after_task_counts_round(tmp_path: Path) -> None:
    output, _ = await _run_session(FakeService(tmp_path), ["任务", "/cost", "/exit"], tmp_path)
    assert "轮次 1" in output
    assert "token 未接通" in output


@pytest.mark.asyncio
async def test_model_show_and_switch(tmp_path: Path) -> None:
    output, session = await _run_session(
        FakeService(tmp_path), ["/model", "/model fake-x", "/model", "/exit"], tmp_path
    )
    assert "当前模型：fake" in output
    assert "已切换模型：fake-x" in output
    assert "当前模型：fake-x" in output
    assert session.model_name == "fake-x"


@pytest.mark.asyncio
async def test_status_shows_model_mode_rounds_and_context(tmp_path: Path) -> None:
    output, _ = await _run_session(FakeService(tmp_path), ["/status", "/exit"], tmp_path)
    assert "模型：fake" in output
    assert "模式：普通" in output
    assert "会话：" in output
    assert "轮次：0" in output
    assert "上下文消息数：0" in output


@pytest.mark.asyncio
async def test_clear_prints_marker(tmp_path: Path) -> None:
    output, _ = await _run_session(FakeService(tmp_path), ["/clear", "/exit"], tmp_path)
    assert "已清屏。" in output


@pytest.mark.asyncio
async def test_compact_disabled_reports_no_standalone_entry(tmp_path: Path) -> None:
    output, _ = await _run_session(FakeService(tmp_path), ["/compact", "/exit"], tmp_path)
    assert "未开启" in output
    assert "没有独立的触发入口" in output


@pytest.mark.asyncio
async def test_compact_enabled_reports_automatic(tmp_path: Path) -> None:
    service = FakeService(tmp_path, limits=RunConfig(compaction_enabled=True))
    output, _ = await _run_session(service, ["/compact", "/exit"], tmp_path)
    assert "已开启" in output
    assert "自动执行" in output


@pytest.mark.asyncio
async def test_sessions_without_storage_reports_hint(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    service.db_path = None
    output, _ = await _run_session(service, ["/sessions", "/exit"], tmp_path)
    assert "未提供会话存储" in output


@pytest.mark.asyncio
async def test_sessions_lists_and_switches(tmp_path: Path) -> None:
    service = _real_service(tmp_path)
    await service.run("先跑一轮", model_name="fake", session_id="s1")
    output, session = await _run_session(
        service, ["/sessions", "1", "/exit"], tmp_path, session_id="other"
    )
    assert "已切换到会话" in output
    assert session.session_id == "s1"
    assert session.plan_mode is False


@pytest.mark.asyncio
async def test_resume_with_arg_uses_service(tmp_path: Path) -> None:
    output, _ = await _run_session(FakeService(tmp_path), ["/resume run-9", "/exit"], tmp_path)
    assert "已恢复：继续执行" in output


@pytest.mark.asyncio
async def test_resume_no_arg_lists_recent_runs(tmp_path: Path) -> None:
    service = _real_service(tmp_path)
    result = await service.run("先跑一轮", model_name="fake", session_id="s1")
    output, _ = await _run_session(service, ["/resume", "/exit"], tmp_path, session_id="s1")
    assert result.run_id[:8] in output
    assert "不可恢复" in output


@pytest.mark.asyncio
async def test_resume_unknown_run_id_reports_failure(tmp_path: Path) -> None:
    service = _real_service(tmp_path)
    output, _ = await _run_session(service, ["/resume no-such-run", "/exit"], tmp_path)
    assert "恢复失败" in output


@pytest.mark.asyncio
async def test_doctor_prints_checks(tmp_path: Path) -> None:
    service = FakeService(tmp_path)
    output, _ = await _run_session(service, ["/doctor", "/exit"], tmp_path)
    assert "python" in output


@pytest.mark.asyncio
async def test_shell_passthrough_runs_command(tmp_path: Path) -> None:
    approvals: list[tuple[str, dict[str, Any]]] = []

    def approve(name: str, args: dict[str, Any]) -> bool:
        approvals.append((name, args))
        return True

    service = _real_service(tmp_path, shell="ask", approve=approve)
    output, _ = await _run_session(service, ["!echo hello", "/exit"], tmp_path)
    assert "hello" in output
    assert approvals and approvals[0][0] == "shell"
    assert approvals[0][1]["command"] == "echo hello"


@pytest.mark.asyncio
async def test_shell_passthrough_escaping_path_refused(tmp_path: Path) -> None:
    approvals: list[str] = []

    def approve(name: str, args: dict[str, Any]) -> bool:
        approvals.append(name)
        return True

    service = _real_service(tmp_path, shell="ask", approve=approve)
    output, _ = await _run_session(service, ["!cat ../secret.txt", "/exit"], tmp_path)
    assert "已拒绝" in output
    assert "工作区外" in output
    assert approvals == []


@pytest.mark.asyncio
async def test_shell_passthrough_deny_policy_refused(tmp_path: Path) -> None:
    service = _real_service(tmp_path, shell="deny", approve=lambda n, a: True)
    output, _ = await _run_session(service, ["!echo hi", "/exit"], tmp_path)
    assert "已拒绝" in output
    assert "shell=deny" in output


@pytest.mark.asyncio
async def test_shell_passthrough_auto_allows_safe_command(tmp_path: Path) -> None:
    service = _real_service(tmp_path, shell="auto")
    output, _ = await _run_session(service, ["!echo hi", "/exit"], tmp_path)
    assert "hi" in output


@pytest.mark.asyncio
async def test_shell_passthrough_empty_is_ignored(tmp_path: Path) -> None:
    service = _real_service(tmp_path)
    output, _ = await _run_session(service, ["!", "/exit"], tmp_path)
    assert "已拒绝" not in output
