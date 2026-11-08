"""Phase 2：状态汇总（轮次/工具数/耗时/token，只读聚合，不编造数字）。"""

from deity.core.contracts.client import ClientEvent
from deity.interfaces.cli.chat_status import ChatStatus


def _event(seq: int, kind: str, **payload: object) -> ClientEvent:
    return ClientEvent(seq=seq, run_id="r1", kind=kind, payload=dict(payload))


def test_status_counts_turns_tools_and_tokens() -> None:
    status = ChatStatus()
    status.update(_event(1, "text_delta", text="hi"))
    status.update(_event(2, "tool_call_completed", call_id="c1", name="read_file", status="ok"))
    status.update(_event(3, "usage", input_tokens=10, output_tokens=4))
    status.update(_event(4, "message_completed"))
    status.update(_event(5, "run_status", status="completed", final_text="hi"))
    assert status.turns == 1
    assert status.tool_calls == 1
    assert status.input_tokens == 10
    assert status.output_tokens == 4
    line = status.summary_line(model_name="fake", mode="普通")
    assert "fake" in line and "普通" in line
    assert "第 1 轮" in line and "工具 1 次" in line
    assert "10" in line and "4" in line


def test_status_without_usage_shows_dash() -> None:
    status = ChatStatus()
    status.update(_event(1, "message_completed"))
    line = status.summary_line(model_name="fake", mode="普通")
    assert "—" in line
    assert "token 未接通" in status.cost_text()


def test_cost_text_lists_rounds_tools_elapsed() -> None:
    status = ChatStatus()
    status.update(_event(1, "tool_call_completed", call_id="c1", name="shell", status="ok"))
    status.update(_event(2, "usage", input_tokens=3, output_tokens=2))
    text = status.cost_text()
    assert "轮次" in text and "工具调用" in text and "耗时" in text
    assert "3" in text and "2" in text
