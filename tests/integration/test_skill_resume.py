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
