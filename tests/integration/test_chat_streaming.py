"""Phase 2：FakeModel 端到端流式（最终文本与 run() 一致、工具树可见）。"""

import io
from pathlib import Path

import pytest
from rich.console import Console

from deity.application.config import ChatConfig, PermissionConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent
from deity.interfaces.cli.chat_session import ChatSession


class FakeModel:
    def __init__(self, turns: list[list[ModelEvent]]) -> None:
        self.turns = iter(turns)

    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        for event in next(self.turns):
            yield event


def _service(tmp_path: Path, model: FakeModel) -> RunService:
    return RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,  # type: ignore[arg-type]
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
    )


async def _run_session(service: RunService, inputs: list[str], tmp_path: Path) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=100)
    session = ChatSession(service, "fake", None, config=ChatConfig(), output=console)
    script = iter(inputs)

    import builtins

    old_input = builtins.input
    builtins.input = lambda prompt_text="": next(script)  # type: ignore[assignment]
    try:
        await session.run()
    finally:
        builtins.input = old_input
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_streaming_final_text_matches_run(tmp_path: Path) -> None:
    turns = [
        [
            ModelEvent("text_delta", text="你好"),
            ModelEvent("text_delta", text="世界"),
            ModelEvent("usage", input_tokens=10, output_tokens=4),
            ModelEvent("message_completed"),
        ]
    ]
    service = _service(tmp_path, FakeModel(turns))
    output = await _run_session(service, ["打招呼", "/exit"], tmp_path)
    assert "你好世界" in output
    assert "第 1 轮" in output
    assert "10" in output and "4" in output

    expected = await _service(tmp_path, FakeModel(turns)).run(
        "打招呼", model_name="fake", session_id="other"
    )
    assert expected.final_text == "你好世界"


@pytest.mark.asyncio
async def test_streaming_shows_tool_call_tree(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    turns = [
        [
            ModelEvent("tool_call_started", call_id="c1", name="read_file"),
            ModelEvent("tool_call_arguments_delta", call_id="c1", text='{"path": "a.txt"}'),
            ModelEvent("message_completed"),
        ],
        [ModelEvent("text_delta", text="读到了"), ModelEvent("message_completed")],
    ]
    service = _service(tmp_path, FakeModel(turns))
    output = await _run_session(service, ["读 a.txt", "/exit"], tmp_path)
    assert "read_file" in output
    assert "a.txt" in output
    assert "读到了" in output
    assert "工具 1 次" in output


@pytest.mark.asyncio
async def test_streaming_disabled_falls_back_to_whole_print(tmp_path: Path) -> None:
    turns = [[ModelEvent("text_delta", text="你好"), ModelEvent("message_completed")]]
    service = _service(tmp_path, FakeModel(turns))
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=100)
    session = ChatSession(service, "fake", None, config=ChatConfig(streaming=False), output=console)
    script = iter(["打招呼", "/exit"])

    import builtins

    old_input = builtins.input
    builtins.input = lambda prompt_text="": next(script)  # type: ignore[assignment]
    try:
        await session.run()
    finally:
        builtins.input = old_input
    output = buffer.getvalue()
    assert "你好" in output
    assert "第 1 轮" not in output
