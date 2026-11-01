import asyncio
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.config import CodingConfig, PermissionConfig, SandboxConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, ModelRequest, RunStatus
from deity.core.contracts.execution import BackendCapabilities, BackendKind


def eval_record(db_path: Path) -> None:
    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT e.id, e.status, e.exit_code, c.status, m.status, "
            "(SELECT COUNT(*) FROM sandbox_artifacts a WHERE a.manifest_id=m.id) "
            "FROM sandbox_executions e "
            "LEFT JOIN sandbox_cleanups c ON c.execution_id=e.id "
            "LEFT JOIN artifact_manifests m ON m.execution_id=e.id"
        ).fetchall()
    for execution_id, status, exit_code, cleanup, manifest, count in rows:
        print(
            "DEITY_EVAL_RECORD:"
            + json.dumps(
                {
                    "execution_id": execution_id,
                    "execution_status": status,
                    "container_exit_code": exit_code,
                    "cleanup_status": cleanup or "UNKNOWN",
                    "artifact_status": manifest or "NOT_COLLECTED",
                    "artifact_count": count,
                }
            )
        )


class ShellModel:
    def __init__(self) -> None:
        self.turn = 0
        self.tool_status = ""

    async def stream(self, request: ModelRequest):
        self.turn += 1
        if self.turn == 1:
            yield ModelEvent("tool_call_started", call_id="shell", name="shell")
            yield ModelEvent(
                "tool_call_arguments_delta", call_id="shell", text='{"argv":["cat","hello.txt"]}'
            )
        else:
            self.tool_status = next(m["status"] for m in request.messages if m["role"] == "tool")
            yield ModelEvent("text_delta", text="done")
        yield ModelEvent("message_completed")


@pytest.mark.asyncio
async def test_disabled_sandbox_uses_local_without_execution_row(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    model = ShellModel()
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,
        approve=lambda _name, _args: True,
        permission=PermissionConfig(shell="ask"),
    )
    result = await service.run("read hello", model_name="fake")
    assert result.status == RunStatus.COMPLETED
    assert model.tool_status == "ok"
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        async with repo.db.execute("SELECT COUNT(*) FROM sandbox_executions") as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_required_sandbox_unavailable_never_uses_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(_self) -> BackendCapabilities:
        return BackendCapabilities(BackendKind.DOCKER, False, True, True, True, True, True, True)

    monkeypatch.setattr(
        "deity.adapters.execution.docker.DockerExecutionBackend.capabilities", unavailable
    )
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    model = ShellModel()
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,
        approve=lambda _name, _args: True,
        permission=PermissionConfig(shell="ask"),
        sandbox=SandboxConfig(
            enabled=True, backend="docker", required=True, image="sha256:" + "a" * 64
        ),
    )
    result = await service.run("read hello", model_name="fake")
    assert result.status == RunStatus.COMPLETED
    assert model.tool_status == "denied"
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        async with repo.db.execute("SELECT COUNT(*) FROM sandbox_executions") as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_run_service_routes_approved_shell_to_docker(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("Docker CLI unavailable")
        pytest.skip("Docker unavailable")
    probe = await asyncio.create_subprocess_exec(
        "docker",
        "image",
        "inspect",
        os.environ.get("DEITY_SANDBOX_TEST_IMAGE", "alpine:3.22"),
        "--format",
        "{{.Id}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await probe.communicate()
    if probe.returncode != 0:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("local fixture image unavailable")
        pytest.skip("local fixture image unavailable")
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    model = ShellModel()
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,
        approve=lambda _name, _args: True,
        permission=PermissionConfig(shell="ask"),
        sandbox=SandboxConfig(
            enabled=True, backend="docker", required=True, image=output.decode().strip()
        ),
    )
    result = await service.run("read hello", model_name="fake")
    assert result.status == RunStatus.COMPLETED
    assert model.tool_status == "ok"
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        events = [event["type"] for event in await repo.events(result.run_id)]
        assert "SandboxExecutionPrepared" in events
        assert "SandboxCleanupCompleted" in events
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_reviewed_artifact_applies_with_changeset(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("Docker CLI unavailable")
        pytest.skip("Docker unavailable")
    probe = await asyncio.create_subprocess_exec(
        "docker",
        "image",
        "inspect",
        os.environ.get("DEITY_SANDBOX_TEST_IMAGE", "alpine:3.22"),
        "--format",
        "{{.Id}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await probe.communicate()
    if probe.returncode != 0:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("local fixture image unavailable")
        pytest.skip("local fixture image unavailable")
    (tmp_path / "a.txt").write_text("before\n", encoding="utf-8")

    class EditModel:
        def __init__(self) -> None:
            self.turn = 0

        async def stream(self, _request: ModelRequest):
            self.turn += 1
            if self.turn == 1:
                yield ModelEvent("tool_call_started", call_id="edit", name="shell")
                yield ModelEvent(
                    "tool_call_arguments_delta",
                    call_id="edit",
                    text='{"argv":["sh","-c","printf after > a.txt"]}',
                )
            else:
                yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=EditModel(),
        approve=lambda _name, _args: True,
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
        sandbox=SandboxConfig(
            enabled=True,
            backend="docker",
            required=True,
            image=output.decode().strip(),
            artifact_policy="review",
        ),
    )
    result = await service.run("edit", model_name="fake")
    assert result.status == RunStatus.COMPLETED
    assert (tmp_path / "a.txt").read_text() == "after"
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        change_set = await repo.change_set_for_run(result.run_id)
        assert change_set.changes[0].path == "a.txt"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_review_cannot_overwrite_initial_dirty_file(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("Docker CLI unavailable")
        pytest.skip("Docker unavailable")
    probe = await asyncio.create_subprocess_exec(
        "docker",
        "image",
        "inspect",
        os.environ.get("DEITY_SANDBOX_TEST_IMAGE", "alpine:3.22"),
        "--format",
        "{{.Id}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await probe.communicate()
    if probe.returncode != 0:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("local fixture image unavailable")
        pytest.skip("local fixture image unavailable")
    (tmp_path / "a.txt").write_text("baseline", encoding="utf-8")
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "a.txt"],
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.test",
            "commit",
            "-qm",
            "基线",
        ],
    ):
        process = await asyncio.create_subprocess_exec(*args, cwd=tmp_path)
        assert await process.wait() == 0
    (tmp_path / "a.txt").write_text("user edit", encoding="utf-8")

    class EditModel:
        def __init__(self) -> None:
            self.turn = 0
            self.tool_status = ""

        async def stream(self, request: ModelRequest):
            self.turn += 1
            if self.turn == 1:
                yield ModelEvent("tool_call_started", call_id="edit", name="shell")
                yield ModelEvent(
                    "tool_call_arguments_delta",
                    call_id="edit",
                    text='{"argv":["sh","-c","printf agent > a.txt"]}',
                )
            else:
                self.tool_status = next(
                    m["status"] for m in request.messages if m["role"] == "tool"
                )
                yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    model = EditModel()
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,
        approve=lambda _name, _args: True,
        sandbox=SandboxConfig(
            enabled=True,
            backend="docker",
            required=True,
            image=output.decode().strip(),
            artifact_policy="review",
        ),
    )
    await service.run("edit", model_name="fake")
    assert model.tool_status == "error"
    assert (tmp_path / "a.txt").read_text() == "user edit"
    eval_record(tmp_path / "deity.db")


