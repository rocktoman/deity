"""Compose a provider, tool dispatcher and session repository for one run."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from deity.adapters.model.anthropic import AnthropicProvider
from deity.adapters.model.openai_compatible import OpenAICompatibleProvider
from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools import LocalToolDispatcher
from deity.application.config import Config, PermissionConfig, RunConfig
from deity.core.context import ContextBuilder
from deity.core.contracts import AgentRequest, AgentResult, ModelProvider
from deity.core.runtime import AgentLoop


class RunService:
    """Start one local run and close its SQLite connection afterward."""

    def __init__(
        self,
        *,
        db_path: Path,
        workspace: Path,
        model: ModelProvider,
        approve: Callable[[str, dict[str, Any]], bool] | None = None,
        limits: RunConfig | None = None,
        permission: PermissionConfig | None = None,
    ) -> None:
        self.db_path = db_path
        self.workspace = workspace.resolve(strict=True)
        self.model = model
        self.approve = approve
        self.limits = limits or RunConfig()
        self.permission = permission or PermissionConfig()

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        db_path: Path,
        workspace: Path,
        approve: Callable[[str, dict[str, Any]], bool] | None = None,
    ) -> "RunService":
        """Resolve an explicitly selected provider and its environment key."""
        if config.model is None:
            raise ValueError("模型未配置")
        key = os.environ.get(config.model.api_key_env)
        if not key:
            raise ValueError("模型密钥环境变量未设置")
        if config.model.provider == "openai_compatible":
            provider: ModelProvider = OpenAICompatibleProvider(
                api_key=key, model=config.model.name, base_url=config.model.base_url
            )
        elif config.model.provider == "anthropic":
            provider = AnthropicProvider(api_key=key, model=config.model.name)
        else:
            raise ValueError("未知模型协议")
        return cls(
            db_path=db_path,
            workspace=workspace,
            model=provider,
            approve=approve,
            limits=config.run,
            permission=config.permission,
        )

    async def run(
        self, task: str, *, model_name: str, session_id: str | None = None
    ) -> AgentResult:
        """Execute and persist a task within the selected workspace."""
        repo = await SQLiteSessionRepository.open(self.db_path)
        try:

            def authorize(name: str, args: dict[str, Any]) -> bool:
                if name == "shell" and self.permission.shell == "deny":
                    return False
                if (
                    name in {"write_file", "apply_patch"}
                    and self.permission.workspace_write == "deny"
                ):
                    return False
                return self.approve(name, args) if self.approve is not None else False

            dispatcher = LocalToolDispatcher(approve=authorize)
            active_session = session_id or str(uuid4())
            request = AgentRequest(
                session_id=active_session,
                task_id=str(uuid4()),
                workspace=str(self.workspace),
                user_text=task,
                model=model_name,
                max_turns=self.limits.max_turns,
                max_tool_calls=self.limits.max_tool_calls,
                deadline_seconds=self.limits.deadline_seconds,
                max_output_tokens=self.limits.max_output_tokens,
            )
            instructions_file = self.workspace / "AGENTS.md"
            instructions = None
            if (
                instructions_file.is_file()
                and instructions_file.resolve().is_relative_to(self.workspace)
                and instructions_file.stat().st_size <= 16_000
            ):
                instructions = instructions_file.read_text(encoding="utf-8")
            result = await AgentLoop(
                self.model,
                dispatcher,
                repository=repo,
                context=ContextBuilder(project_instructions=instructions),
            ).run(request)
            await repo.append_message(active_session, result.run_id, "user", task)
            await repo.append_message(
                active_session, result.run_id, "assistant", result.final_text or ""
            )
            return result
        finally:
            await repo.close()
