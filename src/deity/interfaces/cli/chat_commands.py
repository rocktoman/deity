"""斜杠命令注册表：Phase 1 只注册现状 7 个命令，只搬家，不加功能。

dispatch() 的逻辑从 main.py::_interactive() 的 if/elif 链原样搬运，
输出文本与语义（包括 pending_skill / pending_mcp_tool 只影响下一条任务）
与基线逐行一致；新命令在 Phase 3 再注册。
"""

import shlex
from dataclasses import dataclass, field

from rich.console import Console

from deity.application.plan_service import PlanService
from deity.application.run_service import RunService
from deity.core.runtime import BudgetExceeded, ModelFailure, ProtocolError
from deity.interfaces.cli.chat_input import PlainInput, PromptToolkitInput
from deity.interfaces.cli.plan import (
    authorize_dirty_targets,
    build_approved,
    confirm_build,
)

HELP_TEXT = (
    "/plan 进入持续计划模式；/build [计划ID] [--allow-dirty 路径] 执行已有计划；"
    "/normal 返回普通模式；/skill 选择 Skill；/mcp 查看并选择 MCP 工具；"
    "选择会提示模型在下一条任务中使用；/exit 或 /quit 退出"
)


def run_error_message(exc: Exception) -> str:
    """Turn internal runtime limits into actionable CLI messages."""
    if isinstance(exc, BudgetExceeded) and str(exc) == "max_turns":
        return "已达到最大运行轮次，模型仍未给出最终结论；请缩小任务范围或提高 max_turns。"
    return str(exc)


@dataclass
class ChatState:
    """一次交互会话的可变状态；plan_mode 与 pending_* 语义与基线一致。"""

    session_id: str
    plan_mode: bool = False
    pending_skill: str | None = None
    pending_mcp_tool: str | None = None
    exit_requested: bool = False


@dataclass
class ChatContext:
    """斜杠命令处理器的上下文：服务、模型名、会话状态与输出。"""

    service: RunService
    model_name: str
    state: ChatState
    console: Console = field(default_factory=Console)
    input: PlainInput | PromptToolkitInput = field(default_factory=PlainInput)


@dataclass(frozen=True)
class Command:
    """一条斜杠命令的注册信息：名、用法与一行说明。"""

    name: str
    usage: str
    description: str


COMMANDS: tuple[Command, ...] = (
    Command("/help", "/help", "查看可用命令"),
    Command("/plan", "/plan", "进入持续计划模式"),
    Command("/normal", "/normal", "返回普通模式"),
    Command("/build", "/build [计划ID] [--allow-dirty 路径]", "执行已有计划"),
    Command("/skill", "/skill [名称]", "选择 Skill（只影响下一条任务）"),
    Command("/mcp", "/mcp", "查看并选择 MCP 工具（只影响下一条任务）"),
    Command("/exit", "/exit 或 /quit", "退出交互"),
)


def command_names() -> tuple[str, ...]:
    """已注册的斜杠命令名（供补全器与 /help 使用）。"""
    return tuple(command.name for command in COMMANDS)


async def dispatch(prompt: str, ctx: ChatContext) -> bool:
    """处理一条输入；返回 True 表示已作为命令处理，不发给模型。"""
    if prompt in {"/exit", "/quit"}:
        ctx.state.exit_requested = True
        return True
    if prompt == "/help":
        ctx.console.print(HELP_TEXT)
        return True
    if prompt == "/plan":
        if not ctx.service.coding.plan_build_enabled or not ctx.service.coding.git_guard_enabled:
            ctx.console.print(
                "Plan/Build 未启用，请在配置中开启 plan_build_enabled 和 git_guard_enabled。"
            )
            return True
        ctx.state.plan_mode = True
        ctx.console.print("已进入计划模式。输入任务后将展示计划并询问是否构建。")
        return True
    if prompt == "/normal":
        ctx.state.plan_mode = False
        ctx.console.print("已返回普通模式。")
        return True
    if prompt == "/build" or prompt.startswith("/build "):
        await _handle_build(prompt, ctx)
        return True
    if prompt == "/skill" or prompt.startswith("/skill "):
        _handle_skill(prompt, ctx)
        return True
    if prompt == "/mcp":
        await _handle_mcp(ctx)
        return True
    if prompt.startswith("/"):
        ctx.console.print("未知命令。输入 /help 查看可用命令。")
        return True
    return False


