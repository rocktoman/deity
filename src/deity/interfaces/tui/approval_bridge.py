"""审批桥：把既有 ApprovalController 接到全屏工作台的审批面板（ADR-022）。

不复制审批语义：控制器本体、卡片正文、选项文案、会话允许表、shell
命令模式派生与持久规则落盘都用 CLI 的同一套实现。桥只做两件事：

1. 控制器渲染目标换成捕获用 Console（StringIO），卡片文本原样转交
   界面呈现；
2. 控制器的 input_fn 在工作线程里阻塞，直到界面面板回填选择（数字
   选项与 CLI 卡片一一对应；自动接受确认回填 "yes"/其他）。

界面退出时 abort()：阻塞中的 input_fn 抛 EOFError，控制器按既有
语义折叠为拒绝/取消，不留挂起线程。
"""

import io
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console

from deity.application.approval_rules import ApprovalRules
from deity.interfaces.cli.chat_approval import ApprovalController

_PATTERN_RE = re.compile(r"同类命令（(.+?)）")


@dataclass(frozen=True)
class PendingPrompt:
    """一次等待界面回填的提示：审批卡片或自动接受确认。"""

    kind: Literal["approval", "confirm"]
    text: str
    pattern: str | None = None
    allow_permanent: bool = False


class TuiApprovalBridge:
    """ApprovalController 的线程桥：decide 在工作线程，决策在界面线程。"""

    def __init__(
        self,
        *,
        workspace: Path,
        rules: ApprovalRules | None = None,
    ) -> None:
        self._buffer = io.StringIO()
        self._mark = 0
        capture = Console(file=self._buffer, legacy_windows=False, width=100)
        self.controller = ApprovalController(
            workspace=workspace,
            console=capture,
            input_fn=self._read,
            rules=rules,
        )
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._pending: PendingPrompt | None = None
        self._choice: str | None = None
        self._aborted = False
        # 界面侧回调：由 InteractiveApp 在构造后装配（经 call_from_thread 呈现）。
        self.on_prompt: Callable[[PendingPrompt], None] | None = None
        self.on_prompt_done: Callable[[], None] | None = None

    @property
    def pending(self) -> PendingPrompt | None:
        """当前等待回填的提示；无则 None。"""
        with self._lock:
            return self._pending

    def _drain(self) -> str:
        """取走捕获 Console 自上次以来的新增输出（卡片正文 + 选项行）。"""
        text = self._buffer.getvalue()[self._mark :]
        self._mark = self._buffer.tell()
        return text.strip("\n")

    def _read(self, prompt: str) -> str:
        """input_fn 实现：在工作线程阻塞，等界面回填后返回选择字符串。"""
        text = self._drain()
        kind: Literal["approval", "confirm"] = "confirm" if "自动接受" in prompt else "approval"
        match = _PATTERN_RE.search(text)
        pending = PendingPrompt(
            kind=kind,
            text=text,
            pattern=match.group(1) if match else None,
            allow_permanent="[4]" in text,
        )
        with self._lock:
            self._pending = pending
            self._choice = None
            self._event.clear()
        if self.on_prompt is not None:
            self.on_prompt(pending)
        while not self._event.wait(timeout=0.05):
            if self._aborted:
                raise EOFError("界面已退出")
        if self._aborted:
            raise EOFError("界面已退出")
        with self._lock:
            choice = self._choice or ""
            self._pending = None
        if self.on_prompt_done is not None:
            self.on_prompt_done()
        return choice

    def submit(self, choice: str) -> bool:
        """界面回填一次选择；无待决提示时返回 False。"""
        with self._lock:
            if self._pending is None:
                return False
            self._choice = choice
        self._event.set()
        return True

    def abort(self) -> None:
        """界面退出：让阻塞中的 input_fn 以 EOFError 收口（折叠为拒绝）。"""
        self._aborted = True
        self._event.set()

    def request_auto(self) -> bool:
        """进入自动接受模式（控制器自带显式确认，确认走本桥的 confirm 提示）。

        本方法会阻塞调用线程（其 input_fn 等界面回填），须从工作线程调用。
        """
        return self.controller.request_auto()


__all__ = ["PendingPrompt", "TuiApprovalBridge"]
