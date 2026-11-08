"""Textual TUI：视图状态独立于 Run 状态，经 ClientEvent 折叠渲染。

本模块需要 `tui` extra（Textual）；未安装时由 CLI 给出安装提示。
"""

from collections.abc import Callable
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Static

from deity.core.contracts.client import ClientEvent
from deity.interfaces.tui.view_model import RunViewModel


class TuiApp(App[None]):
    """模型流式文本、工具调用树、审批列表与状态的只读 TUI。"""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "退出"),
        Binding("a", "approve", "批准"),
        Binding("d", "deny", "拒绝"),
        Binding("c", "cancel", "取消"),
    ]

    def __init__(
        self,
        view_model: RunViewModel | None = None,
        *,
        on_approve: Callable[[str], None] | None = None,
        on_deny: Callable[[str], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.view_model = view_model or RunViewModel()
        self._on_approve = on_approve
        self._on_deny = on_deny
        self._on_cancel = on_cancel

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="run-status")
        yield Static("", id="run-text")
        yield Static("", id="run-tools")
        yield Static("", id="run-approvals")
        yield Footer()

    def on_mount(self) -> None:
        """首屏即刷新一次空状态。"""
        self._refresh()

    def feed(self, event: ClientEvent) -> None:
        """喂入一条客户端事件并刷新视图；渲染背压只丢弃旧流式增量。"""
        self.view_model.apply(event)
        self._refresh()

    def _refresh(self) -> None:
        snapshot = self.view_model.snapshot()
        try:
            self.query_one("#run-status", Static).update(f"状态：{snapshot['status']}")
            self.query_one("#run-text", Static).update(str(snapshot["text"]))
            tools = "\n".join(
                f"{item['call_id']} {item['name']} [{item['status']}]"
                for item in snapshot["tool_calls"]
            )
            self.query_one("#run-tools", Static).update(tools or "（无工具调用）")
            approvals = "\n".join(
                f"{item['approval_id']} {item['tool_name']} [{item['status']}]"
                for item in snapshot["approvals"]
            )
            self.query_one("#run-approvals", Static).update(approvals or "（无待审批）")
        except Exception:  # noqa: BLE001, S110 - 未挂载时刷新是空操作
            pass

    def _first_pending_approval(self) -> str | None:
        pending = self.view_model.pending_approvals()
        return pending[0].approval_id if pending else None

    def action_approve(self) -> None:
        """批准首个待决审批；无待决时不做任何事。"""
        approval_id = self._first_pending_approval()
        if approval_id is not None and self._on_approve is not None:
            self._on_approve(approval_id)

    def action_deny(self) -> None:
        """拒绝首个待决审批；无待决时不做任何事。"""
        approval_id = self._first_pending_approval()
        if approval_id is not None and self._on_deny is not None:
            self._on_deny(approval_id)

    def action_cancel(self) -> None:
        """请求取消当前 Run；无回调时不做任何事。"""
        if self._on_cancel is not None:
            self._on_cancel()


def run_tui() -> None:
    """启动 TUI；调用方负责处理 Textual 未安装的情况。"""
    TuiApp().run()


__all__ = ["TuiApp", "run_tui"]
