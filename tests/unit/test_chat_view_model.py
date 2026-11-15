"""V1.1-M2：CLI 视图模型（事件折叠成状态树、delta、工具节点折叠/展开）。"""

from deity.core.contracts.client import ClientEvent
from deity.interfaces.cli.chat_view_model import ChatViewModel


def _event(seq: int, kind: str, **payload: object) -> ClientEvent:
    return ClientEvent(seq=seq, run_id="r1", kind=kind, payload=dict(payload))


def test_view_model_folds_text_tools_and_terminal() -> None:
    now = [100.0]
    view = ChatViewModel(clock=lambda: now[0])
    delta = view.apply(_event(1, "text_delta", text="你好"))
    assert delta.text_appended and view.text == "你好"
    delta = view.apply(_event(2, "tool_call_started", call_id="c1", name="read_file"))
    assert delta.tool_started is not None and delta.tool_started.name == "read_file"
    assert view.current_tool is not None and view.current_tool.call_id == "c1"
    view.apply(_event(3, "tool_call_arguments_delta", call_id="c1", text='{"path": "a.txt"}'))
    now[0] = 101.5
    delta = view.apply(
        _event(4, "tool_call_completed", call_id="c1", name="read_file", status="ok")
    )
    assert delta.tool_completed is not None
    assert delta.tool_completed.elapsed(lambda: now[0]) == 1.5
    assert delta.tool_completed.arguments_summary() == '{"path": "a.txt"}'
    assert view.current_tool is None
    view.apply(_event(5, "usage", input_tokens=10, output_tokens=4))
    view.apply(_event(6, "message_completed"))
    delta = view.apply(_event(7, "run_status", status="completed", final_text="你好"))
    assert delta.terminal and view.terminal
    assert view.final_text == "你好"
    assert view.status_text == "completed"
    assert view.status.turns == 1 and view.status.tool_calls == 1
    assert view.status.input_tokens == 10 and view.status.output_tokens == 4


def test_events_after_terminal_are_dropped() -> None:
    view = ChatViewModel()
    view.apply(_event(1, "run_status", status="cancelled", final_text=None))
    delta = view.apply(_event(2, "text_delta", text="must-not-appear"))
    assert not delta.text_appended
    assert view.text == ""
    assert view.final_text is None


def test_toggle_expanded_flips_node_state() -> None:
    view = ChatViewModel()
    view.apply(_event(1, "tool_call_started", call_id="c1", name="shell"))
    assert view.tools["c1"].expanded is False
    assert view.toggle_expanded("c1") is True
    assert view.tools["c1"].expanded is True
    assert view.toggle_expanded("missing") is False


def test_arguments_summary_truncates_and_hides_content() -> None:
    view = ChatViewModel()
    view.apply(_event(1, "tool_call_started", call_id="c1", name="write_file"))
    view.apply(
        _event(
            2,
            "tool_call_arguments_delta",
            call_id="c1",
            text='{"path": "b.txt", "content": "secret-body"}',
        )
    )
    summary = view.tools["c1"].arguments_summary()
    assert "b.txt" in summary
    assert "secret-body" not in summary
