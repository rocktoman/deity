"""ADR-020 A7：run 的 JSONL 导出——meta 首行、逐条事件、summary 末行。"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.run_export import export_run_jsonl
from deity.core.contracts import AgentRequest
from deity.interfaces.cli.main import app


async def _seed_run(db: Path, workspace: Path) -> str:
    repo = await SQLiteSessionRepository.open(db)
    try:
        run_id = await repo.create_run(
            AgentRequest("s", "t", str(workspace), "导出目标任务", "fake")
        )
        await repo.record_event(run_id, "turn_diff", {"files_changed": 1, "files": []})
        await repo.record_evidence(run_id, "test", "pytest passed", None)
        return run_id
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_export_structure(tmp_path: Path) -> None:
    db = tmp_path / "deity.db"
    run_id = await _seed_run(db, tmp_path)
    repo = await SQLiteSessionRepository.open(db)
    try:
        text = await export_run_jsonl(repo, run_id)
    finally:
        await repo.close()
    lines = [json.loads(line) for line in text.splitlines()]
    assert lines[0]["type"] == "session_meta"
    assert lines[0]["run_id"] == run_id
    assert lines[0]["prompt"] == "导出目标任务"
    events = [line for line in lines if line["type"] == "event"]
    assert events and all("seq" in line and "event_type" in line for line in events)
    assert any(line["event_type"] == "turn_diff" for line in events)
    assert lines[-1] == {
        "type": "summary",
        "run_id": run_id,
        "events": len(events),
        "evidence": 1,
        "delegations": 0,
    }
    with pytest.raises(KeyError):
        repo2 = await SQLiteSessionRepository.open(db)
        try:
            await export_run_jsonl(repo2, "missing")
        finally:
            await repo2.close()


def test_export_cli_stdout_and_file(tmp_path: Path) -> None:
    import asyncio

    db = tmp_path / "deity.db"
    run_id = asyncio.run(_seed_run(db, tmp_path))
    result = CliRunner().invoke(app, ["export", run_id, "--db", str(db)])
    assert result.exit_code == 0, result.output
    first = json.loads(result.output.splitlines()[0])
    assert first["type"] == "session_meta" and first["run_id"] == run_id
    out = tmp_path / "run.jsonl"
    result = CliRunner().invoke(app, ["export", run_id, "--db", str(db), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text(encoding="utf-8").splitlines()[0])["run_id"] == run_id
    missing = CliRunner().invoke(app, ["export", "missing", "--db", str(db)])
    assert missing.exit_code == 2
