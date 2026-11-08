"""Phase 2：非 TTY / TERM=dumb 时行为与基线一致（整段打印、不写历史文件）。"""

import importlib

import pytest
from typer.testing import CliRunner

from deity.application.config import ChatConfig, CodingConfig, Config, ModelConfig
from deity.core.contracts import AgentResult, RunStatus
from deity.core.contracts.client import ClientEvent


def _invoke(monkeypatch: pytest.MonkeyPatch, script: str, *, env: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    cli = importlib.import_module("deity.interfaces.cli.main")

    class FakeService:
        coding = CodingConfig()

        async def run(self, task: str, **kwargs: object) -> AgentResult:
            return AgentResult("run-1", RunStatus.COMPLETED, "ok")

        async def run_stream(self, task: str, **kwargs: object):  # type: ignore[no-untyped-def]
            yield ClientEvent(seq=1, run_id="r", kind="text_delta", payload={"text": "流式"})
            yield ClientEvent(
                seq=2,
                run_id="r",
                kind="run_status",
                payload={"status": "completed", "final_text": "流式"},
            )

    config = Config(model=ModelConfig("openai_compatible", "fake", "FAKE_KEY"))
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli.RunService, "from_config", lambda *args, **kwargs: FakeService())
    return CliRunner().invoke(cli.app, [], input=script, env=env)


def test_non_tty_uses_whole_print_not_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, "你好\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "ok" in result.output
    assert "流式" not in result.output
    assert "第 1 轮" not in result.output


def test_non_tty_help_output_matches_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, "/help\n\n/unknowncmd\n/exit\n")
    assert result.exit_code == 0, result.output
    assert result.output == (
        "欢迎来到 Deity，我是你的编程助手。\n"
        "Deity> /plan 进入持续计划模式；/build [计划ID] [--allow-dirty 路径] \n"
        "执行已有计划；/normal 返回普通模式；/skill 选择 Skill；/mcp 查看并选择 MCP \n"
        "工具；选择会提示模型在下一条任务中使用；/exit 或 /quit 退出\n"
        "Deity> Deity> 未知命令。输入 /help 查看可用命令。\n"
        "Deity> "
    )


def test_non_tty_does_not_write_history_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from pathlib import Path

    history = Path(str(tmp_path)) / "chat_history"
    cli = importlib.import_module("deity.interfaces.cli.main")

    class FakeService:
        coding = CodingConfig()

        async def run(self, task: str, **kwargs: object) -> AgentResult:
            return AgentResult("run-1", RunStatus.COMPLETED, "ok")

    config = Config(
        model=ModelConfig("openai_compatible", "fake", "FAKE_KEY"),
        chat=ChatConfig(history_file=str(history)),
    )
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli.RunService, "from_config", lambda *args, **kwargs: FakeService())
    result = CliRunner().invoke(cli.app, [], input="你好\n/exit\n")
    assert result.exit_code == 0, result.output
    assert not history.exists()


def test_dumb_terminal_disables_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(monkeypatch, "你好\n/exit\n", env={"TERM": "dumb"})
    assert result.exit_code == 0, result.output
    assert "ok" in result.output
    assert "流式" not in result.output
