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
        max_text_chars: int | None = None,
        max_tool_output_chars: int | None = None,
    ) -> None:
        """默认不设字符上限：正文与工具输出都完整保留，用户能看到全文。

        `max_text_chars` / `max_tool_output_chars` 仅在显式传入正整数时
        启用截断（保留给测试与需要背压的调用方），传 None 表示不限。
        """
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
            self._complete_tool(
                str(payload.get("call_id", "")),
                str(payload.get("tool_name", "")),
                str(payload.get("status", "ok")),
                str(payload.get("output", "")),
            )
        elif event.kind == "tool_call_completed":
            # live 事件（V1.1-M2 事件总线）：与 ToolCompleted 同义，
            # 工具名在 name 字段、无 output 字段。
            self._complete_tool(
                str(payload.get("call_id", "")),
                str(payload.get("name", "")),
                str(payload.get("status", "ok")),
                "",
            )
        elif event.kind == "RunStatusChanged":
            status = str(payload.get("status", ""))
            if status:
                self.status = status
        elif event.kind == "run_status":
            # live 终态事件（事件总线）：与 RunStatusChanged 同义。
            status = str(payload.get("status", ""))
            if status:
                self.status = status
        return True

    def _complete_tool(self, call_id: str, name: str, status: str, output: str) -> None:
        """工具完成折叠：持久化 ToolCompleted 与 live tool_call_completed 共用。"""
        view = self._tools.get(call_id)
        if view is None and call_id:
            view = ToolCallView(call_id=call_id, name=name)
            self._tools[call_id] = view
        if view is not None:
            view.status = (
                cast(ToolStatus, status)
                if status in {"ok", "error", "denied", "timeout", "cancelled", "unknown"}
                else "unknown"
            )
            tool_limit = self.max_tool_output_chars
            if tool_limit is not None and len(output) > tool_limit:
                self.dropped_chars += len(output) - tool_limit
                output = output[:tool_limit] + "…"
            view.output = output

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
        """追加流式文本；未设上限时原样累积，不丢弃任何前缀。"""
        if not chunk:
            return
        limit = self.max_text_chars
        if limit is None:
            self.text += chunk
            return
        if len(chunk) > limit:
            self.dropped_chars += len(chunk) - limit
            chunk = chunk[: limit - 1] + "…"
        self.text += chunk
        if len(self.text) > limit:
            overflow = len(self.text) - limit + 1
            self.text = self.text[overflow:] + "…"
            self.dropped_chars += overflow
