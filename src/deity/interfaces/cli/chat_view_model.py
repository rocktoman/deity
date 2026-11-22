"""CLI 视图模型（V1.1-M2）：把 live ClientEvent 折叠成状态树，渲染器只画树。

状态树 = 累积文本 + 工具节点表（有序）+ 计数状态（ChatStatus）+ 终态。
apply() 返回 ViewDelta 说明本次折叠改变了什么，StreamingRenderer 据此
增量绘制，不再自己维护事件语义（M2 前渲染器同时做折叠与绘制）。

工具节点默认折叠（expanded=False）：渲染器只画一行摘要；展开后可看
完整参数（见 /tools 命令与 StreamingRenderer.render_tool_details）。
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from deity.core.contracts.client import ClientEvent, approval_args_summary
from deity.interfaces.cli.chat_status import ChatStatus


@dataclass
class ToolNode:
    """一个工具调用节点：参数片段、状态与耗时都挂在节点上。"""

    call_id: str
    name: str
    fragments: list[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float | None = None
    status: str | None = None
    expanded: bool = False
    # ADR-023：live tool_call_completed 事件带的输出预览与退出码。
    output_preview: str = ""
    exit_code: int | None = None

    @property
    def args_text(self) -> str:
        """流式参数片段拼成的原始参数文本。"""
        return "".join(self.fragments)

    def arguments_summary(self) -> str:
        """参数摘要：去掉文件内容全文、超长截断（与审批摘要同一口径）。"""
        raw = self.args_text
        if not raw:
            return ""
        try:
            args = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw[:120]
        if not isinstance(args, dict):
            return raw[:120]
        summary = approval_args_summary(args)
        text = json.dumps(summary, ensure_ascii=False)
        return text[:120]

    def parsed_args(self) -> dict[str, object]:
        """解析后的参数字典；解析失败返回空字典。"""
        try:
            args = json.loads(self.args_text)
        except (json.JSONDecodeError, ValueError):
            return {}
        return args if isinstance(args, dict) else {}

    def display_label(self) -> str:
        """工具条目动词标签（Codex ExecCell 式）：动词 + 主要目标。

        read_file→`读 <path>`、grep→`搜 "<q>"`、list_files→`列 <path>`、
        shell→`$ <argv>`、write/apply_patch/delete→`写/改/删 <path>`；
        其余工具保持 `名(参数摘要)` 原样。参数缺失时回落到原样。
        """
        args = self.parsed_args()
        path = args.get("path") if isinstance(args.get("path"), str) else None
        if self.name == "read_file" and path:
            return f"读 {path}"
        if self.name == "list_files":
            return f"列 {path or '.'}"
        if self.name == "glob":
            pattern = args.get("pattern") if isinstance(args.get("pattern"), str) else None
            return f"找 {pattern}" if pattern else "找文件"
        if self.name == "grep":
            # SearchArgs：pattern（必填）+ path（默认 "."，不显示）。
            query = args.get("pattern") if isinstance(args.get("pattern"), str) else None
            if query:
                target = f"（位于 {path}）" if path and path != "." else ""
                return f"搜 “{query}”{target}"
            return "搜索"
        if self.name == "shell":
            argv = args.get("argv")
            if isinstance(argv, list) and argv:
                command = " ".join(str(item) for item in argv)
                return f"$ {command[:120]}"
            if isinstance(args.get("session"), str):
                return f"$ 会话 {args['session']}（轮询）"
            return "$ shell"
        if self.name == "write_file" and path:
            return f"写 {path}"
        if self.name == "apply_patch":
            patch = args.get("patch") if isinstance(args.get("patch"), str) else None
            if path:
                return f"改 {path}"
            if patch:
                return "改文件（补丁）"
            return "改文件"
        if self.name == "delete_file" and path:
            return f"删 {path}"
        summary = self.arguments_summary()
        return f"{self.name}({summary})" if summary else self.name

    def elapsed(self, clock: Callable[[], float]) -> float:
        """节点耗时：已完成用完成时刻，未完成用当前时刻。"""
        end = self.finished_at if self.finished_at is not None else clock()
        return max(0.0, end - self.started_at)


@dataclass
class ViewDelta:
    """一次 apply 的变化量：渲染器按 delta 增量绘制。"""

    text_appended: bool = False
    tool_started: ToolNode | None = None
    tool_completed: ToolNode | None = None
    terminal: bool = False
    diff: dict[str, object] | None = None


class ChatViewModel:
    """折叠 live 事件流为可渲染状态树；终态之后的事件一律丢弃（取消竞态）。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.status = ChatStatus(clock=clock)
        self.text: str = ""
        self.tools: dict[str, ToolNode] = {}
        self.diffs: list[dict[str, object]] = []
        self.run_id: str | None = None
        self.terminal_event: ClientEvent | None = None

    @property
    def terminal(self) -> bool:
        return self.terminal_event is not None

    @property
    def current_tool(self) -> ToolNode | None:
        """最近一个尚未完成的工具节点（状态条用）；无则 None。"""
        for node in reversed(tuple(self.tools.values())):
            if node.status is None:
                return node
        return None

    @property
    def final_text(self) -> str | None:
        """终态文本：优先终态事件的 final_text，否则用累积文本。"""
        if self.terminal_event is not None:
            raw = self.terminal_event.payload.get("final_text")
            return raw if isinstance(raw, str) else None
        return self.text

    @property
    def status_text(self) -> str:
        """终态状态字符串；未终态时为空串。"""
        if self.terminal_event is None:
            return ""
        return str(self.terminal_event.payload.get("status", ""))

    def apply(self, event: ClientEvent) -> ViewDelta:
        """应用一条事件并返回变化量；终态之后到达的事件返回空 delta。"""
        delta = ViewDelta()
        if self.terminal_event is not None:
            return delta
        if self.run_id is None:
            self.run_id = event.run_id
        if event.kind == "text_delta":
            self.text += str(event.payload.get("text", ""))
            delta.text_appended = True
        elif event.kind == "tool_call_started":
            call_id = str(event.payload.get("call_id", ""))
            started_node = ToolNode(
                call_id=call_id,
                name=str(event.payload.get("name", "")),
                started_at=self._clock(),
            )
            self.tools[call_id] = started_node
            delta.tool_started = started_node
        elif event.kind == "tool_call_arguments_delta":
            args_node = self.tools.get(str(event.payload.get("call_id", "")))
            if args_node is not None:
                args_node.fragments.append(str(event.payload.get("text", "")))
        elif event.kind == "tool_call_completed":
            self.status.update(event)
            call_id = str(event.payload.get("call_id", ""))
            completed_node = self.tools.get(call_id)
            if completed_node is None:
                completed_node = ToolNode(
                    call_id=call_id,
                    name=str(event.payload.get("name", "")),
                    started_at=self._clock(),
                )
                self.tools[call_id] = completed_node
            completed_node.status = str(event.payload.get("status", ""))
            completed_node.finished_at = self._clock()
            preview = event.payload.get("output_preview")
            completed_node.output_preview = preview if isinstance(preview, str) else ""
            exit_code = event.payload.get("exit_code")
            completed_node.exit_code = exit_code if isinstance(exit_code, int) else None
            delta.tool_completed = completed_node
        elif event.kind == "message_completed" or event.kind == "usage":
            self.status.update(event)
        elif event.kind == "turn_diff":
            # 每轮末的 unified diff（ADR-018 A4）：存入 diffs 供渲染器挂块。
            payload = event.payload if isinstance(event.payload, dict) else {}
            self.diffs.append(payload)
            delta.diff = payload
        elif event.kind == "run_status":
            self.terminal_event = event
            self.status.update(event)
            delta.terminal = True
        return delta

    def toggle_expanded(self, call_id: str) -> bool:
        """翻转一个工具节点的折叠状态；节点不存在返回 False。"""
        node = self.tools.get(call_id)
        if node is None:
            return False
        node.expanded = not node.expanded
        return True


__all__ = ["ChatViewModel", "ToolNode", "ViewDelta"]
