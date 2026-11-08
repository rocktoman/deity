"""统一客户端服务：CLI 与 TUI 共用的 Run 入口。

快照 + 增量订阅、after_seq 重连、重复审批去重、慢消费者断开。
订阅读 SQLite 已持久化事件，不重复执行工具。
"""

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any
from uuid import uuid4

from deity.adapters.events import TERMINAL_STATUSES, SQLiteEventSubscriber
from deity.adapters.storage import SQLiteSessionRepository
from deity.application.run_service import RunService
from deity.core.contracts import AgentResult, RunStatus
from deity.core.contracts.client import (
    ApprovalDecision,
    ApprovalPort,
    ApprovalRequest,
    ClientError,
    ClientEvent,
    EventSubscription,
    RunHandle,
    approval_args_summary,
    approval_id_for,
)

_SENTINEL = object()


class _EventSubscriptionImpl:
    """有界队列订阅；队列满则断开并报错，不丢事件后静默续跑。"""

    def __init__(self, queue: asyncio.Queue[ClientEvent], max_queue: int) -> None:
        self._queue = queue
        self._max_queue = max_queue
        self._done = asyncio.Event()
        self._error: ClientError | None = None
        self._producer: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or self._done.is_set()

    @property
    def error(self) -> ClientError | None:
        return self._error

    def _attach_producer(self, producer: asyncio.Task[None]) -> None:
        self._producer = producer

    def _fail(self, error: ClientError) -> None:
        self._error = error
        self._done.set()

    def _finish(self) -> None:
        self._done.set()

    def _offer(self, event: ClientEvent) -> bool:
        """投递一条事件；队列满返回 False，调用方据此断开订阅。"""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            return False
        return True

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        while True:
            if self._error is not None and self._queue.empty():
                raise self._error
            if self._done.is_set() and self._queue.empty():
                return
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            yield item

    async def close(self) -> None:
        """断开订阅并停止轮询生产者。"""
        self._closed = True
        self._done.set()
        if self._producer is not None and not self._producer.done():
            self._producer.cancel()
            try:
                await self._producer
            except asyncio.CancelledError:
                pass


