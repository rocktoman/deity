"""Deity CLI entry point."""

import asyncio
import shlex
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from rich.console import Console

from deity import __version__
from deity.adapters.storage import SQLiteSessionRepository
from deity.application.config import load_config
from deity.application.plan_service import PlanService
from deity.application.run_service import RunService
from deity.core.runtime import BudgetExceeded, ModelFailure, ProtocolError
from deity.interfaces.cli.code import code_app
from deity.interfaces.cli.menu import choose
from deity.interfaces.cli.plan import (
    authorize_dirty_targets,
    build_approved,
    build_command,
    confirm_build,
    plan_command,
    plans_app,
)
from deity.interfaces.cli.verify import verify_command

app = typer.Typer(help="Deity AI Coding Agent Harness")
runs_app = typer.Typer(help="Inspect persisted runs")
app.add_typer(runs_app, name="runs")
mcp_app = typer.Typer(help="Manage local MCP servers")
app.add_typer(mcp_app, name="mcp")
skills_app = typer.Typer(help="Inspect configured Skills")
app.add_typer(skills_app, name="skills")
app.add_typer(plans_app, name="plans")
app.add_typer(code_app, name="code")
app.command("plan")(plan_command)
app.command("build")(build_command)
app.command("verify")(verify_command)
console = Console()


def _run_error_message(exc: Exception) -> str:
    """Turn internal runtime limits into actionable CLI messages."""
    if isinstance(exc, BudgetExceeded) and str(exc) == "max_turns":
        return "已达到最大运行轮次，模型仍未给出最终结论；请缩小任务范围或提高 max_turns。"
    return str(exc)


