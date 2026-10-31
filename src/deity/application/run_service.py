"""Compose a provider, tool dispatcher and session repository for one run."""

import os
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from deity.adapters.model.anthropic import AnthropicProvider
from deity.adapters.model.openai_compatible import OpenAICompatibleProvider
from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools import LocalToolDispatcher, auto_shell_allowed
from deity.adapters.tools.workspace import workspace_digest
from deity.application.config import Config, PermissionConfig, RunConfig
from deity.core.context import ContextBuilder
from deity.core.context.reliable import normalize_unicode
from deity.core.contracts import AgentRequest, AgentResult, ModelProvider, RecoveryDecision
from deity.core.runtime import AgentLoop


class RunService:
    """Start one local run and close its SQLite connection afterward."""

    def __init__(
        self,
        *,
        db_path: Path,
        workspace: Path,
        model: ModelProvider | None = None,
        approve: Callable[[str, dict[str, Any]], bool] | None = None,
        limits: RunConfig | None = None,
        permission: PermissionConfig | None = None,
        context_window_tokens: int | None = None,
        provider_id: str = "",
    ) -> None:
        self.db_path = db_path
        self.workspace = workspace.resolve(strict=True)
        self.model = model
        self.approve = approve
        self.limits = limits or RunConfig()
        self.permission = permission or PermissionConfig()
        self.context_window_tokens = context_window_tokens
        self.provider_id = provider_id
        self._active_loop: AgentLoop | None = None

    def cancel(self, run_id: str) -> bool:
        """Signal cancellation to this service's active run."""
        return self._active_loop.cancel(run_id) if self._active_loop is not None else False

    def _authorize(self, name: str, args: dict[str, Any]) -> bool:
        """Apply configured permission before any optional interactive approval."""
        policy = self.permission.shell if name == "shell" else self.permission.workspace_write
        if policy == "deny":
            return False
        if policy == "auto":
            return auto_shell_allowed(args, self.workspace) if name == "shell" else True
        return self.approve(name, args) if self.approve is not None else False

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
            context_window_tokens=config.model.context_window_tokens,
            provider_id=config.model.provider,
        )

    async def run(
        self, task: str, *, model_name: str, session_id: str | None = None
    ) -> AgentResult:
        """Execute and persist a task within the selected workspace."""
        if self.model is None:
            raise ValueError("model is required to run")
        task = normalize_unicode(task)
        repo = await SQLiteSessionRepository.open(self.db_path)
        try:
            dispatcher = LocalToolDispatcher(approve=self._authorize)
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
                context_window_tokens=self.context_window_tokens,
                compaction_enabled=self.limits.compaction_enabled,
                max_input_tokens_total=self.limits.max_input_tokens_total,
                max_output_tokens_total=self.limits.max_output_tokens_total,
                provider_name=self.provider_id,
            )
            instructions_file = self.workspace / "AGENTS.md"
            instructions = None
            if (
                instructions_file.is_file()
                and instructions_file.resolve().is_relative_to(self.workspace)
                and instructions_file.stat().st_size <= 16_000
            ):
                instructions = instructions_file.read_text(encoding="utf-8")
            context = ContextBuilder(project_instructions=instructions)
            loop = AgentLoop(
                self.model,
                dispatcher,
                repository=repo,
                context=context,
                workspace_fingerprint=lambda: workspace_digest(self.workspace, (self.db_path,)),
            )
            prior = [
                {"role": message["role"], "content": normalize_unicode(message["content"])}
                for message in (await repo.messages(active_session))[-8:]
                if message["role"] in {"user", "assistant"} and isinstance(message["content"], str)
            ]
            initial_messages = context.initial(task)
            initial_messages[-1:-1] = prior
            self._active_loop = loop
            result = await loop.run(request, initial_messages=initial_messages)
            await repo.append_message(active_session, result.run_id, "user", task)
            await repo.append_message(
                active_session, result.run_id, "assistant", result.final_text or ""
            )
            return result
        finally:
            self._active_loop = None
            await repo.close()

    async def inspect(self, run_id: str) -> dict[str, Any]:
        """Classify an interrupted run without replaying any tool."""
        repo = await SQLiteSessionRepository.open(self.db_path)
        try:
            info = await repo.run_info(run_id)
            checkpoint = await repo.latest_checkpoint(run_id)
            unresolved = await repo.unresolved_tools(run_id)
            decision: Literal["resume", "manual_review", "abort"] = "abort"
            reason = "no_valid_checkpoint"
            if info["status"] != "interrupted":
                reason = "run_not_interrupted"
            elif await repo.has_active_owner(run_id):
                reason = "owner_may_still_be_active"
            elif unresolved:
                decision, reason = "manual_review", "unresolved_tool"
            elif checkpoint is None:
                reason = "no_valid_checkpoint"
            elif await repo.effects_after_checkpoint(run_id, checkpoint["last_event_seq"]):
                decision, reason = "manual_review", "effect_after_checkpoint"
            elif info["workspace_root"] != str(self.workspace):
                reason = "workspace_mismatch"
            elif self.provider_id and info["model_provider"] != self.provider_id:
                reason = "model_provider_changed"
            elif checkpoint["budget"].get("config_fingerprint") is not None and (
                checkpoint["budget"]["config_fingerprint"]
                != self._recovery_request(info).config_fingerprint()
            ):
                reason = "config_changed"
            elif checkpoint["budget"].get("workspace_fingerprint") is None:
                reason = "unsupported_legacy_checkpoint"
            elif checkpoint["budget"]["workspace_fingerprint"] != workspace_digest(
                self.workspace, (self.db_path,)
            ):
                reason = "workspace_changed"
            elif checkpoint["budget"].get("turns", 0) >= self.limits.max_turns:
                reason = "turn_budget_exhausted"
            elif checkpoint["budget"].get("elapsed_seconds", 0) >= self.limits.deadline_seconds:
                reason = "deadline_exhausted"
            elif checkpoint["budget"].get("tool_calls", 0) >= self.limits.max_tool_calls:
                reason = "tool_budget_exhausted"
            elif (
                self.limits.max_input_tokens_total is not None
                and checkpoint["budget"].get("input_tokens", 0)
                >= self.limits.max_input_tokens_total
            ):
                reason = "input_token_budget_exhausted"
            elif (
                self.limits.max_output_tokens_total is not None
                and checkpoint["budget"].get("output_tokens", 0)
                >= self.limits.max_output_tokens_total
            ):
                reason = "output_token_budget_exhausted"
            else:
                decision, reason = "resume", "safe_checkpoint"
            return {
                **asdict(
                    RecoveryDecision(
                        run_id, decision, reason, checkpoint["id"] if checkpoint else None
                    )
                ),
                "unresolved_tools": unresolved,
                "budget": checkpoint["budget"] if checkpoint else None,
            }
        finally:
            await repo.close()

    async def resume(self, run_id: str) -> AgentResult:
        """Continue an interrupted task as a new run from a safe checkpoint."""
        if not self.limits.resume_enabled:
            raise ValueError("resume is disabled")
        if self.model is None:
            raise ValueError("model is required to resume")
        inspection = await self.inspect(run_id)
        if inspection["decision"] != "resume":
            raise ValueError(f"cannot resume: {inspection['reason']}")
        repo = await SQLiteSessionRepository.open(self.db_path)
        try:
            info = await repo.run_info(run_id)
            checkpoint = await repo.latest_checkpoint(run_id)
            if checkpoint is None or checkpoint["id"] != inspection["checkpoint_id"]:
                raise ValueError("checkpoint changed during recovery")
            if checkpoint["budget"]["workspace_fingerprint"] != workspace_digest(
                self.workspace, (self.db_path,)
            ):
                raise ValueError("workspace changed during recovery")
            successor = await repo.create_resumed_run(run_id, checkpoint["id"])

            request = AgentRequest(
                session_id=info["session_id"],
                task_id=info["task_id"],
                workspace=str(self.workspace),
                user_text=info["prompt"],
                model=info["model_name"],
                max_turns=self.limits.max_turns,
                max_tool_calls=self.limits.max_tool_calls,
                deadline_seconds=self.limits.deadline_seconds,
                max_output_tokens=self.limits.max_output_tokens,
                context_window_tokens=self.context_window_tokens,
                compaction_enabled=self.limits.compaction_enabled,
                max_input_tokens_total=self.limits.max_input_tokens_total,
                max_output_tokens_total=self.limits.max_output_tokens_total,
                provider_name=info["model_provider"],
            )
            loop = AgentLoop(
                self.model,
                LocalToolDispatcher(approve=self._authorize),
                repository=repo,
                workspace_fingerprint=lambda: workspace_digest(self.workspace, (self.db_path,)),
            )
            self._active_loop = loop
            return await loop.run_existing(
                request, successor, checkpoint["messages"], checkpoint["budget"]
            )
        finally:
            self._active_loop = None
            await repo.close()

    def _recovery_request(self, info: dict[str, Any]) -> AgentRequest:
        """Construct current settings for a secret-free recovery comparison."""
        return AgentRequest(
            session_id=info["session_id"],
            task_id=info["task_id"],
            workspace=str(self.workspace),
            user_text=info["prompt"],
            model=info["model_name"],
            max_turns=self.limits.max_turns,
            max_tool_calls=self.limits.max_tool_calls,
            deadline_seconds=self.limits.deadline_seconds,
            max_output_tokens=self.limits.max_output_tokens,
            context_window_tokens=self.context_window_tokens,
            compaction_enabled=self.limits.compaction_enabled,
            max_input_tokens_total=self.limits.max_input_tokens_total,
            max_output_tokens_total=self.limits.max_output_tokens_total,
            provider_name=self.provider_id or info["model_provider"],
        )
