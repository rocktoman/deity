"""TUI 斜杠命令注册表与命令调度器（ADR-024 拆分自 interactive.py）。"""

import re
import shlex
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from deity.adapters.code_intelligence.repo_map import RepoMapBuilder
from deity.adapters.model.router import RoutedModelProvider
from deity.adapters.storage import SQLiteSessionRepository
from deity.application.config import KNOWN_MODELS
from deity.application.custom_commands import CustomCommandCatalog
from deity.application.doctor import run_doctor
from deity.application.replay import TraceReplayer
from deity.application.rewind_service import RewindHistory
from deity.application.run_service import RunService
from deity.application.timeline import run_timeline
from deity.core.runtime import BudgetExceeded, ModelFailure, ProtocolError
from deity.interfaces.cli.chat_commands import (
    HELP_TEXT,
    REVIEW_PROMPT,
    ChatState,
    export_session_markdown,
    model_listing_lines,
    run_error_message,
)
from deity.interfaces.cli.chat_status import ChatStatus
from deity.interfaces.cli.chat_view_model import ChatViewModel
from deity.interfaces.cli.plan import build_approved
from deity.interfaces.cli.sandbox import sandbox_status_lines
from deity.interfaces.cli.workspace_diff import git_branch, workspace_diff

_CLI_ONLY_COMMANDS = ("/fork",)
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")

AVAILABLE_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/help", "查看命令帮助与工作台按键"),
    ("/plan", "进入计划模式（建计划 → 审批 → 构建）"),
    ("/build", "执行已批准的计划"),
    ("/normal", "返回普通交互模式"),
    ("/auto", "进入自动接受模式（免审批）"),
    ("/model", "查看或切换当前模型"),
    ("/status", "查看会话状态与 Token 统计"),
    ("/cost", "查看当前会话 Token 消耗详情"),
    ("/usage", "查看本会话用量汇总"),
    ("/rename", "重命名当前会话"),
    ("/sandbox", "查看或切换沙箱策略"),
    ("/tools", "展开或切换工具调用详情"),
    ("/skills", "查看已配置与已启用的 Skills"),
    ("/mcp", "查看已配置的 MCP 工具服务器"),
    ("/sessions", "列出历史会话或切换会话"),
    ("/resume", "从最近检查点恢复 Run"),
    ("/doctor", "运行环境与配置检查"),
    ("/clear", "清屏对话区"),
    ("/compact", "压缩当前会话历史上下文"),
    ("/undo", "撤回上一轮文件改动与对话记录"),
    ("/rewind", "查看或回退到历史检查点"),
    ("/map", "生成并查看当前工作区代码地图"),
    ("/route", "查看模型路由与故障降级状态"),
    ("/ps", "查看运行中的后台 Shell 会话"),
    ("/kill", "终止指定名称的后台 Shell 会话"),
    ("/timeline", "查看运行的证据时间线"),
    ("/replay", "只读回放历史运行的事件与终态"),
    ("/eval-save", "将最近一次运行沉淀为评估用例模板"),
    ("/diff", "查看工作区未提交改动"),
    ("/new", "开始新会话"),
    ("/review", "审查工作区未提交改动"),
    ("/export", "导出当前会话为 Markdown 文件"),
    ("/copy", "复制上一轮回答到剪贴板"),
    ("/theme", "切换工作台深浅主题"),
    ("/archive", "归档当前会话并开始新会话"),
    ("/drawer", "打开或关闭右侧辅助抽屉"),
    ("/exit", "退出 Deity 工作台"),
]


