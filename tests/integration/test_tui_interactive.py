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
async def test_interactive_status_bar_sits_below_input(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        ordered_ids = [w.id for w in app.screen.walk_children(with_self=False) if w.id]
        assert ordered_ids.index("status-bar") > ordered_ids.index("input")
        bar = _status_bar(app)
        assert "普通模式 (shift+tab 切换)" in bar
        assert "fake" in bar
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_shift_tab_cycles_modes(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]), plan_enabled=True)
    bridge = app._bridge
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "普通模式 (shift+tab 切换)" in _status_bar(app)
        # 普通 → 计划（与 /plan 同一启用检查与状态转移）。
        await pilot.press("shift+tab")
        await _wait_until(lambda: app.state.plan_mode)
        await pilot.pause()
        assert "计划模式 (shift+tab 切换)" in _status_bar(app)
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
        assert "自动接受模式 (shift+tab 切换)" in _status_bar(app)
        # 自动接受 → 普通：一轮循环回到起点。
        await pilot.press("shift+tab")
        await _wait_until(lambda: bridge.controller.mode != "auto")
        await pilot.pause()
        assert not app.state.plan_mode
        assert "普通模式 (shift+tab 切换)" in _status_bar(app)
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
        assert "计划模式 (shift+tab 切换)" in _status_bar(app)
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
        # 品牌色：浅底 Klee 深绿原色、深底提亮变体（D1）。
        assert theme.accent in ("#1E4C35", "#2E7D5B")
        assert "text-quaternary" in theme.variables
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_interactive_round_borders(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    app = _make_app(tmp_path, FakeModel([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        for selector in ("#welcome-header", "#input-row", "#prompt-panel"):
            border_type = app.query_one(selector).styles.border[0][0]
            assert border_type == "round", selector
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
        assert "⏺ write_file(" in text
        assert "⎿" in text
        assert "▸ write_file" not in text
        await pilot.press("ctrl+q")
