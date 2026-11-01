"""Plan-mode dispatcher that cannot elevate workspace capabilities."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from deity.core.contracts import AgentRequest, ToolCall, ToolDispatcher, ToolResult
from deity.core.contracts.planning import ExecutionMode, PlanArtifact
from deity.core.planning import parse_plan_draft, plan_sha256, requirements_sha256
from deity.core.policy import ExecutionPolicy

_SUBMIT_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "goal", "assumptions", "steps", "risks", "verification"],
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "goal": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "object"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "verification": {"type": "array", "items": {"type": "string"}},
    },
}


class PlanningDispatcher:
    """Filter advertised tools and persist one validated PlanArtifact."""

    def __init__(self, delegate: ToolDispatcher, repository: Any, snapshots: Any) -> None:
        self.delegate = delegate
        self.repository = repository
        self.snapshots = snapshots
        self.policy = ExecutionPolicy()
        self._drafts: dict[tuple[str, str], Any] = {}

    def definitions(self) -> tuple[dict[str, Any], ...]:
        definitions = getattr(self.delegate, "definitions", lambda: ())()
        allowed = tuple(
            item
            for item in definitions
            if self.policy.authorize(item["name"], {}, ExecutionMode.PLAN).allowed
        )
        return allowed + (
            {
                "name": "submit_plan",
                "description": "Submit the complete structured plan; this does not modify workspace files",
                "parameters": _SUBMIT_PLAN_SCHEMA,
            },
        )

    async def prepare(self, call: ToolCall, context: AgentRequest) -> ToolResult | None:
        decision = self.policy.authorize(call.name, call.arguments, ExecutionMode.PLAN)
        if not decision.allowed:
            return ToolResult(call.call_id, "denied", decision.reason)
        if call.name == "submit_plan":
            try:
                encoded = json.dumps(call.arguments, ensure_ascii=False, allow_nan=False)
                if len(encoded) > 64_000:
                    raise ValueError("plan too large")
                draft = parse_plan_draft(call.arguments)
            except (TypeError, ValueError, UnicodeError):
                return ToolResult(call.call_id, "error", "invalid_plan")
            self._drafts[(context.run_id, call.call_id)] = draft
            return None
        prepare = getattr(self.delegate, "prepare", None)
        return await prepare(call, context) if callable(prepare) else None

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        if call.name != "submit_plan":
            return await self.delegate.dispatch(call, context)
        draft = self._drafts.pop((context.run_id, call.call_id), None)
        if draft is None:
            return ToolResult(call.call_id, "denied", "preparation_required")
        targets = tuple(sorted({path for step in draft.steps for path in step.files}))
        snapshot = await self.snapshots.capture(targets)
        await self.repository.create_workspace_snapshot(snapshot, run_id=context.run_id)
        artifact = PlanArtifact(
            str(uuid4()),
            context.run_id,
            context.session_id,
            draft,
            plan_sha256(draft),
            requirements_sha256(context.user_text),
            snapshot.snapshot_id,
            "draft",
            datetime.now(UTC).isoformat(),
        )
        await self.repository.create_plan(artifact)
        return ToolResult(call.call_id, "ok", artifact.plan_id)
