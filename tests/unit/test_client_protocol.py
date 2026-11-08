"""S1 统一客户端协议：事件、订阅、审批去重、慢消费者。"""

import asyncio
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.client_service import ClientService
from deity.application.run_service import RunService
from deity.core.contracts import AgentRequest, RunStatus
from deity.core.contracts.client import (
    ClientError,
    ClientEvent,
    approval_args_summary,
    approval_id_for,
)


async def _collect(sub: object, seconds: float = 0.6) -> list[ClientEvent]:
    """收集订阅在时限内到达的事件；未终态订阅不会自行结束。"""
    seen: list[ClientEvent] = []
    try:
        async with asyncio.timeout(seconds):
            async for event in sub:  # type: ignore[union-attr]
                seen.append(event)
    except TimeoutError:
        pass
    return seen


def _event(seq: int, kind: str = "text_delta", run_id: str = "r1") -> ClientEvent:
    return ClientEvent(seq=seq, run_id=run_id, kind=kind, payload={"text": f"t{seq}"})


class FakeSubscriber:
    """确定性事件源：按给定顺序返回事件，用于乱序与慢消费者测试。"""

    def __init__(self, events: list[ClientEvent]) -> None:
        self.events = list(events)

    async def read(self, run_id: str, after_seq: int = 0) -> list[ClientEvent]:
        return [event for event in self.events if event.run_id == run_id and event.seq > after_seq]


async def _make_run(tmp_path: Path) -> tuple[SQLiteSessionRepository, str]:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    request = AgentRequest("s1", "t1", str(tmp_path), "hi", "fake")
    run_id = await repo.create_run(request)
    return repo, run_id


def _client(
    tmp_path: Path, subscriber: object | None = None, max_queue: int = 100
) -> ClientService:
    service = RunService(db_path=tmp_path / "deity.db", workspace=tmp_path)
    return ClientService(service, subscriber=subscriber, max_queue=max_queue)  # type: ignore[arg-type]


def test_client_event_defaults_schema_version_one() -> None:
    event = ClientEvent(seq=1, run_id="r", kind="text_delta", payload={})
    assert event.schema_version == 1
    assert event.seq == 1


def test_args_summary_drops_content_and_truncates() -> None:
    summary = approval_args_summary({"path": "a.txt", "content": "x" * 500, "note": "y" * 300})
    assert "content" not in summary
    assert summary["path"] == "a.txt"
    assert len(str(summary["note"])) <= 200


def test_approval_id_is_deterministic() -> None:
    args = {"path": "a.txt"}
    assert approval_id_for("read_file", args) == approval_id_for("read_file", args)
    assert approval_id_for("read_file", args) != approval_id_for("write_file", args)


@pytest.mark.asyncio
async def test_event_seq_monotonic_snapshot(tmp_path: Path) -> None:
    repo, run_id = await _make_run(tmp_path)
    try:
        await repo.set_status(run_id, RunStatus.RUNNING)
        await repo.record_model_turn(run_id, "hello", [])
        await repo.record_model_turn(run_id, "world", [])
        client = _client(tmp_path)
        sub = await client.subscribe_events(run_id, after_seq=0)
        try:
            seen = await _collect(sub)
            seqs = [event.seq for event in seen]
            assert seqs == sorted(seqs)
            assert len(set(seqs)) == len(seqs)
            assert seqs[0] == 1
        finally:
            await sub.close()
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_snapshot_then_incremental_no_dup_no_loss(tmp_path: Path) -> None:
    repo, run_id = await _make_run(tmp_path)
    try:
        await repo.record_model_turn(run_id, "hello", [])
        client = _client(tmp_path)
        sub = await client.subscribe_events(run_id, after_seq=0)
        try:
            first = await _collect(sub)
            assert len(first) >= 2
            await repo.record_model_turn(run_id, "tool", [])
            await repo.record_model_turn(run_id, "tool", [])
            second = await _collect(sub)
            assert second, "增量事件丢失"
            seqs = [event.seq for event in first + second]
            assert len(set(seqs)) == len(seqs)
            assert seqs == sorted(seqs)
        finally:
            await sub.close()
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_reconnect_after_seq_resumes_without_replay(tmp_path: Path) -> None:
    repo, run_id = await _make_run(tmp_path)
    try:
        await repo.record_model_turn(run_id, "hello", [])
        await repo.record_model_turn(run_id, "tool", [])
        client = _client(tmp_path)
        sub = await client.subscribe_events(run_id, after_seq=0)
        try:
            first = await _collect(sub)
        finally:
            await sub.close()
        last_seq = max(event.seq for event in first)
        await repo.record_model_turn(run_id, "tool", [])
        sub2 = await client.subscribe_events(run_id, after_seq=last_seq)
        try:
            second = await _collect(sub2)
        finally:
            await sub2.close()
        assert second, "重连后续订丢失新事件"
        assert all(event.seq > last_seq for event in second)
        assert len({event.seq for event in second}) == len(second)
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_subscribe_unknown_run_raises(tmp_path: Path) -> None:
    client = _client(tmp_path)
    with pytest.raises(ClientError) as error:
        await client.subscribe_events("no-such-run", after_seq=0)
    assert error.value.code == "run_not_found"


