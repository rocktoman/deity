"""交互会话：ChatSession 状态机与 _interactive() 循环本体。

Phase 2 接入流式渲染与增强输入：终端 + 流式开启时任务走
RunService.run_stream() + StreamingRenderer；非 TTY / TERM=dumb /
streaming=false / 无 run_stream 时回落整段打印（与基线一致）。
取消语义（Python 3.12 asyncio.run 的 Runner 机制）：第一次 SIGINT 由
Runner 转成主任务的 CancelledError，_consume_with_cancel 捕获后调
service.cancel() 取消本轮并回到提示符；取消过程中第二次 SIGINT 由
Runner 直接抛 KeyboardInterrupt，透传出 main.run() 后退出码 130。
输入态：prompt_toolkit 按键 Ctrl-C 回到提示符；PlainInput 下第一次
SIGINT 回到提示符，第二次退出（基线行为）。
"""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from rich.console import Console

from deity.application.config import ChatConfig
from deity.application.plan_service import PlanService
from deity.application.run_service import RunService
from deity.core.contracts.client import ClientEvent
from deity.core.runtime import BudgetExceeded, ModelFailure, ProtocolError
from deity.interfaces.cli.chat_commands import (
    ChatContext,
    ChatState,
    command_names,
    dispatch,
    run_error_message,
)
from deity.interfaces.cli.chat_input import PromptToolkitInput, create_chat_input
from deity.interfaces.cli.chat_mentions import expand_task, resolve_mentions
from deity.interfaces.cli.chat_render import RunSummary, StreamingRenderer
from deity.interfaces.cli.plan import (
    authorize_dirty_targets,
    build_approved,
    confirm_build,
)

console = Console()


