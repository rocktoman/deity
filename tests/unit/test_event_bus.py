"""V1.1-M2：进程内事件总线（回放、按 Run 分发、终态结束、慢消费者）。"""

import asyncio

import pytest

from deity.application.event_bus import InProcessEventBus
from deity.core.contracts.client import ClientError, ClientEvent


def _event(
    seq: int, run_id: str = "r1", kind: str = "text_delta", **payload: object
) -> ClientEvent:
    return ClientEvent(seq=seq, run_id=run_id, kind=kind, payload=dict(payload))


async def _collect(subscription: object, limit: int = 100) -> list[ClientEvent]:
    events: list[ClientEvent] = []
    async for event in subscription:  # type: ignore[attr-defined]
        events.append(event)
        if len(events) >= limit:
            break
    return events


@pytest.mark.asyncio
async def test_subscribe_replays_buffer_then_finishes_on_terminal() -> None:
    bus = InProcessEventBus()
    bus.publish(_event(1, text="你"))
    bus.publish(_event(2, text="好"))
    bus.publish(_event(3, kind="run_status", status="completed", final_text="你好"))
    subscription = bus.subscribe("r1", after_seq=0)
    events = await asyncio.wait_for(_collect(subscription), timeout=5)
    assert [event.seq for event in events] == [1, 2, 3]
    assert events[-1].kind == "run_status"
    assert bus.subscriber_count("r1") == 0


@pytest.mark.asyncio
async def test_after_seq_skips_replayed_events() -> None:
    bus = InProcessEventBus()
    bus.publish(_event(1, text="a"))
    bus.publish(_event(2, text="b"))
    bus.publish(_event(3, kind="run_status", status="completed", final_text="ab"))
    subscription = bus.subscribe("r1", after_seq=2)
    events = await asyncio.wait_for(_collect(subscription), timeout=5)
    assert [event.seq for event in events] == [3]


@pytest.mark.asyncio
async def test_live_events_flow_to_matching_run_only() -> None:
    bus = InProcessEventBus()
    sub_r1 = bus.subscribe("r1")
    sub_r2 = bus.subscribe("r2")
    bus.publish(_event(1, run_id="r1", text="for-r1"))
    bus.publish(_event(1, run_id="r2", text="for-r2"))
    bus.publish(_event(2, run_id="r1", kind="run_status", status="completed", final_text="x"))
    bus.publish(_event(2, run_id="r2", kind="run_status", status="failed", final_text=None))
    events_r1 = await asyncio.wait_for(_collect(sub_r1), timeout=5)
    events_r2 = await asyncio.wait_for(_collect(sub_r2), timeout=5)
    assert [event.payload.get("text") for event in events_r1] == ["for-r1", None]
    assert [event.payload.get("text") for event in events_r2] == ["for-r2", None]
    assert bus.has_run("r1") and bus.last_seq("r2") == 2


@pytest.mark.asyncio
async def test_slow_consumer_fails_with_slow_consumer() -> None:
    bus = InProcessEventBus(max_queue=2)
    subscription = bus.subscribe("r1")
    for seq in range(1, 6):
        bus.publish(_event(seq, text="x"))
    with pytest.raises(ClientError) as excinfo:
        await asyncio.wait_for(_collect(subscription), timeout=5)
    assert excinfo.value.code == "slow_consumer"


@pytest.mark.asyncio
async def test_close_detaches_subscriber() -> None:
    bus = InProcessEventBus()
    subscription = bus.subscribe("r1")
    assert bus.subscriber_count("r1") == 1
    await subscription.close()
    assert subscription.closed
    assert bus.subscriber_count("r1") == 0
    bus.publish(_event(1, text="after-close"))
    assert subscription.drain() == []
