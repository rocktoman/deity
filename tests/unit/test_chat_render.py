"""Phase 2：流式渲染器（行式增量输出、工具调用树、diff 截断、状态汇总）。"""

import asyncio
import io
import time

from rich.console import Console

from deity.core.contracts.client import ClientEvent
from deity.interfaces.cli.chat_render import (
    StreamingRenderer,
    print_diff,
    render_diff,
)


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, force_terminal=False, width=100), buffer


def _event(seq: int, kind: str, run_id: str = "r1", **payload: object) -> ClientEvent:
    return ClientEvent(seq=seq, run_id=run_id, kind=kind, payload=dict(payload))


async def _drain(renderer: StreamingRenderer, events: list[ClientEvent]):  # type: ignore[no-untyped-def]
    async def _stream():  # type: ignore[no-untyped-def]
        for event in events:
            yield event

    return await renderer.render(_stream())


def test_text_deltas_stream_in_order() -> None:
    console, buffer = _console()
    renderer = StreamingRenderer(console, model_name="fake")
    summary = asyncio.run(
        _drain(
            renderer,
            [
                _event(1, "text_delta", text="你好"),
                _event(2, "text_delta", text="世界"),
                _event(3, "message_completed"),
                _event(4, "run_status", status="completed", final_text="你好世界"),
            ],
        )
    )
    output = buffer.getvalue()
    assert output.index("你好") < output.index("世界")
    assert summary.final_text == "你好世界"
    assert summary.status == "completed"
    assert summary.turns == 1


def test_tool_call_tree_shows_start_and_completion() -> None:
    console, buffer = _console()
    renderer = StreamingRenderer(console, model_name="fake")
    summary = asyncio.run(
        _drain(
            renderer,
            [
                _event(1, "tool_call_started", call_id="c1", name="read_file"),
                _event(
                    2,
                    "tool_call_arguments_delta",
                    call_id="c1",
                    text='{"path": "a.txt"}',
                ),
                _event(3, "message_completed"),
                _event(4, "tool_call_completed", call_id="c1", name="read_file", status="ok"),
                _event(5, "text_delta", text="done"),
                _event(6, "message_completed"),
                _event(7, "run_status", status="completed", final_text="done"),
            ],
        )
    )
    output = buffer.getvalue()
    assert "read_file" in output
    assert "a.txt" in output
    assert "ok" in output
    assert summary.tool_calls == 1
    assert summary.turns == 2


def test_usage_events_feed_status_summary() -> None:
    console, buffer = _console()
    renderer = StreamingRenderer(console, model_name="fake")
    summary = asyncio.run(
        _drain(
            renderer,
            [
                _event(1, "text_delta", text="hi"),
                _event(2, "usage", input_tokens=10, output_tokens=4),
                _event(3, "message_completed"),
                _event(4, "run_status", status="completed", final_text="hi"),
            ],
        )
    )
    assert summary.input_tokens == 10
    assert summary.output_tokens == 4
    assert "10" in buffer.getvalue() and "4" in buffer.getvalue()


def test_missing_usage_shows_dash_not_fabricated_tokens() -> None:
    console, buffer = _console()
    renderer = StreamingRenderer(console, model_name="fake")
    summary = asyncio.run(
        _drain(
            renderer,
            [
                _event(1, "text_delta", text="hi"),
                _event(2, "message_completed"),
                _event(3, "run_status", status="completed", final_text="hi"),
            ],
        )
    )
    assert summary.input_tokens is None
    assert "—" in buffer.getvalue()


def test_cancelled_run_prints_only_cancel_summary() -> None:
    console, buffer = _console()
    renderer = StreamingRenderer(console, model_name="fake")
    summary = asyncio.run(
        _drain(
            renderer,
            [
                _event(1, "text_delta", text="slow"),
                _event(2, "run_status", status="cancelled", final_text=None),
                _event(3, "text_delta", text="must-not-appear"),
            ],
        )
    )
    output = buffer.getvalue()
    assert summary.status == "cancelled"
    assert "已取消" in output
    assert "must-not-appear" not in output


def test_text_flush_is_throttled_by_time_and_size() -> None:
    console, buffer = _console()
    clock = [0.0]
    renderer = StreamingRenderer(
        console, model_name="fake", clock=lambda: clock[0], flush_min_chars=4096
    )
    renderer.apply(_event(1, "text_delta", text="abc"))
    assert buffer.getvalue() == ""
    clock[0] = 0.06
    renderer.apply(_event(2, "text_delta", text="d"))
    assert "abc" in buffer.getvalue()
    renderer.apply(_event(3, "text_delta", text="e" * 5000))
    assert "e" * 100 in buffer.getvalue()


def test_long_output_backpressure() -> None:
    console, buffer = _console()
    renderer = StreamingRenderer(console, model_name="fake")
    big = "x" * 100_000
    events = [_event(1, "text_delta", text=big)]
    events += [_event(2 + i, "text_delta", text="y") for i in range(999)]
    events.append(_event(1001, "message_completed"))
    events.append(_event(1002, "run_status", status="completed", final_text="done"))
    started = time.monotonic()
    summary = asyncio.run(_drain(renderer, events))
    assert time.monotonic() - started < 10
    assert summary.status == "completed"
    assert buffer.getvalue().count("x") == 100_000
    assert buffer.getvalue().count("y") == 999


def test_render_diff_truncates_long_diffs() -> None:
    old = "".join(f"line {i}\n" for i in range(50))
    new = "".join(f"changed {i}\n" for i in range(300))
    diff = render_diff(old, new, path="a.txt", max_lines=200)
    assert diff.count("\n") <= 201
    assert "已截断" in diff
    assert "a.txt" in diff


def test_render_diff_shows_add_remove_lines() -> None:
    diff = render_diff("a\nb\n", "a\nc\n", path="a.txt")
    assert "-b" in diff
    assert "+c" in diff
    console, buffer = _console()
    print_diff(console, "a\nb\n", "a\nc\n", path="a.txt")
    assert "-b" in buffer.getvalue()
    assert "+c" in buffer.getvalue()
