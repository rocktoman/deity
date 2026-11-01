"""Protect baseline and read preimages around local workspace writes."""

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from deity.adapters.git import SafePatchApplier
from deity.core.contracts import AgentRequest, ToolCall, ToolDispatcher, ToolResult
from deity.core.contracts.workspace import FileChange, PatchApplyStatus, WorkspaceSnapshot

_WRITE_TOOLS = {"write_file", "apply_patch", "delete_file"}


class GitGuardedDispatcher:
    """Enforce dirty-path and read-before-write rules before execution."""

    def __init__(
        self,
        delegate: ToolDispatcher,
        repository: Any,
        baseline: WorkspaceSnapshot,
        *,
        allow_dirty_paths: tuple[str, ...] = (),
        require_read_before_write: bool = True,
        change_set_id: str | None = None,
        plan_targets: tuple[str, ...] | None = None,
    ) -> None:
        self.delegate = delegate
        self.repository = repository
        self.baseline = baseline
        self.allow_dirty_paths = set(allow_dirty_paths)
        self.require_read_before_write = require_read_before_write
        self.change_set_id = change_set_id
        self.plan_targets = set(plan_targets) if plan_targets is not None else None
        self.run_id: str | None = None
        self.preimages: dict[tuple[str, str], str] = {}
        self._prepared: dict[tuple[str, str], dict[str, Any]] = {}
        self._pending_changes: dict[tuple[str, str], FileChange] = {}
        self._dirty = {
            item.path
            for item in baseline.files
            if baseline.git_head is not None and item.status not in {"!", "clean", "absent"}
        }
        self._baseline_paths = {item.path for item in baseline.files}
        self._submodules = {
            item.path for item in baseline.files if item.status.startswith("submodule")
        }

    def definitions(self) -> tuple[dict[str, Any], ...]:
        return getattr(self.delegate, "definitions", lambda: ())()

    async def prepare(self, call: ToolCall, context: AgentRequest) -> ToolResult | None:
        if call.name in _WRITE_TOOLS:
            path_value = call.arguments.get("path")
            if not isinstance(path_value, str):
                return ToolResult(call.call_id, "error", "invalid_arguments")
            try:
                path = self._path(context, path_value)
            except ValueError:
                return ToolResult(call.call_id, "denied", "workspace_boundary")
            relative = path.relative_to(Path(context.workspace).resolve()).as_posix()
            if self.plan_targets is not None and relative not in self.plan_targets:
                return ToolResult(call.call_id, "denied", "outside_approved_plan")
            if any(
                relative == submodule or relative.startswith(f"{submodule}/")
                for submodule in self._submodules
            ):
                return ToolResult(call.call_id, "denied", "submodule_boundary")
            if relative in self._dirty and relative not in self.allow_dirty_paths:
                return ToolResult(call.call_id, "denied", "initial_dirty_path")
            if call.name == "write_file":
                if path.exists() or relative in self._baseline_paths:
                    return ToolResult(call.call_id, "denied", "create_target_exists")
            else:
                expected = self.preimages.get((context.run_id, relative))
                if self.require_read_before_write and expected is None:
                    return ToolResult(call.call_id, "denied", "read_before_write_required")
                if expected is not None and self._sha(path) != expected:
                    return ToolResult(call.call_id, "denied", "preimage_mismatch")
            self._prepared[(context.run_id, call.call_id)] = json.loads(
                json.dumps(call.arguments, ensure_ascii=False)
            )
        prepare = getattr(self.delegate, "prepare", None)
        result = await prepare(call, context) if callable(prepare) else None
        if result is not None:
            self._prepared.pop((context.run_id, call.call_id), None)
        return result

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        key = (context.run_id, call.call_id)
        prepared = self._prepared.pop(key, None)
        if call.name in _WRITE_TOOLS:
            if prepared is None or prepared != call.arguments:
                return ToolResult(call.call_id, "denied", "prepared_arguments_changed")
            path = self._path(context, prepared["path"])
            relative = path.relative_to(Path(context.workspace).resolve()).as_posix()
            before = self._sha(path) if path.is_file() else None
            if call.name == "write_file" and path.exists():
                self._discard_delegate(call, context)
                return ToolResult(call.call_id, "denied", "create_target_exists")
            if call.name == "delete_file":
                expected = self.preimages.get((context.run_id, relative))
                if expected is None or before != expected:
                    self._discard_delegate(call, context)
                    return ToolResult(call.call_id, "denied", "preimage_mismatch")
            operation: Literal["create", "modify", "delete"]
            if call.name == "apply_patch":
                expected = self.preimages[(context.run_id, relative)]
                outcome = SafePatchApplier(Path(context.workspace)).apply(
                    relative,
                    prepared["old"],
                    prepared["new"],
                    expected_sha256=expected,
                )
                self._discard_delegate(call, context)
                if outcome.status != PatchApplyStatus.APPLIED:
                    return ToolResult(call.call_id, "error", outcome.error_code or outcome.status)
                result = ToolResult(call.call_id, "ok", "patched")
                after = outcome.after_sha256
                operation = "modify"
            else:
                result = await self.delegate.dispatch(call, context)
                after = self._sha(path) if path.is_file() else None
                operation = "create" if call.name == "write_file" else "delete"
            if result.status == "ok":
                self._pending_changes[key] = FileChange(
                    relative, operation, before, after, call.call_id
                )
            return result
        result = await self.delegate.dispatch(call, context)
        if call.name == "read_file" and result.status == "ok":
            path_value = call.arguments.get("path")
            if isinstance(path_value, str):
                try:
                    path = self._path(context, path_value)
                except ValueError:
                    pass
                else:
                    if path.is_file() and not path.is_symlink():
                        relative = path.relative_to(Path(context.workspace).resolve()).as_posix()
                        self.preimages[(context.run_id, relative)] = self._sha(path)
        return result

    async def after_persist(
        self, call: ToolCall, result: ToolResult, context: AgentRequest
    ) -> None:
        change = self._pending_changes.pop((context.run_id, call.call_id), None)
        if change is not None and result.status == "ok":
            self.run_id = context.run_id
            change_set_id = self.change_set_id
            if change_set_id is None:
                change_set_id = await self.repository.ensure_change_set(
                    context.run_id, self.baseline.snapshot_id
                )
                self.change_set_id = change_set_id
            await self.repository.record_file_change(change_set_id, context.run_id, change)
        callback = getattr(self.delegate, "after_persist", None)
        if callable(callback):
            await callback(call, result, context)

    @staticmethod
    def _path(context: AgentRequest, value: str) -> Path:
        root = Path(context.workspace).resolve(strict=True)
        relative = Path(value)
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or ".git" in relative.parts
        ):
            raise ValueError("protected path")
        candidate = root
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink():
                raise ValueError("symlink")
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise ValueError("boundary")
        return candidate

    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _discard_delegate(self, call: ToolCall, context: AgentRequest) -> None:
        current: Any = self.delegate
        visited: set[int] = set()
        while id(current) not in visited:
            visited.add(id(current))
            discard = getattr(current, "discard", None)
            if callable(discard):
                discard(call, context)
                return
            current = getattr(
                current,
                "delegate",
                getattr(current, "inner", getattr(current, "local", None)),
            )
            if current is None:
                return
