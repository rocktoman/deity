import asyncio
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools import LocalToolDispatcher
from deity.application.run_service import RunService
from deity.core.contracts import AgentRequest, ModelEvent, ModelRequest, RunStatus, ToolCall
from deity.core.runtime import ModelFailure, ProtocolError

FIXTURE = Path(__file__).parents[1] / "fixtures/python-mini-repo"


class ScriptedModel:
    def __init__(self, turns: list[list[ModelEvent]]) -> None:
        self.turns = iter(turns)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        for event in next(self.turns):
            yield event


def tool_turn(call_id: str, name: str, arguments: str) -> list[ModelEvent]:
    return [
        ModelEvent("tool_call_started", call_id=call_id, name=name),
        ModelEvent("tool_call_arguments_delta", call_id=call_id, text=arguments),
        ModelEvent("message_completed"),
    ]


async def fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Deity Eval",
            "-c",
            "user.email=eval@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
    ):
        process = await asyncio.create_subprocess_exec(*argv, cwd=repo)
        assert await process.wait() == 0
    return repo


async def execute(tmp_path: Path, repo: Path, turns: list[list[ModelEvent]]):
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=repo,
        model=ScriptedModel(turns),
        approve=lambda name, args: True,
    )
    return await service.run("eval", model_name="scripted")


@pytest.mark.asyncio
async def test_e01_read_only_analysis(tmp_path: Path) -> None:
    repo = await fixture_repo(tmp_path)
    result = await execute(
        tmp_path,
        repo,
        [
            tool_turn("r", "read_file", '{"path":"calc.py"}'),
            [
                ModelEvent("text_delta", text="calc.py 中的 add 使用减法"),
                ModelEvent("message_completed"),
            ],
        ],
    )
    assert result.status == RunStatus.COMPLETED
    assert "calc.py" in (result.final_text or "")
    process = await asyncio.create_subprocess_exec(
        "git", "status", "--porcelain", cwd=repo, stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    assert stdout == b""


@pytest.mark.asyncio
async def test_e02_add_function(tmp_path: Path) -> None:
    repo = await fixture_repo(tmp_path)
    result = await execute(
        tmp_path,
        repo,
        [
            tool_turn(
                "w",
                "write_file",
                '{"path":"multiply.py","content":"def multiply(a, b):\\n    return a * b\\n"}',
            ),
            [ModelEvent("text_delta", text="已添加 multiply.py"), ModelEvent("message_completed")],
        ],
    )
    assert result.status == RunStatus.COMPLETED
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "from multiply import multiply; assert multiply(2, 3) == 6", cwd=repo
    )
    assert await process.wait() == 0


@pytest.mark.asyncio
async def test_e03_fix_reproducible_bug(tmp_path: Path) -> None:
    repo = await fixture_repo(tmp_path)
    before = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "test_calc.py",
        cwd=repo,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await before.communicate()
    assert before.returncode != 0
    result = await execute(
        tmp_path,
        repo,
        [
            tool_turn("p", "apply_patch", '{"path":"calc.py","old":"a - b","new":"a + b"}'),
            tool_turn(
                "s",
                "shell",
                '{"argv":["' + sys.executable + '","-m","pytest","-q","test_calc.py"]}',
            ),
            [ModelEvent("text_delta", text="修复完成，测试通过"), ModelEvent("message_completed")],
        ],
    )
    assert result.status == RunStatus.COMPLETED
    after = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "test_calc.py",
        cwd=repo,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await after.communicate()
    assert after.returncode == 0


@pytest.mark.asyncio
async def test_e04_incomplete_parameters_never_execute(tmp_path: Path) -> None:
    repo = await fixture_repo(tmp_path)
    with pytest.raises(ProtocolError):
        await execute(
            tmp_path,
            repo,
            [
                [
                    ModelEvent("tool_call_started", call_id="w", name="write_file"),
                    ModelEvent("tool_call_arguments_delta", call_id="w", text='{"path":"bad.py",'),
                ]
            ],
        )
    assert not (repo / "bad.py").exists()


@pytest.mark.asyncio
async def test_e05_boundary_and_approval(tmp_path: Path) -> None:
    repo = await fixture_repo(tmp_path)
    tools = LocalToolDispatcher()
    context = AgentRequest("s", "t", str(repo), "x", "fake")
    refused = await tools.dispatch(
        ToolCall("w", "write_file", {"path": "new.py", "content": "x"}), context
    )
    escaped = await tools.dispatch(
        ToolCall("e", "write_file", {"path": "../outside.py", "content": "x"}), context
    )
    assert refused.status == escaped.status == "denied"
    assert not (repo / "new.py").exists()
    assert not (tmp_path / "outside.py").exists()
    assert [item[2] for item in tools.audit] == ["denied", "denied"]


@pytest.mark.asyncio
async def test_e06_shell_timeout(tmp_path: Path) -> None:
    repo = await fixture_repo(tmp_path)
    tools = LocalToolDispatcher(approve=lambda name, args: True, timeout_seconds=0.1)
    result = await tools.dispatch(
        ToolCall("s", "shell", {"argv": [sys.executable, "-c", "import time;time.sleep(5)"]}),
        AgentRequest("s", "t", str(repo), "x", "fake"),
    )
    assert result.status == "timeout"


@pytest.mark.asyncio
async def test_e07_retry_without_duplicate_side_effect(tmp_path: Path) -> None:
    repo = await fixture_repo(tmp_path)

    class FlakyModel:
        def __init__(self) -> None:
            self.attempts = 0

        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            self.attempts += 1
            if self.attempts == 1:
                raise ModelFailure("model_rate_limited", retryable=True)
            yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    model = FlakyModel()
    service = RunService(db_path=tmp_path / "deity.db", workspace=repo, model=model)
    result = await service.run("x", model_name="scripted")
    assert result.status == RunStatus.COMPLETED
    assert model.attempts == 2
    assert not (repo / "new.py").exists()


@pytest.mark.asyncio
async def test_e08_restart_marks_interrupted(tmp_path: Path) -> None:
    repo = await fixture_repo(tmp_path)
    path = tmp_path / "deity.db"
    store = await SQLiteSessionRepository.open(path)
    run_id = await store.create_run(AgentRequest("s", "t", str(repo), "x", "fake"))
    await store.set_status(run_id, RunStatus.RUNNING)
    assert await store.claim_tool(run_id, "w", "write_file", {"path": "new.py"})
    await store.close()
    store = await SQLiteSessionRepository.open(path)
    assert await store.status(run_id) == RunStatus.INTERRUPTED
    assert not (repo / "new.py").exists()
    await store.close()
