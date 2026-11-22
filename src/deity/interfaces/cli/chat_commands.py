"""斜杠命令注册表：Phase 3 增加 /resume /sessions /compact /clear /cost
/model /status /auto /doctor；/normal 退出自动接受模式。

命令只做 CLI 侧编排：恢复决策走 RunService.inspect()/resume()，
审批走 ApprovalController，压缩没有独立触发入口（见 /compact）。
"""

import json
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from rich.console import Console

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.config import KNOWN_MODELS
from deity.application.doctor import run_doctor
from deity.application.plan_service import PlanService
from deity.application.run_service import RunService
from deity.core.runtime import BudgetExceeded, ModelFailure, ProtocolError
from deity.interfaces.cli.chat_approval import ApprovalController
from deity.interfaces.cli.chat_input import PlainInput, PromptToolkitInput
from deity.interfaces.cli.chat_status import ChatStatus
from deity.interfaces.cli.chat_view_model import ChatViewModel
from deity.interfaces.cli.diff_render import DARK_DIFF_COLORS, format_diff_text
from deity.interfaces.cli.plan import (
    authorize_dirty_targets,
    build_approved,
    confirm_build,
)
from deity.interfaces.cli.sandbox import sandbox_status_lines
from deity.interfaces.cli.workspace_diff import git_branch, workspace_diff

# /review 的固定提示词（Codex /review 式）：审查未提交改动并逐项给结论。
REVIEW_PROMPT = (
    "审查当前工作区相对 HEAD 的未提交改动：逐文件列出问题（文件、行、"
    "严重程度、修复建议）；没有问题就明确说通过。可用 read_file、"
    "git_diff、grep 等工具查看改动。"
)


def run_error_message(exc: Exception) -> str:
    """Turn internal runtime limits into actionable CLI messages."""
    if isinstance(exc, BudgetExceeded) and str(exc) == "max_turns":
        return "已达到最大运行轮次，模型仍未给出最终结论；请缩小任务范围或提高 max_turns。"
    if isinstance(exc, ModelFailure) and exc.detail:
        return f"{exc.code}（{exc.detail}）"
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
    last_view: ChatViewModel | None = None


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
    Command("/fork", "/fork <运行ID>", "从某运行的最新前沿分叉新运行"),
    Command("/timeline", "/timeline <运行ID>", "查看某运行的证据时间线"),
    Command("/sessions", "/sessions", "列出并切换会话"),
    Command("/compact", "/compact", "查看上下文压缩状态"),
    Command("/clear", "/clear", "清屏（不删除 SQLite 历史）"),
    Command("/cost", "/cost", "查看本会话 token 与轮次"),
    Command("/usage", "/usage", "查看本会话用量汇总"),
    Command("/rename", "/rename <新标题>", "重命名当前会话"),
    Command("/sandbox", "/sandbox [read-only|workspace-write]", "查看或切换沙箱策略"),
    Command("/model", "/model [名称]", "查看或切换模型（仅本次会话）"),
    Command("/status", "/status", "查看会话状态"),
    Command("/tools", "/tools [call_id]", "查看上一轮工具调用（折叠列表/展开参数）"),
    Command("/auto", "/auto", "进入/退出自动接受模式"),
    Command("/doctor", "/doctor", "运行环境检查"),
    Command("/diff", "/diff", "查看工作区未提交改动"),
    Command("/new", "/new", "开始新会话"),
    Command("/review", "/review", "审查工作区未提交改动"),
    Command("/export", "/export", "导出当前会话为 Markdown 文件"),
    Command("/copy", "/copy", "复制上一轮回答（仅工作台）"),
    Command("/theme", "/theme [dark|light]", "切换工作台深浅主题（仅工作台）"),
    Command("/archive", "/archive", "归档当前会话并开始新会话"),
    Command("/exit", "/exit 或 /quit", "退出交互"),
)


def command_names() -> tuple[str, ...]:
    """已注册的斜杠命令名（供补全器与 /help 使用）。"""
    return tuple(command.name for command in COMMANDS)


# /help 的展示文本：由 COMMANDS 注册表生成，每条命令占一行，避免手写
# 文本与注册表漂移（旧版是分号连成的整段，终端里换行后难读）。
HELP_TEXT = "\n".join(f"{command.usage}  {command.description}" for command in COMMANDS)


