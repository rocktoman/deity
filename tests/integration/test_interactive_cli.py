"""CLI prompt and same-session conversation gates."""

import importlib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from deity.application.config import Config, ModelConfig
from deity.application.run_service import RunService
from deity.core.contracts import AgentResult, ModelEvent, ModelRequest, RunStatus


@pytest.mark.asyncio
async def test_second_turn_receives_same_session_history(tmp_path: Path) -> None:
    class Model:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def stream(self, request: ModelRequest):
            self.requests.append(request)
            yield ModelEvent("text_delta", text=f"answer-{len(self.requests)}")
            yield ModelEvent("message_completed")

    model = Model()
    service = RunService(db_path=tmp_path / "deity.db", workspace=tmp_path, model=model)
    await service.run("first question", model_name="fake", session_id="conversation")
    await service.run("second question", model_name="fake", session_id="conversation")
    messages = model.requests[1].messages
    assert messages[0]["role"] == "system"
    assert "Deity" in messages[0]["content"]
    assert [(item["role"], item["content"]) for item in messages[1:]] == [
        ("user", "first question"),
        ("assistant", "answer-1"),
        ("user", "second question"),
    ]


@pytest.mark.asyncio
async def test_repository_instructions_do_not_replace_current_task(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("只修改现有模块")

    class Model:
        async def stream(self, request: ModelRequest):
            self.messages = request.messages
            yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    model = Model()
    service = RunService(db_path=tmp_path / "deity.db", workspace=tmp_path, model=model)
    await service.run("在当前目录创建 bubble_sort.py", model_name="fake")
    assert [item["role"] for item in model.messages] == ["system", "user", "user"]
    assert "只修改现有模块" not in model.messages[0]["content"]
    assert "只修改现有模块" in model.messages[1]["content"]
    assert model.messages[-1] == {"role": "user", "content": "在当前目录创建 bubble_sort.py"}


def test_run_without_task_enters_prompt_and_reuses_session(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = importlib.import_module("deity.interfaces.cli.main")
    calls: list[tuple[str, str | None]] = []

    class FakeService:
        async def run(
            self, task: str, *, model_name: str, session_id: str | None = None
        ) -> AgentResult:
            calls.append((task, session_id))
            return AgentResult(str(len(calls)), RunStatus.COMPLETED, f"reply-{len(calls)}")

    config = Config(
        model=ModelConfig("openai_compatible", "fake", "FAKE_KEY", context_window_tokens=8192)
    )
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli.RunService, "from_config", lambda *args, **kwargs: FakeService())
    result = CliRunner().invoke(cli.app, ["run"], input="我想你\n你是谁\n/exit\n")
    assert result.exit_code == 0, result.output
    assert [task for task, _ in calls] == ["我想你", "你是谁"]
    assert calls[0][1] is not None and calls[0][1] == calls[1][1]
    assert "reply-1" in result.output and "reply-2" in result.output


def test_bare_deity_enters_interactive_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = importlib.import_module("deity.interfaces.cli.main")
    calls: list[str] = []

    class FakeService:
        async def run(self, task: str, **kwargs: Any) -> AgentResult:
            calls.append(task)
            return AgentResult("run-1", RunStatus.COMPLETED, "reply")

    config = Config(model=ModelConfig("openai_compatible", "fake", "FAKE_KEY"))
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli.RunService, "from_config", lambda *args, **kwargs: FakeService())
    result = CliRunner().invoke(cli.app, [], input="你好\n/exit\n")
    assert result.exit_code == 0, result.output
    assert calls == ["你好"]
    assert "reply" in result.output
    assert "欢迎来到 Deity，我是你的编程助手。" in result.output
    assert "Deity> reply" in result.output
    assert "Deity 交互模式" not in result.output
    assert "你>" not in result.output


def test_run_accepts_unquoted_chinese_words(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = importlib.import_module("deity.interfaces.cli.main")
    calls: list[str] = []

    class FakeService:
        async def run(self, task: str, **kwargs: Any) -> AgentResult:
            calls.append(task)
            return AgentResult("run-1", RunStatus.COMPLETED, "ok")

    config = Config(model=ModelConfig("openai_compatible", "fake", "FAKE_KEY"))
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli.RunService, "from_config", lambda *args, **kwargs: FakeService())
    result = CliRunner().invoke(cli.app, ["run", "我想问你", "一件事"])
    assert result.exit_code == 0, result.output
    assert calls == ["我想问你 一件事"]
