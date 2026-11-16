"""ADR-021 B6/B3：可写委派端到端——暂存隔离、patch 回收、审批门。"""

import json
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools.patch import plan_patch
from deity.application.config import MultiAgentConfig, RunConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, ModelRequest, RunStatus


class WritableModel:
    """父：发 writable delegate；子：在暂存区写新文件并改旧文件；父：记录委派结果。"""

    def __init__(self) -> None:
        self.delegate_results: list[str] = []

    async def stream(self, request: ModelRequest):
        user = next(message["content"] for message in request.messages if message["role"] == "user")
        tool_messages = [message for message in request.messages if message["role"] == "tool"]
        if user == "parent" and not tool_messages:
            yield ModelEvent("tool_call_started", call_id="delegate-1", name="delegate")
            yield ModelEvent(
                "tool_call_arguments_delta",
                call_id="delegate-1",
                text=json.dumps(
                    {
                        "tasks": [
                            {
                                "goal": "draft changes",
                                "allowed_tools": ["read_file", "write_file", "apply_patch"],
                                "max_turns": 3,
                                "max_tool_calls": 4,
                                "deadline_seconds": 30,
                                "mode": "writable",
                            }
                        ]
                    }
                ),
            )
        elif user == "draft changes" and not tool_messages:
            assert {tool["name"] for tool in request.tools} == {
                "read_file",
                "write_file",
                "apply_patch",
            }
            yield ModelEvent("tool_call_started", call_id="w1", name="write_file")
            yield ModelEvent(
                "tool_call_arguments_delta",
                call_id="w1",
                text=json.dumps({"path": "draft.txt", "content": "draft body\n"}),
            )
            yield ModelEvent("tool_call_started", call_id="w2", name="apply_patch")
            yield ModelEvent(
                "tool_call_arguments_delta",
                call_id="w2",
                text=json.dumps({"path": "notes.txt", "old": "original", "new": "edited by child"}),
            )
        elif user == "parent":
            self.delegate_results.append(str(tool_messages[-1]["content"]))
            yield ModelEvent("text_delta", text="parent done")
        else:
            yield ModelEvent("text_delta", text="child done")
        yield ModelEvent("message_completed")


def _service(tmp_path: Path, model: WritableModel, **kwargs: object) -> RunService:
    return RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,
        limits=RunConfig(max_turns=8, max_tool_calls=12),
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_writable_delegation_harvests_patch_without_touching_workspace(
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.txt").write_text("original\n", encoding="utf-8")
    model = WritableModel()
    service = _service(
        tmp_path,
        model,
        approve=lambda name, args: True,
        multi_agent=MultiAgentConfig(enabled=True, writable=True),
    )
    result = await service.run("parent", model_name="fake")
    assert result.status == RunStatus.COMPLETED
    # 暂存隔离：真实工作区未被子 Run 改动。
    assert not (tmp_path / "draft.txt").exists()
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "original\n"
    # 回收：父模型拿到的委派结果带 patch 与信任等级。
    assert len(model.delegate_results) == 1
    payload = json.loads(model.delegate_results[0])
    assert payload["status"] == "completed"
    assert payload["trust_grade"] == "high"  # 含 apply_patch → high（ADR-021 B3）
    child_result = payload["results"][0]
    assert set(child_result["patch_files"]) == {"draft.txt", "notes.txt"}
    patch = child_result["patch"]
    assert patch is not None and "*** Add File: draft.txt" in patch

    # patch 可被父级以 plan_patch 校验并应用到真实工作区。
    def read(path: str) -> str | None:
        target = tmp_path / path
        return target.read_text(encoding="utf-8") if target.exists() else None

    changes = {change.path: change for change in plan_patch(patch, read)}
    assert changes["draft.txt"].new_content == "draft body\n"
    assert changes["notes.txt"].new_content == "edited by child\n"
    # 审计：委派树结果含 patch，父运行有回收事件，子运行有 patch 证据。
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        tree = await repo.delegation_tree(result.run_id)
        assert len(tree) == 1
        stored = json.loads(tree[0]["result_json"])
        assert stored["patch"] == patch
        events = await repo.events(result.run_id)
        assert any(event["type"] == "DelegationPatchHarvested" for event in events)
        child_evidence = await repo.evidence(tree[0]["child_run_id"])
        assert any(item["kind"] == "delegation_patch" for item in child_evidence)
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_writable_delegation_rejected_when_switch_off(tmp_path: Path) -> None:
    model = WritableModel()
    service = _service(
        tmp_path,
        model,
        approve=lambda name, args: True,
        multi_agent=MultiAgentConfig(enabled=True, writable=False),
    )
    result = await service.run("parent", model_name="fake")
    assert result.status == RunStatus.COMPLETED
    assert "writable delegation is disabled" in model.delegate_results[0]
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        assert await repo.delegation_tree(result.run_id) == []
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_writable_delegation_never_auto_approved(tmp_path: Path) -> None:
    model = WritableModel()
    service = _service(
        tmp_path,
        model,
        multi_agent=MultiAgentConfig(enabled=True, writable=True),
    )
    result = await service.run("parent", model_name="fake")
    assert result.status == RunStatus.COMPLETED
    assert "writable_delegation_requires_approval" in model.delegate_results[0]
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        assert await repo.delegation_tree(result.run_id) == []
    finally:
        await repo.close()