class ChatSession:
    """一次交互会话的状态：会话 id、计划模式与下一条任务的待用选择。"""

    def __init__(
        self,
        service: RunService,
        model_name: str,
        session_id: str | None = None,
        *,
        config: ChatConfig | None = None,
        output: Console | None = None,
        symbol_search: Any | None = None,
    ) -> None:
        self.service = service
        self.model_name = model_name
        self.config = config or ChatConfig()
        self.console = output or console
        self.state = ChatState(session_id=session_id or str(uuid4()))
        self._workspace = Path(getattr(service, "workspace", Path.cwd()))
        self._symbol_search = symbol_search
        self.input = create_chat_input(
            self.config,
            workspace=self._workspace,
            commands=command_names(),
            status_text=self._status_text,
        )
        self.context = ChatContext(
            service=service,
            model_name=model_name,
            state=self.state,
            console=self.console,
            input=self.input,
        )

    @property
    def session_id(self) -> str:
        """当前会话 id。"""
        return self.state.session_id

    @property
    def plan_mode(self) -> bool:
        """是否处于持续计划模式。"""
        return self.state.plan_mode

    def _status_text(self) -> str:
        """底部状态栏文本：模型、模式、会话 id 前 8 位。"""
        mode = "计划" if self.state.plan_mode else "普通"
        return f"模型 {self.model_name} · {mode} · 会话 {self.state.session_id[:8]}"

    def _use_streaming(self) -> bool:
        """是否走流式渲染：配置开启 + 终端输出 + 非 dumb + 服务有 run_stream。"""
        if not self.config.streaming:
            return False
        if os.environ.get("TERM") == "dumb":
            return False
        if not self.console.is_terminal:
            return False
        return hasattr(self.service, "run_stream")

    async def run(self) -> None:
        """读入 successive prompts within one persisted conversation session."""
        self.console.print("欢迎来到 Deity，我是你的编程助手。")
        while True:
            try:
                prompt = self.input.read_line(
                    "Deity[plan]> " if self.state.plan_mode else "Deity> "
                ).strip()
            except EOFError:
                self.console.print()
                return
            except KeyboardInterrupt:
                # prompt_toolkit 按键 Ctrl-C：回到提示符（进程未收到信号）。
                # PlainInput 下的 KeyboardInterrupt 来自 Runner 第二次 SIGINT：退出。
                if isinstance(self.input, PromptToolkitInput):
                    continue
                raise
            except asyncio.CancelledError:
                # 第一次 SIGINT：Runner 已取消主任务，回到提示符。
                continue
            try:
                # 同步 input() 里收到的第一次 SIGINT 只会挂起取消请求，
                # 在下一个 await 点才抛出；这里主动让出一次以便就地消化。
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                continue
            if await dispatch(prompt, self.context):
                if self.state.exit_requested:
                    return
                continue
            if not prompt:
                continue
            try:
                mentions = await resolve_mentions(
                    prompt,
                    self._workspace,
                    max_bytes=self.config.mention_max_bytes,
                    symbol_search=self._symbol_search,
                )
            except ValueError as exc:
                self.console.print(f"引用解析失败：{exc}", style="red")
                continue
            try:
                instructions: list[str] = []
                if self.state.pending_skill:
                    instructions.append(f"请先加载并使用 {self.state.pending_skill} Skill。")
                if self.state.pending_mcp_tool:
                    instructions.append(f"如适用，请使用 {self.state.pending_mcp_tool} 工具。")
                task = f"{''.join(instructions)}任务：{prompt}" if instructions else prompt
                task = expand_task(task, mentions)
                self.state.pending_skill = None
                self.state.pending_mcp_tool = None
                if self.state.plan_mode:
                    artifact = await PlanService(self.service).create(
                        task, model_name=self.model_name, session_id=self.state.session_id
                    )
                    self.console.print(f"计划 ID：{artifact.plan_id}")
                    if confirm_build(artifact):
                        allowed = await authorize_dirty_targets(self.service, artifact.plan_id)
                        if allowed is None:
                            continue
                        result = await build_approved(self.service, artifact.plan_id, allowed)
                    else:
                        self.console.print("已保留计划；输入 /build 可稍后执行。")
                        continue
                    self.console.print(result.final_text or "无最终回答")
                elif self._use_streaming():
                    await self._run_streaming(task)
                else:
                    result = await self.service.run(
                        task, model_name=self.model_name, session_id=self.state.session_id
                    )
                    self.console.print(result.final_text or "无最终回答")
            except (ModelFailure, ProtocolError, BudgetExceeded, ValueError, OSError) as exc:
                self.console.print(f"运行失败：{run_error_message(exc)}", style="red")
                continue

    async def _run_streaming(self, task: str) -> RunSummary | None:
        """流式执行一轮任务：渲染 live 事件流；取消时返回 None 回到提示符。"""
        renderer = StreamingRenderer(
            self.console,
            model_name=self.model_name,
            mode="计划" if self.state.plan_mode else "普通",
        )
        stream = self.service.run_stream(
            task, model_name=self.model_name, session_id=self.state.session_id
        )
        return await self._consume_with_cancel(stream, renderer)

    async def _consume_with_cancel(
        self, stream: AsyncIterator[ClientEvent], renderer: StreamingRenderer
    ) -> RunSummary | None:
        """消费渲染任务；第一次 Ctrl-C 取消本轮，取消过程中再按一次则退出。

        第一次 SIGINT 到达时 Runner 已把主任务取消（CancelledError），
        这里捕获后调 service.cancel()（无 run_id 时直接取消渲染任务），
        等渲染任务以终态 cancelled 事件正常结束；第二次 SIGINT 由 Runner
        直接抛 KeyboardInterrupt，透传出去由 main.run() 转成退出码 130。
        """
        task = asyncio.ensure_future(renderer.render(stream))
        try:
            return await task
        except asyncio.CancelledError:
            run_id = renderer.current_run_id
            if run_id is not None:
                self.service.cancel(run_id)
            else:
                task.cancel()
            try:
                return await task
            except asyncio.CancelledError:
                self.console.print("已取消本轮运行。", style="yellow")
                return None
            except KeyboardInterrupt:
                raise


async def run_chat_session(
    service: RunService,
    model_name: str,
    session_id: str | None,
    *,
    config: ChatConfig | None = None,
    output: Console | None = None,
    symbol_search: Any | None = None,
) -> None:
    """交互会话入口（原 main.py::_interactive，行为见 ChatSession）。"""
    await ChatSession(
        service, model_name, session_id, config=config, output=output, symbol_search=symbol_search
    ).run()


__all__ = ["ChatSession", "console", "run_chat_session"]
