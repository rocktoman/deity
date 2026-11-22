"""Sandbox diagnostics and explicit command execution."""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer

from deity.adapters.execution.docker import DockerExecutionBackend
from deity.adapters.storage import SQLiteSessionRepository
from deity.application.config import load_config
from deity.application.sandbox_service import SandboxService
from deity.core.contracts import AgentRequest, RunStatus
from deity.core.contracts.execution import (
    BackendKind,
    ExecutionBackend,
    ExecutionRequest,
    ResourceBudget,
    SandboxPolicy,
    SandboxSpec,
)


def sandbox_status_lines(service: Any) -> list[str]:
    """`/sandbox` 无参展示（ADR-023）：后端/策略/启用/网络/可用性。

    bwrap 可用性 = Linux + bwrap 二进制；缺失时明确不可用，绝不回退
    宿主机执行。服务未提供沙箱配置（测试替身）时如实说明。
    """
    sandbox = getattr(service, "sandbox", None)
    if sandbox is None:
        return ["沙箱：当前服务未提供沙箱配置。"]
    lines = [
        "沙箱状态：",
        f"启用：{'是' if sandbox.enabled else '否'}",
        f"后端：{sandbox.backend}",
        f"策略：{sandbox.policy}",
        f"网络：{sandbox.network}",
    ]
    if sandbox.backend == "bwrap":
        available = sys.platform.startswith("linux") and shutil.which("bwrap") is not None
        lines.append(
            "可用性：可用"
            if available
            else "可用性：不可用（需要 Linux + bwrap 二进制；无 bwrap/非 Linux = 明确不可用，不回退宿主机执行）"
        )
    elif sandbox.backend == "docker":
        lines.append("可用性：见 deity sandbox doctor（需 Docker 守护进程）")
    else:
        lines.append("可用性：local 后端无隔离")
    lines.append(
        "切换：/sandbox read-only | /sandbox workspace-write（仅本次会话内生效；"
        "写入配置文件 [sandbox] policy 可永久生效）"
    )
    return lines


sandbox_app = typer.Typer(help="Diagnose and run a constrained local Docker sandbox")


