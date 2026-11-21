"""Textual TUI 监视台：视图状态独立于 Run 状态，经 ClientEvent 折叠渲染。

定位（ADR-022）：本应用保留为「接入已有 Run 的监视台」（看状态、
批/拒、取消、恢复），由 `deity tui --run-id` 进入；产品交互界面是
interactive.py 的全屏交互工作台（原 V1.1-M4 冻结决策已被 ADR-022
取代，本应用行为不变）。

本模块需要 `tui` extra（Textual）；未安装时由 CLI 给出安装提示。

接通方式：调用方（`deity tui` 或测试）传入 `client_service` 与
`run_id`/`task`，TuiApp 在挂载后用 Textual worker 订阅事件并喂入视图，
不阻塞 UI；`a/d` 批准/拒绝首个待决审批，`c` 取消，`r` 恢复当前 Run。
回调可以是同步签名（返回 None）也可以返回协程；返回协程时用
`asyncio.create_task` 调度，不在 UI 线程里阻塞等待。
"""

import asyncio
import inspect
from collections.abc import Callable
from typing import Any, ClassVar, Literal

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Static
from textual.worker import Worker

from deity.core.contracts.client import ClientEvent
from deity.interfaces.tui.theme import apply_deity_theme
from deity.interfaces.tui.view_model import RunViewModel


