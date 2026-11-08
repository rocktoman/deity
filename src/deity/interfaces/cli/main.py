"""Deity CLI entry point."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from deity import __version__
from deity.adapters.storage import SQLiteSessionRepository
from deity.application.config import Config, ConfigResolver, load_config
from deity.application.doctor import run_doctor
from deity.application.run_service import RunService
from deity.core.runtime import BudgetExceeded, ModelFailure, ProtocolError
from deity.interfaces.cli.chat_approval import ApprovalController, make_approve
from deity.interfaces.cli.chat_commands import run_error_message
from deity.interfaces.cli.chat_session import run_chat_session
from deity.interfaces.cli.code import _search, code_app
from deity.interfaces.cli.plan import build_command, plan_command, plans_app
from deity.interfaces.cli.sandbox import sandbox_app
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
app.add_typer(sandbox_app, name="sandbox")
eval_app = typer.Typer(help="Replay eval reports and check the regression gate")
app.add_typer(eval_app, name="eval")
app.command("plan")(plan_command)
app.command("build")(build_command)
app.command("verify")(verify_command)
console = Console()


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
        workdir = (workspace or Path.cwd()).resolve(strict=True)
        settings = _resolve_config(config, workdir)
        db_path = db or Path.home() / ".local/share/deity/deity.db"

        approval = ApprovalController(workspace=workdir, console=console)
        service = RunService.from_config(
            settings, db_path=db_path, workspace=workdir, approve=make_approve(approval)
        )
        model_name = settings.model.name  # type: ignore[union-attr]
        if task is None:
            symbol_search = None
            if settings.coding.code_intelligence_enabled:
                try:
                    symbol_search = _search(workdir, config)
                except (ValueError, OSError):
                    symbol_search = None
            asyncio.run(
                run_chat_session(
                    service,
                    model_name,
                    session,
                    config=settings.chat,
                    output=console,
                    symbol_search=symbol_search,
                    approval=approval,
                    config_path=config,
                )
            )
        else:
            result = asyncio.run(
                service.run(" ".join(task), model_name=model_name, session_id=session)
            )
            console.print(result.final_text or "无最终回答")
    except (ModelFailure, ProtocolError, BudgetExceeded) as exc:
        console.print(f"运行失败：{run_error_message(exc)}", style="red")
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


@runs_app.command("tree")
def delegation_tree(
    run_id: str,
    db: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Show persisted child Run status and bounded summaries for one root Run."""

    async def read_tree() -> list[dict[str, object]]:
        repo = await SQLiteSessionRepository.open(db or Path.home() / ".local/share/deity/deity.db")
        try:
            return await repo.delegation_tree(run_id)
        finally:
            await repo.close()

    try:
        for item in asyncio.run(read_tree()):
            console.print(
                f"{item['id']}  parent={item['parent_run_id']}  "
                f"child={item['child_run_id']}  {item['status']}"
            )
            if isinstance(item["result_json"], str):
                result = json.loads(item["result_json"])
                console.print(f"  {result['summary']}")
                for evidence_id in result["evidence_ids"]:
                    console.print(f"  evidence: {evidence_id}")
    except (ValueError, OSError) as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=2) from None


def _resolve_config(config: Path | None, workspace: Path) -> Config:
    """按 CLI > project > user > default 解析配置。"""
    settings, _sources = ConfigResolver.resolve(
        {},
        workspace / ".deity" / "config.toml",
        config or Path.home() / ".config/deity/config.toml",
        loader=load_config,
    )
    return settings


@eval_app.command("replay")
def eval_replay(
    target: str,
    db: Annotated[Path | None, typer.Option("--db", help="SQLite 路径")] = None,
    reports_dir: Annotated[Path | None, typer.Option("--reports-dir")] = None,
) -> None:
    """只读回放一份评估报告或一个 Run 的事件，不执行真实写入或网络副作用。"""
    from deity.application.replay import TraceReplayer

    _ = reports_dir
    try:
        replayer = TraceReplayer()
        path = Path(target)
        if path.is_file():
            result = replayer.replay_report(path)
        else:
            result = replayer.replay_sqlite(
                db or Path.home() / ".local/share/deity/deity.db", target
            )
        console.print(f"{result.case_id}: {result.status} 事件 {result.events_seen} 条")
        if result.schema_mismatch:
            console.print("schema_mismatch：旧 schema 事件只读，未迁移", style="yellow")
        if result.blocked:
            console.print(f"replay_blocked：{', '.join(result.blocked)}", style="yellow")
    except (ValueError, OSError) as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=2) from None


@eval_app.command("gate")
def eval_gate(
    reports_dir: Annotated[Path | None, typer.Option("--reports-dir")] = None,
) -> None:
    """检查关键安全/协议门禁；任一失败即 gate 失败，退出码 1。"""
    from deity.application.regression_gate import RegressionGate

    reports = reports_dir or Path.cwd() / "evals" / "reports"
    result = RegressionGate.check(reports)
    console.print(
        f"regression gate：{'通过' if result.passed else '失败'} 已查 {', '.join(result.checked)}"
    )
    if result.failures:
        console.print(f"失败：{', '.join(result.failures)}", style="red")
        raise typer.Exit(code=1)


@app.command()
def doctor(
    config: Annotated[Path | None, typer.Option("--config", help="TOML 配置路径")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", help="工作区路径")] = None,
) -> None:
    """检查 Python/依赖/SQLite/Git/rg/模型凭据/Docker 与配置；有 fail 时退出码 1。"""
    workdir = (workspace or Path.cwd()).resolve(strict=True)
    report = run_doctor(config or Path.home() / ".config/deity/config.toml", workdir)
    for check in report.checks:
        console.print(f"{check.name}: {check.status} {check.detail}")
    if report.failed:
        raise typer.Exit(code=1)


@app.command()
def tui(
    config: Annotated[Path | None, typer.Option("--config", help="TOML 配置路径")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", help="工作区路径")] = None,
    task: Annotated[str | None, typer.Option("--task", help="启动新 Run 的任务")] = None,
    run_id: Annotated[str | None, typer.Option("--run-id", help="接入已有 Run")] = None,
) -> None:
    """启动 Textual TUI；未安装 Textual 时提示安装方式并退出码 2。

    经 ClientService 订阅事件喂给 TuiApp：a/d 批准/拒绝首个待决审批，
    c 取消当前 Run，r 恢复当前/指定 Run。
    """
    try:
        from deity.application.client_service import ClientService
        from deity.interfaces.tui.app import run_tui
    except ImportError:
        console.print(
            'TUI 需要安装 Textual：uv sync --extra tui 或 pip install "deity[tui]"',
            style="red",
        )
        raise typer.Exit(code=2) from None
    try:
        workdir = (workspace or Path.cwd()).resolve(strict=True)
        settings = _resolve_config(config, workdir)
        if not settings.tui.enabled:
            console.print(
                "TUI 未启用：请在配置中设置 [tui] enabled = true，或用纯 CLI 运行。",
                style="red",
            )
            raise typer.Exit(code=2) from None
        model_name = settings.model.name if settings.model is not None else "fake"
        service = RunService.from_config(
            settings,
            db_path=Path.home() / ".local/share/deity/deity.db",
            workspace=workdir,
        )
        client = ClientService(service)
        run_tui(client_service=client, run_id=run_id, task=task, model_name=model_name)
    except (ValueError, OSError) as exc:
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
