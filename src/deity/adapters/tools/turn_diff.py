"""Per-turn diff 追踪（ADR-018 A4）：写工具落盘前后的惰性内容快照。

TurnDiffDispatcher 叠在工具栈最外层：写调用 dispatch 前对目标文件
做首次快照（一轮内每文件只快照一次），AgentLoop 在轮末经
flush_turn_diff 取走本轮 unified diff。shell 的改动不在追踪范围。
"""

import difflib
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deity.adapters.tools.patch import patch_target_paths
from deity.core.contracts import AgentRequest, ToolCall, ToolResult

MAX_DIFF_CHARS = 8_000
_MAX_PREIMAGE_BYTES = 2_000_000
_WRITE_TOOLS = {"write_file", "apply_patch", "delete_file"}

# 快照三态：("text", 内容) / ("binary", sha256) / ("absent", None)
_Snapshot = tuple[str, Any]


def _read_snapshot(path: Path) -> _Snapshot:
    if not path.is_file() or path.is_symlink():
        return ("absent", None)
    data = path.read_bytes()
    try:
        return ("text", data.decode("utf-8"))
    except UnicodeDecodeError:
        return ("binary", hashlib.sha256(data).hexdigest())


class TurnDiffTracker:
    """一轮内被写文件的前像集合；flush 时与当前状态对比生成 payload。"""

    def __init__(self) -> None:
        self._before: dict[str, _Snapshot] = {}
        self._order: list[str] = []

    def snapshot(self, root: Path, raw_path: str) -> None:
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            return
        key = relative.as_posix()
        if key in self._before:
            return
        candidate = root / relative
        if not candidate.resolve(strict=False).is_relative_to(root):
            return
        self._before[key] = _read_snapshot(candidate)
        self._order.append(key)

    def flush(self, root: Path) -> dict[str, Any] | None:
        files: list[dict[str, Any]] = []
        for key in self._order:
            before_kind, before_value = self._before[key]
            after_kind, after_value = _read_snapshot(root / key)
            if (before_kind, before_value) == (after_kind, after_value):
                continue
            status = (
                "created"
                if before_kind == "absent"
                else "deleted"
                if after_kind == "absent"
                else "modified"
            )
            entry: dict[str, Any] = {"path": key, "status": status}
            if before_kind != "binary" and after_kind != "binary":
                before_text = before_value if before_kind == "text" else ""
                after_text = after_value if after_kind == "text" else ""
                diff = "\n".join(
                    difflib.unified_diff(
                        before_text.splitlines(),
                        after_text.splitlines(),
                        fromfile=f"a/{key}",
                        tofile=f"b/{key}",
                        lineterm="",
                    )
                )
                if len(diff) > MAX_DIFF_CHARS:
                    entry["diff"] = diff[:MAX_DIFF_CHARS]
                    entry["truncated"] = True
                else:
                    entry["diff"] = diff
            else:
                entry["before_sha256"] = (
                    before_value
                    if before_kind == "binary"
                    else hashlib.sha256(before_value.encode("utf-8")).hexdigest()
                    if before_kind == "text"
                    else None
                )
                entry["after_sha256"] = (
                    after_value
                    if after_kind == "binary"
                    else hashlib.sha256(after_value.encode("utf-8")).hexdigest()
                    if after_kind == "text"
                    else None
                )
            files.append(entry)
        self._before.clear()
        self._order.clear()
        if not files:
            return None
        return {"files": files, "files_changed": len(files)}


class TurnDiffDispatcher:
    """把 TurnDiffTracker 叠加到任意工具分发器之外。"""

    def __init__(
        self,
        inner: Any,
        *,
        on_preimage: Callable[[str, str, bytes | None], None] | None = None,
    ) -> None:
        self.inner = inner
        self._on_preimage = on_preimage
        self._trackers: dict[str, TurnDiffTracker] = {}

    def definitions(self) -> tuple[dict[str, Any], ...]:
        return getattr(self.inner, "definitions", lambda: ())()

    def supports_parallel(self, name: str) -> bool:
        check = getattr(self.inner, "supports_parallel", None)
        return bool(check(name)) if callable(check) else False

    async def prepare(self, call: ToolCall, context: AgentRequest) -> ToolResult | None:
        prepare = getattr(self.inner, "prepare", None)
        return await prepare(call, context) if callable(prepare) else None

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        targets = self._targets(call)
        if targets:
            tracker = self._trackers.setdefault(context.run_id, TurnDiffTracker())
            root = Path(context.workspace).resolve()
            for raw in targets:
                if self._on_preimage is not None:
                    self._capture_preimage(context.run_id, root, raw)
                tracker.snapshot(root, raw)
        result: ToolResult = await self.inner.dispatch(call, context)
        return result

    def _capture_preimage(self, run_id: str, root: Path, raw_path: str) -> None:
        assert self._on_preimage is not None
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            return
        key = relative.as_posix()
        candidate = (root / relative).resolve(strict=False)
        if not candidate.is_relative_to(root):
            return
        try:
            if not candidate.is_file() or candidate.is_symlink():
                self._on_preimage(run_id, key, None)
                return
            if candidate.stat().st_size <= _MAX_PREIMAGE_BYTES:
                self._on_preimage(run_id, key, candidate.read_bytes())
        except OSError:
            return

    async def after_persist(
        self, call: ToolCall, result: ToolResult, context: AgentRequest
    ) -> None:
        callback = getattr(self.inner, "after_persist", None)
        if callable(callback):
            await callback(call, result, context)

    def discard(self, call: ToolCall, context: AgentRequest) -> None:
        discard = getattr(self.inner, "discard", None)
        if callable(discard):
            discard(call, context)

    async def flush_turn_diff(self, context: AgentRequest) -> dict[str, Any] | None:
        """取走本轮 diff payload（一轮一次）；无变更返回 None。"""
        tracker = self._trackers.pop(context.run_id, None)
        if tracker is None:
            return None
        return tracker.flush(Path(context.workspace).resolve())

    @staticmethod
    def _targets(call: ToolCall) -> list[str]:
        if call.name not in _WRITE_TOOLS:
            return []
        if call.name in {"write_file", "delete_file"}:
            path = call.arguments.get("path")
            return [path] if isinstance(path, str) else []
        patch = call.arguments.get("patch")
        if isinstance(patch, str) and call.arguments.get("old") is None:
            return patch_target_paths(patch)
        path = call.arguments.get("path")
        return [path] if isinstance(path, str) else []


__all__ = ["MAX_DIFF_CHARS", "TurnDiffDispatcher", "TurnDiffTracker"]