class TuiApp(App[None]):
    """模型流式文本、工具调用树、审批列表与状态的现代分栏监视台。"""

    # 布局取 53f329d 的分栏结构；观感对齐（D7）：边框圆角化 + 层级色，
    # 不动任何行为与文案语义。两边原先各有一份 CSS，合并时并为一份
    # （类属性同名时后者会整份覆盖前者，必须手动合并）。
    CSS: ClassVar[str] = """
    #monitor-container { height: 1fr; }
    #monitor-header { height: 3; border: round $primary; padding: 0 1; }
    #monitor-body { height: 1fr; }
    #monitor-sidebar { width: 38; border-right: solid $primary; padding: 0 1; }
    #monitor-main { width: 1fr; padding: 0 1; }
    #run-status { width: 1fr; text-style: bold; }
    #run-text { padding: 0 1; }
    #run-approvals-box { border: round $warning; margin-bottom: 1; padding: 0 1; max-height: 10; }
    #run-approvals { color: $warning; }
    #run-tools-box { height: 1fr; border: round $primary; padding: 0 1; }
    #run-tools { color: $text-secondary; }
    .box-title { text-style: bold; color: $accent; margin-bottom: 1; }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "退出"),
        Binding("a", "approve", "批准"),
        Binding("d", "deny", "拒绝"),
        Binding("c", "cancel", "取消"),
        Binding("r", "resume", "恢复"),
    ]

    def __init__(
        self,
        view_model: RunViewModel | None = None,
        *,
        on_approve: Callable[[str], None] | None = None,
        on_deny: Callable[[str], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        on_resume: Callable[[], None] | None = None,
        client_service: Any | None = None,
        run_id: str | None = None,
        task: str | None = None,
        model_name: str = "fake",
    ) -> None:
        super().__init__()
        # 与工作台共用 deity 主题（D7 观感对齐）；行为完全不变。
        apply_deity_theme(self)
        self.view_model = view_model or RunViewModel(run_id=run_id or "")
        self._on_approve = on_approve
        self._on_deny = on_deny
        self._on_cancel = on_cancel
        self._on_resume = on_resume
        self._client = client_service
        self.run_id = run_id
        self._start_task = task
        self._model_name = model_name
        self.last_resume_error: str | None = None
        self.feed_error: str | None = None
        self._feed_worker: Worker[None] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="monitor-container"):
            with Horizontal(id="monitor-header"):
                yield Static("", id="run-status")
            with Horizontal(id="monitor-body"):
                with Vertical(id="monitor-sidebar"):
                    with Vertical(id="run-approvals-box"):
                        yield Static("待决审批 [a 批准 / d 拒绝]:", classes="box-title")
                        yield Static("", id="run-approvals")
                    with VerticalScroll(id="run-tools-box"):
                        yield Static("工具调用流水线:", classes="box-title")
                        yield Static("", id="run-tools")
                with VerticalScroll(id="monitor-main"):
                    yield Static("", id="run-text")
        yield Footer()

    def on_mount(self) -> None:
        """首屏刷新一次；已接 ClientService 时启动事件订阅 worker 与审批同步。"""
        self._refresh()
        if self._client is not None:
            self._start_feed_worker()
            self.set_interval(0.2, self._sync_approvals)

    # -- 事件订阅 ---------------------------------------------------------
    def _start_feed_worker(self) -> None:
        """用 Textual worker 拉取订阅事件喂给视图，不阻塞 UI 线程。"""
        if self._feed_worker is not None and self._feed_worker.is_running:
            self._feed_worker.cancel()
        self._feed_worker = self.run_worker(self._event_loop(), exclusive=False)

    async def _event_loop(self) -> None:
        """订阅事件流：先启动任务（如需），再快照+增量喂入视图。"""
        client = self._client
        if client is None:
            return
        run_id = self.run_id
        if run_id is None and self._start_task is not None:
            try:
                handle = await client.start_run(self._start_task, self._model_name)
            except Exception as exc:  # noqa: BLE001 - 启动失败只记录，不崩 UI
                self.feed_error = str(exc)
                return
            run_id = handle.run_id
            self.run_id = run_id
            self.view_model.run_id = run_id
        if run_id is None:
            return
        try:
            subscription = await client.subscribe_events(run_id)
        except Exception as exc:  # noqa: BLE001 - 订阅失败只记录，不崩 UI
            self.feed_error = str(exc)
            return
        try:
            async for event in subscription:
                self.feed(event)
                self._sync_approvals()
        except Exception as exc:  # noqa: BLE001 - 订阅中断只记录，不崩 UI
            self.feed_error = str(exc)
        finally:
            await subscription.close()

    def _sync_approvals(self) -> None:
        """把 ClientService 的待决审批同步进视图模型（审批不在事件流里）。"""
        if self._client is None:
            return
        for request in self._client.pending_approvals():
            self.view_model.apply_approval(request)

    # -- 回调调度 ---------------------------------------------------------
    def _record_task_error(self, task: asyncio.Task[Any]) -> None:
        """后台回调任务失败时记录，不向 UI 抛异常。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.feed_error = f"{type(error).__name__}: {error}"

    def _schedule(self, coro: Any) -> None:
        """在当前事件循环里调度协程，不阻塞 UI 线程等待。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.call_later(lambda: asyncio.create_task(coro))
            return
        task = loop.create_task(coro)
        task.add_done_callback(self._record_task_error)

    def _dispatch(self, result: Any) -> None:
        """回调返回值是协程时调度执行；同步回调直接返回。"""
        if inspect.isawaitable(result):
            self._schedule(result)

    # -- 视图 -------------------------------------------------------------
    def feed(self, event: ClientEvent) -> None:
        """喂入一条客户端事件并刷新视图；渲染背压只丢弃旧流式增量。"""
        self.view_model.apply(event)
        self._refresh()

    def _refresh(self) -> None:
        snapshot = self.view_model.snapshot()
        try:
            status_text = snapshot["status"]
            self.query_one("#run-status", Static).update(
                f"运行 ID：{snapshot['run_id'] or '未分配'}  |  状态：{status_text}  |  已接收事件：{self.view_model.events_seen}"
            )
            self.query_one("#run-text", Static).update(str(snapshot["text"]))
            tool_lines: list[str] = []
            for item in snapshot["tool_calls"]:
                # 工具行格式与工作台统一（D7）：⏺ 名 [状态] + ⎿ 输出首行；
                # 合并调和：保留 53f329d 新增的 call_id 短前缀信息（括号内），
                # 其 ✓/✗ 状态图标与 [状态] 文本及 ⎿ 行重复，不再叠加。
                tool_lines.append(f"⏺ {item['name']} [{item['status']}] ({item['call_id'][:8]})")
                output = (item.get("output") or "").strip()
                if output:
                    tool_lines.append(f"  ⎿ {output.splitlines()[0]}")
            # 传 Text 而非 str：[状态] 方括号会被 Rich markup 吞掉。
            self.query_one("#run-tools", Static).update(
                Text("\n".join(tool_lines) or "（无工具调用）")
            )
            approvals = "\n".join(
                f"• {item['approval_id'][:8]} {item['tool_name']} [{item['status']}]"
                for item in snapshot["approvals"]
            )
            # 同上：审批行也含 [状态] 方括号，一并传 Text 防 markup 吞掉。
            self.query_one("#run-approvals", Static).update(Text(approvals or "（无待审批）"))
        except Exception:  # noqa: BLE001, S110 - 未挂载时刷新是空操作
            pass

    def _first_pending_approval(self) -> str | None:
        pending = self.view_model.pending_approvals()
        return pending[0].approval_id if pending else None

    async def _decide_via_client(
        self, approval_id: str, decision: Literal["approved", "denied"]
    ) -> None:
        """经 ClientService 决定审批；成功后才在视图里标记已决定。"""
        assert self._client is not None
        await self._client.approve(approval_id, decision)
        self.view_model.decide_approval(approval_id, decision)

    def action_approve(self) -> None:
        """批准首个待决审批；无待决时不做任何事。"""
        approval_id = self._first_pending_approval()
        if approval_id is None:
            return
        if self._on_approve is not None:
            self._dispatch(self._on_approve(approval_id))
            self.view_model.decide_approval(approval_id, "approved")
        elif self._client is not None:
            self._schedule(self._decide_via_client(approval_id, "approved"))

    def action_deny(self) -> None:
        """拒绝首个待决审批；无待决时不做任何事。"""
        approval_id = self._first_pending_approval()
        if approval_id is None:
            return
        if self._on_deny is not None:
            self._dispatch(self._on_deny(approval_id))
            self.view_model.decide_approval(approval_id, "denied")
        elif self._client is not None:
            self._schedule(self._decide_via_client(approval_id, "denied"))

    def action_cancel(self) -> None:
        """请求取消当前 Run；无回调、无客户端时不做任何事。"""
        if self._on_cancel is not None:
            self._dispatch(self._on_cancel())
        elif self._client is not None and self.run_id is not None:
            self._client.cancel(self.run_id)

    async def _resume_via_client(self) -> None:
        """经 ClientService 恢复当前 Run；失败记入 last_resume_error，不抛错。"""
        assert self._client is not None and self.run_id is not None
        try:
            handle = await self._client.resume(self.run_id)
        except Exception as exc:  # noqa: BLE001 - 恢复拒绝是预期路径，记下来
            self.last_resume_error = str(exc)
            return
        self.last_resume_error = None
        self.run_id = handle.run_id
        self.view_model.run_id = handle.run_id
        self._start_feed_worker()

    def action_resume(self) -> None:
        """恢复当前/指定 Run；无 Run 时不做任何事。"""
        if self._on_resume is not None:
            self._dispatch(self._on_resume())
        elif self._client is not None and self.run_id is not None:
            self._schedule(self._resume_via_client())


def run_tui(
    client_service: Any | None = None,
    run_id: str | None = None,
    task: str | None = None,
    model_name: str = "fake",
) -> None:
    """启动 TUI；调用方负责处理 Textual 未安装的情况。

    未传 client_service 时是无回调的裸应用（向后兼容）；传入后经
    ClientService 订阅事件、接通 a/d/c/r 键的真实闭环。
    """
    TuiApp(client_service=client_service, run_id=run_id, task=task, model_name=model_name).run()


__all__ = ["TuiApp", "run_tui"]
