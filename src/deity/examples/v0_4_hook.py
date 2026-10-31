"""Example trusted local policy Hook for explicit configuration."""

import json

from deity.core.hooks import HookContext, HookDecision


async def deny_delete(context: HookContext) -> HookDecision:
    """Reject file deletion; leave all other tool decisions to the Tool Runtime."""
    facts = json.loads(context.facts_json)
    if facts.get("name") == "delete_file":
        return HookDecision(False, "example policy denies deletion")
    return HookDecision(True)
