"""审批卡片：工具调用的三选一决策（允许一次/本次会话允许/拒绝）。

决策折叠成现有 approve(name, args) -> bool，不改 RunService._authorize：
deny/auto 策略仍由 _authorize 先行决定，本模块只处理 ask 策略下回调
到 CLI 的那一步。allow_session 只在内存、只对 (tool_name, workspace)
生效，会话结束即失效；不写磁盘、不跨会话共享。

注意：decide() 由工具分发器经 asyncio.to_thread 在工作线程调用，
卡片渲染与输入均用同步 API（Rich Console 自带锁；input() 直接读 stdin），
不得在这里用 prompt_toolkit（非线程安全）或 await。
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from rich.console import Console
from rich.panel import Panel

from deity.core.contracts.client import approval_args_summary
from deity.interfaces.cli.chat_render import render_diff

ApprovalDecision = Literal["allow_once", "allow_session", "deny"]

_MAX_DIFF_CHARS = 200_000


class SessionAllowlist:
    """会话级允许表：(tool_name, workspace) -> 已允许。仅内存。"""

    def __init__(self) -> None:
        self._allowed: set[tuple[str, str]] = set()

    def allow(self, tool_name: str, workspace: Path) -> None:
        """记录一次 allow_session 决策。"""
        self._allowed.add((tool_name, str(workspace.resolve())))

    def is_allowed(self, tool_name: str, workspace: Path) -> bool:
        """该工具在该工作区是否已被本次会话允许。"""
        return (tool_name, str(workspace.resolve())) in self._allowed

    def __bool__(self) -> bool:
        return bool(self._allowed)

    def __len__(self) -> int:
        return len(self._allowed)


class ApprovalCard:
    """一张审批卡片：工具名、参数摘要、diff/命令全文与风险提示。"""

    def __init__(self, tool_name: str, args: dict[str, Any], *, workspace: Path) -> None:
        self.tool_name = tool_name
        self.args = args
        self.workspace = workspace

    def body_lines(self) -> list[str]:
        """卡片正文：按工具类型展示最关键的执行内容。"""
        lines = [f"工具：{self.tool_name}"]
        if self.tool_name == "shell":
            command = self.args.get("command")
            if not isinstance(command, str):
                argv = self.args.get("argv")
                command = " ".join(argv) if isinstance(argv, list) else ""
            lines.append(f"命令：{command}")
            lines.append("风险：Shell 命令将在本机工作区执行。")
        elif self.tool_name == "write_file":
            path = str(self.args.get("path", ""))
            lines.append(f"文件：{path}")
            lines.extend(self._write_diff_lines(path))
            lines.append("风险：将修改工作区文件。")
        elif self.tool_name == "apply_patch":
            path = str(self.args.get("path", ""))
            lines.append(f"文件：{path}")
            old = self.args.get("old_text", "")
            new = self.args.get("new_text", "")
            if isinstance(old, str) and isinstance(new, str):
                lines.append(render_diff(old, new, path=path))
            lines.append("风险：将修改工作区文件。")
        elif self.tool_name == "delete_file":
            lines.append(f"文件：{self.args.get('path', '')}")
            lines.append("将删除该文件。")
            lines.append("风险：将修改工作区文件。")
        else:
            summary = approval_args_summary(self.args)
            if summary:
                lines.append(f"参数：{summary}")
            lines.append("风险：以当前权限执行该工具。")
        return lines

    def _write_diff_lines(self, path: str) -> list[str]:
        """write_file 的 diff：旧内容读盘，新内容取 args；过大则不展示。"""
        new = self.args.get("content", "")
        if not isinstance(new, str):
            return []
        old = ""
        try:
            candidate = (self.workspace / path).resolve()
            if candidate.is_relative_to(self.workspace.resolve()) and candidate.is_file():
                old = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            old = ""
        if not old:
            return ["新建文件。"]
        if len(old) > _MAX_DIFF_CHARS or len(new) > _MAX_DIFF_CHARS:
            return ["文件过大，未展示 diff。"]
        return [render_diff(old, new, path=path)]

    def render(self, console: Console) -> None:
        """把卡片打印到终端（工作线程调用亦安全）。"""
        console.print(
            Panel(
                "\n".join(self.body_lines()),
                title=f"审批 · {self.tool_name}",
                border_style="yellow",
            )
        )

    def prompt(
        self, console: Console, *, input_fn: Callable[[str], str] | None = None
    ) -> ApprovalDecision:
        """三选一：默认拒绝；输入异常（EOF/Ctrl-C）同样拒绝。"""
        console.print("[1] 允许一次  [2] 本次会话都允许  [3] 拒绝（默认）")
        read = input_fn or input
        try:
            choice = read("选择 [1/2/3]（默认拒绝）> ").strip()
        except (EOFError, KeyboardInterrupt):
            return "deny"
        if choice == "1":
            return "allow_once"
        if choice == "2":
            return "allow_session"
        return "deny"


class ApprovalController:
    """审批状态机：ask/auto/session 三态 + 会话级允许表。

    ask：每次 ask 策略调用都出卡片（allowlist 命中时除外）；
    session：用户对某工具选过 allow_session，该工具后续直接放行，
    未在 allowlist 里的工具仍出卡片；
    auto：/auto 模式，全部 ask 策略调用直接放行（deny 策略仍由
    _authorize 拒绝，与本控制器无关）。
    """

    def __init__(
        self,
        *,
        workspace: Path,
        console: Console | None = None,
        input_fn: Callable[[str], str] | None = None,
    ) -> None:
        self.workspace = workspace
        self.console = console or Console()
        self.allowlist = SessionAllowlist()
        self.mode: Literal["ask", "auto", "session"] = "ask"
        self._input_fn = input_fn

    def decide(self, name: str, args: dict[str, Any]) -> bool:
        """approve(name, args) -> bool 的实现：折叠卡片决策。"""
        if self.mode == "auto":
            return True
        if self.allowlist.is_allowed(name, self.workspace):
            return True
        card = ApprovalCard(name, args, workspace=self.workspace)
        card.render(self.console)
        decision = card.prompt(self.console, input_fn=self._input_fn)
        if decision == "allow_once":
            return True
        if decision == "allow_session":
            self.allowlist.allow(name, self.workspace)
            self.mode = "session"
            return True
        return False

    def request_auto(self) -> bool:
        """显式确认后进入自动接受模式；未确认则不进入。"""
        self.console.print(
            "自动接受模式将对本会话所有 ask 策略的工具调用直接放行"
            "（deny 策略仍拒绝），退出即恢复 ask。",
            style="yellow",
        )
        read = self._input_fn or input
        try:
            answer = (
                read("确认进入自动接受模式吗？输入 yes 确认，其他任意键取消 > ").strip().lower()
            )
        except (EOFError, KeyboardInterrupt):
            return False
        if answer != "yes":
            return False
        self.mode = "auto"
        self.console.print("已进入自动接受模式。输入 /normal 退出并恢复 ask。", style="yellow")
        return True

    def exit_auto(self) -> None:
        """退出自动接受模式：allowlist 非空时回到 session，否则回到 ask。"""
        self.mode = "session" if self.allowlist else "ask"


def make_approve(
    controller: ApprovalController,
) -> Callable[[str, dict[str, Any]], bool]:
    """把控制器折叠成 RunService.from_config 接受的 approve 回调。"""
    return controller.decide


__all__ = [
    "ApprovalCard",
    "ApprovalController",
    "ApprovalDecision",
    "SessionAllowlist",
    "make_approve",
]
