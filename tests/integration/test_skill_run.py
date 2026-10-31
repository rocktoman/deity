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
        assert "SkillActivated" in [event["type"] for event in await repo.events(result.run_id)]
    finally:
        await repo.close()
