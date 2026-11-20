"""TUI 超长正文回归：不设字符上限，完整文本必须到达渲染层。

覆盖 `RunViewModel` 与 `TuiApp` 两端：
- 视图模型不丢弃任何前缀（曾经超过 10_000 字符会丢掉开头）；
- Textual 渲染层拿到完整文本（Screen 为 `overflow-y: auto`，可滚动看全）。
"""

import pytest
from textual.widgets import Static

from deity.core.contracts.client import ClientEvent
from deity.interfaces.tui.view_model import RunViewModel


def test_view_model_keeps_full_long_text() -> None:
    """50_000 字符正文完整保留，头尾都在，无截断标记。"""
    model = RunViewModel(run_id="r1")
    head = "开头：分析结论如下\n"
    body = "内容" * 25_000
    tail = "\n结尾：以上为最终回答"
    model.apply(ClientEvent(seq=1, run_id="r1", kind="text_delta", payload={"text": head}))
    model.apply(ClientEvent(seq=2, run_id="r1", kind="text_delta", payload={"text": body}))
    model.apply(ClientEvent(seq=3, run_id="r1", kind="text_delta", payload={"text": tail}))
    snapshot = model.snapshot()
    assert snapshot["text"] == head + body + tail
    assert snapshot["text"].startswith("开头")
    assert snapshot["text"].endswith("最终回答")
    assert snapshot["dropped_chars"] == 0


@pytest.mark.asyncio
async def test_tui_renders_long_text_without_truncation() -> None:
    """TuiApp 真实渲染：长文本未被丢弃，且完整交给 Static 组件。"""
    pytest.importorskip("textual")
    from deity.interfaces.tui.app import TuiApp

    long_text = "HEAD" + "x" * 20_000 + "TAIL"
    app = TuiApp()
    async with app.run_test() as pilot:
        app.feed(ClientEvent(seq=1, run_id="r1", kind="text_delta", payload={"text": long_text}))
        await pilot.pause()
        assert app.view_model.snapshot()["text"] == long_text
        rendered = str(app.query_one("#run-text", Static).content)
        assert rendered == long_text
        assert rendered.startswith("HEAD")
        assert rendered.endswith("TAIL")
        await pilot.press("q")