@sandbox_app.command("doctor")
def doctor(
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Report whether the local Docker daemon is available without running a container."""
    root = (workspace or Path.cwd()).resolve(strict=True)
    if config is not None and load_config(config).sandbox.backend == "bwrap":
        # ADR-020 B2：bwrap 档单独报告（含无 bwrap 时的明确降级说明）。
        from deity.adapters.execution.bwrap import BwrapExecutionBackend

        bwrap_backend = BwrapExecutionBackend(root, Path(tempfile.gettempdir()) / "deity-sandbox")
        caps = asyncio.run(bwrap_backend.capabilities())
        typer.echo(f"backend=bwrap available={caps.available}")
        typer.echo(
            "capabilities="
            f"process_isolation={caps.process_isolation},"
            f"read_only_rootfs={caps.read_only_rootfs},"
            f"network_none={caps.network_none},"
            f"cpu={caps.cpu_limit},memory={caps.memory_limit},"
            f"pids={caps.pids_limit}（bwrap 不提供配额强制，如实报告）"
        )
        for diagnostic in caps.diagnostics:
            typer.echo(f"diagnostic={diagnostic}")
        if not caps.available:
            raise typer.Exit(code=2)
        return
    backend = DockerExecutionBackend(root, Path(tempfile.gettempdir()) / "deity-sandbox")
    capabilities = asyncio.run(backend.capabilities())
    typer.echo(f"backend=docker available={capabilities.available}")
    typer.echo(
        "capabilities="
        f"process_isolation={capabilities.process_isolation},"
        f"read_only_rootfs={capabilities.read_only_rootfs},"
        f"network_none={capabilities.network_none},"
        f"cpu={capabilities.cpu_limit},memory={capabilities.memory_limit},"
        f"pids={capabilities.pids_limit}"
    )
    if capabilities.available:
        with tempfile.TemporaryDirectory(prefix="deity-doctor-") as probe:
            probe_path = Path(probe) / "probe"
            probe_path.write_text("ok", encoding="utf-8")
            typer.echo(f"staging_probe={probe_path.read_text(encoding='utf-8') == 'ok'}")
        code, server, _ = asyncio.run(
            backend._docker("docker", "version", "--format", "{{.Server.Version}}", timeout=5)
        )
        typer.echo(f"docker_server={server.strip() if code == 0 else 'UNKNOWN'}")
    if config is not None:
        settings = load_config(config)
        typer.echo(
            f"enabled={settings.sandbox.enabled} required={settings.sandbox.required} "
            f"network={settings.sandbox.network} workspace={settings.sandbox.workspace_mode}"
        )
        typer.echo(
            f"cpu={settings.sandbox.cpu_count} memory_mb={settings.sandbox.memory_mb} "
            f"pids={settings.sandbox.pids_limit} timeout={settings.sandbox.timeout_seconds}"
        )
        image_ready = asyncio.run(backend.image_available(settings.sandbox.image))
        typer.echo(f"image={settings.sandbox.image}")
        typer.echo(f"image_local={image_ready}")
        if settings.sandbox.enabled and settings.sandbox.backend == "docker" and not image_ready:
            raise typer.Exit(code=2)
    for diagnostic in capabilities.diagnostics:
        typer.echo(f"diagnostic={diagnostic}")
    if not capabilities.available:
        raise typer.Exit(code=2)


@sandbox_app.command(
    "run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def run_command(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
    db: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Run argv after -- with the configured sandbox (docker/bwrap) and discard artifacts."""
    settings = load_config(config or Path.home() / ".config/deity/config.toml")
    if not settings.sandbox.enabled or settings.sandbox.backend not in {"docker", "bwrap"}:
        raise typer.BadParameter("sandbox is disabled")
    if settings.sandbox.network != "none":
        raise typer.BadParameter("sandbox run requires network=none")
    argv = tuple(ctx.args)
    if not argv or any(not item or "\0" in item for item in argv):
        raise typer.BadParameter("command argv is required after --")
    root = (workspace or Path.cwd()).resolve(strict=True)
    db_path = db or Path.home() / ".local/share/deity/deity.db"

    async def execute() -> tuple[str, str, int | None, str]:
        repo = await SQLiteSessionRepository.open(db_path)
        run_id = ""
        try:
            run_id = await repo.create_run(
                AgentRequest(
                    str(uuid4()),
                    str(uuid4()),
                    str(root),
                    "sandbox command",
                    "sandbox",
                    provider_name="deterministic",
                )
            )
            await repo.set_status(run_id, RunStatus.RUNNING)
            backend: ExecutionBackend
            if settings.sandbox.backend == "bwrap":
                from deity.adapters.execution.bwrap import BwrapExecutionBackend

                backend = BwrapExecutionBackend(
                    root,
                    Path(tempfile.gettempdir()) / "deity-sandbox",
                    excluded_paths=(db_path,),
                    excluded_patterns=settings.sandbox.exclude,
                    max_workspace_files=settings.sandbox.max_workspace_files,
                    max_workspace_bytes=settings.sandbox.max_workspace_bytes,
                    max_artifact_files=settings.sandbox.max_artifact_files,
                    max_artifact_bytes=settings.sandbox.max_artifact_bytes,
                )
            else:
                backend = DockerExecutionBackend(
                    root,
                    Path(tempfile.gettempdir()) / "deity-sandbox",
                    excluded_paths=(db_path,),
                    excluded_patterns=settings.sandbox.exclude,
                    max_workspace_files=settings.sandbox.max_workspace_files,
                    max_workspace_bytes=settings.sandbox.max_workspace_bytes,
                    max_artifact_files=settings.sandbox.max_artifact_files,
                    max_artifact_bytes=settings.sandbox.max_artifact_bytes,
                    cleanup_timeout_seconds=settings.sandbox.cleanup_timeout_seconds,
                )
            sandbox = settings.sandbox
            request = ExecutionRequest(
                str(uuid4()),
                run_id,
                None,
                argv,
                ".",
                SandboxSpec(
                    BackendKind.BWRAP if sandbox.backend == "bwrap" else BackendKind.DOCKER,
                    sandbox.image if sandbox.backend == "docker" else None,
                    ResourceBudget(
                        sandbox.timeout_seconds,
                        sandbox.cpu_count,
                        sandbox.memory_mb * 1024 * 1024,
                        sandbox.pids_limit,
                        sandbox.max_output_chars,
                        sandbox.tmpfs_mb * 1024 * 1024,
                    ),
                    env_names=sandbox.env_names,
                    policy=SandboxPolicy(sandbox.policy),
                ),
            )
            result, manifest = await SandboxService(backend, repo).run(request)
            await repo.set_status(
                run_id, RunStatus.COMPLETED if result.exit_code == 0 else RunStatus.FAILED
            )
            return (
                run_id,
                result.status.value,
                result.exit_code,
                f"artifacts={len(manifest.entries)}",
            )
        except BaseException:
            if run_id and await repo.status(run_id) == RunStatus.RUNNING:
                await repo.set_status(run_id, RunStatus.FAILED)
            raise
        finally:
            await repo.close()

    try:
        run_id, status, exit_code, artifacts = asyncio.run(execute())
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(f"sandbox error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(f"run_id={run_id} status={status} exit={exit_code} {artifacts}")
    if exit_code != 0:
        raise typer.Exit(code=1)
