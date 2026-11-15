"""V1.1-M3：Markdown 渲染、工具节点折叠/展开、状态条、思考指示。"""

import asyncio
import io

from rich.console import Console

from deity.core.contracts.client import ClientEvent
from deity.interfaces.cli.chat_render import StreamingRenderer


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, force_terminal=False, width=100), buffer


def _event(seq: int, kind: str, **payload: object) -> ClientEvent:
    return ClientEvent(seq=seq, run_id="r1", kind=kind, payload=dict(payload))


async def _drain(renderer: StreamingRenderer, events: list[ClientEvent]):  # type: ignore[no-untyped-def]
    async def _stream():  # type: ignore[no-untyped-def]
        for event in events:
            yield event

    return await renderer.render(_stream())


def test_markdown_mode_renders_blocks_and_keeps_text() -> None:
    console, buffer = _console()
    renderer = StreamingRenderer(console, model_name="fake", render_markdown=True)
    summary = asyncio.run(
        _drain(
            renderer,
            [
                _event(1, "text_delta", text="# 标题\n\n正文 **加粗** 内容"),
                _event(2, "message_completed"),
                _event(
                    3, "run_status", status="completed", final_text="# 标题\n\n正文 **加粗** 内容"
                ),
            ],
        )
    )
    output = buffer.getvalue()
    assert "标题" in output
    assert "正文" in output and "加粗" in output
    assert summary.final_text is not None and "加粗" in summary.final_text


def test_markdown_off_keeps_plain_streaming() -> None:
    console, buffer = _console()
    renderer = StreamingRenderer(console, model_name="fake")
    asyncio.run(
        _drain(
            renderer,
            [
                _event(1, "text_delta", text="**原文**"),
                _event(2, "run_status", status="completed", final_text="**原文**"),
            ],
        )
    )
    assert "**原文**" in buffer.getvalue()


def test_tool_details_expand_full_arguments() -> None:
    console, buffer = _console()
    renderer = StreamingRenderer(console, model_name="fake")
    long_value = "x" * 200
    asyncio.run(
        _drain(
            renderer,
            [
                _event(1, "tool_call_started", call_id="c1", name="shell"),
                _event(
                    2,
                    "tool_call_arguments_delta",
                    call_id="c1",
                    text=f'{{"command": "echo {long_value}"}}',
                ),
                _event(3, "tool_call_completed", call_id="c1", name="shell", status="ok"),
                _event(4, "run_status", status="completed", final_text="done"),
            ],
        )
    )
    folded_output = buffer.getvalue()
    assert "已折叠" in folded_output
    renderer.render_tool_details("c1")
    expanded_output = buffer.getvalue()
    # 终端按宽度折行，完整参数以去掉折行换行后的形式断言。
    assert long_value in expanded_output.replace("\n", "")


def test_status_line_shows_current_tool_and_context() -> None:
    console, _buffer = _console()
    renderer = StreamingRenderer(console, model_name="fake", context_window_tokens=1000)
    renderer.apply(_event(1, "tool_call_started", call_id="c1", name="shell"))
    line = renderer.status_line()
    assert "当前工具 shell" in line
    renderer.apply(_event(2, "usage", input_tokens=250, output_tokens=10))
    line = renderer.status_line()
    assert "上下文 250/1000" in line


def test_thinking_indicator_prints_once_per_idle_period() -> None:
    console, buffer = _console()
    now = [0.0]
    renderer = StreamingRenderer(
        console,
        model_name="fake",
        clock=lambda: now[0],
        thinking_indicator=True,
    )
    renderer.note_idle()
    assert "思考中" not in buffer.getvalue()
    now[0] = 3.0
    renderer.note_idle()
    assert "思考中" in buffer.getvalue()
    renderer.note_idle()
    assert buffer.getvalue().count("思考中") == 1
    renderer.apply(_event(1, "text_delta", text="hi"))
    now[0] = 6.0
    renderer.note_idle()
    assert buffer.getvalue().count("思考中") == 2
