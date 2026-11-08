"""Phase 4：录制回放回归（零真实副作用）与 [chat] enabled=false 回退。"""

import builtins
import io
import re
from pathlib import Path

import pytest
from rich.console import Console

from deity.application.config import ChatConfig, PermissionConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent
from deity.interfaces.cli.chat_input import PlainInput, PromptToolkitInput, create_chat_input
from deity.interfaces.cli.chat_session import ChatSession


class FakeModel:
    def __init__(self, turns: list[list[ModelEvent]]) -> None:
        self.turns = iter(turns)

    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        for event in next(self.turns):
            yield event


def _service(tmp_path: Path, db_name: str, model: FakeModel) -> RunService:
    return RunService(
        db_path=tmp_path / db_name,
        workspace=tmp_path,
        model=model,  # type: ignore[arg-type]
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
    )


def _model() -> FakeModel:
    return FakeModel(
        [
            [
                ModelEvent("tool_call_started", call_id="c1", name="read_file"),
                ModelEvent("tool_call_arguments_delta", call_id="c1", text='{"path": "a.txt"}'),
                ModelEvent("message_completed"),
            ],
            [
                ModelEvent("text_delta", text="已读取"),
                ModelEvent("usage", input_tokens=5, output_tokens=2),
                ModelEvent("message_completed"),
            ],
        ]
    )


async def _run_script(
    service: RunService, inputs: list[str], tmp_path: Path, session_id: str
) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=100)
    session = ChatSession(service, "fake", session_id, config=ChatConfig(), output=console)
    script = iter(inputs)
    old_input = builtins.input
    builtins.input = lambda prompt_text="": next(script)  # type: ignore[assignment]
    try:
        await session.run()
    finally:
        builtins.input = old_input
    return buffer.getvalue()


_SCRIPT = [
    "/help",
    "读取 a.txt",
    "/cost",
    "/status",
    "/model fake-x",
    "/model",
    "/compact",
    "/sessions",
    "1",
    "/resume",
    "/auto",
    "no",
    "/clear",
    "/exit",
]


def _normalize(output: str, tmp_path: Path) -> str:
    text = output.replace(str(tmp_path), "<WS>")
    text = re.sub(r"\b[0-9a-f]{8}\b", "<ID>", text)
    text = re.sub(r"\d+\.\d+s", "<T>s", text)
    return text


@pytest.mark.asyncio
async def test_recorded_replay_is_deterministic_and_side_effect_free(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    recording = await _run_script(
        _service(tmp_path, "rec.db", _model()), _SCRIPT, tmp_path, "replay-session"
    )
    replay = await _run_script(
        _service(tmp_path, "replay.db", _model()), _SCRIPT, tmp_path, "replay-session"
    )
    assert _normalize(replay, tmp_path) == _normalize(recording, tmp_path)
    assert "已读取" in recording
    assert "token：输入 5 · 输出 2" in recording
    # 零真实副作用：工作区除夹具文件与两个 SQLite 库（及其 WAL 伴生文件）外
    # 无任何新增/修改。
    names = {item.name for item in tmp_path.iterdir() if not item.name.endswith(("-wal", "-shm"))}
    assert names == {"a.txt", "rec.db", "replay.db"}


def test_chat_disabled_falls_back_to_plain_input(tmp_path: Path) -> None:
    disabled = create_chat_input(ChatConfig(enabled=False), workspace=tmp_path, tty=True)
    assert isinstance(disabled, PlainInput)
    enabled = create_chat_input(ChatConfig(enabled=True), workspace=tmp_path, tty=True)
    assert isinstance(enabled, PromptToolkitInput)


@pytest.mark.asyncio
async def test_chat_disabled_disables_streaming(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=100)
    service = _service(tmp_path, "off.db", _model())
    session = ChatSession(
        service, "fake", "s-off", config=ChatConfig(enabled=False), output=console
    )
    script = iter(["读取 a.txt", "/exit"])
    old_input = builtins.input
    builtins.input = lambda prompt_text="": next(script)  # type: ignore[assignment]
    try:
        await session.run()
    finally:
        builtins.input = old_input
    output = buffer.getvalue()
    assert "已读取" in output
    # 回退走整段打印：无流式工具树、无流式汇总行。
    assert "▸ read_file" not in output
    assert "第 2 轮" not in output


@pytest.mark.asyncio
async def test_chat_enabled_streams_when_terminal(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    output = await _run_script(
        _service(tmp_path, "on.db", _model()),
        ["读取 a.txt", "/exit"],
        tmp_path,
        "s-on",
    )
    assert "已读取" in output
    assert "第 2 轮" in output
