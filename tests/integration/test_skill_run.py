from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, ModelRequest


class SkillModel:
    def __init__(self) -> None:
        self.turn = 0
        self.loaded = ""

    async def stream(self, request: ModelRequest):
        self.turn += 1
        if self.turn == 1:
            assert any("skill3" in item.get("content", "") for item in request.messages)
            assert not any("body 3" in item.get("content", "") for item in request.messages)
            yield ModelEvent("tool_call_started", call_id="load-1", name="load_skill")
            yield ModelEvent(
                "tool_call_arguments_delta", call_id="load-1", text='{"name":"skill3"}'
            )
        else:
            self.loaded = next(
                item["content"]
                for item in request.messages
                if item.get("call_id") == "load-1" and item["role"] == "tool"
            )
            yield ModelEvent("text_delta", text="done")
        yield ModelEvent("message_completed")


@pytest.mark.asyncio
async def test_model_explicitly_loads_one_of_ten_skills(tmp_path: Path) -> None:
    root = tmp_path / ".deity/skills"
    for index in range(10):
        directory = root / f"skill{index}"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: skill{index}\ndescription: skill {index}\n---\nbody {index}"
        )
    model = SkillModel()
    db_path = tmp_path / "deity.db"
    service = RunService(db_path=db_path, workspace=tmp_path, model=model, skills_enabled=True)
    result = await service.run("use skill3", model_name="fake")
    assert result.final_text == "done" and "body 3" in model.loaded
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        activation = next(
            event for event in await repo.events(result.run_id) if event["type"] == "SkillActivated"
        )
        assert activation["payload"]["skill_id"] == "skill3"
        assert activation["payload"]["source"] == "project"
        assert len(activation["payload"]["sha256"]) == 64
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_resource_event_records_explicit_path(tmp_path: Path) -> None:
    root = tmp_path / ".deity/skills/demo"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo\n---\nRead resources/answer.txt"
    )
    (root / "resources").mkdir()
    (root / "resources/answer.txt").write_text("answer")

    class ResourceModel:
        turn = 0

        async def stream(self, _request: ModelRequest):
            self.turn += 1
            if self.turn == 1:
                name, args = "load_skill", '{"name":"demo"}'
            elif self.turn == 2:
                name, args = "load_skill_resource", '{"name":"demo","path":"resources/answer.txt"}'
            else:
                yield ModelEvent("text_delta", text="done")
                yield ModelEvent("message_completed")
                return
            yield ModelEvent("tool_call_started", call_id=f"c{self.turn}", name=name)
            yield ModelEvent("tool_call_arguments_delta", call_id=f"c{self.turn}", text=args)
            yield ModelEvent("message_completed")

    db_path = tmp_path / "deity.db"
    service = RunService(
        db_path=db_path, workspace=tmp_path, model=ResourceModel(), skills_enabled=True
    )
    result = await service.run("read demo", model_name="fake")
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        resource = next(
            event
            for event in await repo.events(result.run_id)
            if event["type"] == "SkillResourceLoaded"
        )
        assert resource["payload"]["path"] == "resources/answer.txt"
        assert resource["payload"]["source"] == "project"
    finally:
        await repo.close()
