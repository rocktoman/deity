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
        "goal": {"type": "string", "minLength": 1, "maxLength": 4000},
        "assumptions": {
            "type": "array",
            "maxItems": 50,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ordinal", "title", "description", "files", "verification"],
                "properties": {
                    "ordinal": {"type": "integer", "minimum": 1},
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "description": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "files": {
                        "type": "array",
                        "maxItems": 100,
                        "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                    },
                    "verification": {
                        "type": "array",
                        "maxItems": 30,
                        "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                    },
                },
            },
        },
        "risks": {
            "type": "array",
            "maxItems": 50,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "verification": {
            "type": "array",
            "maxItems": 50,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
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
                "description": (
                    "提交完整只读计划。每个步骤必须有连续的 ordinal(从1开始)、title、"
                    "description、files 和 verification；不要使用 id/action 等替代字段。"
                ),
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
            except (TypeError, ValueError, UnicodeError) as exc:
                return ToolResult(call.call_id, "error", f"invalid_plan: {exc}")
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
