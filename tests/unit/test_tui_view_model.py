"""S2 TUI ViewModel：与 Textual 解耦的纯数据折叠，可单测。"""

import json

from deity.core.contracts.client import ApprovalRequest, ClientEvent
from deity.interfaces.tui.view_model import RunViewModel


def _event(seq: int, kind: str, payload: dict | None = None) -> ClientEvent:
    return ClientEvent(seq=seq, run_id="r1", kind=kind, payload=payload or {})


def test_folds_text_deltas_in_order() -> None:
    model = RunViewModel(run_id="r1")
    model.apply(_event(1, "text_delta", {"text": "hello "}))
    model.apply(_event(2, "text_delta", {"text": "world"}))
    assert model.snapshot()["text"] == "hello world"
    assert model.snapshot()["events_seen"] == 2


def test_unicode_text_preserved() -> None:
    model = RunViewModel(run_id="r1")
    model.apply(_event(1, "text_delta", {"text": "你好 🎉 ünïcode"}))
    assert "你好 🎉 ünïcode" in model.snapshot()["text"]


def test_long_text_truncated_with_backpressure_count() -> None:
    model = RunViewModel(run_id="r1", max_text_chars=100)
    model.apply(_event(1, "text_delta", {"text": "x" * 500}))
    snapshot = model.snapshot()
    assert len(snapshot["text"]) <= 100
    assert snapshot["dropped_chars"] > 0
    assert snapshot["text"].endswith("…")


def test_tool_tree_dedups_same_call_id() -> None:
    model = RunViewModel(run_id="r1")
    model.apply(_event(1, "tool_call_started", {"call_id": "c1", "name": "write_file"}))
    model.apply(_event(2, "tool_call_started", {"call_id": "c1", "name": "write_file"}))
    model.apply(_event(3, "ToolCompleted", {"call_id": "c1", "status": "ok"}))
    tools = model.snapshot()["tool_calls"]
    assert len(tools) == 1
    assert tools[0]["call_id"] == "c1"
    assert tools[0]["status"] == "ok"


def test_approval_dedup_and_single_decision() -> None:
    model = RunViewModel(run_id="r1")
    request = ApprovalRequest("a1", "r1", "c1", "write_file", {"path": "b.txt"})
    model.apply_approval(request)
    model.apply_approval(request)
    assert len(model.snapshot()["approvals"]) == 1
    assert model.decide_approval("a1", "approved") is True
    assert model.decide_approval("a1", "denied") is False
    assert model.snapshot()["approvals"][0]["status"] == "approved"


def test_status_follows_run_status_events() -> None:
    model = RunViewModel(run_id="r1")
    model.apply(_event(1, "RunStatusChanged", {"status": "running"}))
    assert model.snapshot()["status"] == "running"
    model.apply(_event(2, "RunStatusChanged", {"status": "completed"}))
    assert model.snapshot()["status"] == "completed"


def test_out_of_order_events_not_applied() -> None:
    model = RunViewModel(run_id="r1")
    model.apply(_event(2, "text_delta", {"text": "second"}))
    model.apply(_event(1, "text_delta", {"text": "first"}))
    assert model.snapshot()["text"] == "second"


def test_snapshot_is_json_serializable() -> None:
    model = RunViewModel(run_id="r1")
    model.apply(_event(1, "text_delta", {"text": "hi"}))
    model.apply(_event(2, "tool_call_started", {"call_id": "c1", "name": "shell"}))
    model.apply_approval(ApprovalRequest("a1", "r1", "c1", "shell", {"argv": ["ls"]}))
    json.dumps(model.snapshot(), ensure_ascii=False)


def test_cancel_then_resume_does_not_resubmit_approval() -> None:
    model = RunViewModel(run_id="r1")
    model.apply(_event(1, "RunStatusChanged", {"status": "cancelled"}))
    model.apply_approval(ApprovalRequest("a1", "r1", "c1", "write_file", {"path": "b.txt"}))
    model.decide_approval("a1", "approved")
    model.apply(_event(2, "RunStatusChanged", {"status": "running"}))
    model.apply_approval(ApprovalRequest("a1", "r1", "c1", "write_file", {"path": "b.txt"}))
    approvals = model.snapshot()["approvals"]
    assert len(approvals) == 1
    assert approvals[0]["status"] == "approved"