class ClientService(ApprovalPort):
    """包装一个 RunService，提供类型化的 start/subscribe/approve/cancel/resume。"""

    def __init__(
        self,
        run_service: RunService,
        *,
        subscriber: Any | None = None,
        poll_interval_seconds: float = 0.1,
        max_queue: int = 100,
    ) -> None:
        self._run_service = run_service
        self._subscriber = subscriber or SQLiteEventSubscriber(
            run_service.db_path, poll_interval_seconds=poll_interval_seconds
        )
        self._poll_interval_seconds = poll_interval_seconds
        self._max_queue = max_queue
        self._approvals: dict[str, ApprovalRequest] = {}
        self._approval_events: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task[AgentResult]] = {}
        self._handles: dict[str, RunHandle] = {}

    # -- 审批端口 ---------------------------------------------------------
    def _register_approval(
        self,
        approval_id: str,
        run_id: str,
        call_id: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> ApprovalRequest:
        """创建或返回已登记的审批请求；参数只保留摘要。"""
        existing = self._approvals.get(approval_id)
        if existing is not None:
            return existing
        request = ApprovalRequest(
            approval_id=approval_id,
            run_id=run_id,
            call_id=call_id,
            tool_name=tool_name,
            args=approval_args_summary(args),
        )
        self._approvals[approval_id] = request
        self._approval_events.setdefault(approval_id, asyncio.Event())
        return request

    def register_approval_for_tool(
        self, run_id: str, call_id: str, tool_name: str, args: dict[str, Any]
    ) -> ApprovalRequest:
        """供 RunService 桥接使用：由工具名与参数派生确定性审批 ID。"""
        return self._register_approval(
            approval_id_for(tool_name, args), run_id, call_id, tool_name, args
        )

    async def request_approval(self, request: ApprovalRequest) -> ApprovalRequest:
        """等待一次审批决定；同一 approval_id 的重复请求共用首次结果。"""
        existing = self._approvals.get(request.approval_id)
        if existing is None:
            existing = request
            existing.args = approval_args_summary(dict(request.args))
            self._approvals[request.approval_id] = existing
            self._approval_events.setdefault(request.approval_id, asyncio.Event())
        if existing.status != "pending":
            return existing
        await self._approval_events[request.approval_id].wait()
        return existing

    def pending_approvals(self) -> tuple[ApprovalRequest, ...]:
        """列出尚未决定的审批请求。"""
        return tuple(request for request in self._approvals.values() if request.status == "pending")

    async def approve(self, approval_id: str, decision: ApprovalDecision | bool) -> ApprovalRequest:
        """决定一次审批；重复决定返回首次结果，不同决定报 already_decided。"""
        wanted: ApprovalDecision = (
            "approved" if decision is True else "denied" if decision is False else decision
        )
        request = self._approvals.get(approval_id)
        if request is None:
            raise ClientError("approval_not_found", f"未知审批：{approval_id}")
        if request.status != "pending":
            if request.status != wanted:
                raise ClientError(
                    "already_decided", f"审批 {approval_id} 已决定为 {request.status}"
                )
            return request
        request.decide(wanted)
        self._approval_events[approval_id].set()
        return request

    # -- Run 生命周期 -----------------------------------------------------
    async def start_run(
        self, task: str, model_name: str, session_id: str | None = None
    ) -> RunHandle:
        """启动一次 Run 并返回句柄；run_id 预先生成，订阅可立即按它续订。"""
        active_session = session_id or str(uuid4())
        run_id = str(uuid4())
        self._run_service.approval_port = self
        created = asyncio.Event()
        run_task = asyncio.create_task(
            self._guarded_run(
                self._run_service.run(
                    task,
                    model_name=model_name,
                    session_id=active_session,
                    run_id=run_id,
                    on_run_created=lambda _run_id: created.set(),
                ),
                run_id,
            )
        )
        self._tasks[run_id] = run_task
        try:
            await asyncio.wait_for(created.wait(), timeout=10.0)
        except TimeoutError:
            if run_task.done():
                error = run_task.exception()
                if error is not None:
                    raise error
            raise ClientError("run_not_found", f"Run 未创建：{run_id}")
        status = run_task.result().status if run_task.done() else RunStatus.RUNNING
        handle = RunHandle(run_id=run_id, session_id=active_session, status=status.value)
        self._handles[run_id] = handle
        return handle

    async def _guarded_run(
        self, coro: Coroutine[Any, Any, AgentResult], run_id: str
    ) -> AgentResult:
        """取消时把 CancelledError 转为 CANCELLED 终态；持久化状态已由循环写入。"""
        try:
            return await coro
        except asyncio.CancelledError:
            return AgentResult(run_id, RunStatus.CANCELLED, None)

    async def wait(self, run_id: str) -> AgentResult:
        """等待一次已启动 Run 的终态结果。"""
        task = self._tasks.get(run_id)
        if task is None:
            raise ClientError("run_not_found", f"未知 Run：{run_id}")
        return await task

    def cancel(self, run_id: str) -> bool:
        """向执行中的 Run 传播取消；无活动 Run 返回 False。"""
        cancelled = self._run_service.cancel(run_id)
        task = self._tasks.get(run_id)
        if not cancelled and task is not None and not task.done():
            task.cancel()
            cancelled = True
        return cancelled

    async def resume(self, run_id: str) -> RunHandle:
        """从安全检查点续跑一次中断的 Run，返回后继 Run 的句柄。"""
        result = await self._run_service.resume(run_id)
        repo = await SQLiteSessionRepository.open(self._run_service.db_path)
        try:
            info = await repo.run_info(result.run_id)
        finally:
            await repo.close()
        return RunHandle(
            run_id=result.run_id,
            session_id=str(info["session_id"]),
            status=result.status.value,
        )

    # -- 事件订阅 ---------------------------------------------------------
    async def collect(self, run_id: str, after_seq: int = 0) -> list[ClientEvent]:
        """一次性读出快照事件；不存在的 Run 报 run_not_found。"""
        events = await self._subscriber.read(run_id, after_seq)
        if not events:
            existing = await self._subscriber.read(run_id, 0)
            if not existing:
                raise ClientError("run_not_found", f"未知 Run：{run_id}")
        return events

    async def subscribe_events(self, run_id: str, after_seq: int = 0) -> EventSubscription:
        """订阅事件：先给快照，再跟随增量；断线后用 after_seq 续订。"""
        initial = await self._subscriber.read(run_id, 0)
        if not initial:
            raise ClientError("run_not_found", f"未知 Run：{run_id}")
        snapshot = [event for event in initial if event.seq > after_seq]
        queue: asyncio.Queue[ClientEvent] = asyncio.Queue(maxsize=self._max_queue)
        subscription = _EventSubscriptionImpl(queue, self._max_queue)
        producer = asyncio.create_task(self._produce(subscription, run_id, after_seq, snapshot))
        subscription._attach_producer(producer)
        return subscription

    async def _produce(
        self,
        subscription: _EventSubscriptionImpl,
        run_id: str,
        after_seq: int,
        snapshot: list[ClientEvent],
    ) -> None:
        """快照投递后轮询增量；乱序（seq 不递增）事件跳过不应用。"""
        last_seq = after_seq
        try:
            async with self._subscriber_context():
                for event in snapshot:
                    if event.seq <= last_seq:
                        continue
                    last_seq = event.seq
                    if not subscription._offer(event):
                        subscription._fail(ClientError("slow_consumer", "消费者过慢，订阅已断开"))
                        return
                    if _is_terminal(event):
                        subscription._finish()
                        return
                while not subscription.closed:
                    await asyncio.sleep(self._poll_interval_seconds)
                    if subscription.closed:
                        return
                    try:
                        fresh = await self._subscriber.read(run_id, last_seq)
                    except (OSError, ValueError, KeyError):
                        continue
                    for event in fresh:
                        if event.seq <= last_seq:
                            continue
                        last_seq = event.seq
                        if not subscription._offer(event):
                            subscription._fail(
                                ClientError("slow_consumer", "消费者过慢，订阅已断开")
                            )
                            return
                        if _is_terminal(event):
                            subscription._finish()
                            return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 生产者失败不断开 Run，只结束订阅
            subscription._finish()

    @asynccontextmanager
    async def _subscriber_context(self) -> AsyncIterator[None]:
        """订阅期间持有事件源的持久连接；Fake 源无上下文时直接放行。"""
        stack = AsyncExitStack()
        if hasattr(self._subscriber, "__aenter__"):
            await stack.enter_async_context(self._subscriber)
        try:
            yield
        finally:
            await stack.aclose()


def _is_terminal(event: ClientEvent) -> bool:
    return (
        event.kind == "RunStatusChanged"
        and str(event.payload.get("status", "")) in TERMINAL_STATUSES
    )


__all__ = ["ClientService"]
