"""全屏交互工作台（ADR-022）：Pilot 驱动的端到端行为测试。

覆盖：启动/退出、输入提交跑完 FakeModel Run、界面内批准写文件、
拒绝不写、会话允许与 shell 命令模式允许、取消、状态栏关键字段、
斜杠命令子集、--task 初始任务。真实模型验证为 NOT_RUN（无密钥
通道），行为验证全部用测试替身，与仓库测试规则一致。
"""

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deity.application.config import PermissionConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent
from deity.interfaces.cli import main as cli_main
from deity.interfaces.cli.chat_approval import make_approve
from deity.interfaces.cli.main import app as cli_app
from deity.interfaces.tui.approval_bridge import TuiApprovalBridge
from deity.interfaces.tui.interactive import ChatInput, InteractiveApp


class FakeModel:
    def __init__(self, turns: list[list[ModelEvent]], delay: float = 0.0) -> None:
        self.turns = iter(turns)
        self.delay = delay

    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        for event in next(self.turns):
            yield event


class HangingModel:
    """先吐一段文本，然后在同一轮里挂起（取消测试用）。"""

    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        yield ModelEvent("text_delta", text="开始")
        await asyncio.sleep(60)
        yield ModelEvent("message_completed")


def _write_turns(path: str, content: str, call_id: str = "c1") -> list[list[ModelEvent]]:
    return [
        [
            ModelEvent("tool_call_started", call_id=call_id, name="write_file"),
            ModelEvent(
                "tool_call_arguments_delta",
                call_id=call_id,
                text=f'{{"path": "{path}", "content": "{content}"}}',
            ),
            ModelEvent("message_completed"),
        ],
        [ModelEvent("text_delta", text="done"), ModelEvent("message_completed")],
    ]


def _make_app(
    tmp_path: Path,
    model: object,
    *,
    context_window_tokens: int | None = None,
    initial_task: str | None = None,
) -> InteractiveApp:
    bridge = TuiApprovalBridge(workspace=tmp_path)
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,  # type: ignore[arg-type]
        approve=make_approve(bridge.controller),
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
        context_window_tokens=context_window_tokens,
    )
    return InteractiveApp(
        service,
        bridge=bridge,
        model_name="fake",
        workspace=tmp_path,
        initial_task=initial_task,
    )


async def _wait_until(predicate, timeout: float = 20.0) -> None:  # type: ignore[no-untyped-def]
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return
        await asyncio.sleep(0.05)
        elapsed += 0.05
    raise AssertionError("等待超时")


async def _submit(app: InteractiveApp, pilot, text: str) -> None:  # type: ignore[no-untyped-def]
    box = app.query_one("#input", ChatInput)
    box.text = text
    box.focus()
    await pilot.press("enter")
    await pilot.pause()


def _conversation_text(app: InteractiveApp) -> str:
    parts: list[str] = []
    for widget in app.query_one("#conversation").children:
        parts.append(str(widget.render()))
        for child in widget.children:
            parts.append(str(child.render()))
    return "\n".join(parts)


def _status_bar(app: InteractiveApp) -> str:
    return str(app.query_one("#status-bar").render())


@pytest.mark.asyncio
async def test_interactive_starts_and_quits(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.focused, ChatInput)
        assert "欢迎来到 Deity" in _conversation_text(app)
        bar = _status_bar(app)
        assert "fake" in bar
        assert str(tmp_path) in bar
        assert "空闲" in bar
        await pilot.press("ctrl+q")
    assert app.is_running is False


