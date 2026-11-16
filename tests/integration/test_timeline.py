"""ADR-021：证据时间线聚合、CLI timeline 与顶层 replay 入口。"""

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.timeline import run_timeline
from deity.core.contracts import AgentRequest, RunStatus
from deity.interfaces.cli.main import app


async def _seed(db: Path, workspace: Path) -> str:
    repo = await SQLiteSessionRepository.open(db)
    try:
        run_id = await repo.create_run(
            AgentRequest("s1", "t1", str(workspace), "timeline task", "fake")
        )
        await repo.record_event(
            run_id,
            "turn_diff",
            {"files": [{"path": "a.py", "status": "modified"}], "files_changed": 1},
        )
        await repo.record_evidence(run_id, "test", "pytest passed", "c1")
        await repo.set_status(run_id, RunStatus.RUNNING)
        await repo.set_status(run_id, RunStatus.COMPLETED)
        return run_id
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_run_timeline_orders_events_evidence(tmp_path: Path) -> None:
    run_id = await _seed(tmp_path / "deity.db", tmp_path)
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        entries = await run_timeline(repo, run_id)
    finally:
        await repo.close()
    kinds = [entry.kind for entry in entries]
    assert kinds[0] == "RunCreated"
    assert "turn_diff" in kinds
    diff_entry = next(entry for entry in entries if entry.kind == "turn_diff")
    assert diff_entry.title == "文件变更"
    assert "a.py(modified)" in diff_entry.detail
    evidence_entry = next(entry for entry in entries if entry.kind == "evidence")
    assert "pytest passed" in evidence_entry.detail
    assert [entry.seq for entry in entries] == list(range(1, len(entries) + 1))


@pytest.mark.asyncio
async def test_timeline_unknown_run_raises(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        with pytest.raises(KeyError):
            await run_timeline(repo, "missing")
    finally:
        await repo.close()


def test_timeline_and_replay_cli(tmp_path: Path) -> None:
    db = tmp_path / "deity.db"
    run_id = asyncio.run(_seed(db, tmp_path))
    runner = CliRunner()
    timeline = runner.invoke(app, ["timeline", run_id, "--db", str(db)])
    assert timeline.exit_code == 0
    assert "文件变更" in timeline.stdout
    replay = runner.invoke(app, ["replay", run_id, "--db", str(db)])
    assert replay.exit_code == 0
    assert "completed" in replay.stdout
    missing = runner.invoke(app, ["timeline", "missing", "--db", str(db)])
    assert missing.exit_code == 2


def test_replay_report_file_cli(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"case_id": "case-1", "passed": True, "verification": "PASS"}),
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["replay", str(report)])
    assert result.exit_code == 0
    assert "case-1: completed" in result.stdout
