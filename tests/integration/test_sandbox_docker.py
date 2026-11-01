import asyncio
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from deity.adapters.execution.docker import DockerExecutionBackend
from deity.core.contracts.execution import (
    ArtifactManifest,
    BackendKind,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    ResourceBudget,
    SandboxCleanupReport,
    SandboxSpec,
)


def eval_record(
    result: ExecutionResult | None,
    cleanup: SandboxCleanupReport,
    manifest: ArtifactManifest | None = None,
) -> None:
    print(
        "DEITY_EVAL_RECORD:"
        + json.dumps(
            {
                "execution_id": cleanup.execution_id,
                "execution_status": result.status.value if result else "cancelled",
                "container_exit_code": result.exit_code if result else None,
                "cleanup_status": cleanup.status,
                "artifact_status": manifest.status if manifest else "NOT_COLLECTED",
                "artifact_count": len(manifest.entries) if manifest else None,
            }
        )
    )


async def fixture_image() -> str:
    if shutil.which("docker") is None:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("Docker CLI unavailable")
        pytest.skip("Docker CLI unavailable")
    image = os.environ.get("DEITY_SANDBOX_TEST_IMAGE", "alpine:3.22")
    probe = await asyncio.create_subprocess_exec(
        "docker",
        "image",
        "inspect",
        image,
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
    return output.decode().strip()


@pytest.mark.asyncio
async def test_local_docker_executes_with_copy_and_cleanup(tmp_path: Path) -> None:
    image = await fixture_image()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello", encoding="utf-8")
    backend = DockerExecutionBackend(workspace, tmp_path / "stages")
    execution_id = str(uuid4())
    request = ExecutionRequest(
        execution_id,
        "fixture-run",
        "fixture-call",
        ("cat", "hello.txt"),
        ".",
        SandboxSpec(
            BackendKind.DOCKER,
            image,
            ResourceBudget(15, 1.0, 64 * 1024 * 1024, 32, 1000, 1024 * 1024),
        ),
    )
    handle = await backend.prepare(request)
    try:
        result = await backend.execute(handle, request)
        manifest = await backend.collect(handle, result)
        assert result.status == ExecutionStatus.SUCCEEDED
        assert result.output.strip() == "hello"
        assert not manifest.entries
    finally:
        cleanup = await backend.cleanup(handle)
    assert cleanup.status == "cleaned"
    assert not (tmp_path / "stages" / execution_id).exists()


@pytest.mark.asyncio
async def test_container_nonzero_exit_is_not_success(tmp_path: Path) -> None:
    image = await fixture_image()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = DockerExecutionBackend(workspace, tmp_path / "stages")
    request = ExecutionRequest(
        str(uuid4()),
        "fixture-run",
        "fixture-call",
        ("false",),
        ".",
        SandboxSpec(
            BackendKind.DOCKER,
            image,
            ResourceBudget(15, 1.0, 64 * 1024 * 1024, 32, 1000, 1024 * 1024),
        ),
    )
    handle = await backend.prepare(request)
    try:
        result = await backend.execute(handle, request)
        assert result.status == ExecutionStatus.FAILED
        assert result.exit_code != 0
    finally:
        await backend.cleanup(handle)


@pytest.mark.asyncio
async def test_container_cannot_see_host_secret_or_docker_socket(tmp_path: Path) -> None:
    image = await fixture_image()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("DEITY_PRIVATE_TEST=fixture-secret", encoding="utf-8")
    backend = DockerExecutionBackend(workspace, tmp_path / "stages")
    request = ExecutionRequest(
        str(uuid4()),
        "fixture-run",
        "fixture-call",
        ("env",),
        ".",
        SandboxSpec(
            BackendKind.DOCKER,
            image,
            ResourceBudget(15, 1.0, 64 * 1024 * 1024, 32, 1000, 1024 * 1024),
        ),
    )
    os.environ["DEITY_PRIVATE_TEST"] = "fixture-secret"
    try:
        handle = await backend.prepare(request)
        try:
            result = await backend.execute(handle, request)
            assert result.status == ExecutionStatus.SUCCEEDED
            assert "fixture-secret" not in result.output
            assert not (Path(handle.staging_root or "") / ".env").exists()
            code, network, _ = await backend._docker(
                "docker",
                "inspect",
                "--format",
                "{{.HostConfig.NetworkMode}}",
                handle.opaque_id,
                timeout=5,
            )
            assert code == 0 and network.strip() == "none"
            code, mounts, _ = await backend._docker(
                "docker",
                "inspect",
                "--format",
                "{{json .Mounts}}",
                handle.opaque_id,
                timeout=5,
            )
            assert code == 0 and "docker.sock" not in mounts
        finally:
            cleanup = await backend.cleanup(handle)
        eval_record(result, cleanup)
        allowlisted = replace(
            request,
            execution_id=str(uuid4()),
            spec=replace(request.spec, env_names=("DEITY_PRIVATE_TEST",)),
        )
        handle = await backend.prepare(allowlisted)
        try:
            result = await backend.execute(handle, allowlisted)
            assert "DEITY_PRIVATE_TEST=[redacted]" in result.output
            assert "fixture-secret" not in result.output
        finally:
            cleanup = await backend.cleanup(handle)
        eval_record(result, cleanup)
    finally:
        os.environ.pop("DEITY_PRIVATE_TEST", None)


@pytest.mark.asyncio
async def test_container_security_and_timeout(tmp_path: Path) -> None:
    image = await fixture_image()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = DockerExecutionBackend(workspace, tmp_path / "stages")
    request = ExecutionRequest(
        str(uuid4()),
        "fixture-run",
        "fixture-call",
        ("sh", "-c", "id -u; touch /forbidden"),
        ".",
        SandboxSpec(
            BackendKind.DOCKER,
            image,
            ResourceBudget(10, 0.5, 64 * 1024 * 1024, 16, 1000, 1024 * 1024),
        ),
    )
    handle = await backend.prepare(request)
    try:
        code, guards, _ = await backend._docker(
            "docker",
            "inspect",
            "--format",
            "{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Privileged}} "
            "{{.HostConfig.PidsLimit}} {{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}",
            handle.opaque_id,
            timeout=5,
        )
        assert code == 0
        assert guards.split() == ["true", "false", "16", str(64 * 1024 * 1024), "500000000"]
        result = await backend.execute(handle, request)
        assert result.status == ExecutionStatus.FAILED
        assert result.output.splitlines()[0] != "0"
        assert "Read-only file system" in result.output
    finally:
        cleanup = await backend.cleanup(handle)
    assert cleanup.status == "cleaned"
    eval_record(result, cleanup)

    timed = ExecutionRequest(
        str(uuid4()),
        "fixture-run",
        "timeout-call",
        ("sleep", "5"),
        ".",
        SandboxSpec(
            BackendKind.DOCKER,
            image,
            ResourceBudget(0.2, 0.5, 64 * 1024 * 1024, 16, 1000, 1024 * 1024),
        ),
    )
    handle = await backend.prepare(timed)
    try:
        result = await backend.execute(handle, timed)
        assert result.status == ExecutionStatus.TIMED_OUT
    finally:
        cleanup = await backend.cleanup(handle)
    assert cleanup.status == "cleaned"
    eval_record(result, cleanup)


@pytest.mark.asyncio
async def test_output_limit_is_reported(tmp_path: Path) -> None:
    image = await fixture_image()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = DockerExecutionBackend(workspace, tmp_path / "stages")
    request = ExecutionRequest(
        str(uuid4()),
        "fixture-run",
        "output-call",
        ("printf", "12345678901234567890"),
        ".",
        SandboxSpec(
            BackendKind.DOCKER,
            image,
            ResourceBudget(10, 0.5, 64 * 1024 * 1024, 16, 8, 1024 * 1024),
        ),
    )
    handle = await backend.prepare(request)
    try:
        result = await backend.execute(handle, request)
        assert result.status == ExecutionStatus.SUCCEEDED
        assert result.truncated
        assert len(result.output) <= 8
    finally:
        cleanup = await backend.cleanup(handle)
    assert cleanup.status == "cleaned"
    eval_record(result, cleanup)


@pytest.mark.asyncio
async def test_manifest_is_host_scanned_before_any_host_write(tmp_path: Path) -> None:
    image = await fixture_image()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "old.txt").write_text("before", encoding="utf-8")
    backend = DockerExecutionBackend(workspace, tmp_path / "stages")
    request = ExecutionRequest(
        str(uuid4()),
        "fixture-run",
        "manifest-call",
        ("sh", "-c", "printf after > old.txt; printf new > added.txt"),
        ".",
        SandboxSpec(
            BackendKind.DOCKER,
            image,
            ResourceBudget(10, 0.5, 64 * 1024 * 1024, 16, 1000, 1024 * 1024),
        ),
    )
    handle = await backend.prepare(request)
    try:
        result = await backend.execute(handle, request)
        manifest = await backend.collect(handle, result)
        assert result.status == ExecutionStatus.SUCCEEDED
        assert {(entry.path, entry.operation) for entry in manifest.entries} == {
            ("old.txt", "modify"),
            ("added.txt", "create"),
        }
        assert (workspace / "old.txt").read_text() == "before"
        assert not (workspace / "added.txt").exists()
    finally:
        cleanup = await backend.cleanup(handle)
    assert cleanup.status == "cleaned"
    eval_record(result, cleanup, manifest)


