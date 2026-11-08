"""S2/S4 TUI 回放：录制事件回放渲染快照，断线重连不重复提交审批。"""

import json

from deity.core.contracts.client import ApprovalRequest, ClientEvent
from deity.interfaces.tui.view_model import RunViewModel

RECORDED: list[ClientEvent] = [
    ClientEvent(seq=1, run_id="r1", kind="RunStatusChanged", payload={"status": "running"}),
    ClientEvent(seq=2, run_id="r1", kind="text_delta", payload={"text": "分析中 "}),
    ClientEvent(
        seq=3, run_id="r1", kind="tool_call_started", payload={"call_id": "c1", "name": "shell"}
    ),
    ClientEvent(
        seq=4, run_id="r1", kind="ToolCompleted", payload={"call_id": "c1", "status": "ok"}
    ),
    ClientEvent(seq=5, run_id="r1", kind="text_delta", payload={"text": "完成"}),
    ClientEvent(seq=6, run_id="r1", kind="RunStatusChanged", payload={"status": "completed"}),
]


def _replay(events: list[ClientEvent]) -> dict:
    model = RunViewModel(run_id="r1")
    for event in events:
        model.apply(event)
    return model.snapshot()


def test_replay_renders_final_snapshot() -> None:
    snapshot = _replay(RECORDED)
    assert snapshot["status"] == "completed"
    assert snapshot["text"] == "分析中 完成"
    assert snapshot["tool_calls"][0]["status"] == "ok"
    json.dumps(snapshot, ensure_ascii=False)


def test_replay_after_reconnect_does_not_duplicate() -> None:
    first = _replay(RECORDED)
    model = RunViewModel(run_id="r1")
    for event in RECORDED:
        model.apply(event)
    for event in RECORDED:  # 断线重连后同一批事件再次到达
        model.apply(event)
    second = model.snapshot()
    assert second == first


def test_replay_approval_not_resubmitted_after_reconnect() -> None:
    model = RunViewModel(run_id="r1")
    request = ApprovalRequest("a1", "r1", "c1", "shell", {"argv": ["ls"]})
    model.apply_approval(request)
    model.decide_approval("a1", "approved")
    model.apply_approval(request)  # 重连重放同一审批
    approvals = model.snapshot()["approvals"]
    assert len(approvals) == 1
    assert approvals[0]["status"] == "approved"
