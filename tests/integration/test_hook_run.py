from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.config import Config, ExtensionConfig, HookSpec, ModelConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, ModelRequest
from deity.core.hooks import HookContext, HookDecision, HookRegistry, RegisteredHook
from deity.core.runtime import PolicyVeto


class ToolModel:
    def __init__(self) -> None:
        self.turn = 0
        self.tool_status = ""

    async def stream(self, request: ModelRequest):
        self.turn += 1
        if self.turn == 1:
            yield ModelEvent("tool_call_started", call_id="w", name="write_file")
            yield ModelEvent(
                "tool_call_arguments_delta", call_id="w", text='{"path":"x.txt","content":"x"}'
            )
        else:
            self.tool_status = next(
                item["status"]
                for item in request.messages
                if item.get("call_id") == "w" and item["role"] == "tool"
            )
            yield ModelEvent("text_delta", text="done")
        yield ModelEvent("message_completed")


@pytest.mark.asyncio
async def test_before_tool_hook_veto_is_audited_and_prevents_write(tmp_path: Path) -> None:
    async def veto(_context: HookContext) -> HookDecision:
        return HookDecision(False, "policy")

    hooks = HookRegistry((RegisteredHook("guard", "BeforeTool", "policy", 0, veto),))
    model = ToolModel()
    db_path = tmp_path / "deity.db"
    service = RunService(
        db_path=db_path,
        workspace=tmp_path,
        model=model,
        approve=lambda _name, _args: True,
        hooks_enabled=True,
        hooks=hooks,
    )
    result = await service.run("write", model_name="fake")
    assert result.final_text == "done" and model.tool_status == "denied"
    assert not (tmp_path / "x.txt").exists()
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        events = await repo.events(result.run_id)
        assert any(e["type"] == "HookDecision" and e["payload"]["status"] == "veto" for e in events)
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_explicit_hook_config_loads_trusted_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEITY_TEST_HOOK_KEY", "fixture")
    (tmp_path / "x.txt").write_text("keep")

    class DeleteModel:
        def __init__(self) -> None:
            self.turn = 0
            self.status = ""

        async def stream(self, request: ModelRequest):
            self.turn += 1
            if self.turn == 1:
                yield ModelEvent("tool_call_started", call_id="d", name="delete_file")
                yield ModelEvent("tool_call_arguments_delta", call_id="d", text='{"path":"x.txt"}')
            else:
                self.status = next(
                    m["status"]
                    for m in request.messages
                    if m.get("call_id") == "d" and m["role"] == "tool"
                )
                yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    config = Config(
        model=ModelConfig("openai_compatible", "fake", "DEITY_TEST_HOOK_KEY"),
        extensions=ExtensionConfig(hooks_enabled=True),
        hook_specs=(
            HookSpec("guard", "BeforeTool", "policy", "deity.examples.v0_4_hook:deny_delete"),
        ),
    )
    service = RunService.from_config(
        config,
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        approve=lambda _name, _args: True,
    )
    model = DeleteModel()
    service.model = model
    await service.run("delete x.txt", model_name="fake")
    assert model.status == "denied" and (tmp_path / "x.txt").read_text() == "keep"


@pytest.mark.asyncio
async def test_before_model_veto_records_one_failed_terminal_event(tmp_path: Path) -> None:
    async def veto(_context: HookContext) -> HookDecision:
        return HookDecision(False, "stop")

    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=ToolModel(),
        hooks_enabled=True,
        hooks=HookRegistry((RegisteredHook("stop", "BeforeModel", "policy", 0, veto),)),
    )
    with pytest.raises(PolicyVeto):
        await service.run("task", model_name="fake")
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        async with repo.db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        events = await repo.events(row[0])
        terminal = [
            event
            for event in events
            if event["type"] == "RunStatusChanged" and event["payload"].get("status") == "failed"
        ]
        assert len(terminal) == 1
        assert any(event["type"] == "HookDecision" for event in events)
    finally:
        await repo.close()