@pytest.mark.asyncio
async def test_cancelled_attach_removes_container_and_staging(tmp_path: Path) -> None:
    image = await fixture_image()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = DockerExecutionBackend(workspace, tmp_path / "stages")
    request = ExecutionRequest(
        str(uuid4()),
        "fixture-run",
        "cancel-call",
        ("sleep", "30"),
        ".",
        SandboxSpec(
            BackendKind.DOCKER,
            image,
            ResourceBudget(60, 0.5, 64 * 1024 * 1024, 16, 1000, 1024 * 1024),
        ),
    )
    handle = await backend.prepare(request)
    execution = asyncio.create_task(backend.execute(handle, request))
    await asyncio.sleep(0.3)
    execution.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await execution
    finally:
        cleanup = await backend.cleanup(handle)
    assert cleanup.status == "cleaned"
    assert not (tmp_path / "stages" / request.execution_id).exists()
    code, _, _ = await backend._docker("docker", "inspect", handle.opaque_id, timeout=5)
    assert code != 0
    eval_record(None, cleanup)


@pytest.mark.asyncio
async def test_memory_limit_reports_oom(tmp_path: Path) -> None:
    image = await fixture_image()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = DockerExecutionBackend(workspace, tmp_path / "stages")
    request = ExecutionRequest(
        str(uuid4()),
        "fixture-run",
        "oom-call",
        ("awk", 'BEGIN { s="x"; for (i=0;i<28;i++) s=s s; print length(s) }'),
        ".",
        SandboxSpec(
            BackendKind.DOCKER,
            image,
            ResourceBudget(20, 0.5, 32 * 1024 * 1024, 16, 1000, 1024 * 1024),
        ),
    )
    handle = await backend.prepare(request)
    try:
        result = await backend.execute(handle, request)
        assert result.status == ExecutionStatus.FAILED
        assert result.error_code == "container_oom"
    finally:
        cleanup = await backend.cleanup(handle)
    assert cleanup.status == "cleaned"
    eval_record(result, cleanup)


@pytest.mark.asyncio
async def test_pids_limit_blocks_fork_burst(tmp_path: Path) -> None:
    image = await fixture_image()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = DockerExecutionBackend(workspace, tmp_path / "stages")
    request = ExecutionRequest(
        str(uuid4()),
        "fixture-run",
        "pids-call",
        (
            "sh",
            "-c",
            "i=0; while [ $i -lt 40 ]; do sleep 5 & i=$((i+1)); done; wait",
        ),
        ".",
        SandboxSpec(
            BackendKind.DOCKER,
            image,
            ResourceBudget(8, 0.5, 64 * 1024 * 1024, 8, 2000, 1024 * 1024),
        ),
    )
    handle = await backend.prepare(request)
    try:
        result = await backend.execute(handle, request)
        assert "Resource temporarily unavailable" in result.output
    finally:
        cleanup = await backend.cleanup(handle)
    assert cleanup.status == "cleaned"
    eval_record(result, cleanup)
