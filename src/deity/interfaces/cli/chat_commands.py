"""斜杠命令注册表：Phase 3 增加 /resume /sessions /compact /clear /cost
/model /status /auto /doctor；/normal 退出自动接受模式。

命令只做 CLI 侧编排：恢复决策走 RunService.inspect()/resume()，
审批走 ApprovalController，压缩没有独立触发入口（见 /compact）。
"""

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.doctor import run_doctor
from deity.application.plan_service import PlanService
from deity.application.run_service import RunService
from deity.core.runtime import BudgetExceeded, ModelFailure, ProtocolError
from deity.interfaces.cli.chat_approval import ApprovalController
from deity.interfaces.cli.chat_input import PlainInput, PromptToolkitInput
from deity.interfaces.cli.chat_status import ChatStatus
from deity.interfaces.cli.plan import (
    authorize_dirty_targets,
    build_approved,
    confirm_build,
)

HELP_TEXT = (
    "/plan 进入持续计划模式；/build [计划ID] [--allow-dirty 路径] 执行已有计划；"
    "/normal 返回普通模式；/skill 选择 Skill；/mcp 查看并选择 MCP 工具；"
    "选择会提示模型在下一条任务中使用；/resume [运行ID] 恢复中断的运行；"
    "/sessions 列出并切换会话；/compact 查看上下文压缩状态；/clear 清屏；"
    "/cost 查看本会话 token 与轮次；/model [名称] 查看或切换模型；"
    "/status 查看会话状态；/auto 进入/退出自动接受模式；"
    "/doctor 运行环境检查；/exit 或 /quit 退出"
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
    model_name: str = ""
    plan_mode: bool = False
    pending_skill: str | None = None
    pending_mcp_tool: str | None = None
    exit_requested: bool = False


@dataclass
class ChatContext:
    """斜杠命令处理器的上下文：服务、会话状态、审批控制器与输出。"""

    service: RunService
    state: ChatState
    console: Console = field(default_factory=Console)
    input: PlainInput | PromptToolkitInput = field(default_factory=PlainInput)
    approval: ApprovalController | None = None
    workspace: Path = field(default_factory=Path.cwd)
    config_path: Path | None = None
    status: ChatStatus = field(default_factory=ChatStatus)
    audit: list[dict[str, Any]] = field(default_factory=list)


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
    Command("/resume", "/resume [运行ID]", "恢复中断的运行"),
    Command("/sessions", "/sessions", "列出并切换会话"),
    Command("/compact", "/compact", "查看上下文压缩状态"),
    Command("/clear", "/clear", "清屏（不删除 SQLite 历史）"),
    Command("/cost", "/cost", "查看本会话 token 与轮次"),
    Command("/model", "/model [名称]", "查看或切换模型（仅本次会话）"),
    Command("/status", "/status", "查看会话状态"),
    Command("/auto", "/auto", "进入/退出自动接受模式"),
    Command("/doctor", "/doctor", "运行环境检查"),
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
        if ctx.approval is not None and ctx.approval.mode == "auto":
            ctx.approval.exit_auto()
            ctx.console.print("已退出自动接受模式，恢复 ask。")
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
    if prompt == "/resume" or prompt.startswith("/resume "):
        await _handle_resume(prompt, ctx)
        return True
    if prompt == "/sessions":
        await _handle_sessions(ctx)
        return True
    if prompt == "/compact":
        _handle_compact(ctx)
        return True
    if prompt == "/clear":
        ctx.console.clear()
        ctx.console.print("已清屏。")
        return True
    if prompt == "/cost":
        ctx.console.print(ctx.status.cost_text(), highlight=False, markup=False)
        return True
    if prompt == "/model" or prompt.startswith("/model "):
        _handle_model(prompt, ctx)
        return True
    if prompt == "/status":
        await _handle_status(ctx)
        return True
    if prompt == "/auto":
        _handle_auto(ctx)
        return True
    if prompt == "/doctor":
        _handle_doctor(ctx)
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


async def _resume_run(ctx: ChatContext, run_id: str) -> None:
    """恢复一个运行：决策与执行都走 RunService，失败只打印不退出会话。"""
    try:
        result = await ctx.service.resume(run_id)
    except (KeyError, ValueError, OSError) as exc:
        ctx.console.print(f"恢复失败：{run_error_message(exc)}", style="red")
        return
    ctx.console.print(result.final_text or "无最终回答")


async def _handle_resume(prompt: str, ctx: ChatContext) -> None:
    """执行 /resume：有参数直接恢复；无参数列出本会话最近的运行。"""
    parts = shlex.split(prompt)[1:]
    if parts:
        await _resume_run(ctx, parts[0])
        return
    db_path = getattr(ctx.service, "db_path", None)
    if db_path is None:
        ctx.console.print("请提供 run-id：/resume <run-id>")
        return
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        candidates = await repo.recent_runs(ctx.state.session_id)
    finally:
        await repo.close()
    if not candidates:
        ctx.console.print("本会话暂无运行记录。")
        return
    inspect = getattr(ctx.service, "inspect", None)
    resumable: list[dict[str, str]] = []
    for candidate in candidates:
        decision = "—"
        reason = ""
        if inspect is not None:
            try:
                report = await inspect(candidate["id"])
                decision = str(report.get("decision", "—"))
                reason = str(report.get("reason", ""))
            except (KeyError, ValueError, OSError):
                decision = "unknown"
        ctx.console.print(
            f"{candidate['id'][:8]} · {candidate['status']} · {decision}"
            + (f"（{reason}）" if reason else ""),
            highlight=False,
            markup=False,
        )
        if decision == "resume":
            resumable.append(candidate)
    if not resumable:
        ctx.console.print("最近运行均不可恢复。")
        return
    selected = ctx.input.select(
        "可恢复的运行", [f"{item['id'][:8]} · {item['status']}" for item in resumable]
    )
    if selected is not None:
        await _resume_run(ctx, resumable[selected]["id"])


async def _handle_sessions(ctx: ChatContext) -> None:
    """执行 /sessions：列出历史会话，选中后切换并清空计划模式与待用选择。"""
    db_path = getattr(ctx.service, "db_path", None)
    if db_path is None:
        ctx.console.print("当前服务未提供会话存储，无法列出会话。")
        return
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        sessions = await repo.sessions()
    finally:
        await repo.close()
    if not sessions:
        ctx.console.print("暂无历史会话。")
        return
    selected = ctx.input.select(
        "历史会话",
        [f"{item['id'][:8]} · {item['workspace']}" for item in sessions],
    )
    if selected is None:
        return
    chosen = sessions[selected]
    ctx.state.session_id = chosen["id"]
    ctx.state.plan_mode = False
    ctx.state.pending_skill = None
    ctx.state.pending_mcp_tool = None
    ctx.console.print(
        f"已切换到会话 {chosen['id'][:8]}（{chosen['workspace']}）。计划模式与待用选择已清空。",
        highlight=False,
        markup=False,
    )


def _handle_compact(ctx: ChatContext) -> None:
    """执行 /compact：压缩没有独立触发入口，只报告状态（降级实现）。

    compaction_enabled 只是每轮 Run 的配置旗标，由 AgentLoop 在运行内
    做上下文压缩；本命令不删 SQLite 历史、不伪造一次压缩。
    """
    limits = getattr(ctx.service, "limits", None)
    enabled = bool(limits is not None and limits.compaction_enabled)
    if enabled:
        ctx.console.print(
            "上下文压缩已开启（[run] compaction_enabled = true），"
            "在每轮运行时自动执行；没有独立的触发入口。"
        )
    else:
        ctx.console.print(
            "上下文压缩未开启（[run] compaction_enabled = false）。"
            "/compact 没有独立的触发入口；开启后压缩在每轮运行时自动执行。"
        )


def _handle_model(prompt: str, ctx: ChatContext) -> None:
    """执行 /model：无参数查看当前模型，有参数切换（仅本次会话内生效）。"""
    requested = prompt.removeprefix("/model").strip()
    if not requested:
        provider = getattr(ctx.service, "provider_id", None) or "—"
        ctx.console.print(
            f"当前模型：{ctx.state.model_name}（提供方 {provider}）",
            highlight=False,
            markup=False,
        )
        return
    ctx.state.model_name = requested
    ctx.console.print(
        f"已切换模型：{requested}（仅本次会话内生效，下轮任务生效）。",
        highlight=False,
        markup=False,
    )


async def _context_message_count(ctx: ChatContext) -> int | None:
    """上下文消息数（repo.messages() 的计数口径：最近 8 条中的 user/assistant）。"""
    db_path = getattr(ctx.service, "db_path", None)
    if db_path is None:
        return None
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        messages = await repo.messages(ctx.state.session_id)
    finally:
        await repo.close()
    return len([item for item in messages[-8:] if item.get("role") in {"user", "assistant"}])


async def _handle_status(ctx: ChatContext) -> None:
    """执行 /status：模型、模式、会话、轮次、token、上下文消息数。"""
    if ctx.state.plan_mode:
        mode = "计划"
    elif ctx.approval is not None and ctx.approval.mode == "auto":
        mode = "自动接受"
    else:
        mode = "普通"
    for line in (
        f"模型：{ctx.state.model_name}",
        f"模式：{mode}",
        f"会话：{ctx.state.session_id[:8]}",
        f"工作区：{ctx.workspace}",
        f"轮次：{ctx.status.turns} · 工具调用：{ctx.status.tool_calls} 次",
        f"Token：{ctx.status.token_text()}",
        f"耗时：{ctx.status.elapsed_seconds:.1f}s",
    ):
        ctx.console.print(line, highlight=False, markup=False)
    count = await _context_message_count(ctx)
    ctx.console.print(
        f"上下文消息数：{count if count is not None else '—'}（最近 8 条口径）",
        highlight=False,
        markup=False,
    )
    if ctx.audit:
        ctx.console.print(f"审计拒绝：{len(ctx.audit)} 条")


def _handle_auto(ctx: ChatContext) -> None:
    """执行 /auto：已是 auto 时退出；权限全 auto 时直接进入；否则显式确认。"""
    approval = ctx.approval
    if approval is None:
        ctx.console.print("审批控制器未初始化，无法切换自动接受模式。")
        return
    if approval.mode == "auto":
        approval.exit_auto()
        ctx.console.print("已退出自动接受模式，恢复 ask。")
        return
    permission = getattr(ctx.service, "permission", None)
    if (
        permission is not None
        and permission.shell == "auto"
        and permission.workspace_write == "auto"
    ):
        approval.mode = "auto"
        ctx.console.print("权限已全部为 auto（配置），自动接受模式已生效；输入 /normal 退出。")
        return
    if not approval.request_auto():
        ctx.console.print("未进入自动接受模式。")


def _handle_doctor(ctx: ChatContext) -> None:
    """执行 /doctor：复用 deity doctor 的检查逻辑，只打印不退出会话。"""
    config_path = ctx.config_path or Path.home() / ".config/deity/config.toml"
    report = run_doctor(config_path, ctx.workspace)
    for check in report.checks:
        ctx.console.print(
            f"{check.name}: {check.status} {check.detail}", highlight=False, markup=False
        )
    if report.failed:
        ctx.console.print("doctor 检查有 fail 项。", style="red")


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