@pytest.mark.asyncio
async def test_partial_artifact_apply_fails_run_and_preserves_dirty_file(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("Docker CLI unavailable")
        pytest.skip("Docker unavailable")
    probe = await asyncio.create_subprocess_exec(
        "docker",
        "image",
        "inspect",
        os.environ.get("DEITY_SANDBOX_TEST_IMAGE", "alpine:3.22"),
        "--format",
        "{{.Id}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await probe.communicate()
    if probe.returncode != 0:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("local fixture image unavailable")
        pytest.skip("local fixture image unavailable")
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("baseline", encoding="utf-8")
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "a.txt", "b.txt"],
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.test",
            "commit",
            "-qm",
            "基线",
        ],
    ):
        process = await asyncio.create_subprocess_exec(*args, cwd=tmp_path)
        assert await process.wait() == 0
    (tmp_path / "b.txt").write_text("user edit", encoding="utf-8")

    class EditModel:
        def __init__(self) -> None:
            self.turn = 0

        async def stream(self, _request: ModelRequest):
            self.turn += 1
            if self.turn == 1:
                yield ModelEvent("tool_call_started", call_id="edit", name="shell")
                yield ModelEvent(
                    "tool_call_arguments_delta",
                    call_id="edit",
                    text='{"argv":["sh","-c","printf agent > a.txt; printf agent > b.txt"]}',
                )
            else:
                yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=EditModel(),
        approve=lambda _name, _args: True,
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
        sandbox=SandboxConfig(
            enabled=True,
            backend="docker",
            required=True,
            image=output.decode().strip(),
            artifact_policy="review",
        ),
    )
    with pytest.raises(RuntimeError, match="partial"):
        await service.run("edit both", model_name="fake")
    assert (tmp_path / "a.txt").read_text() == "agent"
    assert (tmp_path / "b.txt").read_text() == "user edit"
    eval_record(tmp_path / "deity.db")


@pytest.mark.asyncio
async def test_git_guard_requires_read_before_sandbox_apply(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("Docker CLI unavailable")
        pytest.skip("Docker unavailable")
    probe = await asyncio.create_subprocess_exec(
        "docker",
        "image",
        "inspect",
        os.environ.get("DEITY_SANDBOX_TEST_IMAGE", "alpine:3.22"),
        "--format",
        "{{.Id}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await probe.communicate()
    if probe.returncode != 0:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("local fixture image unavailable")
        pytest.skip("local fixture image unavailable")
    (tmp_path / "a.txt").write_text("before", encoding="utf-8")

    class EditModel:
        def __init__(self) -> None:
            self.turn = 0
            self.tool_status = ""

        async def stream(self, request: ModelRequest):
            self.turn += 1
            if self.turn == 1:
                yield ModelEvent("tool_call_started", call_id="edit", name="shell")
                yield ModelEvent(
                    "tool_call_arguments_delta",
                    call_id="edit",
                    text='{"argv":["sh","-c","printf after > a.txt"]}',
                )
            else:
                self.tool_status = next(
                    message["status"] for message in request.messages if message["role"] == "tool"
                )
                yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    model = EditModel()
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,
        approve=lambda _name, _args: True,
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
        coding=CodingConfig(git_guard_enabled=True),
        sandbox=SandboxConfig(
            enabled=True,
            backend="docker",
            required=True,
            image=output.decode().strip(),
            artifact_policy="review",
        ),
    )
    await service.run("edit without reading", model_name="fake")
    assert model.tool_status == "error"
    assert (tmp_path / "a.txt").read_text() == "before"
    eval_record(tmp_path / "deity.db")
