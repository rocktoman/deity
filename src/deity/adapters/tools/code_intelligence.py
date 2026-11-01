"""Expose bounded code search through the common tool protocol."""

import json
from dataclasses import asdict
from typing import Any

from deity.core.contracts import AgentRequest, ToolCall, ToolDispatcher, ToolResult
from deity.core.contracts.code_intelligence import CodeSearchPort, ReferenceQuery

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 200},
        "language": {
            "type": "string",
            "enum": ["python", "java", "typescript", "unknown"],
        },
        "path": {"type": "string", "maxLength": 1000},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    },
}


class CodeIntelligenceDispatcher:
    def __init__(self, delegate: ToolDispatcher, search: CodeSearchPort, repository: Any) -> None:
        self.delegate = delegate
        self.search = search
        self.repository = repository
        self._queries: dict[tuple[str, str], ReferenceQuery] = {}
        self._completed: dict[tuple[str, str], dict[str, Any]] = {}

    def definitions(self) -> tuple[dict[str, Any], ...]:
        definitions = getattr(self.delegate, "definitions", lambda: ())()
        return definitions + (
            {
                "name": "find_symbol",
                "description": "Find lexical definition candidates; read the file to verify context",
                "parameters": _SCHEMA,
            },
            {
                "name": "find_references",
                "description": "Find bounded textual references; results are not semantic proof",
                "parameters": _SCHEMA,
            },
        )

    async def prepare(self, call: ToolCall, context: AgentRequest) -> ToolResult | None:
        if call.name not in {"find_symbol", "find_references"}:
            prepare = getattr(self.delegate, "prepare", None)
            return await prepare(call, context) if callable(prepare) else None
        allowed = {"name", "language", "path", "limit"}
        if (
            set(call.arguments) - allowed
            or not isinstance(call.arguments.get("name"), str)
            or not 1 <= len(call.arguments["name"]) <= 200
        ):
            return ToolResult(call.call_id, "error", "invalid_arguments")
        language = call.arguments.get("language")
        if language not in {None, "python", "java", "typescript", "unknown"}:
            return ToolResult(call.call_id, "error", "invalid_arguments")
        path = call.arguments.get("path")
        limit = call.arguments.get("limit", 200)
        if (
            (path is not None and (not isinstance(path, str) or len(path) > 1000))
            or type(limit) is not int
            or not 1 <= limit <= 200
        ):
            return ToolResult(call.call_id, "error", "invalid_arguments")
        self._queries[(context.run_id, call.call_id)] = ReferenceQuery(
            call.arguments["name"], language, path, limit
        )
        return None

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        if call.name not in {"find_symbol", "find_references"}:
            return await self.delegate.dispatch(call, context)
        query = self._queries.pop((context.run_id, call.call_id), None)
        if query is None:
            return ToolResult(call.call_id, "denied", "preparation_required")
        result = (
            await self.search.find_symbol(query)
            if call.name == "find_symbol"
            else await self.search.find_references(query)
        )
        self._completed[(context.run_id, call.call_id)] = {
            "tool": call.name,
            "query": query.symbol,
            "result_count": len(result.locations),
            "partial": result.partial,
            "diagnostics": list(result.diagnostics),
        }
        return ToolResult(
            call.call_id,
            "ok",
            json.dumps(asdict(result), ensure_ascii=False, separators=(",", ":")),
        )

    async def after_persist(
        self, call: ToolCall, result: ToolResult, context: AgentRequest
    ) -> None:
        completed = self._completed.pop((context.run_id, call.call_id), None)
        if completed is not None and result.status == "ok":
            await self.repository.record_code_index_updated(context.run_id, completed)
        callback = getattr(self.delegate, "after_persist", None)
        if callable(callback):
            await callback(call, result, context)