class WorkbenchHost(Protocol):
    """CommandDispatcher 依赖的工作台最小宿主接口。"""

    service: RunService
    state: ChatState
    session_status: ChatStatus
    custom_commands: CustomCommandCatalog
    rewind_history: RewindHistory

    @property
    def workspace(self) -> Path: ...

    @property
    def config_path(self) -> Path | None: ...

    @property
    def run_active(self) -> bool: ...

    @property
    def last_run_id(self) -> str | None: ...

    @property
    def last_plan_id(self) -> str | None: ...

    @property
    def last_view(self) -> ChatViewModel | None: ...

    def mode_label(self) -> str: ...
    def append_system(self, text: str) -> None: ...
    def append_error(self, text: str) -> None: ...
    def append_section_header(self, title: str, subtitle: str = "") -> None: ...
    def mount_raw_diff(self, diff_text: str) -> None: ...
    async def append_assistant_final(self, text: str) -> None: ...
    async def clear_conversation(self, *, mount_welcome: bool) -> None: ...
    def refresh_status(self) -> None: ...
    def enter_plan(self) -> bool: ...
    def enter_normal(self) -> None: ...
    def toggle_auto(self) -> None: ...
    def toggle_tool_node(self, call_id: str) -> None: ...
    def apply_session_switch(self, session_id: str) -> None: ...
    async def refresh_drawer_sessions(self) -> None: ...
    def toggle_drawer(self) -> None: ...
    def switch_theme(self, arg: str) -> None: ...
    def copy_text(self, text: str) -> None: ...
    def open_model_picker(self) -> None: ...
    def start_review(self, prompt: str) -> None: ...
    def submit_expanded_task(self, prompt: str) -> None: ...
    def start_run_task(self, coro: Any) -> None: ...
    async def authorize_dirty(
        self, plan_id: str, allow_dirty: tuple[str, ...]
    ) -> tuple[str, ...] | None: ...
    async def quit_app(self) -> None: ...


