"""Phase 2 能力测试（ADR-025）：reasoning_delta 思考流、/undo & /rewind、.deity/commands/*.md。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deity.adapters.model.anthropic import AnthropicProvider
from deity.adapters.model.openai_compatible import OpenAICompatibleProvider
from deity.adapters.storage import SQLiteSessionRepository
from deity.application.config import PermissionConfig
from deity.application.custom_commands import CustomCommandCatalog
from deity.application.rewind_service import RewindHistory
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent
from deity.interfaces.cli.chat_approval import make_approve
from deity.interfaces.tui.approval_bridge import TuiApprovalBridge
from deity.interfaces.tui.interactive import ChatInput, InteractiveApp
from deity.interfaces.tui.widgets import ThinkingBlock


class _ReasoningModel:
    def __init__(self, turns: list[list[ModelEvent]]) -> None:
        self.turns = iter(turns)
        self.received_prompts: list[str] = []

    async def stream(self, request: Any):  # type: ignore[no-untyped-def]
        if getattr(request, "messages", None):
            self.received_prompts.append(str(request.messages[-1].get("content", "")))
        for event in next(self.turns):
            yield event


async def _wait_until(predicate: Any, timeout: float = 10.0) -> None:
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return
        await asyncio.sleep(0.05)
        elapsed += 0.05
    raise AssertionError("等待超时")


@pytest.mark.asyncio
async def test_openai_and_anthropic_adapters_emit_reasoning_delta() -> None:
    async def _openai_raw():  # type: ignore[no-untyped-def]
        yield SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        reasoning_content="先分析", content=None, tool_calls=None
                    ),
                    finish_reason=None,
                )
            ],
        )
        yield SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        reasoning_content=None, content="最终结论", tool_calls=None
                    ),
                    finish_reason="stop",
                )
            ],
        )

    openai_provider = OpenAICompatibleProvider(api_key="test", model="deepseek")
    openai_events = [ev async for ev in openai_provider.parse(_openai_raw())]
    assert any(ev.kind == "reasoning_delta" and ev.text == "先分析" for ev in openai_events)
    assert any(ev.kind == "text_delta" and ev.text == "最终结论" for ev in openai_events)

    async def _anthropic_raw():  # type: ignore[no-untyped-def]
        yield SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking="思考步骤"),
        )
        yield SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="回答正文"),
        )
        yield SimpleNamespace(type="message_stop")

    anthropic_provider = AnthropicProvider(api_key="test", model="claude")
    anthropic_events = [ev async for ev in anthropic_provider.parse(_anthropic_raw())]
    assert any(ev.kind == "reasoning_delta" and ev.text == "思考步骤" for ev in anthropic_events)
    assert any(ev.kind == "text_delta" and ev.text == "回答正文" for ev in anthropic_events)


def test_custom_command_catalog_priority_and_render(tmp_path: Path) -> None:
    user_dir = tmp_path / "user_cmds"
    proj_dir = tmp_path / ".deity" / "commands"
    user_dir.mkdir(parents=True)
    proj_dir.mkdir(parents=True)

    (user_dir / "deploy.md").write_text("# 用户部署\n运行用户部署 {{args}}", encoding="utf-8")
    (proj_dir / "deploy.md").write_text(
        "---\ndescription: 项目发布流程\n---\n检查并发布 $ARGUMENTS",
        encoding="utf-8",
    )
    (proj_dir / "audit.md").write_text("# 代码审计\n检查安全隐患", encoding="utf-8")
    (proj_dir / "bad name!.md").write_text("ignored", encoding="utf-8")

    catalog = CustomCommandCatalog(tmp_path, user_dir=user_dir)
    cmds = catalog.list_commands()
    assert [c.name for c in cmds] == ["audit", "deploy"]

    deploy = catalog.get("/deploy")
    assert deploy is not None
    assert deploy.source == "project"
    assert deploy.description == "项目发布流程"
    assert deploy.render("v1.2") == "检查并发布 v1.2"

    audit = catalog.get("audit")
    assert audit is not None
    assert "附加参数：src/" in audit.render("src/")


def test_rewind_history_restores_and_deletes_files(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("v1\n", encoding="utf-8")
    created = tmp_path / "new.py"

    history = RewindHistory(tmp_path)
    p1 = history.record(
        session_id="s1",
        run_id="r1",
        prompt="修改 app.py",
        message_count_before=0,
        file_backups={"app.py": b"v1\n"},
    )
    target.write_text("v2\n", encoding="utf-8")

    history.record(
        session_id="s1",
        run_id="r2",
        prompt="新建 new.py",
        message_count_before=2,
        file_backups={"new.py": None},
    )
    created.write_text("fresh\n", encoding="utf-8")

    res1 = history.undo_last("s1")
    assert res1 is not None
    assert res1.points_reverted == 1
    assert res1.deleted_files == ("new.py",)
    assert not created.exists()
    assert target.read_text(encoding="utf-8") == "v2\n"

    res2 = history.rewind_to("s1", p1.point_id)
    assert res2 is not None
    assert res2.restored_files == ("app.py",)
    assert target.read_text(encoding="utf-8") == "v1\n"
    assert history.list_points("s1") == ()


@pytest.mark.asyncio
async def test_tui_thinking_block_undo_and_custom_command_e2e(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    cmd_dir = tmp_path / ".deity" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "quickfix.md").write_text("# 快速修复\n请修复 {{args}}", encoding="utf-8")

    turns = [
        [
            ModelEvent("reasoning_delta", text="正在推导修复步骤"),
            ModelEvent("tool_call_started", call_id="c1", name="write_file"),
            ModelEvent(
                "tool_call_arguments_delta",
                call_id="c1",
                text='{"path": "fixed.txt", "content": "ok"}',
            ),
            ModelEvent("message_completed"),
        ],
        [
            ModelEvent("text_delta", text="已写入 fixed.txt"),
            ModelEvent("message_completed"),
        ],
    ]
    model = _ReasoningModel(turns)
    bridge = TuiApprovalBridge(workspace=tmp_path)
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,  # type: ignore[arg-type]
        approve=make_approve(bridge.controller),
        permission=PermissionConfig(shell="ask", workspace_write="auto"),
    )
    app = InteractiveApp(service, bridge=bridge, model_name="fake", workspace=tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert any(cmd == "/quickfix" for cmd, _ in app.all_slash_commands())

        box = app.query_one("#input", ChatInput)
        box.text = "/quickfix login.py"
        box.focus()
        await pilot.press("enter")
        await _wait_until(lambda: not app._run_active and (tmp_path / "fixed.txt").exists())
        await pilot.pause()

        assert "请修复 login.py" in model.received_prompts[0]
        blocks = list(app.query(ThinkingBlock))
        assert len(blocks) == 1
        assert blocks[0].content == "正在推导修复步骤"
        assert blocks[0].collapsed is True

        # 验证 /rewind 列出检查点，/undo 删除新建文件并回滚消息。
        box.text = "/rewind"
        await pilot.press("enter")
        await pilot.pause()
        assert len(app.rewind_history.list_points(app.state.session_id)) == 1

        box.text = "/undo"
        await pilot.press("enter")
        await _wait_until(lambda: not (tmp_path / "fixed.txt").exists())
        await pilot.pause()

        repo = await SQLiteSessionRepository.open(service.db_path)
        try:
            assert await repo.messages(app.state.session_id) == []
        finally:
            await repo.close()
        await pilot.press("ctrl+q")
