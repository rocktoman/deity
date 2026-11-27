"""TUI 纯 Python 状态模型测试：ToolGroupState、InputQueue、ScrollThrottler 与问候语。"""

import pytest

from deity.interfaces.tui.state import (
    InputQueue,
    ScrollThrottler,
    ToolGroupState,
    greeting_for_hour,
)


def test_greeting_for_hour_periods() -> None:
    assert greeting_for_hour(8) == "早上好"
    assert greeting_for_hour(14) == "下午好"
    assert greeting_for_hour(20) == "晚上好"
    assert greeting_for_hour(2) == "夜深了"


def test_scroll_throttler_coalesces_and_flushes() -> None:
    now = [0.0]
    throttler = ScrollThrottler(interval=0.2, clock=lambda: now[0])
    assert throttler.request() is True
    now[0] = 0.05
    assert throttler.request() is False
    assert throttler.flush_due() is False
    now[0] = 0.25
    assert throttler.flush_due() is True
    assert throttler.flush_due() is False
    throttler.force()
    assert throttler.flush_due() is False


def test_tool_group_state_tracks_progress_and_failures() -> None:
    group = ToolGroupState(group_id="g1", title="代码读取", started_at=10.0)
    group.add_tool("c1")
    group.add_tool("c1")
    group.add_tool("c2")
    assert len(group.tool_ids) == 2
    assert "10.0" not in group.summary_label(lambda: 10.5)
    assert "0/2 个工具 · 0.5s" in group.summary_label(lambda: 10.5)

    group.mark_completed("ok", now=10.8)
    assert "1/2 个工具" in group.summary_label(lambda: 10.8)

    group.mark_completed("error", now=11.2)
    summary = group.summary_label(lambda: 12.0)
    assert "2 个工具（1 失败）· 1.2s" in summary


def test_input_queue_fifo_capacity_and_clear() -> None:
    with pytest.raises(ValueError):
        InputQueue(max_size=0)

    queue = InputQueue(max_size=2)
    assert queue.enqueue("   ") is False
    assert queue.enqueue("任务 A") is True
    assert queue.enqueue("任务 B") is True
    assert queue.enqueue("任务 C") is False
    assert queue.size == 2
    assert queue.items() == ("任务 A", "任务 B")
    assert queue.pop_next() == "任务 A"
    assert queue.clear() == 1
    assert queue.pop_next() is None
