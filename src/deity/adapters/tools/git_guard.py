"""Protect baseline and read preimages around local workspace writes."""

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from deity.adapters.git import SafePatchApplier
from deity.adapters.tools.patch import (
    AddFile,
    DeleteFile,
    PatchError,
    UpdateFile,
    parse_patch,
    plan_patch,
)
from deity.core.contracts import AgentRequest, ToolCall, ToolDispatcher, ToolResult
from deity.core.contracts.workspace import FileChange, PatchApplyStatus, WorkspaceSnapshot

_WRITE_TOOLS = {"write_file", "apply_patch", "delete_file"}


def _patch_form(call: ToolCall) -> bool:
    """apply_patch 新形态（ADR-018）：带 patch 文本且无旧形态 old 字段。"""
    return (
        call.name == "apply_patch"
        and isinstance(call.arguments.get("patch"), str)
        and call.arguments.get("old") is None
    )


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
        self._pending_changes: dict[tuple[str, str], list[FileChange]] = {}
        self._patch_locks: dict[tuple[str, str], dict[str, str | None]] = {}
        self._dirty = {
            item.path
            for item in baseline.files
            if baseline.git_head is not None and item.status not in {"!", "clean", "absent"}
        }
        self._baseline_paths = {item.path for item in baseline.files if item.status != "absent"}
        self._submodules = {
            item.path for item in baseline.files if item.status.startswith("submodule")
        }

    def definitions(self) -> tuple[dict[str, Any], ...]:
        return getattr(self.delegate, "definitions", lambda: ())()

    async def prepare(self, call: ToolCall, context: AgentRequest) -> ToolResult | None:
        if call.name in _WRITE_TOOLS and _patch_form(call):
            failure = self._check_patch(call, context)
            if failure is not None:
                return failure
            self._prepared[(context.run_id, call.call_id)] = json.loads(
                json.dumps(call.arguments, ensure_ascii=False)
            )
            prepare = getattr(self.delegate, "prepare", None)
            result = await prepare(call, context) if callable(prepare) else None
            if result is not None:
                self._prepared.pop((context.run_id, call.call_id), None)
                self._patch_locks.pop((context.run_id, call.call_id), None)
            return result
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
            if call.name == "delete_file" and relative in self._dirty:
                return ToolResult(call.call_id, "denied", "initial_dirty_delete_denied")
            if call.name == "write_file":
                if path.exists() or relative in self._baseline_paths:
                    return ToolResult(call.call_id, "denied", "create_target_exists")
            else:
                expected = self.preimages.get((context.run_id, relative))
                if self.require_read_before_write and expected is None:
                    return ToolResult(call.call_id, "denied", "read_before_write_required")
                if expected is not None and (not path.is_file() or self._sha(path) != expected):
                    return ToolResult(call.call_id, "denied", "preimage_mismatch")
            self._prepared[(context.run_id, call.call_id)] = json.loads(
                json.dumps(call.arguments, ensure_ascii=False)
            )
        prepare = getattr(self.delegate, "prepare", None)
        result = await prepare(call, context) if callable(prepare) else None
        if result is not None:
            self._prepared.pop((context.run_id, call.call_id), None)
        return result

    def _check_patch(self, call: ToolCall, context: AgentRequest) -> ToolResult | None:
        """新形态 patch 的逐目标预检：与单路径写同一套规则，并锁定预像。"""
        try:
            hunks = parse_patch(call.arguments["patch"])
        except PatchError:
            return ToolResult(call.call_id, "error", "invalid_patch")
        root = Path(context.workspace).resolve()
        targets: list[tuple[str, str]] = []
        for hunk in hunks:
            kind = (
                "add"
                if isinstance(hunk, AddFile)
                else "delete"
                if isinstance(hunk, DeleteFile)
                else "update"
            )
            targets.append((hunk.path, kind))
            if isinstance(hunk, UpdateFile) and hunk.move_to is not None:
                targets.append((hunk.move_to, "add"))
        locks: dict[str, str | None] = {}
        seen: set[str] = set()
        for raw_path, kind in targets:
            if raw_path in seen:
                return ToolResult(call.call_id, "error", "invalid_patch")
            seen.add(raw_path)
            try:
                path = self._path(context, raw_path)
            except ValueError:
                return ToolResult(call.call_id, "denied", "workspace_boundary")
            relative = path.relative_to(root).as_posix()
            if self.plan_targets is not None and relative not in self.plan_targets:
                return ToolResult(call.call_id, "denied", "outside_approved_plan")
            if any(
                relative == submodule or relative.startswith(f"{submodule}/")
                for submodule in self._submodules
            ):
                return ToolResult(call.call_id, "denied", "submodule_boundary")
            if relative in self._dirty and relative not in self.allow_dirty_paths:
                return ToolResult(call.call_id, "denied", "initial_dirty_path")
            if kind == "delete" and relative in self._dirty:
                return ToolResult(call.call_id, "denied", "initial_dirty_delete_denied")
            if kind == "add":
                if path.exists() or relative in self._baseline_paths:
                    return ToolResult(call.call_id, "denied", "create_target_exists")
                locks[relative] = None
                continue
            expected = self.preimages.get((context.run_id, relative))
            if self.require_read_before_write and expected is None:
                return ToolResult(call.call_id, "denied", "read_before_write_required")
            current = self._sha(path) if path.is_file() else None
            if expected is not None and current != expected:
                return ToolResult(call.call_id, "denied", "preimage_mismatch")
            locks[relative] = current
        self._patch_locks[(context.run_id, call.call_id)] = locks
        return None

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        key = (context.run_id, call.call_id)
        prepared = self._prepared.pop(key, None)
        if call.name in _WRITE_TOOLS:
            if prepared is None or prepared != call.arguments:
                return ToolResult(call.call_id, "denied", "prepared_arguments_changed")
            if _patch_form(call):
                return self._dispatch_patch(call, context, key, prepared)
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
                self._pending_changes[key] = [
                    FileChange(relative, operation, before, after, call.call_id)
                ]
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

    def _dispatch_patch(
        self,
        call: ToolCall,
        context: AgentRequest,
        key: tuple[str, str],
        prepared: dict[str, Any],
    ) -> ToolResult:
        """新形态 patch 在守卫内落盘：预像复核 → 全量校验 → 逐文件写。"""
        locks = self._patch_locks.pop(key, {})
        root = Path(context.workspace).resolve()

        def read(relative: str) -> str | None:
            try:
                path = self._path(context, relative)
            except ValueError:
                raise PatchError(f"{relative}: 路径越出工作区") from None
            rel = path.relative_to(root).as_posix()
            current = self._sha(path) if path.is_file() else None
            if rel in locks and locks[rel] != current:
                raise PatchError(f"{relative}: preimage_mismatch")
            if not path.is_file():
                return None
            try:
                return path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise PatchError(f"{relative}: 不是 UTF-8 文本文件") from None

        try:
            changes = plan_patch(prepared["patch"], read)
        except PatchError as exc:
            self._discard_delegate(call, context)
            if "preimage_mismatch" in str(exc):
                return ToolResult(call.call_id, "denied", "preimage_mismatch")
            return ToolResult(call.call_id, "error", f"patch_invalid: {exc}")
        self._discard_delegate(call, context)
        file_changes: list[FileChange] = []
        for change in changes:
            source = self._path(context, change.path)
            relative = source.relative_to(root).as_posix()
            before = locks.get(relative)
            if change.kind == "delete":
                source.unlink()
                file_changes.append(FileChange(relative, "delete", before, None, call.call_id))
                continue
            destination = self._path(context, change.move_to) if change.move_to else source
            destination.parent.mkdir(parents=True, exist_ok=True)
            assert change.new_content is not None
            data = change.new_content.encode("utf-8")
            destination.write_bytes(data)
            after = hashlib.sha256(data).hexdigest()
            if change.move_to:
                dest_relative = destination.relative_to(root).as_posix()
                source.unlink()
                file_changes.append(FileChange(relative, "delete", before, None, call.call_id))
                file_changes.append(FileChange(dest_relative, "create", None, after, call.call_id))
            else:
                operation: Literal["create", "modify"] = (
                    "create" if change.kind == "add" else "modify"
                )
                file_changes.append(FileChange(relative, operation, before, after, call.call_id))
        self._pending_changes[key] = file_changes
        return ToolResult(call.call_id, "ok", f"patched {len(changes)} file(s)")

    def supports_parallel(self, name: str) -> bool:
        check = getattr(self.delegate, "supports_parallel", None)
        return bool(check(name)) if callable(check) else False

    async def after_persist(
        self, call: ToolCall, result: ToolResult, context: AgentRequest
    ) -> None:
        changes = self._pending_changes.pop((context.run_id, call.call_id), None)
        if changes and result.status == "ok":
            self.run_id = context.run_id
            change_set_id = self.change_set_id
            if change_set_id is None:
                change_set_id = await self.repository.ensure_change_set(
                    context.run_id, self.baseline.snapshot_id
                )
                self.change_set_id = change_set_id
            for change in changes:
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
