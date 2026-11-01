import json
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.verification import VerificationExecutor
from deity.application.build_service import BuildService
from deity.application.config import CodingConfig, GitGuardConfig, PermissionConfig
from deity.application.plan_service import PlanService
from deity.application.run_service import RunService
from deity.application.verification_service import VerificationService
from deity.core.contracts import ModelEvent, ModelRequest
from deity.core.contracts.verification import VerificationCommand, VerificationPlan

PLAN = {
    "schema_version": 1,
    "goal": "修改 feature.py",
    "assumptions": [],
    "steps": [
        {
            "ordinal": 1,
            "title": "修改",
            "description": "将 1 改为 2",
            "files": ["feature.py"],
            "verification": ["pytest"],
        }
    ],
    "risks": [],
    "verification": ["pytest -q"],
}


class PlanModel:
    def __init__(self) -> None:
        self.turn = 0

    async def stream(self, _request: ModelRequest):
        self.turn += 1
        if self.turn == 1:
            yield ModelEvent("tool_call_started", call_id="write", name="write_file")
            yield ModelEvent(
                "tool_call_arguments_delta",
                call_id="write",
                text='{"path":"blocked.txt","content":"x"}',
            )
            yield ModelEvent("tool_call_started", call_id="plan", name="submit_plan")
            import json

            yield ModelEvent("tool_call_arguments_delta", call_id="plan", text=json.dumps(PLAN))
        else:
            yield ModelEvent("text_delta", text="计划已提交")
        yield ModelEvent("message_completed")


class BuildModel:
    def __init__(self) -> None:
        self.turn = 0

    async def stream(self, _request: ModelRequest):
        self.turn += 1
        if self.turn == 1:
            yield ModelEvent("tool_call_started", call_id="read", name="read_file")
            yield ModelEvent(
                "tool_call_arguments_delta", call_id="read", text='{"path":"feature.py"}'
            )
        elif self.turn == 2:
            yield ModelEvent("tool_call_started", call_id="patch", name="apply_patch")
            yield ModelEvent(
                "tool_call_arguments_delta",
                call_id="patch",
                text='{"path":"feature.py","old":"value = 1","new":"value = 2"}',
            )
        else:
            yield ModelEvent("text_delta", text="构建完成")
        yield ModelEvent("message_completed")


@pytest.mark.asyncio
async def test_invalid_model_plan_reports_rejected_submission(tmp_path: Path) -> None:
    class InvalidPlanModel:
        def __init__(self) -> None:
            self.turn = 0

        async def stream(self, request: ModelRequest):
            self.turn += 1
            if self.turn == 1:
                assert any(
                    item["name"] == "submit_plan"
                    and "ordinal" in item["parameters"]["properties"]["steps"]["items"]["required"]
                    for item in request.tools
                )
                yield ModelEvent("tool_call_started", call_id="bad-plan", name="submit_plan")
                malformed = {
                    **PLAN,
                    "steps": [{"id": 1, "action": "修改", "files": ["feature.py"]}],
                }
                yield ModelEvent(
                    "tool_call_arguments_delta", call_id="bad-plan", text=json.dumps(malformed)
                )
            else:
                yield ModelEvent("text_delta", text="计划完成")
            yield ModelEvent("message_completed")

    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=InvalidPlanModel(),
        coding=CodingConfig(plan_build_enabled=True),
    )
    with pytest.raises(ValueError, match="计划提交被拒绝"):
        await PlanService(service).create("规划修改", model_name="fake")


@pytest.mark.asyncio
async def test_plan_and_build_run_use_real_loop_and_persist_changeset(tmp_path: Path) -> None:
    db = tmp_path / "deity.db"
    (tmp_path / "feature.py").write_text("value = 1\n", encoding="utf-8")
    plan_runner = RunService(
        db_path=db,
        workspace=tmp_path,
        model=PlanModel(),
        coding=CodingConfig(plan_build_enabled=True, git_guard_enabled=True),
        git_guard=GitGuardConfig(),
    )
    artifact = await PlanService(plan_runner).create("修改 feature.py", model_name="fake")
    assert not (tmp_path / "blocked.txt").exists()

    start = await BuildService(db, tmp_path, plan_ttl_seconds=3600).start(artifact.plan_id)
    build_runner = RunService(
        db_path=db,
        workspace=tmp_path,
        model=BuildModel(),
        permission=PermissionConfig(shell="auto", workspace_write="auto"),
        coding=CodingConfig(plan_build_enabled=True, git_guard_enabled=True),
        git_guard=GitGuardConfig(),
    )
    result = await build_runner.run_build(start.build_run_id, artifact.plan_id)
    assert result.final_text == "构建完成"
    assert (tmp_path / "feature.py").read_text(encoding="utf-8") == "value = 2\n"

    repo = await SQLiteSessionRepository.open(db)
    try:
        change_set = await repo.change_set_for_run(start.build_run_id)
        assert change_set.status == "completed"
        assert [(item.path, item.operation) for item in change_set.changes] == [
            ("feature.py", "modify")
        ]
        events = await repo.events(start.build_run_id)
        completed = [event["type"] for event in events]
        assert completed.index("ToolCompleted") < completed.index("FileChangeRecorded")
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_verification_service_persists_evidence_and_final_status(tmp_path: Path) -> None:
    db = tmp_path / "deity.db"
    executor = VerificationExecutor(
        tmp_path,
        permission="auto",
        allowed_commands=("python3",),
    )
    service = VerificationService(db, tmp_path, executor=executor)
    report = await service.run_plan(
        VerificationPlan((VerificationCommand("test", ("python3", "-c", "print('ok')")),))
    )
    assert report.status.value == "passed"
    assert len(report.checks) == 1 and report.checks[0].evidence_id

    repo = await SQLiteSessionRepository.open(db)
    try:
        stored = await repo.verification_report(report.run_id)
        assert stored.status.value == "passed"
        assert stored.checks[0].exit_code == 0
    finally:
        await repo.close()
