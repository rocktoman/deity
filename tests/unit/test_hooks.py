import asyncio
from dataclasses import FrozenInstanceError

import pytest

from deity.core.hooks import HookContext, HookDecision, HookRegistry, RegisteredHook


@pytest.mark.asyncio
async def test_policy_veto_and_observer_failure_are_distinct() -> None:
    async def veto(_context: HookContext) -> HookDecision:
        return HookDecision(False, "blocked")

    async def broken(_context: HookContext) -> None:
        raise RuntimeError("observer failed")

    registry = HookRegistry(
        (
            RegisteredHook("z", "BeforeTool", "policy", 1, veto),
            RegisteredHook("a", "BeforeTool", "observer", 0, broken),
        )
    )
    outcomes = await registry.invoke("run", "BeforeTool", {"path": "a.txt"})
    assert [(item.name, item.status) for item in outcomes] == [("a", "failed"), ("z", "veto")]


@pytest.mark.asyncio
async def test_policy_timeout_fails_closed() -> None:
    async def stuck(_context: HookContext) -> HookDecision:
        await asyncio.sleep(10)
        return HookDecision(True)

    registry = HookRegistry((RegisteredHook("slow", "BeforeModel", "policy", 0, stuck),), 0.01)
    outcomes = await registry.invoke("run", "BeforeModel", {})
    assert outcomes[0].status == "veto" and outcomes[0].reason == "TimeoutError"


@pytest.mark.asyncio
async def test_recursive_invocation_is_vetoed() -> None:
    registry: HookRegistry

    async def recursive(_context: HookContext) -> HookDecision:
        nested = await registry.invoke("run", "BeforeTool", {})
        assert nested[0].reason == "recursive_hook"
        return HookDecision(True)

    registry = HookRegistry((RegisteredHook("recursive", "BeforeTool", "policy", 0, recursive),))
    assert (await registry.invoke("run", "BeforeTool", {}))[0].status == "allow"


@pytest.mark.asyncio
async def test_context_is_immutable_and_cancellation_propagates() -> None:
    started = asyncio.Event()

    async def observer(context: HookContext) -> None:
        with pytest.raises(FrozenInstanceError):
            context.facts_json = "changed"  # type: ignore[misc]
        started.set()
        await asyncio.sleep(10)

    registry = HookRegistry((RegisteredHook("observer", "AfterTool", "observer", 0, observer),))
    task = asyncio.create_task(registry.invoke("run", "AfterTool", {"status": "error"}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_after_tool_rejects_policy_hook() -> None:
    async def allow(_context: HookContext) -> HookDecision:
        return HookDecision(True)

    with pytest.raises(ValueError, match="AfterTool"):
        HookRegistry((RegisteredHook("invalid", "AfterTool", "policy", 0, allow),))