@pytest.mark.asyncio
async def test_out_of_order_events_not_applied(tmp_path: Path) -> None:
    events = [_event(2), _event(1), _event(3)]
    client = _client(tmp_path, subscriber=FakeSubscriber(events))
    sub = await client.subscribe_events("r1", after_seq=0)
    try:
        seen = await _collect(sub)
    finally:
        await sub.close()
    assert [event.seq for event in seen] == [2, 3]


@pytest.mark.asyncio
async def test_slow_consumer_disconnects(tmp_path: Path) -> None:
    events = [_event(seq) for seq in range(1, 8)]
    client = _client(tmp_path, subscriber=FakeSubscriber(events), max_queue=2)
    sub = await client.subscribe_events("r1", after_seq=0)
    await asyncio.sleep(0.5)
    assert sub.closed
    with pytest.raises(ClientError) as error:
        async for _ in sub:
            pass
    assert error.value.code in {"slow_consumer", "subscription_closed"}


@pytest.mark.asyncio
async def test_duplicate_approve_returns_first_result(tmp_path: Path) -> None:
    client = _client(tmp_path)
    request = client._register_approval("a1", "r1", "c1", "read_file", {"path": "a.txt"})
    waiter = asyncio.create_task(client.request_approval(request))
    await asyncio.sleep(0.05)
    first = await client.approve("a1", "approved")
    second = await client.approve("a1", "approved")
    assert first is second
    assert first.status == "approved"
    assert (await waiter).status == "approved"
    assert first.decided_at is not None


@pytest.mark.asyncio
async def test_approve_different_decision_raises_already_decided(tmp_path: Path) -> None:
    client = _client(tmp_path)
    request = client._register_approval("a2", "r1", "c1", "read_file", {"path": "a.txt"})
    waiter = asyncio.create_task(client.request_approval(request))
    await asyncio.sleep(0.05)
    await client.approve("a2", "approved")
    with pytest.raises(ClientError) as error:
        await client.approve("a2", "denied")
    assert error.value.code == "already_decided"
    assert (await waiter).status == "approved"


@pytest.mark.asyncio
async def test_approve_unknown_id_raises(tmp_path: Path) -> None:
    client = _client(tmp_path)
    with pytest.raises(ClientError) as error:
        await client.approve("missing", "approved")
    assert error.value.code == "approval_not_found"


@pytest.mark.asyncio
async def test_approval_denied_completes_waiter(tmp_path: Path) -> None:
    client = _client(tmp_path)
    request = client._register_approval("a3", "r1", "c1", "shell", {"argv": ["ls"]})
    waiter = asyncio.create_task(client.request_approval(request))
    await asyncio.sleep(0.05)
    decided = await client.approve("a3", "denied")
    assert decided.status == "denied"
    assert (await waiter).status == "denied"


@pytest.mark.asyncio
async def test_cancel_unknown_run_returns_false(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.cancel("no-such-run") is False
