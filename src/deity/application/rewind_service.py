"""时间旅行服务（ADR-025）：记录每轮文件前像与消息计数，支持 /undo 与 /rewind。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RewindPoint:
    """一次任务回合的回退检查点。"""

    point_id: int
    session_id: str
    run_id: str | None
    prompt_preview: str
    message_count_before: int
    file_backups: dict[str, bytes | None]


@dataclass(frozen=True)
class RewindResult:
    """一次 /undo 或 /rewind 操作的结果摘要。"""

    points_reverted: int
    restored_files: tuple[str, ...]
    deleted_files: tuple[str, ...]
    target_message_count: int


class RewindHistory:
    """按会话管理回退栈，并在工作区边界内安全恢复文件。"""

    def __init__(self, workspace: Path, *, max_points: int = 50) -> None:
        self._workspace = workspace.resolve(strict=False)
        self._max_points = max_points
        self._points: list[RewindPoint] = []
        self._next_id = 1

    def record(
        self,
        *,
        session_id: str,
        run_id: str | None,
        prompt: str,
        message_count_before: int,
        file_backups: dict[str, bytes | None],
    ) -> RewindPoint:
        """登记一条新检查点；超上限时丢弃最早记录。"""
        preview = " ".join(prompt.strip().split())[:60]
        point = RewindPoint(
            point_id=self._next_id,
            session_id=session_id,
            run_id=run_id,
            prompt_preview=preview,
            message_count_before=max(0, message_count_before),
            file_backups=dict(file_backups),
        )
        self._next_id += 1
        self._points.append(point)
        if len(self._points) > self._max_points:
            self._points.pop(0)
        return point

    def list_points(self, session_id: str) -> tuple[RewindPoint, ...]:
        """按时间顺序列出指定会话的所有检查点。"""
        return tuple(p for p in self._points if p.session_id == session_id)

    def clear_session(self, session_id: str) -> None:
        """清空指定会话的内存回退栈（如 /new 切换会话时）。"""
        self._points = [p for p in self._points if p.session_id != session_id]

    def undo_last(self, session_id: str) -> RewindResult | None:
        """回滚当前会话最近一轮文件改动；无可回退点时返回 None。"""
        session_points = self.list_points(session_id)
        if not session_points:
            return None
        return self.rewind_to(session_id, session_points[-1].point_id)

    def rewind_to(self, session_id: str, point_id: int) -> RewindResult | None:
        """逆序回滚从最新到 point_id（含）的所有检查点，恢复文件到该点执行前。"""
        session_points = self.list_points(session_id)
        target_idx = next(
            (idx for idx, p in enumerate(session_points) if p.point_id == point_id),
            None,
        )
        if target_idx is None:
            return None
        to_revert = list(reversed(session_points[target_idx:]))
        restored: set[str] = set()
        deleted: set[str] = set()
        for point in to_revert:
            for rel_path, before_bytes in point.file_backups.items():
                self._apply_backup(rel_path, before_bytes, restored, deleted)
        reverted_ids = {p.point_id for p in to_revert}
        self._points = [p for p in self._points if p.point_id not in reverted_ids]
        target_msg_count = session_points[target_idx].message_count_before
        return RewindResult(
            points_reverted=len(to_revert),
            restored_files=tuple(sorted(restored)),
            deleted_files=tuple(sorted(deleted)),
            target_message_count=target_msg_count,
        )

    def _apply_backup(
        self,
        rel_path: str,
        before_bytes: bytes | None,
        restored: set[str],
        deleted: set[str],
    ) -> None:
        relative = Path(rel_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            return
        candidate = (self._workspace / relative).resolve(strict=False)
        if not candidate.is_relative_to(self._workspace):
            return
        if before_bytes is None:
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink()
                deleted.add(rel_path)
                restored.discard(rel_path)
            return
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(before_bytes)
        restored.add(rel_path)
        deleted.discard(rel_path)


__all__ = ["RewindHistory", "RewindPoint", "RewindResult"]
