"""Compose a provider, tool dispatcher and session repository for one run."""

import hashlib
import importlib
import inspect
import json
import os
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from deity.adapters.mcp import McpCompositeDispatcher, McpServerConfig, McpSessionManager, McpTool
from deity.adapters.model.anthropic import AnthropicProvider
from deity.adapters.model.openai_compatible import OpenAICompatibleProvider
from deity.adapters.skills import SkillCatalog, SkillDispatcher
from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools import LocalToolDispatcher, auto_shell_allowed
from deity.adapters.tools.workspace import workspace_digest
from deity.application.config import Config, McpServerSettings, PermissionConfig, RunConfig
from deity.core.context import HARNESS_SYSTEM_INSTRUCTIONS, ContextBuilder
from deity.core.context.reliable import normalize_unicode
from deity.core.contracts import (
    AgentRequest,
    AgentResult,
    ModelProvider,
    RecoveryDecision,
    ToolDispatcher,
)
from deity.core.hooks import HookRegistry, RegisteredHook
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
        extension_fingerprint: str = "",
        mcp_enabled: bool = False,
        mcp_servers: tuple[McpServerSettings, ...] = (),
        skills_enabled: bool = False,
        skill_diagnostics: bool = False,
        hooks_enabled: bool = False,
        hooks: HookRegistry | None = None,
    ) -> None:
        self.db_path = db_path
        self.workspace = workspace.resolve(strict=True)
        self.model = model
        self.approve = approve
        self.limits = limits or RunConfig()
        self.permission = permission or PermissionConfig()
        self.context_window_tokens = context_window_tokens
        self.provider_id = provider_id
        self.extension_fingerprint = extension_fingerprint
        self.mcp_enabled = mcp_enabled
        self.mcp_servers = mcp_servers
        self.skills_enabled = skills_enabled
        self.hooks = hooks if hooks_enabled else None
        if self.hooks is not None:
            metadata = [
                (item.name, item.point, item.kind, item.priority) for item in self.hooks.hooks
            ]
            self.extension_fingerprint = hashlib.sha256(
                (self.extension_fingerprint + json.dumps(metadata)).encode()
            ).hexdigest()
        project_skills = self.workspace / ".deity/skills"
        if (
            skills_enabled
            and project_skills.exists()
            and not project_skills.resolve(strict=True).is_relative_to(self.workspace)
        ):
            raise ValueError("project Skill root escapes workspace")
        self.skill_catalog = (
            SkillCatalog(
                (
                    ("user", Path.home() / ".config/deity/skills"),
                    ("project", project_skills),
                ),
                tolerate_errors=skill_diagnostics,
            )
            if skills_enabled
            else None
        )
        if self.skill_catalog is not None:
            self.skill_catalog.index()
            self.extension_fingerprint = hashlib.sha256(
                (self.extension_fingerprint + self.skill_catalog.fingerprint()).encode()
            ).hexdigest()
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
        inspection_only: bool = False,
    ) -> "RunService":
        """Resolve an explicitly selected provider and its environment key."""
        if config.model is None and not inspection_only:
            raise ValueError("模型未配置")
        registered_hooks: list[RegisteredHook] = []
        hook_sources: list[tuple[str, str]] = []
        if config.extensions.hooks_enabled:
            for spec in config.hook_specs:
                module_name, function_name = spec.callable_path.split(":", 1)
                try:
                    function = getattr(importlib.import_module(module_name), function_name)
                    source = inspect.getsourcefile(function)
                except (ImportError, AttributeError, TypeError):
                    raise ValueError(f"Hook unavailable: {spec.name}") from None
                if not inspect.iscoroutinefunction(function) or source is None:
                    raise ValueError(f"Hook must be an async source function: {spec.name}")
                registered_hooks.append(
                    RegisteredHook(spec.name, spec.point, spec.kind, spec.priority, function)
                )
                hook_sources.append(
                    (spec.callable_path, hashlib.sha256(Path(source).read_bytes()).hexdigest())
                )
        provider: ModelProvider | None = None
        if not inspection_only:
            assert config.model is not None
            key = os.environ.get(config.model.api_key_env)
            if not key:
                raise ValueError("模型密钥环境变量未设置")
            if config.model.provider == "openai_compatible":
                provider = OpenAICompatibleProvider(
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
            context_window_tokens=(config.model.context_window_tokens if config.model else None),
            provider_id=(config.model.provider if config.model else ""),
            extension_fingerprint=(
                hashlib.sha256(
                    json.dumps(
                        {
                            "flags": config.extensions.fingerprint(),
                            "servers": [asdict(server) for server in config.mcp_servers],
                            "hooks": [asdict(spec) for spec in config.hook_specs],
                            "hook_sources": hook_sources,
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                if any(
                    (
                        config.extensions.mcp_enabled,
                        config.extensions.skills_enabled,
                        config.extensions.hooks_enabled,
                    )
                )
                else ""
            ),
            mcp_enabled=config.extensions.mcp_enabled,
            mcp_servers=config.mcp_servers,
            skills_enabled=config.extensions.skills_enabled,
            hooks_enabled=config.extensions.hooks_enabled,
            hooks=HookRegistry(tuple(registered_hooks))
            if config.extensions.hooks_enabled
            else None,
        )

    async def _mcp_dispatcher(
        self, repo: SQLiteSessionRepository, local: LocalToolDispatcher
    ) -> McpCompositeDispatcher | LocalToolDispatcher:
        if not self.mcp_enabled:
            return local
        configs: list[McpServerConfig] = []
        for server in self.mcp_servers:
            cwd = (self.workspace / server.cwd).resolve(strict=True)
            if not cwd.is_relative_to(self.workspace):
                raise ValueError("MCP server working directory escapes workspace")
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "name": server.name,
                        "command": server.command,
                        "args": server.args,
                        "cwd": str(cwd),
                        "env_names": server.env_names,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if not await repo.mcp_launch_trusted(server.name, digest):
                if self.approve is None or not self.approve(
                    "mcp_server_start",
                    {
                        "server": server.name,
                        "command": server.command,
                        "args": list(server.args),
                        "cwd": str(cwd),
                        "env_names": list(server.env_names),
                        "digest": digest,
                    },
                ):
                    raise ValueError(f"MCP server launch denied: {server.name}")
                await repo.trust_mcp_launch(server.name, digest)
            configs.append(
                McpServerConfig(
                    server.name,
                    server.command,
                    server.args,
                    cwd,
                    server.env_names,
                    server.timeout_seconds,
                )
            )
        manager = McpSessionManager(tuple(configs))
        discovered: list[McpTool] = []
        for configured_server in configs:
            discovered.extend(await manager.discover(configured_server.name))
        return McpCompositeDispatcher(local, manager, tuple(discovered), self.approve)

    def mcp_list(self) -> list[str]:
        """Return configured server state without starting processes."""
        state = "enabled" if self.mcp_enabled else "disabled"
        return [f"{server.name} [{state}] not_started" for server in self.mcp_servers]

    async def mcp_test(self) -> list[str]:
        """Approve launch, connect and validate schemas without business calls."""
        repo = await SQLiteSessionRepository.open(self.db_path)
        try:
            dispatcher = await self._mcp_dispatcher(repo, LocalToolDispatcher())
            if isinstance(dispatcher, McpCompositeDispatcher):
                return sorted(dispatcher.tools)
            return []
        finally:
            await repo.close()

    def skills_list(self) -> tuple[dict[str, str], ...]:
        """Return bounded Skill metadata without loading bodies."""
        return self.skill_catalog.index() if self.skill_catalog is not None else ()

    def skills_errors(self) -> tuple[str, ...]:
        """Return source-qualified parse errors for inspection commands."""
        return tuple(self.skill_catalog.errors) if self.skill_catalog is not None else ()

    def skills_inspect(self, name: str) -> dict[str, str]:
        """Inspect one Skill's metadata without executing its content."""
        if self.skill_catalog is None:
            raise ValueError("Skills are disabled")
        item = self.skill_catalog.inspect(name)
        return {
            "name": item.name,
            "description": item.description,
            "source": item.source,
            "path": str(item.path),
            "sha256": item.digest,
        }

    async def _skill_resources_unchanged(
        self, repo: SQLiteSessionRepository, run_id: str, last_event_seq: int
    ) -> bool:
        """Verify every resource that was visible before the recovery checkpoint."""
        if self.skill_catalog is None:
            return True
        for event in await repo.events(run_id):
            if event["sequence"] > last_event_seq or event["type"] != "SkillResourceLoaded":
                continue
            payload = event["payload"]
            name, path = payload.get("skill_id"), payload.get("path")
            if not isinstance(name, str) or not isinstance(path, str):
                return False
            try:
                manifest = self.skill_catalog.inspect(name)
                _content, digest = self.skill_catalog.resource(name, path)
            except (KeyError, ValueError, OSError):
                return False
            if manifest.source != payload.get("source") or digest != payload.get("sha256"):
                return False
        return True

    async def run(
        self, task: str, *, model_name: str, session_id: str | None = None
    ) -> AgentResult:
        """Execute and persist a task within the selected workspace."""
        if self.model is None:
            raise ValueError("model is required to run")
        task = normalize_unicode(task)
        repo = await SQLiteSessionRepository.open(self.db_path)
        try:
            dispatcher: ToolDispatcher = await self._mcp_dispatcher(
                repo, LocalToolDispatcher(approve=self._authorize)
            )
            if self.skill_catalog is not None:
                dispatcher = SkillDispatcher(dispatcher, self.skill_catalog)
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
                extension_fingerprint=self.extension_fingerprint,
            )
            instructions_file = self.workspace / "AGENTS.md"
            instructions = None
            if (
                instructions_file.is_file()
                and instructions_file.resolve().is_relative_to(self.workspace)
                and instructions_file.stat().st_size <= 16_000
            ):
                instructions = instructions_file.read_text(encoding="utf-8")
            context = ContextBuilder(
                system_instructions=HARNESS_SYSTEM_INSTRUCTIONS,
                project_instructions=instructions,
            )
            loop = AgentLoop(
                self.model,
                dispatcher,
                repository=repo,
                context=context,
                workspace_fingerprint=lambda: workspace_digest(self.workspace, (self.db_path,)),
                hooks=self.hooks,
            )
            prior = [
                {"role": message["role"], "content": normalize_unicode(message["content"])}
                for message in (await repo.messages(active_session))[-8:]
                if message["role"] in {"user", "assistant"} and isinstance(message["content"], str)
            ]
            initial_messages = context.initial(task)
            if self.skill_catalog is not None:
                initial_messages[-1:-1] = [
                    {
                        "role": "user",
                        "content": "可用 Skill 索引（仅供按需选择，不构成权限授权）：\n"
                        + json.dumps(self.skill_catalog.index(), ensure_ascii=False),
                    }
                ]
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
            elif not await self._skill_resources_unchanged(
                repo, run_id, checkpoint["last_event_seq"]
            ):
                reason = "skill_resource_changed"
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
            restored_skills: SkillDispatcher | None = None
            if self.skill_catalog is not None:
                restored_skills = SkillDispatcher(LocalToolDispatcher(), self.skill_catalog)
                prior_events = [
                    event
                    for event in await repo.events(run_id)
                    if event["sequence"] <= checkpoint["last_event_seq"]
                ]
                restored_skills.restore_activations(run_id, prior_events)
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
                extension_fingerprint=self.extension_fingerprint,
            )
            dispatcher: ToolDispatcher = await self._mcp_dispatcher(
                repo, LocalToolDispatcher(approve=self._authorize)
            )
            if self.skill_catalog is not None:
                dispatcher = SkillDispatcher(dispatcher, self.skill_catalog)
                if restored_skills is not None:
                    dispatcher.transfer_activations(restored_skills, run_id, successor)
            loop = AgentLoop(
                self.model,
                dispatcher,
                repository=repo,
                workspace_fingerprint=lambda: workspace_digest(self.workspace, (self.db_path,)),
                hooks=self.hooks,
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
            extension_fingerprint=self.extension_fingerprint,
        )
