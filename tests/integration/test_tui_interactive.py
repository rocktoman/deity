"""全屏交互工作台（ADR-022）：Pilot 驱动的端到端行为测试。

覆盖：启动/退出、输入提交跑完 FakeModel Run、界面内批准写文件、
拒绝不写、会话允许与 shell 命令模式允许、取消、状态栏关键字段、
斜杠命令子集、--task 初始任务。真实模型验证为 NOT_RUN（无密钥
通道），行为验证全部用测试替身，与仓库测试规则一致。
"""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deity import __version__
from deity.application.config import CodingConfig, PermissionConfig
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
    plan_enabled: bool = False,
    clock: object = None,
) -> InteractiveApp:
    bridge = TuiApprovalBridge(workspace=tmp_path)
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,  # type: ignore[arg-type]
        approve=make_approve(bridge.controller),
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
        context_window_tokens=context_window_tokens,
        coding=(
            CodingConfig(plan_build_enabled=True, git_guard_enabled=True) if plan_enabled else None
        ),
    )
    return InteractiveApp(
        service,
        bridge=bridge,
        model_name="fake",
        workspace=tmp_path,
        initial_task=initial_task,
        clock=clock,  # type: ignore[arg-type]
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
        # 工作区随状态栏瘦身移出状态栏（P2-5）；欢迎头里仍有（见欢迎头用例）。
        assert str(tmp_path) not in bar
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
        assert "> 打个招呼" in _conversation_text(app)
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
async def test_interactive_ctrl_c_quits_when_idle(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
    assert app.is_running is False


@pytest.mark.asyncio
async def test_interactive_escape_does_not_quit_when_idle(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running is True
        await pilot.press("ctrl+q")
    assert app.is_running is False


@pytest.mark.asyncio
async def test_interactive_status_command_shows_context_and_tokens(tmp_path: Path) -> None:
    """P2-5 状态栏瘦身后：token/上下文占用改由 /status 详情呈现。"""
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
        assert "tokens" not in bar
        await _submit(app, pilot, "/status")
        await pilot.pause()
        text = _conversation_text(app)
        assert "120/30 tokens" in text
        assert "上下文 120/1000" in text
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


def test_run_command_tty_launch_failure_falls_back_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTY 下工作台启动失败（如未装 Textual）：回落行式且输出明确提示。"""

    def failing_launch(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(cli_main, "_launch_interactive", failing_launch)
    monkeypatch.setattr(cli_main, "_interactive_tty", lambda: True)
    result = CliRunner().invoke(
        cli_app,
        ["run", "--config", str(_empty_config(tmp_path)), "--workspace", str(tmp_path)],
        input="",
    )
    # 回落行式路径（无模型配置时退出码 2），且提示里说明了回落原因与装法。
    assert result.exit_code == 2
    assert "已回落行式模式" in result.output
    assert "deity[tui]" in result.output


def test_bare_deity_non_tty_keeps_line_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _stub_launch(monkeypatch, calls)
    monkeypatch.setattr(cli_main, "_interactive_tty", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
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


# -- Claude Code 观感对齐（ADR-022 修订）：欢迎头 / 输入区 / 模式循环 / 工具行 --


@pytest.mark.asyncio
async def test_interactive_welcome_header_has_emblem_version_model_workspace(
    tmp_path: Path,
) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#welcome-header")
        emblem = str(app.query_one("#welcome-emblem").render())
        emblem_lines = [line for line in emblem.splitlines() if line.strip()]
        # 徽记只断言存在性与规模（神之眼字符画不写死整幅）。
        assert len(emblem_lines) >= 4
        assert "◉" in emblem
        text = _conversation_text(app)
        assert f"Deity v{__version__}" in text
        assert "模型：fake" in text
        assert str(tmp_path) in text
        # 与原一行欢迎消息合并，不重复两份欢迎。
        assert text.count("欢迎来到 Deity") == 1
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_status_row_sits_above_input(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        # Gemini 式 StatusRow：等待/提示行 + 分隔线 + 模式行都在输入框上方。
        ordered_ids = [w.id for w in app.screen.walk_children(with_self=False) if w.id]
        assert ordered_ids.index("status-top") < ordered_ids.index("status-divider")
        assert ordered_ids.index("status-divider") < ordered_ids.index("status-bar")
        assert ordered_ids.index("status-bar") < ordered_ids.index("input")
        bar = _status_bar(app)
        assert "普通模式" in bar
        assert "fake" in bar
        # 模式动态副提示：普通模式下一步切到计划。
        assert "shift+tab 切换到计划" in bar
        # (shift+tab 切换) 提示仍在输入区状态提示行（P2-5）。
        assert "Shift+Tab 切换模式" in str(app.query_one("#input-hint").render())
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_shift_tab_cycles_modes(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]), plan_enabled=True)
    bridge = app._bridge
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "普通模式" in _status_bar(app)
        # 普通 → 计划（与 /plan 同一启用检查与状态转移）。
        await pilot.press("shift+tab")
        await _wait_until(lambda: app.state.plan_mode)
        await pilot.pause()
        assert "计划模式" in _status_bar(app)
        # 计划 → 自动接受：仍走审批桥显式确认，确认前模式不变。
        await pilot.press("shift+tab")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.pause()
        pending = bridge.pending
        assert pending is not None and pending.kind == "confirm"
        assert app.state.plan_mode  # 确认通过前仍留在计划模式
        await pilot.click("#btn-confirm")
        await _wait_until(lambda: bridge.controller.mode == "auto")
        await pilot.pause()
        assert not app.state.plan_mode  # 确认通过后才离开计划模式
        assert "自动接受模式" in _status_bar(app)
        # 自动接受 → 普通：一轮循环回到起点。
        await pilot.press("shift+tab")
        await _wait_until(lambda: bridge.controller.mode != "auto")
        await pilot.pause()
        assert not app.state.plan_mode
        assert "普通模式" in _status_bar(app)
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_shift_tab_auto_cancel_stays_in_plan(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]), plan_enabled=True)
    bridge = app._bridge
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("shift+tab")
        await _wait_until(lambda: app.state.plan_mode)
        await pilot.press("shift+tab")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.pause()
        # 取消自动接受确认：不进自动接受，仍留在计划模式（不静默绕过确认）。
        await pilot.click("#btn-cancel")
        await _wait_until(lambda: bridge.pending is None)
        await pilot.pause()
        assert bridge.controller.mode != "auto"
        assert app.state.plan_mode
        assert "计划模式" in _status_bar(app)
        await pilot.press("ctrl+q")


def _plan_turns() -> list[list[ModelEvent]]:
    payload = {
        "schema_version": 1,
        "goal": "实现功能",
        "assumptions": [],
        "steps": [
            {
                "ordinal": 1,
                "title": "实现",
                "description": "最小实现",
                "files": ["feature.py"],
                "verification": ["pytest"],
            }
        ],
        "risks": [],
        "verification": ["pytest -q"],
    }
    return [
        [
            ModelEvent("tool_call_started", call_id="p1", name="submit_plan"),
            ModelEvent(
                "tool_call_arguments_delta",
                call_id="p1",
                text=json.dumps(payload, ensure_ascii=False),
            ),
            ModelEvent("message_completed"),
        ],
        [ModelEvent("text_delta", text="计划已提交"), ModelEvent("message_completed")],
    ]


def _git_init_repo(root: Path) -> None:
    """同步 git 助手：给计划模式用例准备一个带基线提交的工作区。"""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=root, check=True)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "基线"], cwd=root, check=True)


@pytest.mark.asyncio
async def test_interactive_plan_mode_submit_uses_plan_flow(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    _git_init_repo(tmp_path)
    app = _make_app(tmp_path, FakeModel(_plan_turns()), plan_enabled=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("shift+tab")
        await _wait_until(lambda: app.state.plan_mode)
        await _submit(app, pilot, "实现 feature")
        # 计划路径：先展示计划并询问是否构建，而不是直接执行任务。
        await _wait_until(lambda: "计划 ID：" in _conversation_text(app))
        await _wait_until(lambda: app.query_one("#prompt-panel").has_class("show"))
        await pilot.click("#btn-cancel")  # 保留草案，不构建
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        text = _conversation_text(app)
        assert "已保留计划" in text
        assert app.state.plan_mode
        assert not (tmp_path / "feature.py").exists()
        await pilot.press("ctrl+q")


# -- Klee 风格对齐 P0：自定义主题 / 圆角边框 / Markdown 定制 --


@pytest.mark.asyncio
async def test_interactive_uses_deity_theme(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme in ("deity-dark", "deity-light")
        theme = app.get_theme(app.theme)
        assert theme is not None
        # 品牌色：深底黑色主题中性强调色、浅底 Klee 深绿原色（2026-11-22 黑色主题）。
        assert theme.accent in ("#E8EAE8", "#1E4C35")
        assert "text-quaternary" in theme.variables
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_round_borders(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        for selector in ("#welcome-header", "#prompt-panel"):
            border_type = app.query_one(selector).styles.border[0][0]
            assert border_type == "round", selector
        # Gemini 式去框化输入框：仅上下两条圆角线，左右无边框（未设边为空串）。
        edges = app.query_one("#input-row").styles.border
        assert edges[0][0] == "round", "input-row top"
        assert edges[2][0] == "round", "input-row bottom"
        assert edges[1][0] == "", "input-row right"
        assert edges[3][0] == "", "input-row left"
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_markdown_fence_rendered(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    reply = "# 标题\n\n```python\nprint('hi')\n```\n\n- 条目一\n"
    app = _make_app(
        tmp_path,
        FakeModel([[ModelEvent("text_delta", text=reply), ModelEvent("message_completed")]]),
    )
    async with app.run_test() as pilot:
        await _submit(app, pilot, "给我代码")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        fences = app.query("MarkdownFence")
        assert len(fences) == 1
        # 代码块内容在 MarkdownFence.code 上（渲染为语法高亮，不走纯文本）。
        assert "print('hi')" in fences[0].code  # type: ignore[attr-defined]
        assert "标题" in _conversation_text(app)
        await pilot.press("ctrl+q")


# -- Klee 风格对齐 P1：等待指示 / 错误块 / 指标行 / 输入提示 / 问候语 --


class SlowStartModel:
    """延迟后才吐第一个事件（等待指示测试用）。"""

    def __init__(self, delay: float = 1.2) -> None:
        self.delay = delay

    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        await asyncio.sleep(self.delay)
        yield ModelEvent("text_delta", text="慢启动完成")
        yield ModelEvent("message_completed")


@pytest.mark.asyncio
async def test_interactive_waiting_indicator_lifecycle(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, SlowStartModel())
    async with app.run_test() as pilot:
        await _submit(app, pilot, "慢任务")
        await _wait_until(lambda: len(app.query("#waiting-row")) == 1)
        waiting = str(app.query_one("#waiting-row").render())
        assert "Thinking..." in waiting
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        # 首个增量到达后等待行撤下，不留在消息流里。
        assert len(app.query("#waiting-row")) == 0
        assert "慢启动完成" in _conversation_text(app)
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_error_block_for_unknown_resume(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await _submit(app, pilot, "/resume no-such-run")
        await _wait_until(lambda: len(app.query(".msg-error")) >= 1)
        await pilot.pause()
        error_text = str(app.query(".msg-error")[0].render())
        assert "运行失败" in error_text
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_metrics_line_after_run(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    turns = [
        [
            ModelEvent("text_delta", text="回答"),
            ModelEvent("usage", input_tokens=120, output_tokens=30),
            ModelEvent("message_completed"),
        ]
    ]
    app = _make_app(tmp_path, FakeModel(turns))
    async with app.run_test() as pilot:
        await _submit(app, pilot, "带用量的任务")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        metrics = app.query(".msg-metrics")
        assert len(metrics) == 1
        line = str(metrics[0].render())
        assert "⚡" in line
        assert "30 tokens" in line
        assert "工具 0 次" in line
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_input_hint_states(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, HangingModel())
    async with app.run_test() as pilot:
        await pilot.pause()
        hint = str(app.query_one("#input-hint").render())
        assert "输入任务开始" in hint
        assert "可能会出错" in str(app.query_one("#disclaimer").render())
        await _submit(app, pilot, "慢任务")
        await _wait_until(lambda: app._run_active)
        await pilot.pause()
        app._refresh_status()
        hint = str(app.query_one("#input-hint").render())
        assert "生成中" in hint
        assert app.query_one("#input-row").has_class("streaming")
        await pilot.press("ctrl+c")
        await _wait_until(lambda: not app._run_active)
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_welcome_greeting_by_clock(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from datetime import datetime

    morning = _make_app(tmp_path, FakeModel([]), clock=lambda: datetime(2026, 1, 1, 9, 0))
    async with morning.run_test() as pilot:
        await pilot.pause()
        assert "早上好" in _conversation_text(morning)
        await pilot.press("ctrl+q")
    evening = _make_app(tmp_path, FakeModel([]), clock=lambda: datetime(2026, 1, 1, 20, 0))
    async with evening.run_test() as pilot:
        await pilot.pause()
        text = _conversation_text(evening)
        assert "晚上好" in text
        assert "今天想让我帮你做什么？" in text
        await pilot.press("ctrl+q")


# -- Klee 风格对齐 P2：会话侧栏 --


@pytest.mark.asyncio
async def test_interactive_sidebar_toggle_and_current_session(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    turns = [[ModelEvent("text_delta", text="完成"), ModelEvent("message_completed")]]
    app = _make_app(tmp_path, FakeModel(turns))
    async with app.run_test(size=(120, 30)) as pilot:
        await _submit(app, pilot, "建会话的任务")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        sidebar = app.query_one("#sidebar")
        assert not sidebar.has_class("show")  # 默认隐藏（D3）
        await pilot.press("ctrl+b")
        await _wait_until(lambda: len(app.query(".session-row")) >= 1)
        await pilot.pause()
        assert sidebar.has_class("show")
        rows = app.query(".session-row")
        assert len(rows) == 1
        assert rows[0].has_class("current")
        assert "●" in str(rows[0].label)
        assert app.state.session_id[:8] in str(rows[0].label)
        assert "模型：fake" in str(app.query_one("#sidebar-model").render())
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert not sidebar.has_class("show")
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_sidebar_refused_when_narrow(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert not app.query_one("#sidebar").has_class("show")
        assert "窗口太窄" in _conversation_text(app)
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_sidebar_row_hover_feedback(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    turns = [[ModelEvent("text_delta", text="完成"), ModelEvent("message_completed")]]
    app = _make_app(tmp_path, FakeModel(turns))
    async with app.run_test(size=(120, 30)) as pilot:
        await _submit(app, pilot, "建会话的任务")
        await _wait_until(lambda: not app._run_active)
        await pilot.press("ctrl+b")
        await _wait_until(lambda: len(app.query(".session-row")) >= 1)
        row = app.query(".session-row")[0]
        before = row.styles.background
        await pilot.hover(row)
        await pilot.pause()
        # hover 反馈（P2-1）：行背景切到主题 hover 底色。
        assert row.styles.background != before
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_tool_lines_are_compact(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel(_write_turns("b.txt", "hello")))
    bridge = app._bridge
    async with app.run_test() as pilot:
        await _submit(app, pilot, "写 b.txt")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.pause()
        await pilot.click("#btn-allow-once")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        text = _conversation_text(app)
        # 一行式条目 + 缩进结果行；不再是旧的 ▸/▾ 面板式呈现。
        # Codex 移植后工具行改动词标签：⏺ 写 b.txt（不再是 ⏺ write_file(…））。
        assert "⏺ 写 b.txt" in text
        assert "⎿" in text
        assert "▸ write_file" not in text
        await pilot.press("ctrl+q")


# -- Gemini 风格对齐：StatusRow 两行化 / 上下文着色 / 模式副提示 /
#    等待行左竖线 / SectionHeader / 审批折叠输入区 --


@pytest.mark.asyncio
async def test_interactive_status_row_tip_visible_when_wide(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # 宽屏：等待/提示行右侧显示 Tip，分隔线为淡色单线。
        assert "Tip：" in str(app.query_one("#status-tip").render())
        assert app.query_one("#status-divider").styles.border[0][0] == "solid"
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_status_row_tip_hidden_when_narrow(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert str(app.query_one("#status-tip").render()).strip() == ""
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_context_usage_colored_by_ratio(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    turns = [
        [
            ModelEvent("text_delta", text="回答"),
            ModelEvent("usage", input_tokens=800, output_tokens=30),
            ModelEvent("message_completed"),
        ]
    ]
    app = _make_app(tmp_path, FakeModel(turns), context_window_tokens=1000)
    async with app.run_test(size=(120, 40)) as pilot:
        await _submit(app, pilot, "带用量的任务")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        # 80% ≥ 0.75 压缩软限：右侧显示「上下文 80%」，警告色。
        bar = str(app.query_one("#status-bar").render())
        assert "上下文 80%" in bar
        rendered = app.query_one("#status-bar").render()
        assert any("217,164,65" in str(span.style) for span in rendered.spans)  # 警告色 #D9A441
        # 满额：直接改状态再刷新，变红。
        app.session_status.last_input_tokens = 1000
        app._refresh_status()
        bar = str(app.query_one("#status-bar").render())
        assert "上下文 100%" in bar
        rendered = app.query_one("#status-bar").render()
        assert any("229,83,75" in str(span.style) for span in rendered.spans)  # 错误色 #E5534B
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_context_usage_percent_only_when_narrow(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    turns = [
        [
            ModelEvent("text_delta", text="回答"),
            ModelEvent("usage", input_tokens=500, output_tokens=30),
            ModelEvent("message_completed"),
        ]
    ]
    app = _make_app(tmp_path, FakeModel(turns), context_window_tokens=1000)
    async with app.run_test(size=(80, 24)) as pilot:
        await _submit(app, pilot, "带用量的任务")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        bar = str(app.query_one("#status-bar").render())
        assert "50%" in bar
        assert "上下文" not in bar
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_mode_subhint_follows_mode(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]), plan_enabled=True)
    bridge = app._bridge
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "shift+tab 切换到计划" in _status_bar(app)
        await pilot.press("shift+tab")
        await _wait_until(lambda: app.state.plan_mode)
        await pilot.pause()
        assert "shift+tab 切换到自动接受" in _status_bar(app)
        await pilot.press("shift+tab")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.pause()
        await pilot.click("#btn-confirm")
        await _wait_until(lambda: bridge.controller.mode == "auto")
        await pilot.pause()
        assert "shift+tab 回到普通" in _status_bar(app)
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_waiting_row_left_border(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, SlowStartModel())
    async with app.run_test() as pilot:
        await _submit(app, pilot, "慢任务")
        await _wait_until(lambda: len(app.query("#waiting-row")) == 1)
        waiting = app.query_one("#waiting-row")
        assert "Thinking..." in str(waiting.render())
        # 仅左竖线 + 斜体（不伪造思考内容，仍只显示等待态）。
        assert waiting.styles.border[3][0] == "solid"
        assert "italic" in str(waiting.styles.text_style)
        await _wait_until(lambda: not app._run_active)
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_help_section_header(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await _submit(app, pilot, "/help")
        await _wait_until(lambda: len(app.query(".section-header")) >= 1)
        await pilot.pause()
        headers = app.query(".section-header")
        assert "命令" in str(headers[0].render())
        assert headers[0].styles.border[0][0] == "solid"
        assert "/plan" in _conversation_text(app)
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_approval_collapses_input(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel(_write_turns("b.txt", "hello")))
    bridge = app._bridge
    async with app.run_test() as pilot:
        await _submit(app, pilot, "写 b.txt")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.pause()
        assert app.query_one("#prompt-panel").has_class("show")
        # 审批独占焦点：输入框与下方提示行折叠，面板标题为 SectionHeader。
        assert app.query_one("#input-row").has_class("collapsed")
        assert app.query_one("#input-hint").has_class("collapsed")
        assert not app.query_one("#input-row").display
        assert "审批" in str(app.query_one("#prompt-title").render())
        await pilot.click("#btn-allow-once")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        assert not app.query_one("#input-row").has_class("collapsed")
        assert app.query_one("#input-row").display
        assert isinstance(app.focused, ChatInput)
        assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "hello"
        await pilot.press("ctrl+q")


# -- Codex 行为移植：diff 块 / 耗时 / 动词标签 / /theme / /copy / /new / @ 补全 --


@pytest.mark.asyncio
async def test_codex_diff_block_mounts_on_turn_diff(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from deity.interfaces.tui.interactive import DiffBlock

    app = _make_app(tmp_path, FakeModel(_write_turns("feature.py", "print(1)")))
    bridge = app._bridge
    async with app.run_test() as pilot:
        await _submit(app, pilot, "写 feature.py")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.click("#btn-allow-once")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        assert (tmp_path / "feature.py").read_text(encoding="utf-8") == "print(1)"
        blocks = list(app.query(DiffBlock))
        assert len(blocks) >= 1
        assert "feature.py" in str(blocks[0].render())
        assert "print(1)" in str(blocks[0].render())
        # 工具行动词标签（Codex ExecCell 式）：写 feature.py。
        assert "写 feature.py" in _conversation_text(app)
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_codex_working_indicator_shows_elapsed(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    import re

    app = _make_app(tmp_path, HangingModel())
    async with app.run_test() as pilot:
        await _submit(app, pilot, "慢任务")
        await _wait_until(lambda: app._run_active)
        await _wait_until(lambda: "生成中" in str(app.query_one("#status-wait").render()))
        text = str(app.query_one("#status-wait").render())
        assert "生成中" in text
        assert re.search(r"\(\d+s · Ctrl\+C 取消\)", text) is not None
        await pilot.press("ctrl+c")
        await _wait_until(lambda: not app._run_active)
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_codex_theme_command_switches_theme(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/theme light")
        await _wait_until(lambda: app.theme == "deity-light")
        assert "已切换到浅色主题" in _conversation_text(app)
        await _submit(app, pilot, "/theme")
        await _wait_until(lambda: app.theme == "deity-dark")
        assert app._palette["accent"] == "#E8EAE8"
        assert "已切换到深色主题" in _conversation_text(app)
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_codex_copy_command_copies_last_reply(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(
        tmp_path,
        FakeModel([[ModelEvent("text_delta", text="复制我"), ModelEvent("message_completed")]]),
    )
    async with app.run_test() as pilot:
        await _submit(app, pilot, "说点什么")
        await _wait_until(lambda: not app._run_active)
        await _submit(app, pilot, "/copy")
        await _wait_until(lambda: "已复制上一轮回答" in _conversation_text(app))
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_codex_new_command_starts_fresh_session(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        before = app.state.session_id
        await _submit(app, pilot, "/new")
        await _wait_until(lambda: "已开始新会话" in _conversation_text(app))
        assert app.state.session_id != before
        assert "欢迎来到 Deity" in _conversation_text(app)
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_codex_mention_completion_lists_and_completes_files(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    (tmp_path / "feature.py").write_text("x = 1\n", encoding="utf-8")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#input", ChatInput)
        box.text = "看看 @fea"
        box.focus()
        await pilot.pause()
        await _wait_until(lambda: "文件 (Tab 补全)" in str(app.query_one("#cmd-hint").render()))
        assert "@feature.py" in str(app.query_one("#cmd-hint").render())
        await pilot.press("tab")
        await pilot.pause()
        assert box.text == "看看 @feature.py "
        await pilot.press("ctrl+q")


# -- Codex 行为移植第二轮（ADR-023）：/model 弹窗、/usage、/rename、/sandbox、
#    审批 diff 预览、工具输出预览 + (exit N) --


@pytest.mark.asyncio
async def test_codex2_model_dialog_opens_filters_and_selects(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Input

    app = _make_app(tmp_path, FakeModel([]))
    app.service.provider_id = "qwen"
    app.state.model_name = "qwen3.8-omni-flash"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/model")
        await _wait_until(lambda: app.query_one("#model-dialog").has_class("show"))
        assert app._model_dialog_open
        rows = list(app.query(".model-row"))
        assert len(rows) == 5
        assert "● qwen3.8-omni-flash" in str(rows[0].render())
        # 过滤框获得焦点，输入 qwen4 后只剩一个候选。
        await _wait_until(lambda: isinstance(app.focused, Input))
        await pilot.press("q", "w", "e", "n", "4")
        await _wait_until(lambda: len(list(app.query(".model-row"))) == 1)
        # 回车选中第一个可见候选。
        await pilot.press("enter")
        await _wait_until(lambda: app.state.model_name == "qwen4-max")
        assert not app.query_one("#model-dialog").has_class("show")
        assert "已切换模型：qwen4-max" in _conversation_text(app)
        # Esc 关闭：重开后按 Esc，焦点回到输入框，模型不变。
        await _submit(app, pilot, "/model")
        await _wait_until(lambda: app.query_one("#model-dialog").has_class("show"))
        await pilot.press("escape")
        await _wait_until(lambda: not app.query_one("#model-dialog").has_class("show"))
        assert isinstance(app.focused, ChatInput)
        assert app.state.model_name == "qwen4-max"
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_codex2_model_dialog_click_selects_row(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    app.service.provider_id = "qwen"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/model")
        await _wait_until(lambda: app.query_one("#model-dialog").has_class("show"))
        await pilot.click("#model-row-2")
        await _wait_until(lambda: app.state.model_name == "qwen-max")
        assert not app.query_one("#model-dialog").has_class("show")
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_codex2_model_dialog_falls_back_without_catalog(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/model")
        await _wait_until(lambda: "无固定模型列表" in _conversation_text(app))
        assert not app.query_one("#model-dialog").has_class("show")
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_codex2_usage_command_shows_session_summary(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/usage")
        await _wait_until(lambda: "本会话用量" in _conversation_text(app))
        assert "Token：未接通" in _conversation_text(app)
        assert "无账号配额后端" in _conversation_text(app)
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_codex2_rename_command_updates_title_and_sidebar(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    turns = [[ModelEvent("text_delta", text="完成"), ModelEvent("message_completed")]]
    app = _make_app(tmp_path, FakeModel(turns))
    async with app.run_test(size=(120, 30)) as pilot:
        await _submit(app, pilot, "建会话的任务")
        await _wait_until(lambda: not app._run_active)
        await _submit(app, pilot, "/rename 我的会话")
        await _wait_until(lambda: "已重命名当前会话为：我的会话" in _conversation_text(app))
        await pilot.press("ctrl+b")
        await _wait_until(lambda: len(app.query(".session-row")) >= 1)
        await pilot.pause()
        assert "我的会话" in str(app.query(".session-row")[0].label)
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_codex2_sandbox_command_shows_status_and_switches(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from deity.application.config import SandboxConfig

    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/sandbox")
        await _wait_until(lambda: "沙箱状态" in _conversation_text(app))
        assert "启用：否" in _conversation_text(app)
        await _submit(app, pilot, "/sandbox read-only")
        await _wait_until(lambda: "切换失败" in _conversation_text(app))
        # 启用 bwrap 后切换成功（仅本次会话内生效）。
        app.service.sandbox = SandboxConfig(enabled=True, backend="bwrap", policy="workspace-write")
        await _submit(app, pilot, "/sandbox read-only")
        await _wait_until(lambda: "已切换沙箱策略为：read-only" in _conversation_text(app))
        assert app.service.sandbox.policy == "read-only"
        await _submit(app, pilot, "/sandbox full-access")
        await _wait_until(lambda: "full-access 不可切换" in _conversation_text(app))
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_codex2_approval_panel_shows_diff_preview(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    (tmp_path / "b.txt").write_text("one\ntwo\n", encoding="utf-8")
    # write_file 只建不覆盖：改已存在文件用 apply_patch（参数经 json.dumps，
    # _write_turns 的 f-string 会把内容里的换行拼成非法 JSON）。
    turns = [
        [
            ModelEvent("tool_call_started", call_id="c1", name="apply_patch"),
            ModelEvent(
                "tool_call_arguments_delta",
                call_id="c1",
                text=json.dumps(
                    {
                        "path": "b.txt",
                        "old": "one\ntwo\n",
                        "new": "one\nTWO\nthree\n",
                    }
                ),
            ),
            ModelEvent("message_completed"),
        ],
        [ModelEvent("text_delta", text="done"), ModelEvent("message_completed")],
    ]
    app = _make_app(tmp_path, FakeModel(turns))
    bridge = app._bridge
    async with app.run_test() as pilot:
        await _submit(app, pilot, "改 b.txt")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.pause()
        assert app.query_one("#prompt-panel").has_class("show")
        # 原始 diff 在 pending 上；渲染后的 diff 块带行号（`2 - two` 格式）。
        assert bridge.pending is not None and bridge.pending.diff is not None
        assert "-two" in bridge.pending.diff
        assert "+TWO" in bridge.pending.diff
        diff_text = str(app.query_one("#prompt-diff").render())
        assert "two" in diff_text and "TWO" in diff_text
        assert "--- a/b.txt" not in str(app.query_one("#prompt-text").render())
        await pilot.click("#btn-allow-once")
        await _wait_until(lambda: not app._run_active)
        assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "one\nTWO\nthree\n"
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_codex2_tool_line_shows_preview_and_exit_code(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    # shell 工具参数是 argv；用 sh -c 拿到确定的退出码 3 与输出行 oops。
    turns = [
        [
            ModelEvent("tool_call_started", call_id="c1", name="shell"),
            ModelEvent(
                "tool_call_arguments_delta",
                call_id="c1",
                text=json.dumps({"argv": ["sh", "-c", "echo oops; exit 3"]}),
            ),
            ModelEvent("message_completed"),
        ],
        [ModelEvent("text_delta", text="done"), ModelEvent("message_completed")],
    ]
    app = _make_app(tmp_path, FakeModel(turns))
    bridge = app._bridge
    async with app.run_test() as pilot:
        await _submit(app, pilot, "跑个失败命令")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.click("#btn-allow-once")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()
        text = _conversation_text(app)
        # 工具行带动词标签与 (exit N)；输出预览行挂在工具行下。
        assert "$ sh" in text
        assert "(exit 3)" in text
        assert "oops" in text
        await pilot.press("ctrl+q")
