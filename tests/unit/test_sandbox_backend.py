import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from deity.adapters.execution.docker import DockerExecutionBackend
from deity.adapters.execution.workspace import WorkspaceStager, file_hashes
from deity.core.contracts.execution import (
    BackendKind,
    ExecutionRequest,
    ExecutionStatus,
    ResourceBudget,
    SandboxHandle,
    SandboxSpec,
)


def request() -> ExecutionRequest:
    return ExecutionRequest(
        "execution-1",
        "run-1",
        "call-1",
        ("python", "-V"),
        ".",
        SandboxSpec(
            BackendKind.DOCKER,
            "sha256:" + "a" * 64,
            ResourceBudget(30, 1.0, 64 * 1024 * 1024, 32, 1000, 1024 * 1024),
        ),
    )


def test_docker_create_argv_has_required_guards(tmp_path: Path) -> None:
    argv = DockerExecutionBackend.create_argv(request(), tmp_path, "deity-execution-1")
    assert argv[:2] == ("docker", "create")
    for flag in (
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--network=none",
        "--pull=never",
    ):
        assert flag in argv
    assert "--privileged" not in argv
    assert not any("docker.sock" in item for item in argv)


def test_named_network_requires_explicit_approval(tmp_path: Path) -> None:
    base = request()
    named = replace(base, spec=replace(base.spec, network="isolated-fixture"))
    with pytest.raises(ValueError, match="network"):
        DockerExecutionBackend.create_argv(named, tmp_path, "deity-execution-1")
    approved = replace(named, spec=replace(named.spec, network_approved=True))
    assert "--network=isolated-fixture" in DockerExecutionBackend.create_argv(
        approved, tmp_path, "deity-execution-1"
    )
    host = replace(approved, spec=replace(approved.spec, network="host"))
    with pytest.raises(ValueError, match="network"):
        DockerExecutionBackend.create_argv(host, tmp_path, "deity-execution-1")


def test_allowed_environment_is_named_without_value_in_argv(tmp_path: Path) -> None:
    base = request()
    allowed = replace(base, spec=replace(base.spec, env_names=("DEITY_TEST_SECRET",)))
    argv = DockerExecutionBackend.create_argv(allowed, tmp_path, "deity-execution-1")
    assert argv[argv.index("--env") + 1] == "DEITY_TEST_SECRET"
    assert not any("fixture-secret" in part for part in argv)
    invalid = replace(base, spec=replace(base.spec, env_names=("BAD-NAME",)))
    with pytest.raises(ValueError, match="environment"):
        DockerExecutionBackend.create_argv(invalid, tmp_path, "deity-execution-1")


def test_stager_rejects_symlink_and_secret(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.py").write_text("print(1)", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=private", encoding="utf-8")
    stager = WorkspaceStager(workspace, tmp_path / "stage")
    staged = stager.copy()
    assert (staged / "source.py").exists()
    assert not (staged / ".env").exists()
    (workspace / "escape").symlink_to(tmp_path)
    stager = WorkspaceStager(workspace, tmp_path / "stage2")
    with pytest.raises(ValueError, match="symlink"):
        stager.copy()


def test_stager_rejects_limits_and_source_drift(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.py"
    source.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="limit"):
        WorkspaceStager(workspace, tmp_path / "small", max_bytes=1).copy()

    from shutil import copyfile

    def drift(src: Path, dst: Path, *, follow_symlinks: bool) -> None:
        copyfile(src, dst, follow_symlinks=follow_symlinks)
        source.write_text("changed", encoding="utf-8")

    with (
        patch("deity.adapters.execution.workspace.shutil.copyfile", side_effect=drift),
        pytest.raises(ValueError, match="workspace_changed_during_stage"),
    ):
        WorkspaceStager(workspace, tmp_path / "drift").copy()
    assert not (tmp_path / "drift").exists()


@pytest.mark.asyncio
async def test_remote_docker_host_is_rejected_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(os.environ, "DOCKER_HOST", "tcp://example.invalid:2375")
    backend = DockerExecutionBackend(tmp_path, tmp_path / "stages")
    capabilities = await backend.capabilities()
    assert not capabilities.available


@pytest.mark.asyncio
async def test_missing_docker_cli_and_image_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = DockerExecutionBackend(tmp_path, tmp_path / "stages")
    with patch("deity.adapters.execution.docker.shutil.which", return_value=None):
        assert not (await backend.capabilities()).available

    async def missing_image(*_args: str, **_kwargs: object) -> tuple[int, str, bool]:
        return 1, "", False

    monkeypatch.setattr(backend, "_docker", missing_image)
    assert not await backend.preflight(request())
    assert not (tmp_path / "stages").exists()


@pytest.mark.asyncio
async def test_daemon_response_loss_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = DockerExecutionBackend(tmp_path, tmp_path / "stages")

    async def disconnected(*_args: str, **_kwargs: object) -> tuple[int, str, bool]:
        raise OSError("daemon disconnected")

    monkeypatch.setattr(backend, "_docker", disconnected)
    result = await backend.execute(
        SandboxHandle("execution-1", BackendKind.DOCKER, "a" * 64, None), request()
    )
    assert result.status == ExecutionStatus.UNKNOWN
    assert result.error_code == "result_unknown"


@pytest.mark.asyncio
async def test_image_id_drift_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = DockerExecutionBackend(tmp_path, tmp_path / "stages")

    async def changed(*_args: str, **_kwargs: object) -> tuple[int, str, bool]:
        return 0, "sha256:" + "b" * 64, False

    monkeypatch.setattr(backend, "_docker", changed)
    assert not await backend.image_available("sha256:" + "a" * 64)


def test_artifact_scan_rejects_excess_bytes(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("x" * 20, encoding="utf-8")
    with pytest.raises(ValueError, match="scan limit"):
        file_hashes(tmp_path, max_bytes=10)


def test_stager_rejects_fifo_and_hardlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.mkfifo(workspace / "pipe")
    with pytest.raises(ValueError, match="non-regular"):
        WorkspaceStager(workspace, tmp_path / "stage-fifo").copy()
    (workspace / "pipe").unlink()
    (workspace / "first").write_text("x", encoding="utf-8")
    os.link(workspace / "first", workspace / "second")
    with pytest.raises(ValueError, match="non-regular"):
        WorkspaceStager(workspace, tmp_path / "stage-links").copy()


def test_stager_applies_extra_excludes_without_removing_default_secrets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "keep.py").write_text("pass", encoding="utf-8")
    (workspace / "output.log").write_text("no", encoding="utf-8")
    (workspace / ".env.test").write_text("SECRET=x", encoding="utf-8")
    stage = WorkspaceStager(workspace, tmp_path / "stage", excluded_patterns=("*.log",)).copy()
    assert (stage / "keep.py").exists()
    assert not (stage / "output.log").exists()
    assert not (stage / ".env.test").exists()
