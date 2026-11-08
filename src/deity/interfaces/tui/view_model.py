"""TUI 视图模型：从 ClientEvent 折叠出可渲染状态，与 Textual 解耦、可单测。

不导入 Textual；TuiApp 只负责把 snapshot() 画出来。
"""

from dataclasses import dataclass, field
from typing import Any, Literal, cast

from deity.core.contracts.client import ApprovalRequest, ClientEvent

ToolStatus = Literal["pending", "ok", "error", "denied", "timeout", "cancelled", "unknown"]


@dataclass
class ToolCallView:
    call_id: str
    name: str
    status: ToolStatus = "pending"
    output: str = ""


@dataclass
class ApprovalView:
    approval_id: str
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    status: Literal["pending", "approved", "denied"] = "pending"


class RunViewModel:
    """折叠事件流为文本、工具树、审批列表与状态；乱序事件不应用。"""

    def __init__(
        self,
        run_id: str = "",
        *,
        max_text_chars: int = 10_000,
        max_tool_output_chars: int = 2_000,
    ) -> None:
        self.run_id = run_id
        self.max_text_chars = max_text_chars
        self.max_tool_output_chars = max_tool_output_chars
        self.status: str = "created"
        self.text: str = ""
        self.dropped_chars: int = 0
        self.events_seen: int = 0
        self._last_seq: int = 0
        self._tools: dict[str, ToolCallView] = {}
        self._approvals: dict[str, ApprovalView] = {}

    def apply(self, event: ClientEvent) -> bool:
        """应用一条事件；seq 不递增返回 False（乱序/重放，不应用）。"""
        if event.seq <= self._last_seq:
            return False
        self._last_seq = event.seq
        self.events_seen += 1
        payload = event.payload
        if event.kind == "text_delta":
            self._append_text(str(payload.get("text", "")))
        elif event.kind == "tool_call_started":
            call_id = str(payload.get("call_id", ""))
            if call_id and call_id not in self._tools:
                self._tools[call_id] = ToolCallView(
                    call_id=call_id, name=str(payload.get("name", ""))
                )
        elif event.kind == "ToolClaimed":
            call_id = str(payload.get("call_id", ""))
            if call_id and call_id not in self._tools:
                self._tools[call_id] = ToolCallView(
                    call_id=call_id, name=str(payload.get("tool_name", ""))
                )
        elif event.kind == "ToolCompleted":
            call_id = str(payload.get("call_id", ""))
            view = self._tools.get(call_id)
            if view is None and call_id:
                view = ToolCallView(call_id=call_id, name=str(payload.get("tool_name", "")))
                self._tools[call_id] = view
            if view is not None:
                status = str(payload.get("status", "ok"))
                view.status = (
                    cast(ToolStatus, status)
                    if status
                    in {
                        "ok",
                        "error",
                        "denied",
                        "timeout",
                        "cancelled",
                        "unknown",
                    }
                    else "unknown"
                )
                output = str(payload.get("output", ""))
                if len(output) > self.max_tool_output_chars:
                    output = output[: self.max_tool_output_chars] + "…"
                view.output = output
        elif event.kind == "RunStatusChanged":
            status = str(payload.get("status", ""))
            if status:
                self.status = status
        return True

    def apply_approval(self, request: ApprovalRequest) -> bool:
        """登记审批；同一 approval_id 只保留首次登记。"""
        if request.approval_id in self._approvals:
            return False
        self._approvals[request.approval_id] = ApprovalView(
            approval_id=request.approval_id,
            tool_name=request.tool_name,
            args=dict(request.args),
            status=request.status,
        )
        return True

    def decide_approval(self, approval_id: str, decision: Literal["approved", "denied"]) -> bool:
        """决定一次审批；已决定返回 False，不重复提交。"""
        view = self._approvals.get(approval_id)
        if view is None or view.status != "pending":
            return False
        view.status = decision
        return True

    def pending_approvals(self) -> tuple[ApprovalView, ...]:
        """列出待决审批。"""
        return tuple(view for view in self._approvals.values() if view.status == "pending")

    def snapshot(self) -> dict[str, Any]:
        """可序列化的视图快照，用于录制测试与 TUI 渲染。"""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "text": self.text,
            "tool_calls": [
                {
                    "call_id": view.call_id,
                    "name": view.name,
                    "status": view.status,
                    "output": view.output,
                }
                for view in self._tools.values()
            ],
            "approvals": [
                {
                    "approval_id": view.approval_id,
                    "tool_name": view.tool_name,
                    "args": view.args,
                    "status": view.status,
                }
                for view in self._approvals.values()
            ],
            "events_seen": self.events_seen,
            "dropped_chars": self.dropped_chars,
        }

    def _append_text(self, chunk: str) -> None:
        if not chunk:
            return
        if len(chunk) > self.max_text_chars:
            self.dropped_chars += len(chunk) - self.max_text_chars
            chunk = chunk[: self.max_text_chars - 1] + "…"
        self.text += chunk
        if len(self.text) > self.max_text_chars:
            overflow = len(self.text) - self.max_text_chars + 1
            self.text = self.text[overflow:] + "…"
            self.dropped_chars += overflow
