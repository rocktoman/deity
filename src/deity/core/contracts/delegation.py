"""Provider-independent contracts for bounded read-only delegation."""

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

READ_ONLY_TOOLS = frozenset({"read_file", "list_files", "glob", "grep"})


@dataclass(frozen=True)
class DelegationBudget:
    """Hard limits reserved from a parent Run before creating a child."""

    max_turns: int
    max_tool_calls: int
    deadline_seconds: float
    max_input_tokens_total: int | None = None
    max_output_tokens_total: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.max_turns) is not int
            or type(self.max_tool_calls) is not int
            or type(self.deadline_seconds) not in (int, float)
            or min(self.max_turns, self.max_tool_calls, self.deadline_seconds) <= 0
        ):
            raise ValueError("invalid delegation budget")
        for value in (self.max_input_tokens_total, self.max_output_tokens_total):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("invalid delegation token budget")


@dataclass(frozen=True)
class DelegationRequest:
    """An idempotent task contract selected by the parent Run."""

    delegation_id: str
    parent_run_id: str
    goal: str
    allowed_tools: tuple[str, ...]
    budget: DelegationBudget
    expected_result: str = "summary_with_evidence"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or not self.delegation_id.strip()
            or not self.parent_run_id.strip()
            or not self.goal.strip()
            or len(self.goal) > 8000
            or not self.allowed_tools
            or len(set(self.allowed_tools)) != len(self.allowed_tools)
            or not set(self.allowed_tools) <= READ_ONLY_TOOLS
            or self.expected_result != "summary_with_evidence"
        ):
            raise ValueError("invalid delegation contract")

    def digest(self) -> str:
        """Hash a canonical contract for duplicate-ID conflict detection."""
        encoded = json.dumps(
            asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ReadOnlyAgentProfile:
    """System-owned ceiling for child capability and tree size."""

    max_depth: int = 1
    max_children: int = 4
    max_concurrency: int = 2
    max_result_chars: int = 4000

    def __post_init__(self) -> None:
        values = (self.max_depth, self.max_children, self.max_concurrency, self.max_result_chars)
        if any(type(value) is not int or value <= 0 for value in values) or (
            self.max_concurrency > self.max_children
        ):
            raise ValueError("invalid read-only agent profile")


@dataclass(frozen=True)
class SubAgentResult:
    """Bounded result backed by a persisted child Run."""

    delegation_id: str
    child_run_id: str
    status: Literal["completed", "failed", "cancelled", "interrupted"]
    summary: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentTree:
    """Rooted, persisted delegation result snapshot."""

    root_run_id: str
    children: tuple[SubAgentResult, ...]