@skills_app.command("list")
def skills_list(
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    """Show project and user Skills without loading their bodies."""
    service = RunService(
        db_path=Path.home() / ".local/share/deity/deity.db",
        workspace=(workspace or Path.cwd()).resolve(strict=True),
        skills_enabled=True,
        skill_diagnostics=True,
    )
    for item in service.skills_list():
        typer.echo(
            f"{item['name']} [{item['source']}] sha256:{item['sha256'][:12]} {item['description']}"
        )
    for error in service.skills_errors():
        typer.echo(f"ERROR {error}")
    if service.skills_errors():
        raise typer.Exit(code=2)


@skills_app.command("inspect")
def skills_inspect(
    name: str,
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    """Show one Skill's source and description without executing it."""
    service: RunService | None = None
    try:
        service = RunService(
            db_path=Path.home() / ".local/share/deity/deity.db",
            workspace=(workspace or Path.cwd()).resolve(strict=True),
            skills_enabled=True,
            skill_diagnostics=True,
        )
        item = service.skills_inspect(name)
        typer.echo(
            f"{item['name']} [{item['source']}] sha256:{item['sha256']} "
            f"{item['path']}\n{item['description']}"
        )
    except (KeyError, ValueError, OSError) as exc:
        for error in service.skills_errors() if service is not None else ():
            typer.echo(f"ERROR {error}")
        console.print(str(exc), style="red")
        raise typer.Exit(code=2) from None


@mcp_app.command("list")
def mcp_list(
    config: Annotated[Path | None, typer.Option("--config")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    """Show configured MCP servers without starting them."""
    settings = load_config(config or Path.home() / ".config/deity/config.toml")
    service = RunService(
        db_path=Path.home() / ".local/share/deity/deity.db",
        workspace=(workspace or Path.cwd()).resolve(strict=True),
        mcp_enabled=settings.extensions.mcp_enabled,
        mcp_servers=settings.mcp_servers,
    )
    for name in service.mcp_list():
        typer.echo(name)


@mcp_app.command("test")
def mcp_test(
    config: Annotated[Path | None, typer.Option("--config")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
    db: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Check MCP transport and tool schemas without invoking business tools."""
    try:
        settings = load_config(config or Path.home() / ".config/deity/config.toml")
        if not settings.extensions.mcp_enabled:
            raise ValueError("MCP is disabled")

        service = RunService(
            db_path=db or Path.home() / ".local/share/deity/deity.db",
            workspace=(workspace or Path.cwd()).resolve(strict=True),
            mcp_enabled=True,
            mcp_servers=settings.mcp_servers,
        )
        for name in asyncio.run(service.mcp_test()):
            typer.echo(name)
    except (ValueError, OSError, TimeoutError) as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=2) from None


def version_callback(value: bool) -> None:
    """Print the package version and exit."""
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", callback=version_callback, is_eager=True),
) -> None:
    """Enter the interactive session when no subcommand is provided."""
    if ctx.invoked_subcommand is None:
        run(task=None, config=None, workspace=None, db=None, session=None)


@app.command()
def run(
    task: Annotated[list[str] | None, typer.Argument(help="单次任务；省略后进入交互模式")] = None,
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
        model_name = settings.model.name  # type: ignore[union-attr]
        if task is None:
            asyncio.run(_interactive(service, model_name, session))
        else:
            result = asyncio.run(
                service.run(" ".join(task), model_name=model_name, session_id=session)
            )
            console.print(result.final_text or "无最终回答")
    except (ModelFailure, ProtocolError, BudgetExceeded) as exc:
        console.print(f"运行失败：{_run_error_message(exc)}", style="red")
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=2) from None
    except KeyboardInterrupt:
        console.print("运行已取消", style="yellow")
        raise typer.Exit(code=130) from None


async def _interactive(service: RunService, model_name: str, session_id: str | None) -> None:
    """Read successive prompts within one persisted conversation session."""
    active_session = session_id or str(uuid4())
    pending_skill: str | None = None
    pending_mcp_tool: str | None = None
    plan_mode = False
    console.print("欢迎来到 Deity，我是你的编程助手。")
    while True:
        try:
            prompt = input("Deity[plan]> " if plan_mode else "Deity> ").strip()
        except EOFError:
            console.print()
            return
        if prompt in {"/exit", "/quit"}:
            return
        if not prompt:
            continue
        if prompt == "/help":
            console.print(
                "/plan 进入持续计划模式；/build [计划ID] [--allow-dirty 路径] 执行已有计划；"
                "/normal 返回普通模式；/skill 选择 Skill；/mcp 查看并选择 MCP 工具；"
                "选择会提示模型在下一条任务中使用；/exit 或 /quit 退出"
            )
            continue
        if prompt == "/plan":
            if not service.coding.plan_build_enabled or not service.coding.git_guard_enabled:
                console.print(
                    "Plan/Build 未启用，请在配置中开启 plan_build_enabled 和 git_guard_enabled。"
                )
                continue
            plan_mode = True
            console.print("已进入计划模式。输入任务后将展示计划并询问是否构建。")
            continue
        if prompt == "/normal":
            plan_mode = False
            console.print("已返回普通模式。")
            continue
        if prompt == "/build" or prompt.startswith("/build "):
            if not service.coding.plan_build_enabled or not service.coding.git_guard_enabled:
                console.print(
                    "Plan/Build 未启用，请在配置中开启 plan_build_enabled 和 git_guard_enabled。"
                )
                continue
            try:
                parts = shlex.split(prompt)[1:]
                dirty_paths: list[str] = []
                plan_id: str | None = None
                index = 0
                while index < len(parts):
                    if parts[index] == "--allow-dirty":
                        if index + 1 >= len(parts):
                            raise ValueError("--allow-dirty 需要文件路径")
                        dirty_paths.append(parts[index + 1])
                        index += 2
                    elif plan_id is None:
                        plan_id = parts[index]
                        index += 1
                    else:
                        raise ValueError("/build 只接受一个计划 ID")
                planner = PlanService(service)
                if plan_id is None:
                    candidates = await planner.buildable(active_session)
                    selected = choose(
                        "当前会话待执行计划",
                        [f"{item.plan_id} {item.draft.goal}" for item in candidates],
                    )
                    if selected is None:
                        continue
                    artifact = candidates[selected]
                else:
                    artifact = await planner.get_buildable(plan_id, active_session)
                if dirty_paths and any(
                    path not in {name for step in artifact.draft.steps for name in step.files}
                    for path in dirty_paths
                ):
                    raise ValueError("--allow-dirty 路径必须属于计划目标")
                if confirm_build(artifact, tuple(dirty_paths)):
                    allowed = await authorize_dirty_targets(
                        service, artifact.plan_id, tuple(dirty_paths)
                    )
                    if allowed is not None:
                        result = await build_approved(service, artifact.plan_id, allowed)
                        console.print(result.final_text or "无最终回答")
                        plan_mode = True
                else:
                    console.print("已取消 Build，计划仍可再次查看和批准。")
            except (
                ValueError,
                OSError,
                KeyError,
                ModelFailure,
                ProtocolError,
                BudgetExceeded,
            ) as exc:
                console.print(f"Build 失败：{_run_error_message(exc)}", style="red")
            continue
        if prompt == "/skill" or prompt.startswith("/skill "):
            skills = service.skills_list()
            names = [item["name"] for item in skills]
            requested = prompt.removeprefix("/skill").strip()
            selected = (
                names.index(requested)
                if requested in names
                else choose(
                    "可用 Skills", [f"{item['name']} [{item['source']}]" for item in skills]
                )
                if not requested
                else None
            )
            if selected is None:
                if requested:
                    console.print(f"未找到 Skill：{requested}")
                continue
            item = service.skills_inspect(names[selected])
            pending_skill = item["name"]
            console.print(
                f"已选择 {pending_skill} [{item['source']}]：{item['description']}\n"
                f"来源：{item['path']}\n下一条任务将提示模型加载此 Skill。"
            )
            continue
        if prompt == "/mcp":
            servers = service.mcp_list()
            selected = choose("已配置的 MCP servers", servers)
            if selected is None:
                continue
            server_name = servers[selected].split()[0]
            if "[disabled]" in servers[selected]:
                console.print(f"{server_name} 已禁用；请在配置中开启 MCP。")
                continue
            try:
                tools = await service.mcp_test(server_name)
            except (ValueError, OSError, TimeoutError) as exc:
                console.print(f"MCP 检查失败：{exc}", style="red")
                continue
            except Exception as exc:  # noqa: BLE001 - keep an external server failure in this menu
                console.print(f"MCP 检查失败：{type(exc).__name__}", style="red")
                continue
            tool_index = choose(f"{server_name} 的工具（仅发现，未调用）", tools)
            if tool_index is not None:
                pending_mcp_tool = tools[tool_index]
                console.print(f"已选择 {pending_mcp_tool}。下一条任务将提示模型使用此工具。")
            continue
        if prompt.startswith("/"):
            console.print("未知命令。输入 /help 查看可用命令。")
            continue
        try:
            instructions: list[str] = []
            if pending_skill:
                instructions.append(f"请先加载并使用 {pending_skill} Skill。")
            if pending_mcp_tool:
                instructions.append(f"如适用，请使用 {pending_mcp_tool} 工具。")
            task = f"{''.join(instructions)}任务：{prompt}" if instructions else prompt
            pending_skill = None
            pending_mcp_tool = None
            if plan_mode:
                artifact = await PlanService(service).create(
                    task, model_name=model_name, session_id=active_session
                )
                console.print(f"计划 ID：{artifact.plan_id}")
                if confirm_build(artifact):
                    allowed = await authorize_dirty_targets(service, artifact.plan_id)
                    if allowed is None:
                        continue
                    result = await build_approved(service, artifact.plan_id, allowed)
                else:
                    console.print("已保留计划；输入 /build 可稍后执行。")
                    continue
            else:
                result = await service.run(task, model_name=model_name, session_id=active_session)
        except (ModelFailure, ProtocolError, BudgetExceeded, ValueError, OSError) as exc:
            console.print(f"运行失败：{_run_error_message(exc)}", style="red")
            continue
        console.print(result.final_text or "无最终回答")


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
        typer.echo(f"{item['id']}  {item['workspace']}")


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
        service = RunService.from_config(
            settings,
            db_path=db or Path.home() / ".local/share/deity/deity.db",
            workspace=(workspace or Path.cwd()).resolve(strict=True),
            inspection_only=True,
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
