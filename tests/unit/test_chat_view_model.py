"""V1.1-M2：CLI 视图模型（事件折叠成状态树、delta、工具节点折叠/展开）。"""

from deity.core.contracts.client import ClientEvent
from deity.interfaces.cli.chat_view_model import ChatViewModel, ToolNode


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


def _tool_node(name: str, args_text: str) -> ToolNode:
    return ToolNode(call_id="c1", name=name, fragments=[args_text])


def test_display_label_verb_labels() -> None:
    assert _tool_node("read_file", '{"path": "a.txt"}').display_label() == "读 a.txt"
    assert _tool_node("list_files", '{"path": "src"}').display_label() == "列 src"
    assert _tool_node("glob", '{"pattern": "**/*.py"}').display_label() == "找 **/*.py"
    assert _tool_node("grep", '{"pattern": "foo", "path": "src"}').display_label() == (
        "搜 “foo”（位于 src）"
    )
    assert _tool_node("grep", '{"pattern": "foo"}').display_label() == "搜 “foo”"
    assert _tool_node("grep", '{"pattern": "foo", "path": "."}').display_label() == "搜 “foo”"
    assert _tool_node("shell", '{"argv": ["pytest", "-q"]}').display_label() == "$ pytest -q"
    assert _tool_node("shell", '{"session": "s1"}').display_label() == "$ 会话 s1（轮询）"
    assert _tool_node("write_file", '{"path": "b.txt"}').display_label() == "写 b.txt"
    assert _tool_node("apply_patch", '{"path": "c.txt"}').display_label() == "改 c.txt"
    assert _tool_node("delete_file", '{"path": "d.txt"}').display_label() == "删 d.txt"


def test_display_label_falls_back_to_name_summary() -> None:
    node = _tool_node("unknown_tool", '{"x": 1}')
    assert node.display_label() == 'unknown_tool({"x": 1})'
    assert _tool_node("read_file", "").display_label() == "read_file"


def test_turn_diff_event_folds_into_view() -> None:
    view = ChatViewModel()
    payload = {
        "files": [{"path": "a.py", "status": "modified", "diff": "@@ -1,1 +1,1 @@\n-x\n+y\n"}],
        "files_changed": 1,
    }
    delta = view.apply(_event(1, "turn_diff", **payload))
    assert delta.diff == payload
    assert view.diffs == [payload]


def test_turn_diff_after_terminal_is_dropped() -> None:
    view = ChatViewModel()
    view.apply(_event(1, "run_status", status="completed", final_text="done"))
    delta = view.apply(_event(2, "turn_diff", files=[], files_changed=0))
    assert delta.diff is None
    assert view.diffs == []
