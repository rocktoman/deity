"""进程内事件总线（V1.1-M2）：live ClientEvent 的统一通道。

此前 live 事件有两套投递：CLI 的 run_stream 用一条裸 Queue，ClientService
只靠 SQLite 每 0.1 秒轮询持久化事件。M2 起进程内 live 统一走本总线：
发布者（AgentLoop 的 live_event_sink、RunService、ClientService）只管
publish，订阅者按 run_id 订阅；SQLite 只做持久化、跨进程接入与事后
重放，不再是进程内 live 的通道。

语义：
- 按 run_id 分发；run_id=None 的订阅接收全部 Run 的事件（仅 live，
  不回放，因为不同 Run 的 seq 空间互不相关，见 ADR-016）。
- 每个 Run 保留有界回放缓冲（默认 4096 条）：晚订阅者先收到 seq 大于
  after_seq 的缓冲事件，再跟随 live 增量，与「快照 + 增量」语义一致。
- 订阅队列有界；队列满时该订阅以 slow_consumer 失败并结束，
  不静默丢事件后继续（与 ClientService 既有语义一致）。
- kind == "run_status" 且 status 为终态时，该 Run 的单 Run 订阅在
  投递这条事件后结束。
"""

import asyncio
from collections import deque

from deity.core.contracts.client import ClientError, ClientEvent

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})

__all__ = ["EventBusSubscription", "InProcessEventBus", "is_live_terminal"]


def is_live_terminal(event: ClientEvent) -> bool:
    """live 终态事件：run_status 且 status 属于终态集合。"""
    return event.kind == "run_status" and str(event.payload.get("status", "")) in TERMINAL_STATUSES


class EventBusSubscription:
    """总线订阅：异步迭代 ClientEvent；close() 后停止投递。"""

    def __init__(
        self,
        bus: "InProcessEventBus",
        run_id: str | None,
        queue: "asyncio.Queue[ClientEvent]",
    ) -> None:
        self._bus = bus
        self.run_id = run_id
        self._queue = queue
        self._done = asyncio.Event()
        self._error: ClientError | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or self._done.is_set()

    @property
    def error(self) -> ClientError | None:
        return self._error

    def _offer(self, event: ClientEvent) -> bool:
        """投递一条事件；队列满返回 False，调用方据此断开订阅。"""
        if self._closed:
            return True
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            return False
        return True

    def _fail(self, error: ClientError) -> None:
        self._error = error
        self._done.set()

    def _finish(self) -> None:
        self._done.set()

    def drain(self) -> list[ClientEvent]:
        """非阻塞取出当前已排队的事件（供轮询式消费方，如 run_stream）。"""
        events: list[ClientEvent] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return events

    async def next_event(self, timeout: float) -> ClientEvent | None:
        """等待下一条事件；超时返回 None。"""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    def detach(self) -> None:
        """同步断开（不等待）：标记关闭并从总线摘除。"""
        self._closed = True
        self._done.set()
        self._bus._detach(self)

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
        """断开订阅并从总线摘除。"""
        self.detach()


class InProcessEventBus:
    """单进程内的 live 事件总线：回放缓冲 + 按 Run 分发。"""

    def __init__(self, *, max_queue: int = 256, replay_limit: int = 4096) -> None:
        if max_queue <= 0 or replay_limit <= 0:
            raise ValueError("invalid event bus limits")
        self._max_queue = max_queue
        self._replay_limit = replay_limit
        self._buffers: dict[str, deque[ClientEvent]] = {}
        self._terminal_runs: set[str] = set()
        self._subscribers: set[EventBusSubscription] = set()

    def publish(self, event: ClientEvent) -> None:
        """同步投递一条事件（与 live_event_sink 的回调签名兼容）。

        先入回放缓冲，再分发给匹配的订阅者；观测端失败不影响发布者。
        """
        buffer = self._buffers.setdefault(event.run_id, deque(maxlen=self._replay_limit))
        buffer.append(event)
        terminal = is_live_terminal(event)
        if terminal:
            self._terminal_runs.add(event.run_id)
        for subscription in tuple(self._subscribers):
            if subscription.run_id is not None and subscription.run_id != event.run_id:
                continue
            if not subscription._offer(event):
                subscription._fail(ClientError("slow_consumer", "消费者过慢，订阅已断开"))
                self._detach(subscription)
            elif terminal and subscription.run_id == event.run_id:
                subscription._finish()
                self._detach(subscription)

    def subscribe(self, run_id: str | None = None, *, after_seq: int = 0) -> EventBusSubscription:
        """订阅事件：单 Run 订阅先回放缓冲（seq > after_seq），再跟随 live。"""
        queue: asyncio.Queue[ClientEvent] = asyncio.Queue(maxsize=self._max_queue)
        subscription = EventBusSubscription(self, run_id, queue)
        self._subscribers.add(subscription)
        if run_id is not None:
            replayed_terminal = False
            for event in tuple(self._buffers.get(run_id, ())):
                if event.seq <= after_seq:
                    continue
                if not subscription._offer(event):
                    subscription._fail(ClientError("slow_consumer", "消费者过慢，订阅已断开"))
                    break
                if is_live_terminal(event):
                    replayed_terminal = True
            if replayed_terminal:
                subscription._finish()
                self._detach(subscription)
        return subscription

    def has_run(self, run_id: str) -> bool:
        """该 Run 是否已经过本总线发布过事件。"""
        return run_id in self._buffers

    def last_seq(self, run_id: str) -> int:
        """该 Run 已发布事件的最大 seq；无事件时为 0。"""
        buffer = self._buffers.get(run_id)
        return buffer[-1].seq if buffer else 0

    def subscriber_count(self, run_id: str | None = None) -> int:
        """当前订阅数（可按 Run 过滤），供诊断与测试。"""
        if run_id is None:
            return len(self._subscribers)
        return sum(1 for sub in self._subscribers if sub.run_id == run_id)

    def _detach(self, subscription: EventBusSubscription) -> None:
        self._subscribers.discard(subscription)