def model_listing_lines(model_name: str, provider: str) -> list[str]:
    """` /model`（无参数）的展示行：当前模型 + 本提供方的可选模型列表。"""
    lines = [f"当前模型：{model_name}（提供方 {provider or '—'}）"]
    known = KNOWN_MODELS.get(provider or "", ())
    if known:
        lines.append(f"可选模型（{provider}）：")
        for name in known:
            lines.append(f"  ● {name}（当前）" if name == model_name else f"    {name}")
        lines.append("/model <名称> 切换（仅本次会话内生效）")
    else:
        lines.append("本提供方无固定模型列表（端点自定模型名），用 /model <名称> 直接切换。")
    return lines


async def dispatch(prompt: str, ctx: ChatContext) -> bool:
    """处理一条输入；返回 True 表示已作为命令处理，不发给模型。"""
    if prompt in {"/exit", "/quit"}:
        ctx.state.exit_requested = True
        return True
    if prompt == "/help":
        # markup=False：用法里的 [call_id] 等方括号是字面量，不能被 Rich 当样式吞掉。
        ctx.console.print(HELP_TEXT, markup=False)
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
        await _handle_skill(prompt, ctx)
        return True
    if prompt == "/mcp":
        await _handle_mcp(ctx)
        return True
    if prompt == "/resume" or prompt.startswith("/resume "):
        await _handle_resume(prompt, ctx)
        return True
    if prompt == "/fork" or prompt.startswith("/fork "):
        await _handle_fork(prompt, ctx)
        return True
    if prompt == "/timeline" or prompt.startswith("/timeline "):
        await _handle_timeline(prompt, ctx)
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
    if prompt == "/usage":
        ctx.console.print(ctx.status.usage_text(), highlight=False, markup=False)
        return True
    if prompt == "/rename" or prompt.startswith("/rename "):
        await _handle_rename(prompt, ctx)
        return True
    if prompt == "/sandbox" or prompt.startswith("/sandbox "):
        _handle_sandbox(prompt, ctx)
        return True
    if prompt == "/model" or prompt.startswith("/model "):
        _handle_model(prompt, ctx)
        return True
    if prompt == "/status":
        await _handle_status(ctx)
        return True
    if prompt == "/tools" or prompt.startswith("/tools "):
        _handle_tools(prompt, ctx)
        return True
    if prompt == "/auto":
        _handle_auto(ctx)
        return True
    if prompt == "/doctor":
        _handle_doctor(ctx)
        return True
    if prompt == "/diff":
        await _handle_diff(ctx)
        return True
    if prompt == "/new":
        _handle_new(ctx)
        return True
    if prompt == "/review":
        await _handle_review(ctx)
        return True
    if prompt == "/export":
        await _handle_export(ctx)
        return True
    if prompt == "/copy":
        ctx.console.print("复制仅在工作台中使用（/copy 在行式界面不可用）。")
        return True
    if prompt == "/theme" or prompt.startswith("/theme "):
        ctx.console.print("主题切换仅在工作台中使用（行式界面无主题）。")
        return True
    if prompt == "/archive":
        await _handle_archive(ctx)
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
            selected = await ctx.input.select_async(
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


async def _handle_skill(prompt: str, ctx: ChatContext) -> None:
    """执行 /skill：选中结果只影响下一条任务。"""
    skills = ctx.service.skills_list()
    names = [item["name"] for item in skills]
    requested = prompt.removeprefix("/skill").strip()
    selected = (
        names.index(requested)
        if requested in names
        else await ctx.input.select_async(
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
    selected = await ctx.input.select_async("已配置的 MCP servers", servers)
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
    tool_index = await ctx.input.select_async(f"{server_name} 的工具（仅发现，未调用）", tools)
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
    selected = await ctx.input.select_async(
        "可恢复的运行", [f"{item['id'][:8]} · {item['status']}" for item in resumable]
    )
    if selected is not None:
        await _resume_run(ctx, resumable[selected]["id"])


async def _handle_fork(prompt: str, ctx: ChatContext) -> None:
    """执行 /fork <run-id>：从该运行最新前沿分叉，打印新 run id 供 /resume 继续。"""
    parts = shlex.split(prompt)[1:]
    db_path = getattr(ctx.service, "db_path", None)
    if not parts or db_path is None:
        ctx.console.print("用法：/fork <run-id>")
        return
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        new_id = await repo.create_forked_run(parts[0])
    except (KeyError, ValueError, OSError) as exc:
        ctx.console.print(f"分叉失败：{exc}", style="red")
        return
    finally:
        await repo.close()
    ctx.console.print(
        f"已分叉：{new_id}（用 /resume {new_id} 继续执行）",
        highlight=False,
        markup=False,
    )


async def _handle_timeline(prompt: str, ctx: ChatContext) -> None:
    """执行 /timeline <run-id>：打印该运行的证据时间线（ADR-021，只读）。"""
    parts = shlex.split(prompt)[1:]
    db_path = getattr(ctx.service, "db_path", None)
    if not parts or db_path is None:
        ctx.console.print("用法：/timeline <run-id>")
        return
    from deity.application.timeline import format_timeline, run_timeline

    repo = await SQLiteSessionRepository.open(db_path)
    try:
        text = format_timeline(await run_timeline(repo, parts[0]))
    except (KeyError, ValueError, OSError) as exc:
        ctx.console.print(f"时间线不可用：{exc}", style="red")
        return
    finally:
        await repo.close()
    ctx.console.print(text, highlight=False, markup=False)


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
    selected = await ctx.input.select_async(
        "历史会话",
        [f"{item['id'][:8]} · {item['title'] or item['workspace']}" for item in sessions],
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
        provider = getattr(ctx.service, "provider_id", None) or ""
        ctx.console.print(
            "\n".join(model_listing_lines(ctx.state.model_name, provider)),
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
    branch = git_branch(ctx.workspace)
    status_lines = [
        f"模型：{ctx.state.model_name}",
        f"模式：{mode}",
        f"会话：{ctx.state.session_id[:8]}",
        f"工作区：{ctx.workspace}",
    ]
    if branch is not None:
        status_lines.append(f"分支：{branch}")
    status_lines.extend(
        [
            f"轮次：{ctx.status.turns} · 工具调用：{ctx.status.tool_calls} 次",
            f"Token：{ctx.status.token_text()}",
            f"耗时：{ctx.status.elapsed_seconds:.1f}s",
        ]
    )
    for line in status_lines:
        ctx.console.print(line, highlight=False, markup=False)
    count = await _context_message_count(ctx)
    ctx.console.print(
        f"上下文消息数：{count if count is not None else '—'}（最近 8 条口径）",
        highlight=False,
        markup=False,
    )
    if ctx.audit:
        ctx.console.print(f"审计拒绝：{len(ctx.audit)} 条")


def _handle_tools(prompt: str, ctx: ChatContext) -> None:
    """执行 /tools：折叠列出上一轮工具节点；带 call_id 时展开其完整参数。"""
    view = ctx.last_view
    if view is None or not view.tools:
        ctx.console.print("上一轮没有工具调用记录。", highlight=False, markup=False)
        return
    parts = prompt.split(maxsplit=1)
    if len(parts) == 2:
        call_id = parts[1].strip()
        node = view.tools.get(call_id)
        if node is None:
            ctx.console.print(f"未找到工具调用：{call_id}", highlight=False, markup=False)
            return
        view.toggle_expanded(call_id)
        ctx.console.print(
            f"{node.name}（{node.call_id}）· {node.status or '运行中'}",
            highlight=False,
            markup=False,
        )
        ctx.console.print(f"参数：{node.args_text or '—'}", highlight=False, markup=False)
        return
    for node in view.tools.values():
        summary = node.arguments_summary() or "—"
        ctx.console.print(
            f"{node.name}（{node.call_id}）· {node.status or '运行中'} · {summary}",
            highlight=False,
            markup=False,
        )
    ctx.console.print("展开完整参数：/tools <call_id>", highlight=False, markup=False)


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


async def _handle_diff(ctx: ChatContext) -> None:
    """执行 /diff（Codex 式）：工作区未提交改动的 unified diff + 未追踪文件。"""
    result = await workspace_diff(ctx.workspace)
    diff_text = str(result.get("diff", ""))
    untracked = result.get("untracked", [])
    if not diff_text and not untracked:
        ctx.console.print("工作区无未提交改动。", highlight=False, markup=False)
        return
    if diff_text:
        ctx.console.print(format_diff_text(diff_text, DARK_DIFF_COLORS), highlight=False)
    if untracked:
        ctx.console.print(f"未追踪文件（{len(untracked)}）：", highlight=False, markup=False)
        for path in untracked:
            ctx.console.print(f"  + {path}", highlight=False, markup=False)


def _handle_new(ctx: ChatContext) -> None:
    """执行 /new（Codex 式）：换新会话 id，回到普通模式。"""
    ctx.state.session_id = str(uuid4())
    ctx.state.plan_mode = False
    ctx.console.print("已开始新会话。", highlight=False, markup=False)


async def _handle_review(ctx: ChatContext) -> None:
    """执行 /review（Codex 式）：把审查当作一次普通任务提交，复用 run 通路。"""
    try:
        result = await ctx.service.run(
            REVIEW_PROMPT,
            model_name=ctx.state.model_name,
            session_id=ctx.state.session_id,
        )
    except (ModelFailure, ProtocolError, BudgetExceeded, ValueError, OSError) as exc:
        ctx.console.print(f"运行失败：{run_error_message(exc)}", style="red")
        return
    ctx.console.print(result.final_text or "无最终回答")


async def export_session_markdown(
    service: RunService, session_id: str, workspace: Path
) -> Path | None:
    """把会话消息导出为 Markdown 文件；无消息时返回 None（行式与工作台共用）。"""
    repo = await SQLiteSessionRepository.open(service.db_path)
    try:
        messages = await repo.messages(session_id)
    finally:
        await repo.close()
    if not messages:
        return None
    target = workspace / f"deity-export-{session_id[:8]}.md"
    lines = [
        "# Deity 会话导出",
        "",
        f"会话：{session_id} · 导出时间：{datetime.now(UTC).isoformat()}",
        "",
    ]
    for item in messages:
        role = str(item.get("role", ""))
        title = {"user": "## 用户", "assistant": "## 助手"}.get(role, f"## {role}")
        content = item.get("content", "")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        lines.extend([title, "", text, ""])
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


async def _handle_export(ctx: ChatContext) -> None:
    """执行 /export（Codex 式）：当前会话消息导出为 Markdown 文件。"""
    target = await export_session_markdown(ctx.service, ctx.state.session_id, ctx.workspace)
    if target is None:
        ctx.console.print("当前会话暂无消息可导出。", highlight=False, markup=False)
        return
    ctx.console.print(f"已导出：{target}", highlight=False, markup=False)


async def _handle_archive(ctx: ChatContext) -> None:
    """执行 /archive（Codex 式）：归档当前会话后开始新会话。"""
    repo = await SQLiteSessionRepository.open(ctx.service.db_path)
    try:
        await repo.archive_session(ctx.state.session_id, archived=True)
    finally:
        await repo.close()
    ctx.state.session_id = str(uuid4())
    ctx.state.plan_mode = False
    ctx.console.print("已归档当前会话，并开始新会话。", highlight=False, markup=False)


async def _handle_rename(prompt: str, ctx: ChatContext) -> None:
    """执行 /rename（ADR-023）：重命名当前会话；只改 sessions.title。"""
    title = prompt.removeprefix("/rename").strip()
    if not title:
        ctx.console.print("用法：/rename <新标题>", highlight=False, markup=False)
        return
    repo = await SQLiteSessionRepository.open(ctx.service.db_path)
    try:
        await repo.rename_session(ctx.state.session_id, title, workspace_root=str(ctx.workspace))
    except (KeyError, ValueError) as exc:
        ctx.console.print(f"重命名失败：{exc}", style="red", highlight=False, markup=False)
        return
    finally:
        await repo.close()
    ctx.console.print(f"已重命名当前会话为：{title.strip()[:80]}", highlight=False, markup=False)


def _handle_sandbox(prompt: str, ctx: ChatContext) -> None:
    """执行 /sandbox（ADR-023）：无参展示状态；有参切换本次会话策略。"""
    arg = prompt.removeprefix("/sandbox").strip()
    if not arg:
        for line in sandbox_status_lines(ctx.service):
            ctx.console.print(line, highlight=False, markup=False)
        return
    setter = getattr(ctx.service, "set_sandbox_policy", None)
    if setter is None:
        ctx.console.print("当前服务不支持沙箱策略切换。", style="red")
        return
    try:
        setter(arg)
    except ValueError as exc:
        ctx.console.print(f"切换失败：{exc}", style="red", highlight=False, markup=False)
        return
    ctx.console.print(
        f"已切换沙箱策略为：{arg}（仅本次会话内生效；写入配置文件 [sandbox] policy 可永久生效）。",
        highlight=False,
        markup=False,
    )


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