@pytest.mark.asyncio
async def test_interactive_submit_completes_run(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(
        tmp_path,
        FakeModel([[ModelEvent("text_delta", text="你好，世界"), ModelEvent("message_completed")]]),
    )
    async with app.run_test() as pilot:
        await _submit(app, pilot, "打个招呼")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        assert app._view is not None
        assert "你好，世界" in app._view.text
        assert "你好，世界" in _conversation_text(app)
        assert "❯ 打个招呼" in _conversation_text(app)
        assert app.session_status.turns == 1
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_initial_task_auto_runs(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(
        tmp_path,
        FakeModel([[ModelEvent("text_delta", text="自动完成"), ModelEvent("message_completed")]]),
        initial_task="启动任务",
    )
    async with app.run_test() as pilot:
        await _wait_until(lambda: not app._run_active and app._view is not None)
        await pilot.pause()
        assert app._view is not None and "自动完成" in app._view.text
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_approve_writes_file(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel(_write_turns("b.txt", "hello")))
    bridge = app._bridge
    async with app.run_test() as pilot:
        await _submit(app, pilot, "写 b.txt")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.pause()
        assert app.query_one("#prompt-panel").has_class("show")
        prompt_text = str(app.query_one("#prompt-text").render())
        assert "write_file" in prompt_text
        assert "b.txt" in prompt_text
        await pilot.click("#btn-allow-once")
        await _wait_until(lambda: not app._run_active)
        assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "hello"
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_deny_blocks_write(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel(_write_turns("b.txt", "hello")))
    bridge = app._bridge
    async with app.run_test() as pilot:
        await _submit(app, pilot, "写 b.txt")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.pause()
        await pilot.click("#btn-deny")
        await _wait_until(lambda: not app._run_active)
        assert not (tmp_path / "b.txt").exists()
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_session_allow_skips_second_prompt(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    turns = _write_turns("a.txt", "one") + _write_turns("b.txt", "two", call_id="c2")
    app = _make_app(tmp_path, FakeModel(turns))
    bridge = app._bridge
    async with app.run_test() as pilot:
        await _submit(app, pilot, "写 a.txt")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.pause()
        await pilot.click("#btn-allow-session")
        await _wait_until(lambda: not app._run_active)
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "one"
        await _submit(app, pilot, "写 b.txt")
        await _wait_until(lambda: not app._run_active)
        assert bridge.pending is None
        assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "two"
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_shell_pattern_allow(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    shell_turn = [
        ModelEvent("tool_call_started", call_id="s1", name="shell"),
        ModelEvent(
            "tool_call_arguments_delta",
            call_id="s1",
            text='{"argv": ["echo", "hello"]}',
        ),
        ModelEvent("message_completed"),
    ]
    done_turn = [ModelEvent("text_delta", text="done"), ModelEvent("message_completed")]
    app = _make_app(tmp_path, FakeModel([shell_turn, done_turn, shell_turn, done_turn]))
    bridge = app._bridge
    async with app.run_test() as pilot:
        await _submit(app, pilot, "跑 echo")
        await _wait_until(lambda: bridge.pending is not None)
        pending = bridge.pending
        assert pending is not None and pending.pattern == "echo *"
        await pilot.pause()
        await pilot.click("#btn-allow-session")
        await _wait_until(lambda: not app._run_active)
        # 同模式命令（echo *）第二轮直接放行，不再出卡。
        await _submit(app, pilot, "再跑 echo")
        await _wait_until(lambda: not app._run_active)
        assert bridge.pending is None
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_cancel_stops_run(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, HangingModel())
    async with app.run_test() as pilot:
        await _submit(app, pilot, "慢任务")
        await _wait_until(lambda: app._current_run_id is not None)
        await pilot.press("ctrl+c")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        assert app._view is not None
        assert app._view.status_text == "cancelled"
        # 界面仍可用：退出干净。
        await pilot.press("ctrl+q")
    assert app.is_running is False


@pytest.mark.asyncio
async def test_interactive_status_bar_shows_context_and_tokens(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    turns = [
        [
            ModelEvent("text_delta", text="回答"),
            ModelEvent("usage", input_tokens=120, output_tokens=30),
            ModelEvent("message_completed"),
        ]
    ]
    app = _make_app(tmp_path, FakeModel(turns), context_window_tokens=1000)
    async with app.run_test() as pilot:
        await _submit(app, pilot, "带用量的任务")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        bar = _status_bar(app)
        assert "120/30 tokens" in bar, bar
        assert "上下文 120/1000" in bar, bar
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_slash_status_cost_and_tools(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel(_write_turns("b.txt", "hello")))
    bridge = app._bridge
    async with app.run_test() as pilot:
        await _submit(app, pilot, "写 b.txt")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.pause()
        await pilot.click("#btn-allow-once")
        await _wait_until(lambda: not app._run_active)
        await _submit(app, pilot, "/status")
        await pilot.pause()
        text = _conversation_text(app)
        assert "模型：fake" in text
        assert "工具调用：1 次" in text
        await _submit(app, pilot, "/cost")
        await pilot.pause()
        assert "本会话" in _conversation_text(app)
        await _submit(app, pilot, "/tools c1")
        await pilot.pause()
        text = _conversation_text(app)
        assert "参数：" in text and "b.txt" in text
        await _submit(app, pilot, "/help")
        await pilot.pause()
        assert "/plan" in _conversation_text(app)
        await pilot.press("ctrl+q")


# -- 入口路由（ADR-022）：TTY 进工作台、非 TTY 保持行式、--run-id 走监视台 --


def _empty_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    return config


def _stub_launch(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    def fake_launch(*args: object, **kwargs: object) -> bool:
        calls.append("interactive")
        return True

    monkeypatch.setattr(cli_main, "_launch_interactive", fake_launch)


def test_run_command_non_tty_keeps_line_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _stub_launch(monkeypatch, calls)
    monkeypatch.setattr(cli_main, "_interactive_tty", lambda: False)
    result = CliRunner().invoke(
        cli_app,
        ["run", "--config", str(_empty_config(tmp_path)), "--workspace", str(tmp_path)],
        input="",
    )
    # 行式路径在无模型配置时以退出码 2 收口；工作台未被启动。
    assert result.exit_code == 2
    assert calls == []


def test_run_command_tty_routes_to_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _stub_launch(monkeypatch, calls)
    monkeypatch.setattr(cli_main, "_interactive_tty", lambda: True)
    result = CliRunner().invoke(
        cli_app,
        ["run", "--config", str(_empty_config(tmp_path)), "--workspace", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert calls == ["interactive"]


def test_bare_deity_tty_routes_to_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _stub_launch(monkeypatch, calls)
    monkeypatch.setattr(cli_main, "_interactive_tty", lambda: True)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli_app, [])
    assert result.exit_code == 0, result.output
    assert calls == ["interactive"]


def test_bare_deity_non_tty_keeps_line_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _stub_launch(monkeypatch, calls)
    monkeypatch.setattr(cli_main, "_interactive_tty", lambda: False)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli_app, [], input="")
    assert result.exit_code == 2
    assert calls == []


def test_tui_command_without_run_id_uses_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _stub_launch(monkeypatch, calls)
    result = CliRunner().invoke(
        cli_app,
        ["tui", "--config", str(_empty_config(tmp_path)), "--workspace", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert calls == ["interactive"]


def test_tui_command_run_id_uses_monitor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _stub_launch(monkeypatch, calls)
    config = tmp_path / "config.toml"
    init_result = CliRunner().invoke(cli_app, ["init", "--config", str(config)])
    assert init_result.exit_code == 0, init_result.output
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dummy")
    seen: dict[str, object] = {}

    def fake_run_tui(**kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr("deity.interfaces.tui.app.run_tui", fake_run_tui)
    result = CliRunner().invoke(
        cli_app,
        ["tui", "--config", str(config), "--workspace", str(tmp_path), "--run-id", "run-123"],
    )
    assert result.exit_code == 0, result.output
    assert seen.get("run_id") == "run-123"
    assert seen.get("task") is None
    assert calls == []


@pytest.mark.asyncio
async def test_interactive_busy_submit_is_rejected_with_note(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, HangingModel())
    async with app.run_test() as pilot:
        await _submit(app, pilot, "慢任务")
        await _wait_until(lambda: app._run_active)
        await _submit(app, pilot, "插队任务")
        await pilot.pause()
        assert "当前有 Run 在运行" in _conversation_text(app)
        await pilot.press("ctrl+c")
        await _wait_until(lambda: not app._run_active)
        await pilot.press("ctrl+q")
