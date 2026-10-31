import sqlite3
from pathlib import Path

import pytest

from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, ModelRequest


class FinalModel:
    async def stream(self, _request: ModelRequest):
        yield ModelEvent("text_delta", text="done")
        yield ModelEvent("message_completed")


@pytest.mark.asyncio
async def test_skill_content_change_blocks_silent_resume(tmp_path: Path) -> None:
    directory = tmp_path / ".deity/skills/demo"
    directory.mkdir(parents=True)
    skill = directory / "SKILL.md"
    skill.write_text("---\nname: demo\ndescription: Demo\n---\nfirst")
    db_path = tmp_path / "deity.db"
    first = RunService(db_path=db_path, workspace=tmp_path, model=FinalModel(), skills_enabled=True)
    result = await first.run("use demo", model_name="fake")
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE runs SET status='interrupted' WHERE id=?", (result.run_id,))
    skill.write_text("---\nname: demo\ndescription: Demo\n---\nchanged")
    second = RunService(
        db_path=db_path, workspace=tmp_path, model=FinalModel(), skills_enabled=True
    )
    inspection = await second.inspect(result.run_id)
    assert inspection["decision"] == "abort" and inspection["reason"] == "config_changed"


@pytest.mark.asyncio
async def test_safe_resume_can_load_resource_of_activated_skill(tmp_path: Path) -> None:
    directory = tmp_path / ".deity/skills/demo"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: demo\ndescription: Demo\n---\nRead answer.txt")
    (directory / "answer.txt").write_text("42")

    class ActivationModel:
        turns = 0

        async def stream(self, _request: ModelRequest):
            self.turns += 1
            if self.turns == 1:
                yield ModelEvent("tool_call_started", call_id="activate", name="load_skill")
                yield ModelEvent(
                    "tool_call_arguments_delta", call_id="activate", text='{"name":"demo"}'
                )
            else:
                yield ModelEvent("text_delta", text="initial")
            yield ModelEvent("message_completed")

    db_path = tmp_path / "deity.db"
    first = RunService(
        db_path=db_path, workspace=tmp_path, model=ActivationModel(), skills_enabled=True
    )
    initial = await first.run("activate demo", model_name="fake")
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE runs SET status='interrupted' WHERE id=?", (initial.run_id,))

    class ResourceModel:
        turns = 0
        resource_status = ""

        async def stream(self, request: ModelRequest):
            self.turns += 1
            if self.turns == 1:
                yield ModelEvent(
                    "tool_call_started", call_id="resource", name="load_skill_resource"
                )
                yield ModelEvent(
                    "tool_call_arguments_delta",
                    call_id="resource",
                    text='{"name":"demo","path":"answer.txt"}',
                )
            else:
                self.resource_status = next(
                    item["status"]
                    for item in request.messages
                    if item.get("call_id") == "resource" and item["role"] == "tool"
                )
                yield ModelEvent("text_delta", text="resumed")
            yield ModelEvent("message_completed")

    model = ResourceModel()
    second = RunService(db_path=db_path, workspace=tmp_path, model=model, skills_enabled=True)
    assert (await second.inspect(initial.run_id))["decision"] == "resume"
    resumed = await second.resume(initial.run_id)
    assert resumed.final_text == "resumed" and model.resource_status == "ok"


@pytest.mark.asyncio
async def test_loaded_resource_change_blocks_resume(tmp_path: Path) -> None:
    directory = tmp_path / ".deity/skills/demo"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: demo\ndescription: Demo\n---\nRead answer.txt")
    resource = directory / "answer.txt"
    resource.write_text("first")

    class ResourceModel:
        turns = 0

        async def stream(self, _request: ModelRequest):
            self.turns += 1
            if self.turns == 1:
                name, args = "load_skill", '{"name":"demo"}'
            elif self.turns == 2:
                name, args = "load_skill_resource", '{"name":"demo","path":"answer.txt"}'
            else:
                yield ModelEvent("text_delta", text="done")
                yield ModelEvent("message_completed")
                return
            yield ModelEvent("tool_call_started", call_id=f"c{self.turns}", name=name)
            yield ModelEvent("tool_call_arguments_delta", call_id=f"c{self.turns}", text=args)
            yield ModelEvent("message_completed")

    db_path = tmp_path / "deity.db"
    first = RunService(
        db_path=db_path, workspace=tmp_path, model=ResourceModel(), skills_enabled=True
    )
    result = await first.run("use demo", model_name="fake")
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE runs SET status='interrupted' WHERE id=?", (result.run_id,))
    resource.write_text("changed")
    second = RunService(
        db_path=db_path, workspace=tmp_path, model=FinalModel(), skills_enabled=True
    )
    inspection = await second.inspect(result.run_id)
    assert inspection["decision"] == "abort" and inspection["reason"] == "skill_resource_changed"
