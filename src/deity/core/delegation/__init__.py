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


def grade_delegation(
    contracts: tuple[DelegationRequest, ...],
) -> Literal["standard", "elevated", "high"]:
    """ADR-021 B3：批次级信任分级（纯函数、确定性）。

    全部只读为 standard；任一可写为 elevated；可写且含 apply_patch 或
    批次预算合计较大（≥16 轮或 ≥40 次工具调用）为 high。
    """
    if all(contract.mode == "read_only" for contract in contracts):
        return "standard"
    total_turns = sum(contract.budget.max_turns for contract in contracts)
    total_calls = sum(contract.budget.max_tool_calls for contract in contracts)
    if (
        any("apply_patch" in contract.allowed_tools for contract in contracts)
        or total_turns >= 16
        or total_calls >= 40
    ):
        return "high"
    return "elevated"


class ResultReducer:
    """Build a bounded, structured parent-visible result."""

    @staticmethod
    def reduce(
        results: tuple[SubAgentResult, ...],
        max_chars: int = 16000,
        trust_grade: Literal["standard", "elevated", "high"] = "standard",
    ) -> str:
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
                {
                    "status": aggregate,
                    "trust_grade": trust_grade,
                    "results": [asdict(result) for result in bounded],
                },
                ensure_ascii=False,
            )
            if len(encoded) <= max_chars:
                return encoded
            # ADR-021：超限时先整段丢弃 patch（半截 patch 不可应用），再减半摘要。
            with_patch = [result for result in bounded if result.patch is not None]
            if with_patch:
                bounded = [
                    replace(
                        result,
                        patch=None,
                        summary=result.summary + " [patch omitted: result size limit]",
                    )
                    if result.patch is not None
                    else result
                    for result in bounded
                ]
                continue
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
