"""Deterministic, bounded hook decisions without permission escalation."""

import asyncio
import contextvars
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

HookPoint = Literal["BeforeModel", "BeforeTool", "AfterTool", "BeforeFinish"]
HookKind = Literal["observer", "policy"]


class HookFailurePolicy(StrEnum):
    """Fixed V0.4 handling for untrusted local Hook failures."""

    FAIL_CLOSED = "fail_closed"
    ISOLATE = "isolate"


@dataclass(frozen=True)
class HookContext:
    run_id: str
    point: HookPoint
    facts_json: str

    @classmethod
    def from_facts(cls, run_id: str, point: HookPoint, facts: dict[str, Any]) -> "HookContext":
        """Serialize facts so hooks cannot mutate shared Runtime state."""
        return cls(run_id, point, json.dumps(facts, sort_keys=True, ensure_ascii=False))


@dataclass(frozen=True)
class HookDecision:
    allow: bool
    reason: str = ""


@dataclass(frozen=True)
class RegisteredHook:
    name: str
    point: HookPoint
    kind: HookKind
    priority: int
    callback: Callable[[HookContext], Awaitable[HookDecision | None]]


@dataclass(frozen=True)
class HookOutcome:
    name: str
    point: HookPoint
    kind: HookKind
    status: Literal["allow", "veto", "observed", "failed"]
    reason: str


class HookRegistry:
    """Run explicitly registered local hooks in stable order."""

    def __init__(self, hooks: tuple[RegisteredHook, ...], timeout_seconds: float = 2.0) -> None:
        if timeout_seconds <= 0 or len({(h.name, h.point) for h in hooks}) != len(hooks):
            raise ValueError("invalid Hook registration")
        if any(h.point == "AfterTool" and h.kind == "policy" for h in hooks):
            raise ValueError("AfterTool only accepts observer Hooks")
        self.hooks = tuple(sorted(hooks, key=lambda h: (h.priority, h.name, h.point)))
        self.timeout_seconds = timeout_seconds
        self._inside: contextvars.ContextVar[bool] = contextvars.ContextVar(
            "deity_hook_inside", default=False
        )

    async def invoke(
        self, run_id: str, point: HookPoint, facts: dict[str, Any]
    ) -> tuple[HookOutcome, ...]:
        """Policy failures veto; observer failures are isolated and reported."""
        if self._inside.get():
            return (HookOutcome("registry", point, "policy", "veto", "recursive_hook"),)
        context = HookContext.from_facts(run_id, point, facts)
        token = self._inside.set(True)
        outcomes: list[HookOutcome] = []
        try:
            for hook in self.hooks:
                if hook.point != point:
                    continue
                try:
                    decision = await asyncio.wait_for(hook.callback(context), self.timeout_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - third-party hooks must be isolated
                    failure_policy = (
                        HookFailurePolicy.FAIL_CLOSED
                        if hook.kind == "policy"
                        else HookFailurePolicy.ISOLATE
                    )
                    outcomes.append(
                        HookOutcome(
                            hook.name,
                            point,
                            hook.kind,
                            "veto" if failure_policy == HookFailurePolicy.FAIL_CLOSED else "failed",
                            type(exc).__name__,
                        )
                    )
                    continue
                if hook.kind == "observer":
                    outcomes.append(HookOutcome(hook.name, point, hook.kind, "observed", ""))
                elif decision is not None and not decision.allow:
                    outcomes.append(
                        HookOutcome(hook.name, point, hook.kind, "veto", decision.reason[:500])
                    )
                else:
                    outcomes.append(HookOutcome(hook.name, point, hook.kind, "allow", ""))
        finally:
            self._inside.reset(token)
        return tuple(outcomes)
