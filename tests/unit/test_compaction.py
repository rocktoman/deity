"""Deterministic context budget and compaction gates."""

import pytest

from deity.core.context.reliable import ContextBudget, ContextCompactor, ContextOverflow


def test_compaction_keeps_goal_failures_and_recent_turns() -> None:
    messages = [{"role": "user", "content": "必须保留约束 XYZ"}]
    for index in range(100):
        messages.append({"role": "assistant", "content": f"turn-{index}"})
        messages.append(
            {
                "role": "tool",
                "call_id": f"c{index}",
                "content": "large-output-" * 100,
                "status": "error" if index == 17 else "ok",
                "evidence_id": "ev17" if index == 17 else None,
            }
        )
    budget = ContextBudget(4000, 400, 200)
    snapshot = ContextCompactor().compact(messages, budget)
    encoded = str(snapshot.messages)
    assert "必须保留约束 XYZ" in encoded
    assert "c17" in encoded and "ev17" in encoded
    assert "turn-99" in encoded and "turn-96" in encoded
    assert snapshot.estimated_tokens <= budget.input_limit
    assert snapshot.artifacts
    assert all(ref.id == ref.sha256 and ref.truncated for ref in snapshot.artifacts)
    assert ContextCompactor().compact(list(snapshot.messages), budget).sha256 == snapshot.sha256


def test_uncompressible_user_constraints_fail_explicitly() -> None:
    with pytest.raises(ContextOverflow):
        ContextCompactor().compact(
            [{"role": "user", "content": "不可删" * 2000}], ContextBudget(1000, 200, 100)
        )


def test_budget_rejects_unknown_or_impossible_window() -> None:
    with pytest.raises(ValueError):
        ContextBudget(0, 100, 100)
    with pytest.raises(ValueError):
        ContextBudget(100, 80, 20)


def test_twenty_successive_compactions_keep_old_evidence_and_modifications() -> None:
    messages: list[dict[str, object]] = [{"role": "user", "content": "do not remove constraint"}]
    messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"call_id": "write-1", "name": "apply_patch", "arguments": {"path": "calc.py"}}
            ],
        }
    )
    messages.append(
        {"role": "tool", "call_id": "write-1", "content": "patched", "evidence_id": "ev1"}
    )
    compactor = ContextCompactor()
    for index in range(20):
        messages.append({"role": "assistant", "content": f"turn {index}"})
        messages.append({"role": "tool", "call_id": f"read-{index}", "content": "data" * 300})
        messages = list(compactor.compact(messages, ContextBudget(4000, 400, 200)).messages)
    final = str(messages)
    assert "constraint" in final
    assert "calc.py" in final
    assert "ev1" in final
