"""ADR-020 B2：bwrap 沙箱——argv 守卫、能力诚实、staging 生命周期。"""

import os
import stat
import sys
from pathlib import Path

import pytest

from deity.adapters.execution.bwrap import BwrapExecutionBackend
from deity.core.contracts.execution import (
    BackendKind,
    ExecutionRequest,
    ExecutionStatus,
    ResourceBudget,
    SandboxPolicy,
    SandboxSpec,
)

FAKE_BWRAP = """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
stage = None
chdir = None
index = 0
while index < len(args):
    if args[index] in {"--bind", "--ro-bind"}:
        stage = args[index + 1]
        index += 2
    elif args[index] == "--chdir":
        chdir = args[index + 1]
        index += 2
    elif args[index] == "--":
        index += 1
        break
    else:
        index += 1
target = chdir.replace("/workspace", stage, 1) if chdir and stage else stage
os.chdir(target)
os.execvp(args[index], args[index:])
"""


def _request(**spec_overrides: object) -> ExecutionRequest:
    spec = SandboxSpec(
        BackendKind.BWRAP,
        None,
        ResourceBudget(30, 1.0, 64 * 1024 * 1024, 32, 4000, 1024 * 1024),
        **spec_overrides,  # type: ignore[arg-type]
    )
    return ExecutionRequest(
        "execution-1", "run-1", "call-1", ("python3", "-c", "print('hi')"), ".", spec
    )


def test_argv_has_isolation_flags_and_workspace_bind(tmp_path: Path) -> None:
    argv = BwrapExecutionBackend.create_argv(_request(), tmp_path / "stage")
    for flag in (
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--clearenv",
    ):
        assert flag in argv
    bind = argv.index("--bind")
    assert argv[bind + 1] == str(tmp_path / "stage") and argv[bind + 2] == "/workspace"
    assert argv[argv.index("--ro-bind") + 1] == "/"


def test_read_only_policy_binds_workspace_read_only(tmp_path: Path) -> None:
    argv = BwrapExecutionBackend.create_argv(
        _request(policy=SandboxPolicy.READ_ONLY), tmp_path / "stage"
    )
    workspace_binds = [
        index
        for index, item in enumerate(argv)
        if item in {"--bind", "--ro-bind"} and argv[index + 2] == "/workspace"
    ]
    assert len(workspace_binds) == 1
    assert argv[workspace_binds[0]] == "--ro-bind"


def test_full_access_and_network_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full-access"):
        BwrapExecutionBackend.create_argv(
            _request(policy=SandboxPolicy.FULL_ACCESS), tmp_path / "stage"
        )
    with pytest.raises(ValueError, match="network"):
        BwrapExecutionBackend.create_argv(_request(network="isolated"), tmp_path / "stage")


@pytest.mark.asyncio
async def test_capabilities_honest_when_bwrap_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("deity.adapters.execution.bwrap.shutil.which", lambda name: None)
    backend = BwrapExecutionBackend(tmp_path, tmp_path / "stages")
    capabilities = await backend.capabilities()
    assert capabilities.available is False
    # 不冒充配额隔离：三项如实为 False。
    assert capabilities.cpu_limit is False
    assert capabilities.memory_limit is False
    assert capabilities.pids_limit is False
    assert capabilities.process_isolation is True
    assert not await backend.preflight(_request())


@pytest.fixture
def fake_bwrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "bwrap"
    script.write_text(FAKE_BWRAP, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return bin_dir


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bwrap 后端仅 Linux")
@pytest.mark.asyncio
async def test_prepare_execute_collect_cleanup_with_fake_bwrap(
    tmp_path: Path, fake_bwrap: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    backend = BwrapExecutionBackend(workspace, tmp_path / "stages")
    assert (await backend.capabilities()).available
    request = ExecutionRequest(
        "execution-9",
        "run-1",
        "call-1",
        ("python3", "-c", "open('made.txt', 'w').write('made\\n')"),
        ".",
        _request().spec,
    )
    assert await backend.preflight(request)
    handle = await backend.prepare(request)
    result = await backend.execute(handle, request)
    assert result.status == ExecutionStatus.SUCCEEDED
    manifest = await backend.collect(handle, result)
    assert [(entry.path, entry.operation) for entry in manifest.entries] == [("made.txt", "create")]
    # 沙箱只改 staging，真实工作区不被触碰。
    assert not (workspace / "made.txt").exists()
    report = await backend.cleanup(handle)
    assert report.status == "cleaned"
    assert handle.staging_root is not None and not Path(handle.staging_root).exists()


class _StubRepo:
    """SandboxService 所需的最小记录接口（只记调用顺序）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def prepare_sandbox_execution(self, request: object) -> None:
        self.calls.append("prepare")

    async def record_sandbox_container(self, run_id: str, execution_id: str, opaque: str) -> None:
        self.calls.append("container")

    async def record_sandbox_result(self, run_id: str, result: object) -> None:
        self.calls.append("result")

    async def record_sandbox_manifest(self, run_id: str, manifest: object) -> None:
        self.calls.append("manifest")

    async def record_sandbox_cleanup(self, run_id: str, report: object) -> None:
        self.calls.append("cleanup")

    async def mark_sandbox_manifest_unknown(self, run_id: str, execution_id: str) -> None:
        self.calls.append("unknown")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bwrap 后端仅 Linux")
@pytest.mark.asyncio
async def test_sandbox_service_accepts_bwrap_capability_profile(
    tmp_path: Path, fake_bwrap: Path
) -> None:
    from deity.application.sandbox_service import SandboxService

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("a\n", encoding="utf-8")
    backend = BwrapExecutionBackend(workspace, tmp_path / "stages")
    repo = _StubRepo()
    result, _manifest = await SandboxService(backend, repo).run(_request())  # type: ignore[arg-type]
    assert result.status == ExecutionStatus.SUCCEEDED
    assert repo.calls == ["prepare", "container", "result", "manifest", "cleanup"]
