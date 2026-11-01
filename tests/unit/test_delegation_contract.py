"""V0.7 delegation contracts must fail closed before any child Run exists."""

import pytest

from deity.core.contracts.delegation import (
    DelegationBudget,
    DelegationRequest,
    ReadOnlyAgentProfile,
)


def test_contract_rejects_empty_goal_and_unsafe_tools() -> None:
    budget = DelegationBudget(2, 4, 30)
    with pytest.raises(ValueError):
        DelegationRequest("d1", "parent", "", ("read_file",), budget)
    with pytest.raises(ValueError):
        DelegationRequest("d1", "parent", "inspect", ("shell",), budget)


def test_contract_rejects_bad_budget_and_depth() -> None:
    with pytest.raises(ValueError):
        DelegationBudget(0, 4, 30)
    with pytest.raises(ValueError):
        ReadOnlyAgentProfile(max_depth=0)


def test_contract_has_stable_hash() -> None:
    request = DelegationRequest(
        "d1", "parent", "inspect", ("read_file", "grep"), DelegationBudget(2, 4, 30)
    )
    assert request.digest() == request.digest()
    assert len(request.digest()) == 64
