"""混合代码地图生成器（ADR-026）：Python 标准库 AST + 多语言词法提取，零额外依赖。"""

import ast
import os
import re
from dataclasses import dataclass
from pathlib import Path

_SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    ".Trash",
    "Library",
    "System",
    "Volumes",
    "Applications",
    "dev",
    "proc",
    "sys",
    "private",
}

_LEXICAL_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    ".ts": (
        re.compile(
            r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+|async\s+)?"
            r"(class|interface|type|enum|function)\s+([A-Za-z_$][\w$]*)"
        ),
    ),
    ".tsx": (
        re.compile(
            r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+|async\s+)?"
            r"(class|interface|type|enum|function)\s+([A-Za-z_$][\w$]*)"
        ),
    ),
    ".js": (
        re.compile(
            r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(class|function)\s+([A-Za-z_$][\w$]*)"
        ),
    ),
    ".java": (
        re.compile(
            r"^\s*(?:(?:public|protected|private|abstract|final|static)\s+)*"
            r"(class|interface|enum|record)\s+([A-Za-z_]\w*)"
        ),
    ),
    ".go": (re.compile(r"^\s*(func|type)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"),),
    ".rs": (
        re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?(fn|struct|enum|trait|impl)\s+([A-Za-z_]\w*)"),
    ),
}


@dataclass(frozen=True)
class RepoMapResult:
    """一次代码地图生成结果。"""

    text: str
    file_count: int
    symbol_count: int
    truncated: bool


def _python_symbols(source: str) -> list[str]:
    """用标准库 AST 提取类、方法与顶层函数签名及行号范围。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            end = getattr(node, "end_lineno", node.lineno)
            lines.append(f"  class {node.name} [L{node.lineno}-L{end}]")
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = _format_args(item.args)
                    prefix = "async def" if isinstance(item, ast.AsyncFunctionDef) else "def"
                    lines.append(f"    {prefix} {item.name}({args}) [L{item.lineno}]")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = _format_args(node.args)
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            end = getattr(node, "end_lineno", node.lineno)
            lines.append(f"  {prefix} {node.name}({args}) [L{node.lineno}-L{end}]")
    return lines


def _format_args(args: ast.arguments) -> str:
    names = [arg.arg for arg in args.posonlyargs + args.args]
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    if args.kwonlyargs:
        names.extend(arg.arg for arg in args.kwonlyargs)
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    rendered = ", ".join(names)
    return rendered if len(rendered) <= 48 else rendered[:45] + "…"


def _lexical_symbols(source: str, suffix: str) -> list[str]:
    patterns = _LEXICAL_PATTERNS.get(suffix, ())
    if not patterns:
        return []
    lines: list[str] = []
    for lineno, raw_line in enumerate(source.splitlines(), start=1):
        for pat in patterns:
            match = pat.match(raw_line)
            if match:
                lines.append(f"  {match.group(1)} {match.group(2)} [L{lineno}]")
                break
    return lines


class RepoMapBuilder:
    """有界扫描工作区并构建紧凑代码地图（按路径排序、字符预算截断）。"""

    def __init__(
        self,
        workspace: Path,
        *,
        max_files: int = 500,
        max_file_bytes: int = 512_000,
        max_chars: int = 8_000,
    ) -> None:
        self._workspace = workspace.resolve(strict=False)
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_chars = max_chars

    def build(self) -> RepoMapResult:
        blocks: list[str] = []
        file_count = 0
        symbol_count = 0
        truncated = False
        used_chars = 0
        supported = {".py", *_LEXICAL_PATTERNS.keys()}

        for root, dirs, files in os.walk(
            self._workspace, followlinks=False, onerror=lambda _: None
        ):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
            for name in sorted(files):
                path = Path(root) / name
                if path.suffix not in supported:
                    continue
                if file_count >= self._max_files:
                    truncated = True
                    break
                try:
                    if not path.is_file() or path.is_symlink():
                        continue
                    if path.stat().st_size > self._max_file_bytes:
                        continue
                    source = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                symbols = (
                    _python_symbols(source)
                    if path.suffix == ".py"
                    else _lexical_symbols(source, path.suffix)
                )
                if not symbols:
                    continue
                rel = path.relative_to(self._workspace).as_posix()
                block = f"{rel}:\n" + "\n".join(symbols)
                if used_chars + len(block) + 1 > self._max_chars:
                    truncated = True
                    break
                blocks.append(block)
                used_chars += len(block) + 1
                file_count += 1
                symbol_count += len(symbols)
            if truncated:
                break

        text = "\n".join(blocks)
        if truncated and text:
            text += "\n…（代码地图已按字符/文件上限截断）"
        return RepoMapResult(
            text=text,
            file_count=file_count,
            symbol_count=symbol_count,
            truncated=truncated,
        )


__all__ = ["RepoMapBuilder", "RepoMapResult"]
