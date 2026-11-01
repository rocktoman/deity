import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from deity.adapters.git import GitWorkspaceAdapter
from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools import LocalToolDispatcher
from deity.adapters.tools.planning import PlanningDispatcher
from deity.application.build_service import BuildService
from deity.core.contracts import AgentRequest, ToolCall
from deity.core.contracts.planning import ExecutionMode


def _payload(path: str = "feature.py") -> dict[str, object]:
    return {
        "schema_version": 1,
        "goal": "实现功能",
        "assumptions": [],
        "steps": [
            {
                "ordinal": 1,
                "title": "实现",
                "description": "最小实现",
                "files": [path],
                "verification": ["pytest"],
            }
        ],
        "risks": [],
        "verification": ["pytest -q"],
    }


async def _git(root: Path, *args: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "git", *args, cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, (stdout + stderr).decode(errors="replace")


@pytest.mark.asyncio
async def test_plan_dispatcher_denies_effects_and_persists_artifact(tmp_path: Path) -> None:
    db_path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        request = AgentRequest(
            "session",
            "task",
            str(tmp_path),
            "实现功能",
            "fake",
            execution_mode=ExecutionMode.PLAN,
        )
        run_id = await repo.create_run(request)
        request = replace(request, run_id=run_id)
        local = LocalToolDispatcher(approve=lambda _name, _args: True)
        dispatcher = PlanningDispatcher(local, repo, GitWorkspaceAdapter(tmp_path))

        names = {item["name"] for item in dispatcher.definitions()}
        assert "submit_plan" in names and "read_file" in names
        assert not {"write_file", "apply_patch", "delete_file", "shell"} & names
        schema = next(
            item["parameters"] for item in dispatcher.definitions() if item["name"] == "submit_plan"
        )
        assert Draft202012Validator(schema).is_valid(_payload())
        malformed = _payload()
        malformed["steps"] = [{"id": 1, "action": "修改", "files": ["feature.py"]}]
        assert not Draft202012Validator(schema).is_valid(malformed)

        denied = await dispatcher.prepare(
            ToolCall("write-1", "write_file", {"path": "blocked.txt", "content": "x"}),
            request,
        )
        assert denied is not None and denied.status == "denied"
        assert not (tmp_path / "blocked.txt").exists()

        submit = ToolCall("plan-1", "submit_plan", _payload())
        assert await dispatcher.prepare(submit, request) is None
        result = await dispatcher.dispatch(submit, request)
        assert result.status == "ok"
        artifact = await repo.plan_for_run(run_id)
        assert artifact.draft.goal == "实现功能"
        assert artifact.source_run_id == run_id
        events = await repo.events(run_id)
        assert any(event["type"] == "PlanSubmitted" for event in events)
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_build_approval_is_unique_and_detects_workspace_drift(tmp_path: Path) -> None:
    db_path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        request = AgentRequest(
            "session",
            "task",
            str(tmp_path),
            "实现功能",
            "fake",
            execution_mode=ExecutionMode.PLAN,
        )
        run_id = await repo.create_run(request)
        request = replace(request, run_id=run_id)
        dispatcher = PlanningDispatcher(
            LocalToolDispatcher(), repo, GitWorkspaceAdapter(tmp_path, ignored=(db_path,))
        )
        submit = ToolCall("plan-1", "submit_plan", _payload())
        assert await dispatcher.prepare(submit, request) is None
        assert (await dispatcher.dispatch(submit, request)).status == "ok"
        artifact = await repo.plan_for_run(run_id)
    finally:
        await repo.close()

    service = BuildService(db_path, tmp_path, plan_ttl_seconds=3600)
    build = await service.start(artifact.plan_id)
    assert build.plan_id == artifact.plan_id
    assert build.build_run_id and build.change_set_id
    with pytest.raises(ValueError, match="already approved"):
        await service.start(artifact.plan_id)

    other = tmp_path / "unrelated.py"
    other.write_text("changed", encoding="utf-8")
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        new_run = await repo.create_run(
            replace(request, task_id="task-2", run_id="", user_text="新需求")
        )
        new_request = replace(request, task_id="task-2", run_id=new_run, user_text="新需求")
        dispatcher = PlanningDispatcher(
            LocalToolDispatcher(), repo, GitWorkspaceAdapter(tmp_path, ignored=(db_path,))
        )
        submit = ToolCall("plan-2", "submit_plan", _payload("other.py"))
        assert await dispatcher.prepare(submit, new_request) is None
        assert (await dispatcher.dispatch(submit, new_request)).status == "ok"
        second = await repo.plan_for_run(new_run)
    finally:
        await repo.close()
    other.write_text("drifted", encoding="utf-8")
    with pytest.raises(ValueError, match="workspace drift"):
        await service.start(second.plan_id)


@pytest.mark.asyncio
async def test_build_rejects_initial_dirty_target_without_exact_allowlist(tmp_path: Path) -> None:
    # A non-Git workspace still protects the captured target preimage.
    target = tmp_path / "feature.py"
    target.write_text("user baseline", encoding="utf-8")
    db_path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        request = AgentRequest(
            "session",
            "task",
            str(tmp_path),
            "实现功能",
            "fake",
            execution_mode=ExecutionMode.PLAN,
        )
        run_id = await repo.create_run(request)
        request = replace(request, run_id=run_id)
        dispatcher = PlanningDispatcher(
            LocalToolDispatcher(), repo, GitWorkspaceAdapter(tmp_path, ignored=(db_path,))
        )
        submit = ToolCall("plan-1", "submit_plan", _payload())
        assert await dispatcher.prepare(submit, request) is None
        assert (await dispatcher.dispatch(submit, request)).status == "ok"
        artifact = await repo.plan_for_run(run_id)
    finally:
        await repo.close()

    service = BuildService(db_path, tmp_path, plan_ttl_seconds=3600)
    # Non-Git tracked preimages are not classified dirty, but invalid allowlists still fail closed.
    with pytest.raises(ValueError, match="invalid allow-dirty"):
        await service.start(artifact.plan_id, allow_dirty_paths=("../feature.py",))


@pytest.mark.asyncio
async def test_git_dirty_target_requires_exact_allowlist(tmp_path: Path) -> None:
    await _git(tmp_path, "init", "-q")
    await _git(tmp_path, "config", "user.email", "fixture@example.com")
    await _git(tmp_path, "config", "user.name", "Fixture")
    target = tmp_path / "feature.py"
    target.write_text("baseline\n", encoding="utf-8")
    await _git(tmp_path, "add", "feature.py")
    await _git(tmp_path, "commit", "-qm", "基线")
    target.write_text("user dirty\n", encoding="utf-8")

    db_path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        request = AgentRequest(
            "session",
            "task",
            str(tmp_path),
            "实现功能",
            "fake",
            execution_mode=ExecutionMode.PLAN,
        )
        run_id = await repo.create_run(request)
        request = replace(request, run_id=run_id)
        dispatcher = PlanningDispatcher(
            LocalToolDispatcher(), repo, GitWorkspaceAdapter(tmp_path, ignored=(db_path,))
        )
        submit = ToolCall("plan-dirty", "submit_plan", _payload())
        assert await dispatcher.prepare(submit, request) is None
        assert (await dispatcher.dispatch(submit, request)).status == "ok"
        artifact = await repo.plan_for_run(run_id)
    finally:
        await repo.close()

    service = BuildService(db_path, tmp_path, plan_ttl_seconds=3600)
    assert await service.initial_dirty_targets(artifact.plan_id) == ("feature.py",)
    with pytest.raises(ValueError, match="initial dirty path"):
        await service.start(artifact.plan_id)
    started = await service.start(artifact.plan_id, allow_dirty_paths=("feature.py",))
    assert started.build_run_id


@pytest.mark.asyncio
async def test_expired_plan_is_marked_stale(tmp_path: Path) -> None:
    db_path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        request = AgentRequest(
            "session",
            "task",
            str(tmp_path),
            "实现功能",
            "fake",
            execution_mode=ExecutionMode.PLAN,
        )
        run_id = await repo.create_run(request)
        request = replace(request, run_id=run_id)
        dispatcher = PlanningDispatcher(
            LocalToolDispatcher(), repo, GitWorkspaceAdapter(tmp_path, ignored=(db_path,))
        )
        submit = ToolCall("plan-expired", "submit_plan", _payload())
        assert await dispatcher.prepare(submit, request) is None
        assert (await dispatcher.dispatch(submit, request)).status == "ok"
        artifact = await repo.plan_for_run(run_id)
        await repo.db.execute(
            "UPDATE plans SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (artifact.plan_id,),
        )
        await repo.db.commit()
    finally:
        await repo.close()
    with pytest.raises(ValueError, match="expired"):
        await BuildService(db_path, tmp_path, plan_ttl_seconds=300).start(artifact.plan_id)
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        assert (await repo.get_plan(artifact.plan_id)).status == "stale"
    finally:
        await repo.close()
