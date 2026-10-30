"""Deity CLI entry point."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from deity import __version__
from deity.adapters.storage import SQLiteSessionRepository
from deity.application.config import load_config
from deity.application.run_service import RunService
from deity.core.runtime import BudgetExceeded, ModelFailure, ProtocolError

app = typer.Typer(help="Deity AI Coding Agent Harness")
runs_app = typer.Typer(help="Inspect persisted runs")
app.add_typer(runs_app, name="runs")
console = Console()


def version_callback(value: bool) -> None:
    """Print the package version and exit."""
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", callback=version_callback, is_eager=True),
) -> None:
    """Run Deity from the local command line."""


@app.command()
def run(
    task: str,
    config: Annotated[Path | None, typer.Option("--config", help="TOML 配置路径")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", help="工作区路径")] = None,
    db: Annotated[Path | None, typer.Option("--db", help="SQLite 路径")] = None,
    session: Annotated[str | None, typer.Option("--session", help="已有会话 ID")] = None,
) -> None:
    """Run a coding task with an explicitly configured model."""
    try:
        settings = load_config(config or Path.home() / ".config/deity/config.toml")
        workdir = (workspace or Path.cwd()).resolve(strict=True)
        db_path = db or Path.home() / ".local/share/deity/deity.db"

        def approve(name: str, args: dict[str, object]) -> bool:
            displayed = {key: value for key, value in args.items() if key != "content"}
            console.print(f"工具 {name} 请求执行：{displayed}")
            return typer.confirm("允许本次调用？", default=False)

        service = RunService.from_config(
            settings, db_path=db_path, workspace=workdir, approve=approve
        )
        result = asyncio.run(service.run(task, model_name=settings.model.name, session_id=session))  # type: ignore[union-attr]
        console.print(result.final_text or "无最终回答")
    except (ModelFailure, ProtocolError, BudgetExceeded) as exc:
        console.print(f"运行失败：{exc}", style="red")
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=2) from None
    except KeyboardInterrupt:
        console.print("运行已取消", style="yellow")
        raise typer.Exit(code=130) from None


@app.command()
def sessions(
    db: Annotated[Path | None, typer.Option("--db", help="SQLite 路径")] = None,
) -> None:
    """List stored local sessions."""

    async def list_sessions() -> list[dict[str, str]]:
        repo = await SQLiteSessionRepository.open(db or Path.home() / ".local/share/deity/deity.db")
        try:
            return await repo.sessions()
        finally:
            await repo.close()

    for item in asyncio.run(list_sessions()):
        console.print(f"{item['id']}  {item['workspace']}")


@runs_app.command("inspect")
def inspect_run(
    run_id: str,
    db: Annotated[Path | None, typer.Option("--db")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Show the safe recovery decision and unresolved tool IDs."""
    try:
        settings = load_config(config or Path.home() / ".config/deity/config.toml")
        service = RunService(
            db_path=db or Path.home() / ".local/share/deity/deity.db",
            workspace=(workspace or Path.cwd()).resolve(strict=True),
            limits=settings.run,
        )
        report = asyncio.run(service.inspect(run_id))
        console.print(f"{report['decision']}: {report['reason']}")
        for item in report["unresolved_tools"]:
            console.print(f"{item['call_id']} {item['tool_name']} {item['status']}")
    except (ValueError, OSError, KeyError) as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=2) from None


@app.command("resume")
def resume_run(
    run_id: str,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
    db: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Continue an interrupted task as a new run from a safe checkpoint."""
    try:
        settings = load_config(config or Path.home() / ".config/deity/config.toml")
        workdir = (workspace or Path.cwd()).resolve(strict=True)

        def approve(name: str, args: dict[str, object]) -> bool:
            displayed = {key: value for key, value in args.items() if key != "content"}
            console.print(f"工具 {name} 请求执行：{displayed}")
            return typer.confirm("允许本次调用？", default=False)

        service = RunService.from_config(
            settings,
            db_path=db or Path.home() / ".local/share/deity/deity.db",
            workspace=workdir,
            approve=approve,
        )
        result = asyncio.run(service.resume(run_id))
        console.print(result.final_text or "无最终回答")
    except (ValueError, OSError, KeyError, ModelFailure, ProtocolError, BudgetExceeded) as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=2) from None


if __name__ == "__main__":
    app()
