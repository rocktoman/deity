import json

import pytest

from deity.core.contracts import AgentRequest
from deity.core.contracts.delegation import (
    DelegationBudget,
    DelegationRequest,
    ReadOnlyAgentProfile,
    SubAgentResult,
)
from deity.core.delegation import DelegationPolicy, ResultReducer


def test_batch_policy_rejects_duplicate_and_overcommit() -> None:
    parent = AgentRequest("s", "t", "/tmp", "inspect", "fake", 4, 5, run_id="p")
    contract = DelegationRequest("d", "p", "inspect", ("read_file",), DelegationBudget(2, 2, 5))
    policy = DelegationPolicy(ReadOnlyAgentProfile())
    with pytest.raises(ValueError, match="duplicate"):
        policy.validate_batch((contract, contract), parent)
    with pytest.raises(ValueError, match="turn budget"):
        policy.validate_batch(
            (
                contract,
                DelegationRequest("d2", "p", "inspect", ("grep",), DelegationBudget(2, 2, 5)),
            ),
            parent,
        )


def test_result_reducer_preserves_partial_status() -> None:
    encoded = ResultReducer.reduce(
        (
            SubAgentResult("a", "one", "completed", "ok"),
            SubAgentResult("b", "two", "failed", ""),
        )
    )
    assert json.loads(encoded)["status"] == "partial"


def test_result_reducer_keeps_json_valid_under_output_limit() -> None:
    encoded = ResultReducer.reduce(
        (SubAgentResult("a", "one", "completed", "x" * 10000),), max_chars=500
    )
    assert len(encoded) <= 500
    assert json.loads(encoded)["results"][0]["status"] == "completed"
