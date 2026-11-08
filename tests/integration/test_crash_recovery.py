"""V1.0 S4：崩溃注入——kill 后恢复，终态不回 running，未知副作用不重放。"""

import asyncio
import os
import signal
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.run_service import RunService
from deity.core.contracts import AgentRequest, RunStatus

ROOT = Path(__file__).parents[2]

CHILD_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import os
    import sys
    import time
    from pathlib import Path

    sys.path.insert(0, "src")
    from deity.adapters.storage import SQLiteSessionRepository
    from deity.core.contracts import AgentRequest, RunStatus


    async def main(db_path: str) -> None:
        repo = await SQLiteSessionRepository.open(Path(db_path))
        run_id = await repo.create_run(
            AgentRequest("s", "t", ".", "crash me", "fake"), run_id="crash-run-0001"
        )
        await repo.save_checkpoint(run_id, [{"role": "user", "content": "crash me"}], {"turns": 0})
        lease = await repo.acquire_lease(run_id, f"{os.getpid()}:crash-test")
        await repo.set_status(run_id, RunStatus.RUNNING, epoch=lease.epoch)
        assert await repo.claim_tool(
            run_id, "crash-call-1", "write_file", {"path": "x.txt"}, epoch=lease.epoch
        )
        print("RUNNING", flush=True)
        time.sleep(120)


    asyncio.run(main(sys.argv[1]))
    """
)


async def _wait_for_status(db_path: Path, status: str, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            with sqlite3.connect(db_path) as db:
                row = db.execute("SELECT status FROM runs WHERE id='crash-run-0001'").fetchone()
            if row is not None and row[0] == status:
                return
        except sqlite3.OperationalError:
            pass
        await asyncio.sleep(0.1)
    raise AssertionError(f"run 未进入 {status} 状态")


@pytest.mark.asyncio
async def test_kill_then_reopen_marks_interrupted_and_unknown(tmp_path: Path) -> None:
    """真实 SIGKILL 注入：子进程被 kill 后重开库，终态为 interrupted，待决工具为 unknown。"""
    script = tmp_path / "crash_child.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")
    db_path = tmp_path / "deity.db"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        str(db_path),
        cwd=ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await _wait_for_status(db_path, "running")
        child.send_signal(signal.SIGKILL)
        assert await asyncio.wait_for(child.wait(), timeout=30) == -signal.SIGKILL
    finally:
        if child.returncode is None:
            child.kill()
            await asyncio.wait_for(child.wait(), timeout=10)
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        assert await repo.status("crash-run-0001") == RunStatus.INTERRUPTED
        unresolved = await repo.unresolved_tools("crash-run-0001")
        assert [item["call_id"] for item in unresolved] == ["crash-call-1"]
        assert unresolved[0]["status"] == "unknown"
        events = await repo.events("crash-run-0001")
        kinds = {event["type"] for event in events}
        assert "ToolOutcomeUnknown" in kinds
        # 未知副作用不重放：带未决副作用时拒绝创建续跑，须先人工复核。
        checkpoint = await repo.latest_checkpoint("crash-run-0001")
        assert checkpoint is not None
        with pytest.raises(ValueError, match="unresolved side effect requires review"):
            await repo.create_resumed_run("crash-run-0001", checkpoint["id"])
    finally:
        await repo.close()
    service = RunService(db_path=db_path, workspace=tmp_path)
    report = await service.inspect("crash-run-0001")
    assert report["decision"] == "manual_review"
    assert report["reason"] == "unresolved_tool"
    assert report["unresolved_tools"] == [
        {"call_id": "crash-call-1", "tool_name": "write_file", "status": "unknown"}
    ]


@pytest.mark.asyncio
async def test_clean_crash_resumes_from_checkpoint_without_replay(tmp_path: Path) -> None:
    """无未决工具的崩溃：重开后终态为 interrupted，可从崩溃前 checkpoint 续跑，新 Run 不重放旧工具。"""
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    # checkpoint 必须在崩溃前、位于安全边界上保存：有未决/未知工具时不允许建 checkpoint。
    checkpoint = await repo.save_checkpoint(
        run_id, [{"role": "user", "content": "goal"}], {"turns": 0}
    )
    lease = await repo.acquire_lease(run_id, "99999999:dead-owner")
    await repo.set_status(run_id, RunStatus.RUNNING, epoch=lease.epoch)
    await repo.close()
    # 模拟崩溃后重开：owner 已死，open() 应标记 interrupted，无未决工具。
    reopened = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        assert await reopened.status(run_id) == RunStatus.INTERRUPTED
        assert await reopened.unresolved_tools(run_id) == []
        new_run = await reopened.create_resumed_run(run_id, checkpoint)
        assert new_run != run_id
        assert await reopened.status(run_id) == RunStatus.INTERRUPTED
        assert await reopened.status(new_run) == RunStatus.CREATED
        # 不重放：新 Run 里没有任何 tool_executions 记录。
        with sqlite3.connect(tmp_path / "deity.db") as db:
            rows = db.execute(
                "SELECT COUNT(*) FROM tool_executions WHERE run_id=?", (new_run,)
            ).fetchone()[0]
        assert rows == 0
    finally:
        await reopened.close()
