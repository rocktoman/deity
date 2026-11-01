"""Standalone code intelligence diagnostics."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Literal, cast

import typer

from deity.adapters.code_intelligence import LexicalCodeSearch
from deity.application.config import load_config
from deity.core.contracts.code_intelligence import ReferenceQuery

code_app = typer.Typer(help="Locate lexical code definitions and references")


def _language(
    value: str | None,
) -> Literal["python", "java", "typescript", "unknown"] | None:
    if value not in {None, "python", "java", "typescript", "unknown"}:
        raise ValueError("invalid language")
    return cast(Literal["python", "java", "typescript", "unknown"] | None, value)


def _search(workspace: Path, config_path: Path | None) -> LexicalCodeSearch:
    settings = load_config(config_path or Path.home() / ".config/deity/config.toml")
    if not settings.coding.code_intelligence_enabled:
        raise ValueError("Code Intelligence is disabled")
    cache_path: Path | None = None
    if settings.code_intelligence.cache_enabled:
        root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        identity = hashlib.sha256(str(workspace).encode()).hexdigest()[:24]
        cache_path = root / "deity/code-index" / f"{identity}.json"
    return LexicalCodeSearch(
        workspace,
        cache_path=cache_path,
        max_files=settings.code_intelligence.max_files,
        max_file_bytes=settings.code_intelligence.max_file_bytes,
    )


def _render(result: object) -> None:
    typer.echo(
        json.dumps(result, ensure_ascii=False, default=lambda value: value.__dict__, indent=2)
    )


@code_app.command("symbol")
def symbol(
    name: str,
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    language: Annotated[str | None, typer.Option("--language")] = None,
    path: Annotated[str | None, typer.Option("--path")] = None,
) -> None:
    """Find lexical definition candidates."""
    try:
        root = (workspace or Path.cwd()).resolve(strict=True)
        result = asyncio.run(
            _search(root, config).find_symbol(ReferenceQuery(name, _language(language), path))
        )
        _render(result)
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None


@code_app.command("references")
def references(
    name: str,
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    language: Annotated[str | None, typer.Option("--language")] = None,
    path: Annotated[str | None, typer.Option("--path")] = None,
) -> None:
    """Find bounded textual references."""
    try:
        root = (workspace or Path.cwd()).resolve(strict=True)
        result = asyncio.run(
            _search(root, config).find_references(ReferenceQuery(name, _language(language), path))
        )
        _render(result)
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
