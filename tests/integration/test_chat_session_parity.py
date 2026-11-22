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
        "Deity> /help  查看可用命令\n"
        "/plan  进入持续计划模式\n"
        "/normal  返回普通模式\n"
        "/build [计划ID] [--allow-dirty 路径]  执行已有计划\n"
        "/skill [名称]  选择 Skill（只影响下一条任务）\n"
        "/mcp  查看并选择 MCP 工具（只影响下一条任务）\n"
        "/resume [运行ID]  恢复中断的运行\n"
        "/fork <运行ID>  从某运行的最新前沿分叉新运行\n"
        "/timeline <运行ID>  查看某运行的证据时间线\n"
        "/sessions  列出并切换会话\n"
        "/compact  查看上下文压缩状态\n"
        "/clear  清屏（不删除 SQLite 历史）\n"
        "/cost  查看本会话 token 与轮次\n"
        "/model [名称]  查看或切换模型（仅本次会话）\n"
        "/status  查看会话状态\n"
        "/tools [call_id]  查看上一轮工具调用（折叠列表/展开参数）\n"
        "/auto  进入/退出自动接受模式\n"
        "/doctor  运行环境检查\n"
        "/diff  查看工作区未提交改动\n"
        "/new  开始新会话\n"
        "/review  审查工作区未提交改动\n"
        "/export  导出当前会话为 Markdown 文件\n"
        "/copy  复制上一轮回答（仅工作台）\n"
        "/theme [dark|light]  切换工作台深浅主题（仅工作台）\n"
        "/archive  归档当前会话并开始新会话\n"
        "/exit 或 /quit  退出交互\n"
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
