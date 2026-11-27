"""TUI 工作台纯 Python 状态模型：不导入 Textual，100% 可单测。"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

DrawerTab = Literal["sessions", "files", "tasks"]

# 欢迎头徽记：神之眼（三角形 + 眼睛 + 光芒），字符绘制、对称极简。
WELCOME_EMBLEM = "   \\ | /\n    \\|/\n    / \\\n   / ◉ \\\n  /______\\"


def greeting_for_hour(hour: int) -> str:
    """按小时给时间问候语：5–12 早上好、12–17 下午好、17–22 晚上好、其余夜深了。"""
    if 5 <= hour < 12:
        return "早上好"
    if 12 <= hour < 17:
        return "下午好"
    if 17 <= hour < 22:
        return "晚上好"
    return "夜深了"


class ScrollThrottler:
    """流式滚动节流：间隔内的请求合并，到期由 flush_due 补滚。"""

    def __init__(self, interval: float = 0.15, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval
        self._clock = clock
        self._last: float | None = None
        self._pending = False

    def request(self) -> bool:
        """若允许立即滚动返回 True，否则记 pending 返回 False。"""
        now = self._clock()
        if self._last is None or now - self._last >= self._interval:
            self._last = now
            self._pending = False
            return True
        self._pending = True
        return False

    def flush_due(self) -> bool:
        """合并的尾随滚动到期时返回 True 并清 pending。"""
        if (
            self._pending
            and self._last is not None
            and self._clock() - self._last >= self._interval
        ):
            self._last = self._clock()
            self._pending = False
            return True
        return False

    def force(self) -> None:
        """终态强制滚动前调用：清 pending 并重置节流窗口。"""
        self._last = self._clock()
        self._pending = False


@dataclass
class ToolGroupState:
    """一组连续工具调用的阶段聚合状态（AGY 式折叠步骤组）。"""

    group_id: str
    title: str = "工具执行"
    tool_ids: list[str] = field(default_factory=list)
    completed: int = 0
    failed: int = 0
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    collapsed: bool = False

    def add_tool(self, call_id: str) -> None:
        """登记一个新工具调用 ID。"""
        if call_id not in self.tool_ids:
            self.tool_ids.append(call_id)

    def mark_completed(self, status: str, *, now: float | None = None) -> None:
        """记录一个工具完成及失败数，并在全部完成时记录结束时刻。"""
        self.completed += 1
        if status in {"error", "denied", "timeout", "cancelled"}:
            self.failed += 1
        if self.completed >= len(self.tool_ids):
            self.finished_at = time.monotonic() if now is None else now

    def elapsed(self, clock: Callable[[], float] = time.monotonic) -> float:
        """组内已耗时或总耗时（秒）。"""
        end = self.finished_at if self.finished_at is not None else clock()
        return max(0.0, end - self.started_at)

    def summary_label(self, clock: Callable[[], float] = time.monotonic) -> str:
        """阶段摘要文案：如 `工具执行 · 3 个工具 · 0.8s`。"""
        total = len(self.tool_ids)
        elapsed_str = f"{self.elapsed(clock):.1f}s"
        if self.completed < total:
            return f"{self.title} · {self.completed}/{total} 个工具 · {elapsed_str}"
        if self.failed > 0:
            return f"{self.title} · {total} 个工具（{self.failed} 失败）· {elapsed_str}"
        return f"{self.title} · {total} 个工具 · {elapsed_str}"


class InputQueue:
    """运行中用户预输入 FIFO 队列，带容量上限保护。"""

    def __init__(self, max_size: int = 10) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self._max_size = max_size
        self._items: deque[str] = deque()

    @property
    def size(self) -> int:
        return len(self._items)

    @property
    def max_size(self) -> int:
        return self._max_size

    def enqueue(self, text: str) -> bool:
        """压入一条非空任务；队列满或为空返回 False。"""
        cleaned = text.strip()
        if not cleaned or len(self._items) >= self._max_size:
            return False
        self._items.append(cleaned)
        return True

    def pop_next(self) -> str | None:
        """弹出下一条待执行任务；为空返回 None。"""
        if not self._items:
            return None
        return self._items.popleft()

    def clear(self) -> int:
        """清空队列并返回被清除的条数。"""
        count = len(self._items)
        self._items.clear()
        return count

    def items(self) -> tuple[str, ...]:
        """只读快照，用于界面提示。"""
        return tuple(self._items)


__all__ = [
    "WELCOME_EMBLEM",
    "DrawerTab",
    "InputQueue",
    "ScrollThrottler",
    "ToolGroupState",
    "greeting_for_hour",
]
