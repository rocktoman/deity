"""Phase 1：拆分前后交互输出逐行一致（parity）。

Golden 输出在拆分前用 CliRunner 对基线 _interactive() 实测录制；
重构后同一组输入脚本必须输出逐字节相同的文本。
"""

import importlib

import pytest
from typer.testing import CliRunner

from deity.application.config import CodingConfig, Config, ModelConfig
from deity.core.contracts import AgentResult, RunStatus


def _invoke(monkeypatch: pytest.MonkeyPatch, script: str):  # type: ignore[no-untyped-def]
    cli = importlib.import_module("deity.interfaces.cli.main")

    class FakeService:
        coding = CodingConfig()

        async def run(self, task: str, **kwargs: object) -> AgentResult:
            return AgentResult("run-1", RunStatus.COMPLETED, "ok")

    config = Config(model=ModelConfig("openai_compatible", "fake", "FAKE_KEY"))
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli.RunService, "from_config", lambda *args, **kwargs: FakeService())
    return CliRunner().invoke(cli.app, [], input=script)


def test_parity_help_empty_unknown_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, "/help\n\n/unknowncmd\n/exit\n")
    assert result.exit_code == 0, result.output
    assert result.output == (
        "欢迎来到 Deity，我是你的编程助手。\n"
        "Deity> /plan 进入持续计划模式；/build [计划ID] [--allow-dirty 路径] \n"
        "执行已有计划；/normal 返回普通模式；/skill 选择 Skill；/mcp 查看并选择 MCP \n"
        "工具；选择会提示模型在下一条任务中使用；/resume [运行ID] \n"
        "恢复中断的运行；/sessions 列出并切换会话；/compact 查看上下文压缩状态；/clear \n"
        "清屏；/cost 查看本会话 token 与轮次；/model [名称] 查看或切换模型；/status \n"
        "查看会话状态；/auto 进入/退出自动接受模式；/doctor 运行环境检查；/exit 或 /quit \n"
        "退出\n"
        "Deity> Deity> 未知命令。输入 /help 查看可用命令。\n"
        "Deity> "
    )


def test_parity_plan_disabled_then_normal(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, "/plan\n/normal\n/exit\n")
    assert result.exit_code == 0, result.output
    assert result.output == (
        "欢迎来到 Deity，我是你的编程助手。\n"
        "Deity> Plan/Build 未启用，请在配置中开启 plan_build_enabled 和 git_guard_enabled。\n"
        "Deity> 已返回普通模式。\n"
        "Deity> "
    )


def test_parity_empty_lines_only(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, "\n\n/exit\n")
    assert result.exit_code == 0, result.output
    assert result.output == "欢迎来到 Deity，我是你的编程助手。\nDeity> Deity> Deity> "


def test_parity_task_then_quit(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, "你好\n/quit\n")
    assert result.exit_code == 0, result.output
    assert result.output == "欢迎来到 Deity，我是你的编程助手。\nDeity> ok\nDeity> "
