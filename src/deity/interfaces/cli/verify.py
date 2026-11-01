"""Verification CLI backed by committed command evidence."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from deity.adapters.verification import (
    DEFAULT_ALLOWED_COMMANDS,
    ProjectVerificationDetector,
    VerificationExecutor,
)
from deity.application.config import load_config
from deity.application.verification_service import VerificationService
from deity.core.contracts.verification import FinalReport

console = Console()


def verify_command(
    run_id: Annotated[str | None, typer.Option("--run")] = None,
    show_plan: Annotated[bool, typer.Option("--show-plan")] = False,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
    db: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Detect and run fixed verification commands, optionally linked to a Run."""
    try:
        settings = load_config(config or Path.home() / ".config/deity/config.toml")
        if not settings.coding.verification_enabled:
            raise ValueError("Verification is disabled")
        root = (workspace or Path.cwd()).resolve(strict=True)
        db_path = db or Path.home() / ".local/share/deity/deity.db"

        def approve(argv: tuple[str, ...]) -> bool:
            console.print(f"验证命令：{list(argv)}")
            return typer.confirm("执行此验证？", default=False)

        service = VerificationService(
            db_path,
            root,
            detector=ProjectVerificationDetector(
                root, custom_commands=settings.verification.commands
            ),
            executor=VerificationExecutor(
                root,
                permission=settings.verification.permission,
                approve=approve,
                timeout_seconds=settings.verification.timeout_seconds,
                max_output_chars=settings.verification.max_output_chars,
                repeat=settings.verification.repeat,
                allowed_commands=tuple(
                    dict.fromkeys(
                        (
                            *DEFAULT_ALLOWED_COMMANDS,
                            *(item[0] for item in settings.verification.commands),
                        )
                    )
                ),
            ),
            sandbox=settings.sandbox,
        )

        async def execute() -> FinalReport | None:
            plan = await service.detect()
            if show_plan:
                for command in plan.commands:
                    console.print(f"{command.kind}: {list(command.argv)}")
                for diagnostic in plan.diagnostics:
                    console.print(f"diagnostic: {diagnostic}")
                return None
            return await service.run_plan(plan, run_id=run_id)

        report = asyncio.run(execute())
        if report is not None:
            console.print(f"Verification: {report.status.value}")
            for item in report.checks:
                console.print(
                    f"{item.command.kind}: {item.status.value} "
                    f"exit={item.exit_code} evidence={item.evidence_id}"
                )
    except (ValueError, OSError, KeyError) as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=2) from None
