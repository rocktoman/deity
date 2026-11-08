"""交互会话：ChatSession 状态机与 _interactive() 循环本体。

Phase 1 只搬家，不加功能：输入仍用内置 input()，任务仍走 RunService.run()，
斜杠命令分发给 chat_commands.dispatch()。输出文本与基线逐行一致。
"""

from uuid import uuid4

from rich.console import Console

from deity.application.config import ChatConfig
from deity.application.plan_service import PlanService
from deity.application.run_service import RunService
from deity.core.runtime import BudgetExceeded, ModelFailure, ProtocolError
from deity.interfaces.cli.chat_commands import (
    ChatContext,
    ChatState,
    dispatch,
    run_error_message,
)
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
    ) -> None:
        self.service = service
        self.model_name = model_name
        self.config = config or ChatConfig()
        self.console = output or console
        self.state = ChatState(session_id=session_id or str(uuid4()))
        self.context = ChatContext(
            service=service,
            model_name=model_name,
            state=self.state,
            console=self.console,
        )

    @property
    def session_id(self) -> str:
        """当前会话 id。"""
        return self.state.session_id

    @property
    def plan_mode(self) -> bool:
        """是否处于持续计划模式。"""
        return self.state.plan_mode

    async def run(self) -> None:
        """读入 successive prompts within one persisted conversation session."""
        self.console.print("欢迎来到 Deity，我是你的编程助手。")
        while True:
            try:
                prompt = input("Deity[plan]> " if self.state.plan_mode else "Deity> ").strip()
            except EOFError:
                self.console.print()
                return
            if await dispatch(prompt, self.context):
                if self.state.exit_requested:
                    return
                continue
            if not prompt:
                continue
            try:
                instructions: list[str] = []
                if self.state.pending_skill:
                    instructions.append(f"请先加载并使用 {self.state.pending_skill} Skill。")
                if self.state.pending_mcp_tool:
                    instructions.append(f"如适用，请使用 {self.state.pending_mcp_tool} 工具。")
                task = f"{''.join(instructions)}任务：{prompt}" if instructions else prompt
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
                else:
                    result = await self.service.run(
                        task, model_name=self.model_name, session_id=self.state.session_id
                    )
            except (ModelFailure, ProtocolError, BudgetExceeded, ValueError, OSError) as exc:
                self.console.print(f"运行失败：{run_error_message(exc)}", style="red")
                continue
            self.console.print(result.final_text or "无最终回答")


async def run_chat_session(
    service: RunService,
    model_name: str,
    session_id: str | None,
    *,
    config: ChatConfig | None = None,
    output: Console | None = None,
) -> None:
    """交互会话入口（原 main.py::_interactive，行为不变）。"""
    await ChatSession(service, model_name, session_id, config=config, output=output).run()


__all__ = ["ChatSession", "console", "run_chat_session"]
