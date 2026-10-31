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
    assert [(item["role"], item["content"]) for item in model.requests[1].messages] == [
        ("user", "first question"),
        ("assistant", "answer-1"),
        ("user", "second question"),
    ]


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