async def _handle_build(prompt: str, ctx: ChatContext) -> None:
    """执行 /build：无 plan_id 时用选择器列出本会话可 Build 的计划。"""
    if not ctx.service.coding.plan_build_enabled or not ctx.service.coding.git_guard_enabled:
        ctx.console.print(
            "Plan/Build 未启用，请在配置中开启 plan_build_enabled 和 git_guard_enabled。"
        )
        return
    try:
        parts = shlex.split(prompt)[1:]
        dirty_paths: list[str] = []
        plan_id: str | None = None
        index = 0
        while index < len(parts):
            if parts[index] == "--allow-dirty":
                if index + 1 >= len(parts):
                    raise ValueError("--allow-dirty 需要文件路径")
                dirty_paths.append(parts[index + 1])
                index += 2
            elif plan_id is None:
                plan_id = parts[index]
                index += 1
            else:
                raise ValueError("/build 只接受一个计划 ID")
        planner = PlanService(ctx.service)
        if plan_id is None:
            candidates = await planner.buildable(ctx.state.session_id)
            selected = ctx.input.select(
                "当前会话待执行计划",
                [f"{item.plan_id} {item.draft.goal}" for item in candidates],
            )
            if selected is None:
                return
            artifact = candidates[selected]
        else:
            artifact = await planner.get_buildable(plan_id, ctx.state.session_id)
        if dirty_paths and any(
            path not in {name for step in artifact.draft.steps for name in step.files}
            for path in dirty_paths
        ):
            raise ValueError("--allow-dirty 路径必须属于计划目标")
        if confirm_build(artifact, tuple(dirty_paths)):
            allowed = await authorize_dirty_targets(
                ctx.service, artifact.plan_id, tuple(dirty_paths)
            )
            if allowed is not None:
                result = await build_approved(ctx.service, artifact.plan_id, allowed)
                ctx.console.print(result.final_text or "无最终回答")
                ctx.state.plan_mode = True
        else:
            ctx.console.print("已取消 Build，计划仍可再次查看和批准。")
    except (
        ValueError,
        OSError,
        KeyError,
        ModelFailure,
        ProtocolError,
        BudgetExceeded,
    ) as exc:
        ctx.console.print(f"Build 失败：{run_error_message(exc)}", style="red")


def _handle_skill(prompt: str, ctx: ChatContext) -> None:
    """执行 /skill：选中结果只影响下一条任务。"""
    skills = ctx.service.skills_list()
    names = [item["name"] for item in skills]
    requested = prompt.removeprefix("/skill").strip()
    selected = (
        names.index(requested)
        if requested in names
        else ctx.input.select(
            "可用 Skills", [f"{item['name']} [{item['source']}]" for item in skills]
        )
        if not requested
        else None
    )
    if selected is None:
        if requested:
            ctx.console.print(f"未找到 Skill：{requested}")
        return
    item = ctx.service.skills_inspect(names[selected])
    ctx.state.pending_skill = item["name"]
    ctx.console.print(
        f"已选择 {ctx.state.pending_skill} [{item['source']}]：{item['description']}\n"
        f"来源：{item['path']}\n下一条任务将提示模型加载此 Skill。"
    )


async def _handle_mcp(ctx: ChatContext) -> None:
    """执行 /mcp：只做发现，不调用业务工具；选中结果只影响下一条任务。"""
    servers = ctx.service.mcp_list()
    selected = ctx.input.select("已配置的 MCP servers", servers)
    if selected is None:
        return
    server_name = servers[selected].split()[0]
    if "[disabled]" in servers[selected]:
        ctx.console.print(f"{server_name} 已禁用；请在配置中开启 MCP。")
        return
    try:
        tools = await ctx.service.mcp_test(server_name)
    except (ValueError, OSError, TimeoutError) as exc:
        ctx.console.print(f"MCP 检查失败：{exc}", style="red")
        return
    except Exception as exc:  # noqa: BLE001 - keep an external server failure in this menu
        ctx.console.print(f"MCP 检查失败：{type(exc).__name__}", style="red")
        return
    tool_index = ctx.input.select(f"{server_name} 的工具（仅发现，未调用）", tools)
    if tool_index is not None:
        ctx.state.pending_mcp_tool = tools[tool_index]
        ctx.console.print(f"已选择 {ctx.state.pending_mcp_tool}。下一条任务将提示模型使用此工具。")


__all__ = [
    "COMMANDS",
    "HELP_TEXT",
    "ChatContext",
    "ChatState",
    "Command",
    "command_names",
    "dispatch",
    "run_error_message",
]
