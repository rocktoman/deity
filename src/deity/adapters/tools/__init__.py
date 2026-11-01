"""Validated local tool dispatch and approval policy."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from deity.adapters.execution.local import run_local_process
from deity.core.contracts import AgentRequest, ToolCall, ToolResult


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathArgs(Args):
    path: str


class WriteArgs(PathArgs):
    content: str


class PatchArgs(PathArgs):
    old: str
    new: str


class SearchArgs(Args):
    pattern: str
    path: str = "."


class GlobArgs(Args):
    pattern: str


class ShellArgs(Args):
    argv: list[str] = Field(min_length=1)


class EmptyArgs(Args):
    pass


TOOL_ARGS: dict[str, type[Args]] = {
    "read_file": PathArgs,
    "list_files": PathArgs,
    "glob": GlobArgs,
    "grep": SearchArgs,
    "write_file": WriteArgs,
    "apply_patch": PatchArgs,
    "delete_file": PathArgs,
    "shell": ShellArgs,
    "git_status": EmptyArgs,
    "git_diff": EmptyArgs,
}
WRITE_TOOLS = {"write_file", "apply_patch", "delete_file"}


def auto_shell_allowed(args: dict[str, Any], root: Path) -> bool:
    """Allow common local checks without opening arbitrary shell commands."""
    argv = args.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        return False
    command = Path(argv[0]).name
    if argv[0] != command and argv[0] != "./gradlew":
        return False
    if command == "uv" and len(argv) >= 3 and argv[1] == "run":
        nested = argv[2:]
        if nested and nested[0] == "--locked":
            nested = nested[1:]
        return auto_shell_allowed({"argv": nested}, root)
    if command in {"pwd", "echo"}:
        return True
    if command == "ls":
        return all(
            item.startswith("-")
            or (not Path(item).is_absolute() and (root / item).resolve().is_relative_to(root))
            for item in argv[1:]
        )
    if command in {"pytest", "mypy"}:
        return True
    if command == "ruff":
        if len(argv) < 2:
            return False
        if argv[1] == "check":
            return not any(flag.startswith("--fix") for flag in argv[2:]) and (
                "--unsafe-fixes" not in argv
            )
        return argv[1] == "format" and "--check" in argv
    if command in {"python", "python3"}:
        if len(argv) >= 3 and argv[1] == "-m":
            return argv[2] in {"pytest", "unittest", "compileall"}
        if len(argv) >= 2 and not argv[1].startswith("-"):
            script = Path(argv[1])
            return (
                not script.is_absolute()
                and script.suffix == ".py"
                and (root / script).resolve().is_relative_to(root)
            )
    if command == "mvn":
        return len(argv) >= 2 and argv[1] == "test"
    if command == "gradlew" and argv[0] == "./gradlew":
        return len(argv) >= 2 and argv[1] == "test"
    return False


class BoundaryError(ValueError):
    """A path escapes the configured workspace."""


class LocalToolDispatcher:
    """Validate, authorize, execute, and audit local tools."""

    def __init__(
        self,
        *,
        approve: Callable[[str, dict[str, Any]], bool] | None = None,
        timeout_seconds: float = 30,
        max_output_chars: int = 16_000,
        sandbox_shell: Callable[[ToolCall, AgentRequest], Awaitable[ToolResult]] | None = None,
    ) -> None:
        self.approve = approve
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self.sandbox_shell = sandbox_shell
        self.audit: list[tuple[str, str, str]] = []
        self.seen: set[tuple[str, str]] = set()
        self._prepared: dict[tuple[str, str], tuple[dict[str, Any], Path, Path]] = {}

    def definitions(self) -> tuple[dict[str, Any], ...]:
        """Return provider-neutral JSON schemas for the model."""
        return tuple(
            {
                "name": name,
                "description": (
                    "Delete one regular workspace file selected from the user's request and conversation; never directories"
                    if name == "delete_file"
                    else "Replace one exact string in an existing file; old may be empty only when the file is empty"
                    if name == "apply_patch"
                    else name.replace("_", " ")
                ),
                "parameters": schema.model_json_schema(),
            }
            for name, schema in TOOL_ARGS.items()
        )

    async def prepare(self, call: ToolCall, context: AgentRequest) -> ToolResult | None:
        """Validate and authorize a complete call before durable intent."""
        key = (context.run_id or context.task_id, call.call_id)
        if key in self.seen:
            return self._record(call, "error", "duplicate_call_id")
        self.seen.add(key)
        schema = TOOL_ARGS.get(call.name)
        if schema is None:
            return self._record(call, "error", "unknown_tool")
        try:
            args = schema.model_validate(call.arguments).model_dump()
        except ValidationError:
            return self._record(call, "error", "invalid_arguments")
        try:
            json.dumps(args, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            return self._record(call, "error", "invalid_unicode")
        root = Path(context.workspace).resolve(strict=True)
        try:
            path = (
                self._path(root, args.get("path", "."))
                if call.name
                in {"read_file", "list_files", "grep", "write_file", "apply_patch", "delete_file"}
                else root
            )
        except (BoundaryError, OSError):
            return self._record(call, "denied", "workspace_boundary")
        if call.name == "delete_file":
            relative = Path(args["path"])
            if not relative.parts or ".." in relative.parts or ".git" in relative.parts:
                return self._record(call, "denied", "protected_path")
            candidate = root
            for part in relative.parts:
                candidate /= part
                if candidate.is_symlink():
                    return self._record(call, "denied", "symlink_boundary")
            if not candidate.is_file():
                return self._record(call, "denied", "not_regular_file")
            path = candidate
        if call.name in WRITE_TOOLS | {"shell"}:
            if self.approve is None:
                return self._record(call, "denied", "approval_required")
            if not await asyncio.to_thread(self.approve, call.name, args):
                return self._record(call, "denied", "permission_denied")
        self._prepared[key] = (args, root, path)
        return None

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        """Execute an approved call, or prepare first for standalone use."""
        key = (context.run_id or context.task_id, call.call_id)
        if key not in self._prepared:
            early = await self.prepare(call, context)
            if early is not None:
                return early
        args, root, path = self._prepared.pop(key)
        if call.name == "shell" and self.sandbox_shell is not None:
            result = await self.sandbox_shell(call, context)
            self.audit.append((call.call_id, call.name, result.status))
            return result
        try:
            output, exit_code, stream_truncated = await self._execute(call.name, args, root, path)
        except TimeoutError:
            return self._record(call, "timeout", "timeout")
        except (OSError, UnicodeError, ValueError) as exc:
            return self._record(call, "error", type(exc).__name__)
        truncated = stream_truncated or len(output) > self.max_output_chars
        result = ToolResult(
            call.call_id,
            "ok" if exit_code in (None, 0) else "error",
            output[: self.max_output_chars],
            exit_code,
            truncated,
        )
        self.audit.append((call.call_id, call.name, result.status))
        return result

    def discard(self, call: ToolCall, context: AgentRequest) -> None:
        """Forget an authorized call when an outer safety policy replaces execution."""
        key = (context.run_id or context.task_id, call.call_id)
        self._prepared.pop(key, None)

    @staticmethod
    def _path(root: Path, user_path: str) -> Path:
        path = Path(user_path)
        if path.is_absolute():
            raise BoundaryError
        resolved = (root / path).resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise BoundaryError
        return resolved

    def _record(
        self,
        call: ToolCall,
        status: Literal["error", "denied", "timeout"],
        output: str,
    ) -> ToolResult:
        self.audit.append((call.call_id, call.name, status))
        return ToolResult(call.call_id, status, output)

    async def _execute(
        self, name: str, args: dict[str, Any], root: Path, path: Path
    ) -> tuple[str, int | None, bool]:
        if name == "read_file":
            with path.open(encoding="utf-8") as handle:
                return handle.read(self.max_output_chars + 1), None, False
        if name == "list_files":
            return "\n".join(sorted(child.name for child in path.iterdir())), None, False
        if name == "glob":
            matches = [
                str(p.relative_to(root))
                for p in root.glob(args["pattern"])
                if p.resolve().is_relative_to(root)
            ]
            return "\n".join(sorted(matches)), None, False
        if name == "write_file":
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as handle:
                handle.write(args["content"])
            return "created", None, False
        if name == "apply_patch":
            current = path.read_text(encoding="utf-8")
            if (not args["old"] and current) or (args["old"] and current.count(args["old"]) != 1):
                return "patch_conflict", 1, False
            updated = current.replace(args["old"], args["new"], 1) if args["old"] else args["new"]
            path.write_text(updated, encoding="utf-8")
            return "patched", None, False
        if name == "delete_file":
            path.unlink()
            return "deleted", None, False
        if name == "grep":
            return await self._process(["rg", "--", args["pattern"], str(path)], root)
        if name == "shell":
            return await self._process(args["argv"], root)
        if name == "git_status":
            return await self._process(["git", "status", "--short"], root)
        if name == "git_diff":
            return await self._process(["git", "diff", "--"], root)
        raise ValueError("unknown tool")

    async def _process(self, argv: list[str], root: Path) -> tuple[str, int, bool]:
        return await run_local_process(
            tuple(argv), root, self.timeout_seconds, self.max_output_chars
        )
