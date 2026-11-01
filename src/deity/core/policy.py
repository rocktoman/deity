"""Deterministic capability policy applied before durable tool intent."""

from dataclasses import dataclass
from typing import Any

from deity.core.contracts.planning import ExecutionMode

_PLAN_ALLOWED = {
    "read_file",
    "list_files",
    "glob",
    "grep",
    "git_status",
    "git_diff",
    "find_symbol",
    "find_references",
    "load_skill",
    "load_skill_resource",
    "submit_plan",
}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


class ExecutionPolicy:
    """Allow all known Build/Verify tools and fail closed in Plan mode."""

    def authorize(
        self, tool_name: str, arguments: dict[str, Any], mode: ExecutionMode
    ) -> PolicyDecision:
        del arguments
        if mode == ExecutionMode.PLAN:
            return PolicyDecision(
                tool_name in _PLAN_ALLOWED,
                "allowed" if tool_name in _PLAN_ALLOWED else "mode_policy_denied",
            )
        return PolicyDecision(True, "allowed")
