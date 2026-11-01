import dataclasses

import pytest

from deity.core.contracts import AgentRequest
from deity.core.contracts.planning import ExecutionMode, PlanDraft, PlanStep
from deity.core.planning import canonical_plan_json, parse_plan_draft, plan_sha256
from deity.core.policy import ExecutionPolicy


def _draft_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "goal": "实现安全功能",
        "assumptions": ["保持兼容"],
        "steps": [
            {
                "ordinal": 1,
                "title": "补测试",
                "description": "先建立失败基线",
                "files": ["tests/test_feature.py"],
                "verification": ["pytest"],
            }
        ],
        "risks": ["兼容风险"],
        "verification": ["pytest -q"],
    }


def test_plan_draft_is_strict_immutable_and_hash_stable() -> None:
    first = parse_plan_draft(_draft_payload())
    second = parse_plan_draft(_draft_payload())
    assert isinstance(first, PlanDraft)
    assert first.steps == (
        PlanStep(
            1,
            "补测试",
            "先建立失败基线",
            ("tests/test_feature.py",),
            ("pytest",),
        ),
    )
    assert canonical_plan_json(first) == canonical_plan_json(second)
    assert plan_sha256(first) == plan_sha256(second)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.goal = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=True),
        lambda value: value.pop("goal"),
        lambda value: value["steps"].append(dict(value["steps"][0])),  # type: ignore[index,union-attr]
        lambda value: value.update(schema_version=2),
    ],
)
def test_plan_draft_rejects_extra_missing_duplicate_and_unknown_version(mutate) -> None:
    payload = _draft_payload()
    mutate(payload)
    with pytest.raises(ValueError):
        parse_plan_draft(payload)


def test_execution_policy_fails_closed_in_plan_mode() -> None:
    policy = ExecutionPolicy()
    assert policy.authorize("read_file", {}, ExecutionMode.PLAN).allowed
    assert policy.authorize("submit_plan", {}, ExecutionMode.PLAN).allowed
    for tool in ("write_file", "apply_patch", "delete_file", "shell", "mcp:one:echo"):
        decision = policy.authorize(tool, {}, ExecutionMode.PLAN)
        assert not decision.allowed and decision.reason == "mode_policy_denied"
    assert not policy.authorize("future_unknown", {}, ExecutionMode.PLAN).allowed


def test_default_request_fingerprint_stays_compatible_but_v05_policy_changes_it() -> None:
    baseline = AgentRequest("s", "t", "/tmp", "task", "m")
    explicit_default = AgentRequest(
        "s", "t", "/tmp", "task", "m", execution_mode=ExecutionMode.BUILD
    )
    planned = AgentRequest(
        "s",
        "t",
        "/tmp",
        "task",
        "m",
        execution_mode=ExecutionMode.PLAN,
        plan_id="plan-1",
        policy_fingerprint="policy-1",
    )
    assert baseline.config_fingerprint() == explicit_default.config_fingerprint()
    assert planned.config_fingerprint() != baseline.config_fingerprint()
