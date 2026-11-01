"""Plan/Build CLI commands kept outside the main entry module."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.build_service import BuildService
from deity.application.config import Config, load_config
from deity.application.plan_service import PlanService
from deity.application.run_service import RunService
from deity.core.contracts import AgentResult
from deity.core.contracts.planning import PlanArtifact
from deity.core.runtime import BudgetExceeded, ModelFailure, ProtocolError

plans_app = typer.Typer(help="List and inspect persisted plans")
console = Console()


def plan_command(
    task: Annotated[list[str], typer.Argument(help="只读分析任务")],
    config: Annotated[Path | None, typer.Option("--config")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
    db: Annotated[Path | None, typer.Option("--db")] = None,
    session: Annotated[str | None, typer.Option("--session")] = None,
) -> None:
    """Create one read-only structured plan."""
    try:
        settings = load_config(config or Path.home() / ".config/deity/config.toml")
        if not settings.coding.plan_build_enabled:
            raise ValueError("Plan/Build is disabled")
        workdir = (workspace or Path.cwd()).resolve(strict=True)
        runner = RunService.from_config(
            settings,
            db_path=db or Path.home() / ".local/share/deity/deity.db",
            workspace=workdir,
        )
        assert settings.model is not None
        artifact = asyncio.run(
            PlanService(runner).create(
                " ".join(task), model_name=settings.model.name, session_id=session
            )
        )
        console.print(f"{artifact.plan_id} [{artifact.status}] {artifact.draft.goal}")
        _show_plan(artifact)
        if typer.confirm("批准此计划并立即执行 Build？", default=False):
            result = _execute_build(
                settings,
                workdir,
                db or Path.home() / ".local/share/deity/deity.db",
                artifact.plan_id,
                (),
            )
            console.print(result.final_text or "无最终回答")
    except (ValueError, OSError, KeyError, ModelFailure, ProtocolError, BudgetExceeded) as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=2) from None


@plans_app.command("list")
def plans_list(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    session: Annotated[str | None, typer.Option("--session")] = None,
) -> None:
    """List persisted plan facts without starting a model."""

    async def load() -> tuple[PlanArtifact, ...]:
        repo = await SQLiteSessionRepository.open(db or Path.home() / ".local/share/deity/deity.db")
        try:
            return await repo.list_plans(session)
        finally:
            await repo.close()

    for artifact in asyncio.run(load()):
        console.print(f"{artifact.plan_id} [{artifact.status}] {artifact.draft.goal}")


@plans_app.command("show")
def plans_show(
    plan_id: str,
    db: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Show one structured plan and immutable hashes."""

    async def load() -> PlanArtifact:
        repo = await SQLiteSessionRepository.open(db or Path.home() / ".local/share/deity/deity.db")
        try:
            return await repo.get_plan(plan_id)
        finally:
            await repo.close()

    try:
        artifact = asyncio.run(load())
        console.print(
            json.dumps(
                {
                    "plan_id": artifact.plan_id,
                    "status": artifact.status,
                    "sha256": artifact.sha256,
                    "requirements_sha256": artifact.requirements_sha256,
                    "draft": artifact.draft,
                },
                ensure_ascii=False,
                default=lambda value: value.__dict__,
                indent=2,
            )
        )
    except (KeyError, ValueError, OSError) as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=2) from None


def _show_plan(artifact: PlanArtifact, allow_dirty: tuple[str, ...] = ()) -> None:
    console.print(f"计划：{artifact.draft.goal}")
    for step in artifact.draft.steps:
        console.print(f"{step.ordinal}. {step.title}：{step.description}")
        console.print(f"   目标文件：{', '.join(step.files) or '无'}")
    if artifact.draft.risks:
        console.print(f"风险：{'；'.join(artifact.draft.risks)}")
    if allow_dirty:
        console.print(f"允许修改已有改动：{', '.join(allow_dirty)}")


def _execute_build(
    settings: Config,
    workdir: Path,
    db_path: Path,
    plan_id: str,
    allow_dirty: tuple[str, ...],
) -> AgentResult:
    def approve(name: str, args: dict[str, object]) -> bool:
        displayed = {key: value for key, value in args.items() if key != "content"}
        console.print(f"工具 {name} 请求执行：{displayed}")
        return typer.confirm("允许本次调用？", default=False)

    runner = RunService.from_config(settings, db_path=db_path, workspace=workdir, approve=approve)

    async def execute() -> AgentResult:
        started = await BuildService(
            db_path, workdir, plan_ttl_seconds=settings.coding.plan_ttl_seconds
        ).start(plan_id, allow_dirty_paths=allow_dirty)
        return await runner.run_build(started.build_run_id, plan_id)

    return asyncio.run(execute())


def build_command(
    plan_id: Annotated[str, typer.Option("--plan")],
    allow_dirty: Annotated[list[str] | None, typer.Option("--allow-dirty")] = None,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
    db: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Explicitly approve one plan and execute a new Build Run."""
    try:
        settings = load_config(config or Path.home() / ".config/deity/config.toml")
        if not settings.coding.plan_build_enabled or not settings.coding.git_guard_enabled:
            raise ValueError("Plan/Build requires Git Guard")
        workdir = (workspace or Path.cwd()).resolve(strict=True)
        db_path = db or Path.home() / ".local/share/deity/deity.db"

        async def load_plan() -> PlanArtifact:
            repo = await SQLiteSessionRepository.open(db_path)
            try:
                return await repo.get_plan(plan_id)
            finally:
                await repo.close()

        artifact = asyncio.run(load_plan())
        if artifact.status != "draft":
            raise ValueError("plan is not buildable")
        _show_plan(artifact, tuple(allow_dirty or ()))
        if not typer.confirm("批准此计划并执行 Build？", default=False):
            console.print("已取消 Build，计划仍可再次查看和批准。")
            return

        result = _execute_build(settings, workdir, db_path, plan_id, tuple(allow_dirty or ()))
        console.print(result.final_text or "无最终回答")
    except (ValueError, OSError, KeyError, ModelFailure, ProtocolError, BudgetExceeded) as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=2) from None
