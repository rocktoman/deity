"""User-selected hooks for protecting deletion and recording tool outcomes."""

import asyncio
import json
from pathlib import Path

from deity.core.hooks import HookContext, HookDecision


async def protect_file_deletion(context: HookContext) -> HookDecision:
    """Require a separate workflow for file deletion requests."""
    facts = json.loads(context.facts_json)
    if facts.get("name") == "delete_file":
        return HookDecision(False, "文件删除已由用户 Hook 禁止")
    return HookDecision(True)


def _append_tool_outcome(record: dict[str, str]) -> None:
    path = Path.home() / ".local/state/deity/tool-outcomes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


async def record_tool_outcome(context: HookContext) -> None:
    """Record tool status without arguments, output, or model secrets."""
    facts = json.loads(context.facts_json)
    record = {
        "run_id": context.run_id,
        "call_id": str(facts.get("call_id", "")),
        "name": str(facts.get("name", "")),
        "status": str(facts.get("status", "")),
    }
    await asyncio.to_thread(_append_tool_outcome, record)
