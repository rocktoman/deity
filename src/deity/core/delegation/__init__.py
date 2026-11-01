"""Pure delegation policy and result reduction."""

import json
from dataclasses import asdict, dataclass, replace
from typing import Literal

from deity.core.contracts import AgentRequest
from deity.core.contracts.delegation import DelegationRequest, ReadOnlyAgentProfile, SubAgentResult


@dataclass(frozen=True)
class DelegationPolicy:
    """Validate one batch before it can reserve persistent child Runs."""

    profile: ReadOnlyAgentProfile

    def validate_batch(
        self, contracts: tuple[DelegationRequest, ...], parent: AgentRequest
    ) -> None:
        """Reject a malformed or overcommitted batch before any child is created."""
        if not contracts or len(contracts) > self.profile.max_children:
            raise ValueError("delegation child limit exceeded")
        if len({item.delegation_id for item in contracts}) != len(contracts):
            raise ValueError("duplicate delegation ID in batch")
        if any(item.parent_run_id != parent.run_id for item in contracts):
            raise ValueError("delegation parent mismatch")
        if sum(item.budget.max_turns for item in contracts) >= parent.max_turns:
            raise ValueError("delegation turn budget exceeded")
        if sum(item.budget.max_tool_calls for item in contracts) >= parent.max_tool_calls:
            raise ValueError("delegation tool budget exceeded")


class ResultReducer:
    """Build a bounded, structured parent-visible result."""

    @staticmethod
    def reduce(results: tuple[SubAgentResult, ...], max_chars: int = 16000) -> str:
        """Preserve each child status while separating aggregate partial state."""
        if not results:
            raise ValueError("no child result")
        successes = sum(result.status == "completed" for result in results)
        aggregate: Literal["completed", "partial", "failed"] = (
            "completed" if successes == len(results) else "partial" if successes else "failed"
        )
        bounded = list(results)
        while True:
            encoded = json.dumps(
                {"status": aggregate, "results": [asdict(result) for result in bounded]},
                ensure_ascii=False,
            )
            if len(encoded) <= max_chars:
                return encoded
            next_results = [
                replace(
                    result,
                    summary=result.summary[: len(result.summary) // 2],
                    evidence_ids=result.evidence_ids[: len(result.evidence_ids) // 2],
                )
                for result in bounded
            ]
            if next_results == bounded:
                raise ValueError("delegation result metadata exceeds output limit")
            bounded = next_results