class CommandDispatcher:
    """工作台斜杠命令处理器：与界面容器解耦。"""

    def __init__(self, host: WorkbenchHost) -> None:
        self._host = host

    async def dispatch(self, prompt: str) -> None:
        host = self._host
        if prompt in {"/exit", "/quit"}:
            await host.quit_app()
            return
        if prompt == "/help":
            host.append_section_header("命令", "斜杠命令与工作台按键")
            custom = host.custom_commands.list_commands()
            extra = ""
            if custom:
                custom_lines = "\n".join(
                    f"{item.slash_name}  [{item.source}] {item.description}" for item in custom
                )
                extra = f"\n\n自定义命令（.deity/commands）：\n{custom_lines}"
            host.append_system(
                HELP_TEXT + "\n工作台按键：Enter 提交 · Ctrl+J 换行 · Ctrl+P 命令面板"
                " · Ctrl+B 辅助抽屉 · Shift+Tab 切换模式 · Ctrl+C 取消/退出 · Ctrl+Q 退出" + extra
            )
            return
        if prompt == "/drawer":
            host.toggle_drawer()
            return
        if prompt == "/status":
            await self._command_status()
            return
        if prompt == "/cost":
            host.append_system(host.session_status.cost_text())
            return
        if prompt == "/usage":
            host.append_system(host.session_status.usage_text())
            return
        if prompt == "/rename" or prompt.startswith("/rename "):
            host.start_run_task(self._command_rename(prompt.removeprefix("/rename").strip()))
            return
        if prompt == "/sandbox" or prompt.startswith("/sandbox "):
            self._command_sandbox(prompt.removeprefix("/sandbox").strip())
            return
        if prompt == "/tools" or prompt.startswith("/tools "):
            self._command_tools(prompt)
            return
        if prompt == "/clear":
            await host.clear_conversation(mount_welcome=False)
            host.append_system("已清屏。")
            return
        if prompt == "/compact":
            await self._command_compact()
            return
        if prompt == "/undo":
            await self._command_undo()
            return
        if prompt == "/rewind" or prompt.startswith("/rewind "):
            await self._command_rewind(prompt.removeprefix("/rewind").strip())
            return
        if prompt == "/map":
            self._command_map()
            return
        if prompt == "/route":
            self._command_route()
            return
        if prompt == "/ps":
            self._command_ps()
            return
        if prompt == "/kill" or prompt.startswith("/kill "):
            await self._command_kill(prompt.removeprefix("/kill").strip())
            return
        if prompt == "/timeline" or prompt.startswith("/timeline "):
            await self._command_timeline(prompt.removeprefix("/timeline").strip())
            return
        if prompt == "/replay" or prompt.startswith("/replay "):
            self._command_replay(prompt.removeprefix("/replay").strip())
            return
        if prompt == "/eval-save" or prompt.startswith("/eval-save "):
            await self._command_eval_save(prompt.removeprefix("/eval-save").strip())
            return
        if prompt == "/plan":
            host.enter_plan()
            return
        if prompt == "/normal":
            host.enter_normal()
            return
        if prompt == "/model" or prompt.startswith("/model "):
            requested = prompt.removeprefix("/model").strip()
            if not requested:
                known = KNOWN_MODELS.get(host.service.provider_id or "", ())
                if not known:
                    host.append_system(
                        "\n".join(
                            model_listing_lines(
                                host.state.model_name, host.service.provider_id or ""
                            )
                        )
                    )
                else:
                    host.open_model_picker()
            else:
                host.state.model_name = requested
                host.append_system(f"已切换模型：{requested}（仅本次会话内生效，下轮任务生效）。")
                host.refresh_status()
            return
        if prompt == "/auto":
            host.toggle_auto()
            return
        if prompt == "/doctor":
            report = run_doctor(
                host.config_path or Path.home() / ".config/deity/config.toml", host.workspace
            )
            lines = [f"{check.name}: {check.status} {check.detail}" for check in report.checks]
            host.append_system("\n".join(lines))
            return
        if prompt == "/sessions" or prompt.startswith("/sessions "):
            await self._command_sessions(prompt)
            return
        if prompt == "/build" or prompt.startswith("/build "):
            await self._command_build(prompt)
            return
        if prompt == "/resume" or prompt.startswith("/resume "):
            await self._command_resume(prompt)
            return
        if prompt in {"/skills", "/skill"} or prompt.startswith(("/skills ", "/skill ")):
            self._command_skills()
            return
        if prompt == "/mcp" or prompt.startswith("/mcp "):
            self._command_mcp()
            return
        if prompt == "/diff":
            host.start_run_task(self._command_diff())
            return
        if prompt == "/new":
            host.start_run_task(self._command_new())
            return
        if prompt == "/review":
            if host.run_active:
                host.append_system("已有任务在运行中，请等待完成后再审查。")
                return
            host.start_review(REVIEW_PROMPT)
            return
        if prompt == "/export":
            host.start_run_task(self._command_export())
            return
        if prompt == "/copy":
            self._command_copy()
            return
        if prompt == "/theme" or prompt.startswith("/theme "):
            host.switch_theme(prompt.removeprefix("/theme").strip())
            return
        if prompt == "/archive":
            host.start_run_task(self._command_archive())
            return
        if prompt.split(maxsplit=1)[0] in _CLI_ONLY_COMMANDS:
            host.append_system("该命令请在行式 CLI 中使用（非 TTY 或 deity run 的行式界面）。")
            return
        if prompt.startswith("/"):
            cmd_token, _, args = prompt.partition(" ")
            custom_cmd = host.custom_commands.get(cmd_token)
            if custom_cmd is not None:
                rendered = custom_cmd.render(args)
                host.append_system(f"执行自定义命令 {custom_cmd.slash_name} [{custom_cmd.source}]…")
                host.submit_expanded_task(rendered)
                return
            host.append_system("未知命令。输入 /help 查看可用命令。")

    async def _command_undo(self) -> None:
        host = self._host
        if host.run_active:
            host.append_system("当前有 Run 在运行；请等待完成或取消后再撤回。")
            return
        result = host.rewind_history.undo_last(host.state.session_id)
        if result is None:
            host.append_system("当前会话暂无可撤回的历史检查点。")
            return
        await self._truncate_session_messages(result.target_message_count)
        host.append_system(self._format_rewind_summary("已撤回上一轮", result))
        host.refresh_status()

    def _command_map(self) -> None:
        host = self._host
        result = RepoMapBuilder(host.workspace).build()
        if not result.text:
            host.append_system("当前工作区未发现可索引的源代码文件。")
            return
        header = f"代码地图（{result.file_count} 个文件 · {result.symbol_count} 个符号）：\n"
        host.append_system(header + result.text)

    def _command_route(self) -> None:
        host = self._host
        model = host.service.model
        if isinstance(model, RoutedModelProvider):
            host.append_system(model.status_summary())
            return
        host.append_system(
            f"当前模型：{host.state.model_name}（直连模式，未配置多路由/Fallback 链）。"
        )

    def _command_ps(self) -> None:
        host = self._host
        sessions = host.service.active_shell_sessions()
        if not sessions:
            host.append_system("当前没有运行中的后台 Shell 会话。")
            return
        lines = ["后台 Shell 会话："]
        for item in sessions:
            status = "运行中" if item.get("running") else f"已退出({item.get('exit_code')})"
            lines.append(f"  $ {item.get('name')}  pid={item.get('pid')}  [{status}]")
        lines.append("使用 /kill <会话名> 可终止指定会话。")
        host.append_system("\n".join(lines))

    async def _command_kill(self, name: str) -> None:
        host = self._host
        if not name:
            host.append_system("用法：/kill <会话名>")
            return
        killed = await host.service.kill_shell_session(name)
        if killed:
            host.append_system(f"已终止后台 Shell 会话：{name}")
        else:
            host.append_system(f"未找到后台 Shell 会话：{name}")

    async def _command_timeline(self, arg: str) -> None:
        host = self._host
        target_id = arg or host.last_run_id
        if not target_id:
            host.append_system("暂无运行记录：请先执行一轮任务，或指定 /timeline <运行ID>。")
            return
        repo = await SQLiteSessionRepository.open(host.service.db_path)
        try:
            entries = await run_timeline(repo, target_id)
        except KeyError:
            host.append_system(f"未找到运行记录：{target_id}")
            return
        finally:
            await repo.close()
        if not entries:
            host.append_system(f"运行 {target_id[:8]} 暂无时间线事件。")
            return
        lines = [f"证据时间线（{target_id[:8]} · 共 {len(entries)} 条）："]
        for entry in entries:
            suffix = f" — {entry.detail}" if entry.detail else ""
            lines.append(f"  {entry.seq:02d}. [{entry.title}]{suffix}")
        host.append_system("\n".join(lines))

    def _command_replay(self, arg: str) -> None:
        host = self._host
        target_id = arg or host.last_run_id
        if not target_id:
            host.append_system("暂无运行记录：请先执行一轮任务，或指定 /replay <运行ID>。")
            return
        try:
            result = TraceReplayer().replay_sqlite(host.service.db_path, target_id)
        except (ValueError, KeyError) as exc:
            host.append_system(f"回放失败：{exc}")
            return
        tools = result.summary.get("tool_calls", [])
        tool_count = len(tools) if isinstance(tools, list) else 0
        lines = [
            f"只读回放（{result.run_id[:8]}）：",
            f"终态：{result.status} · 事件数：{result.events_seen} · 工具调用：{tool_count} 次",
        ]
        if result.schema_mismatch:
            lines.append("注意：检测到旧事件 schema_version，已按只读兼容模式展示。")
        host.append_system("\n".join(lines))

    async def _command_eval_save(self, case_id: str) -> None:
        host = self._host
        if not case_id or _CASE_ID_RE.fullmatch(case_id) is None:
            host.append_system("用法：/eval-save <用例ID>（仅限字母、数字、-、_，如 U01-fix-bug）")
            return
        target_id = host.last_run_id
        if not target_id:
            host.append_system("暂无可沉淀的运行记录，请先完成一轮任务。")
            return
        out_dir = host.workspace / "evals" / "cases"
        out_path = out_dir / f"{case_id}.yaml"
        if out_path.exists():
            host.append_system(f"用例文件已存在，拒绝覆盖：{out_path}")
            return
        repo = await SQLiteSessionRepository.open(host.service.db_path)
        try:
            info = await repo.run_info(target_id)
            events = await repo.events(target_id)
        except KeyError:
            host.append_system(f"未找到运行记录：{target_id}")
            return
        finally:
            await repo.close()
        out_dir.mkdir(parents=True, exist_ok=True)
        model_name = str(info.get("model_name", host.state.model_name))
        status = str(info.get("status", "completed"))
        yaml_text = (
            f"id: {case_id}\n"
            f"source_run_id: {target_id}\n"
            f"model: {model_name}\n"
            f"expected_status: {status}\n"
            f"recorded_events: {len(events)}\n"
            "assertions:\n"
            "  - kind: exit_status\n"
            f"    expected: {status}\n"
        )
        out_path.write_text(yaml_text, encoding="utf-8")
        host.append_system(f"已沉淀评估用例模板：{out_path.relative_to(host.workspace)}")

    async def _command_rewind(self, arg: str) -> None:
        host = self._host
        points = host.rewind_history.list_points(host.state.session_id)
        if not arg:
            if not points:
                host.append_system("当前会话暂无可回退的历史检查点。")
                return
            lines = ["可回退检查点（输入 /rewind <序号> 回退到该轮执行前）："]
            for p in points:
                file_note = f"{len(p.file_backups)} 个文件" if p.file_backups else "无文件改动"
                lines.append(f"  #{p.point_id}  {p.prompt_preview or '（空）'}  [{file_note}]")
            host.append_system("\n".join(lines))
            return
        if host.run_active:
            host.append_system("当前有 Run 在运行；请等待完成或取消后再回退。")
            return
        try:
            target_id = int(arg.lstrip("#"))
        except ValueError:
            host.append_system("用法：/rewind [序号]，例如 /rewind 1")
            return
        result = host.rewind_history.rewind_to(host.state.session_id, target_id)
        if result is None:
            host.append_system(f"未找到检查点 #{target_id}。输入 /rewind 查看可用序号。")
            return
        await self._truncate_session_messages(result.target_message_count)
        host.append_system(self._format_rewind_summary(f"已回退至 #{target_id} 之前", result))
        host.refresh_status()

    async def _truncate_session_messages(self, keep_count: int) -> None:
        host = self._host
        repo = await SQLiteSessionRepository.open(host.service.db_path)
        try:
            await repo.truncate_messages(host.state.session_id, keep_count)
        finally:
            await repo.close()

    @staticmethod
    def _format_rewind_summary(prefix: str, result: Any) -> str:
        parts = [f"{prefix}（回滚 {result.points_reverted} 轮）"]
        if result.restored_files:
            parts.append("已恢复文件：" + "、".join(result.restored_files))
        if result.deleted_files:
            parts.append("已删除新建文件：" + "、".join(result.deleted_files))
        if not result.restored_files and not result.deleted_files:
            parts.append("无文件变动，已同步回滚会话消息。")
        return "\n".join(parts)

    async def _command_compact(self) -> None:
        host = self._host
        enabled = bool(host.service.limits.compaction_enabled)
        repo = await SQLiteSessionRepository.open(host.service.db_path)
        try:
            old_count, new_count, old_tokens, new_tokens = await repo.compact_messages(
                host.state.session_id
            )
        finally:
            await repo.close()
        auto_note = (
            "运行中自动压缩已开启（[run] compaction_enabled = true）。"
            if enabled
            else "运行中自动压缩未开启（[run] compaction_enabled = false）。"
        )
        if old_count == 0:
            host.append_system(f"当前会话暂无可压缩历史消息。{auto_note}")
        elif new_count < old_count:
            host.append_system(
                f"已压缩会话上下文：{old_count} 条 → {new_count} 条"
                f"（估算约 {old_tokens} → {new_tokens} tokens）。{auto_note}"
            )
        else:
            host.append_system(
                f"当前会话仅 {old_count} 条历史消息（估算约 {old_tokens} tokens），无需压缩。{auto_note}"
            )

    def _command_skills(self) -> None:
        host = self._host
        try:
            skills = host.service.skills_list()
            errors = host.service.skills_errors()
            if not skills and not errors:
                host.append_system(
                    "未发现配置的 Skills。可在 .deity/skills 或 ~/.config/deity/skills 中添加。"
                )
                return
            lines = ["[bold]已配置的 Skills:[/bold]"]
            for item in skills:
                lines.append(
                    f"• [cyan]{item['name']}[/cyan] [{item['source']}]: {item['description']}"
                )
            for err in errors:
                lines.append(f"[red]错误: {err}[/red]")
            host.append_system("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            host.append_system(f"获取 Skills 失败：{exc}")

    def _command_mcp(self) -> None:
        host = self._host
        try:
            mcp_servers = getattr(host.service, "mcp_servers", {})
            if not mcp_servers:
                host.append_system("未配置 MCP 服务器。可在配置文件的 [mcp.servers.*] 段中添加。")
                return
            lines = ["[bold]已配置的 MCP 服务器:[/bold]"]
            for name, srv in mcp_servers.items():
                cmd_str = (
                    f"{srv.command} {' '.join(srv.args)}" if hasattr(srv, "command") else str(srv)
                )
                lines.append(f"• [cyan]{name}[/cyan]: {cmd_str}")
            host.append_system("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            host.append_system(f"获取 MCP 配置失败：{exc}")

    async def _command_status(self) -> None:
        host = self._host
        mode = host.mode_label()
        status = host.session_status
        lines = [
            f"模型：{host.state.model_name}",
            f"模式：{mode}",
            f"会话：{host.state.session_id[:8]}",
            f"工作区：{host.workspace}",
        ]
        branch = git_branch(host.workspace)
        if branch is not None:
            lines.append(f"分支：{branch}")
        lines.extend(
            [
                f"轮次：{status.turns} · 工具调用：{status.tool_calls} 次",
                f"Token：{status.token_text()}",
                f"耗时：{status.elapsed_seconds:.1f}s",
            ]
        )
        context_text = status.context_text()
        if context_text is not None:
            lines.append(context_text)
        repo = await SQLiteSessionRepository.open(host.service.db_path)
        try:
            messages = await repo.messages(host.state.session_id)
        finally:
            await repo.close()
        count = len([m for m in messages[-8:] if m.get("role") in {"user", "assistant"}])
        lines.append(f"上下文消息数：{count}（最近 8 条口径）")
        host.append_system("\n".join(lines))

    async def _command_diff(self) -> None:
        host = self._host
        result = await workspace_diff(host.workspace)
        diff_text = str(result.get("diff", ""))
        untracked = result.get("untracked", [])
        if not diff_text and not untracked:
            host.append_system("工作区无未提交改动。")
            return
        if diff_text:
            host.mount_raw_diff(diff_text)
        if untracked:
            host.append_system(
                f"未追踪文件（{len(untracked)}）：\n"
                + "\n".join(f"  + {path}" for path in untracked)
            )

    async def _command_new(self) -> None:
        host = self._host
        host.state.session_id = str(uuid4())
        host.state.plan_mode = False
        await host.clear_conversation(mount_welcome=True)
        host.append_system("已开始新会话。")
        host.refresh_status()

    async def _command_archive(self) -> None:
        host = self._host
        repo = await SQLiteSessionRepository.open(host.service.db_path)
        try:
            await repo.archive_session(host.state.session_id, archived=True)
        finally:
            await repo.close()
        host.append_system("已归档上一会话。")
        await self._command_new()

    async def _command_rename(self, title: str) -> None:
        host = self._host
        if not title:
            host.append_system("用法：/rename <新标题>")
            return
        repo = await SQLiteSessionRepository.open(host.service.db_path)
        try:
            await repo.rename_session(
                host.state.session_id, title, workspace_root=str(host.workspace)
            )
        except (KeyError, ValueError) as exc:
            host.append_system(f"重命名失败：{exc}")
            return
        finally:
            await repo.close()
        host.append_system(f"已重命名当前会话为：{title.strip()[:80]}")
        await host.refresh_drawer_sessions()

    def _command_sandbox(self, arg: str) -> None:
        host = self._host
        if not arg:
            host.append_system("\n".join(sandbox_status_lines(host.service)))
            return
        setter = getattr(host.service, "set_sandbox_policy", None)
        if setter is None:
            host.append_system("当前服务不支持沙箱策略切换。")
            return
        try:
            setter(arg)
        except ValueError as exc:
            host.append_system(f"切换失败：{exc}")
            return
        host.append_system(
            f"已切换沙箱策略为：{arg}（仅本次会话内生效；写入配置文件 [sandbox] policy 可永久生效）。"
        )

    async def _command_export(self) -> None:
        host = self._host
        target = await export_session_markdown(host.service, host.state.session_id, host.workspace)
        if target is None:
            host.append_system("当前会话暂无消息可导出。")
            return
        host.append_system(f"已导出：{target}")

    def _command_copy(self) -> None:
        host = self._host
        view = host.last_view
        text = view.text if view is not None else ""
        if not text:
            host.append_system("暂无可复制的回答。")
            return
        host.copy_text(text)
        host.append_system("已复制上一轮回答。")

    def _command_tools(self, prompt: str) -> None:
        host = self._host
        view = host.last_view
        if view is None or not view.tools:
            host.append_system(
                "上一轮没有工具调用记录。先完成一轮会用到工具的任务，再用 /tools 查看。"
            )
            return
        parts = prompt.split(maxsplit=1)
        targets = (
            [view.tools[parts[1].strip()]]
            if len(parts) == 2 and parts[1].strip() in view.tools
            else list(view.tools.values())
        )
        if len(parts) == 2 and parts[1].strip() not in view.tools:
            host.append_system(f"未找到工具调用：{parts[1].strip()}")
            return
        for node in targets:
            host.toggle_tool_node(node.call_id)

    async def _command_sessions(self, prompt: str) -> None:
        host = self._host
        parts = prompt.split(maxsplit=1)
        repo = await SQLiteSessionRepository.open(host.service.db_path)
        try:
            items = await repo.sessions(include_archived=True)
        finally:
            await repo.close()
        if len(parts) == 2:
            target = parts[1].strip()
            if host.run_active:
                host.append_system("有 Run 在运行，结束后再切换会话。")
                return
            if not any(item["id"] == target for item in items):
                host.append_system(f"未知会话：{target}")
                return
            host.apply_session_switch(target)
            return
        if not items:
            host.append_system("（暂无已存储会话）直接在下方输入任务即可开始新会话。")
            return
        lines = []
        for item in items:
            marker = "●" if item["id"] == host.state.session_id else " "
            archived = " [已归档]" if item["archived"] == "1" else ""
            title = f"  {item['title']}" if item["title"] else ""
            lines.append(f"{marker} {item['id'][:8]}  {item['workspace']}{title}{archived}")
        lines.append("切换会话：/sessions <完整会话ID>")
        host.append_system("\n".join(lines))

    async def _command_build(self, prompt: str) -> None:
        host = self._host
        if host.run_active:
            host.append_system("当前有 Run 在运行；结束后再执行 Build。")
            return
        tokens = shlex.split(prompt)
        plan_id: str | None = None
        allow_dirty: list[str] = []
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token == "--allow-dirty" and index + 1 < len(tokens):
                allow_dirty.append(tokens[index + 1])
                index += 2
                continue
            if plan_id is None:
                plan_id = token
            index += 1
        plan_id = plan_id or host.last_plan_id
        if plan_id is None:
            host.append_system("暂无计划：先 /plan 进入计划模式并提交任务，或 /build <计划ID>。")
            return

        async def execute() -> None:
            try:
                allowed = await host.authorize_dirty(plan_id, tuple(allow_dirty))
                if allowed is None:
                    return
                result = await build_approved(host.service, plan_id, allowed)
                await host.append_assistant_final(result.final_text or "无最终回答")
            except (ModelFailure, ProtocolError, BudgetExceeded, ValueError, OSError) as exc:
                host.append_error(f"运行失败：{run_error_message(exc)}")

        host.start_run_task(execute())

    async def _command_resume(self, prompt: str) -> None:
        host = self._host
        parts = prompt.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            hint = f"（最近一次：{host.last_run_id[:8]}）" if host.last_run_id else ""
            host.append_system(f"用法：/resume <运行ID>{hint}")
            return
        if host.run_active:
            host.append_system("当前有 Run 在运行；结束后再恢复。")
            return
        run_id = parts[1].strip()

        async def execute() -> None:
            try:
                result = await host.service.resume(run_id)
                await host.append_assistant_final(result.final_text or "无最终回答")
                host.append_system(f"恢复完成：{result.run_id[:8]} · {result.status.value}")
            except (
                ModelFailure,
                ProtocolError,
                BudgetExceeded,
                ValueError,
                OSError,
                KeyError,
            ) as exc:
                host.append_error(f"运行失败：{run_error_message(exc)}")

        host.start_run_task(execute())


__all__ = ["AVAILABLE_SLASH_COMMANDS", "CommandDispatcher", "WorkbenchHost"]
