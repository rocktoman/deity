"""Compose a provider, tool dispatcher and session repository for one run."""

import asyncio
import hashlib
import importlib
import inspect
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from deity.adapters.code_intelligence import LexicalCodeSearch
from deity.adapters.execution.artifacts import ArtifactApplier
from deity.adapters.execution.docker import DockerExecutionBackend
from deity.adapters.git import GitWorkspaceAdapter
from deity.adapters.mcp import McpCompositeDispatcher, McpServerConfig, McpSessionManager, McpTool
from deity.adapters.model.anthropic import AnthropicProvider
from deity.adapters.model.openai_compatible import OpenAICompatibleProvider
from deity.adapters.skills import SkillCatalog, SkillDispatcher
from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools import LocalToolDispatcher, auto_shell_allowed
from deity.adapters.tools.code_intelligence import CodeIntelligenceDispatcher
from deity.adapters.tools.delegation import DelegationDispatcher
from deity.adapters.tools.git_guard import GitGuardedDispatcher
from deity.adapters.tools.planning import PlanningDispatcher
from deity.adapters.tools.read_only import ReadOnlyToolDispatcher
from deity.adapters.tools.workspace import workspace_digest
from deity.application.config import (
    CodeIntelligenceConfig,
    CodingConfig,
    Config,
    GitGuardConfig,
    McpServerSettings,
    MultiAgentConfig,
    PermissionConfig,
    RunConfig,
    SandboxConfig,
    VerificationConfig,
)
from deity.application.sandbox_service import SandboxService, SandboxUnavailable
from deity.core.context import HARNESS_SYSTEM_INSTRUCTIONS, ContextBuilder
from deity.core.context.reliable import normalize_unicode
from deity.core.contracts import (
    AgentRequest,
    AgentResult,
    ExecutionMode,
    ModelProvider,
    RecoveryDecision,
    RunStatus,
    ToolCall,
    ToolDispatcher,
    ToolResult,
)
from deity.core.contracts.delegation import (
    DelegationRequest,
    ReadOnlyAgentProfile,
    SubAgentResult,
)
from deity.core.contracts.execution import (
    ArtifactManifest,
    BackendKind,
    ExecutionRequest,
    ExecutionStatus,
    ResourceBudget,
    SandboxHandle,
    SandboxSpec,
)
from deity.core.delegation import DelegationPolicy, ResultReducer
from deity.core.hooks import HookRegistry, RegisteredHook
from deity.core.runtime import AgentLoop, BudgetExceeded, ModelFailure, PolicyVeto, ProtocolError


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
        coding: CodingConfig | None = None,
        code_intelligence: CodeIntelligenceConfig | None = None,
        git_guard: GitGuardConfig | None = None,
        verification: VerificationConfig | None = None,
        coding_fingerprint: str = "",
        sandbox: SandboxConfig | None = None,
        multi_agent: MultiAgentConfig | None = None,
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
        self.coding = coding or CodingConfig()
        self.code_intelligence = code_intelligence or CodeIntelligenceConfig()
        self.git_guard = git_guard or GitGuardConfig()
        self.verification = verification or VerificationConfig()
        self.sandbox = sandbox or SandboxConfig()
        self.multi_agent = multi_agent or MultiAgentConfig()
        self.multi_agent_fingerprint = (
            hashlib.sha256(
                json.dumps(asdict(self.multi_agent), sort_keys=True).encode()
            ).hexdigest()
            if self.multi_agent.enabled
            else ""
        )
        self.coding_fingerprint = (
            hashlib.sha256(
                (coding_fingerprint + json.dumps(asdict(self.sandbox), sort_keys=True)).encode()
            ).hexdigest()
            if self.sandbox.enabled
            else coding_fingerprint
        )
        if self.multi_agent.enabled:
            self.coding_fingerprint = hashlib.sha256(
                (self.coding_fingerprint + self.multi_agent_fingerprint).encode()
            ).hexdigest()
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
        self._active_children: dict[str, AgentLoop] = {}
        self._sandbox_active_run_id: str | None = None

    def cancel(self, run_id: str) -> bool:
        """Signal cancellation to this service's active run."""
        cancelled = self._active_loop.cancel(run_id) if self._active_loop is not None else False
        for child_id, loop in tuple(self._active_children.items()):
            if cancelled or child_id == run_id:
                cancelled = loop.cancel(child_id) or cancelled
        return cancelled

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
            coding=config.coding,
            code_intelligence=config.code_intelligence,
            git_guard=config.git_guard,
            verification=config.verification,
            coding_fingerprint=(
                config.coding_fingerprint()
                if any(
                    (
                        config.coding.plan_build_enabled,
                        config.coding.code_intelligence_enabled,
                        config.coding.git_guard_enabled,
                        config.coding.verification_enabled,
                    )
                )
                else ""
            ),
            sandbox=config.sandbox,
            multi_agent=config.multi_agent,
        )

    async def _delegate(
        self, contracts: tuple[DelegationRequest, ...], parent: AgentRequest
    ) -> str:
        """Run a bounded child batch with independent contexts and durable lineage."""
        model = self.model
        if model is None or not self.multi_agent.enabled:
            raise ValueError("multi-agent is disabled")
        DelegationPolicy(
            ReadOnlyAgentProfile(
                self.multi_agent.max_depth,
                self.multi_agent.max_children,
                self.multi_agent.max_concurrency,
                self.multi_agent.max_result_chars,
            )
        ).validate_batch(contracts, parent)
        repo = await SQLiteSessionRepository.open(self.db_path)
        children: list[tuple[DelegationRequest, AgentRequest, str]] = []
        try:
            try:
                for contract in contracts:
                    budget = contract.budget
                    child = AgentRequest(
                        session_id=parent.session_id,
                        task_id=str(uuid4()),
                        workspace=str(self.workspace),
                        user_text=contract.goal,
                        model=parent.model,
                        max_turns=budget.max_turns,
                        max_tool_calls=budget.max_tool_calls,
                        deadline_seconds=min(budget.deadline_seconds, parent.deadline_seconds),
                        max_input_tokens_total=budget.max_input_tokens_total,
                        max_output_tokens_total=budget.max_output_tokens_total,
                        context_window_tokens=self.context_window_tokens,
                        provider_name=self.provider_id,
                        policy_fingerprint=self.multi_agent_fingerprint,
                    )
                    child_id = await repo.create_delegation(
                        contract,
                        child,
                        max_depth=self.multi_agent.max_depth,
                        max_children=self.multi_agent.max_children,
                        parent_deadline_seconds=parent.deadline_seconds,
                        parent_input_cap=parent.max_input_tokens_total,
                        parent_output_cap=parent.max_output_tokens_total,
                    )
                    children.append((contract, child, child_id))
            except BaseException:
                for contract, _child, child_id in children:
                    if await repo.status(child_id) == RunStatus.CREATED:
                        await repo.set_status(child_id, RunStatus.CANCELLED)
                        await repo.finish_delegation(
                            SubAgentResult(contract.delegation_id, child_id, "cancelled", "")
                        )
                raise
            semaphore = asyncio.Semaphore(self.multi_agent.max_concurrency)
            results: list[SubAgentResult | None] = [None] * len(children)

            async def run_one(
                index: int, item: tuple[DelegationRequest, AgentRequest, str]
            ) -> None:
                contract, child, child_id = item
                try:
                    async with semaphore:
                        child_repo = await SQLiteSessionRepository.open(self.db_path)
                        loop = AgentLoop(
                            model,
                            ReadOnlyToolDispatcher(contract.allowed_tools),
                            repository=child_repo,
                            context=ContextBuilder(system_instructions=HARNESS_SYSTEM_INSTRUCTIONS),
                        )
                        self._active_children[child_id] = loop
                        try:
                            result = await loop.run_created(
                                child,
                                child_id,
                                ContextBuilder(
                                    system_instructions=HARNESS_SYSTEM_INSTRUCTIONS
                                ).initial(contract.goal),
                            )
                            evidence_ids = tuple(
                                [
                                    await child_repo.record_evidence(
                                        child_id, "delegation_read", name, call_id
                                    )
                                    for call_id, name in await child_repo.completed_read_calls(
                                        child_id
                                    )
                                ]
                            )
                            sub_result = SubAgentResult(
                                contract.delegation_id,
                                child_id,
                                "completed",
                                (result.final_text or "")[: self.multi_agent.max_result_chars],
                                evidence_ids,
                            )
                        except asyncio.CancelledError:
                            raise
                        except (
                            BudgetExceeded,
                            ModelFailure,
                            OSError,
                            PolicyVeto,
                            ProtocolError,
                            TimeoutError,
                            ValueError,
                        ):
                            sub_result = SubAgentResult(
                                contract.delegation_id, child_id, "failed", ""
                            )
                        finally:
                            self._active_children.pop(child_id, None)
                            await child_repo.close()
                        await repo.finish_delegation(sub_result)
                        results[index] = sub_result
                finally:
                    if results[index] is None:
                        state = await repo.status(child_id)
                        if state == RunStatus.CREATED:
                            await repo.set_status(child_id, RunStatus.CANCELLED)
                            state = RunStatus.CANCELLED
                        if state in {RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.INTERRUPTED}:
                            status: Literal["cancelled", "failed", "interrupted"] = (
                                "cancelled"
                                if state == RunStatus.CANCELLED
                                else "failed"
                                if state == RunStatus.FAILED
                                else "interrupted"
                            )
                            await repo.finish_delegation(
                                SubAgentResult(
                                    contract.delegation_id,
                                    child_id,
                                    status,
                                    "",
                                )
                            )

            queue: asyncio.Queue[tuple[int, tuple[DelegationRequest, AgentRequest, str]]] = (
                asyncio.Queue()
            )
            for index, item in enumerate(children):
                queue.put_nowait((index, item))

            async def worker() -> None:
                while not queue.empty():
                    index, item = queue.get_nowait()
                    try:
                        await run_one(index, item)
                    finally:
                        queue.task_done()

            try:
                async with asyncio.TaskGroup() as group:
                    for _ in range(min(self.multi_agent.max_concurrency, len(children))):
                        group.create_task(worker())
            finally:
                for contract, _child, child_id in children:
                    if await repo.status(child_id) == RunStatus.CREATED:
                        await repo.set_status(child_id, RunStatus.CANCELLED)
                        await repo.finish_delegation(
                            SubAgentResult(contract.delegation_id, child_id, "cancelled", "")
                        )
            return ResultReducer.reduce(tuple(result for result in results if result is not None))
        finally:
            await repo.close()

    async def _sandbox_shell(
        self,
        repo: SQLiteSessionRepository,
        call: ToolCall,
        context: AgentRequest,
        guard: GitGuardedDispatcher | None = None,
    ) -> ToolResult:
        """Route an approved shell call through the required Docker backend."""
        if not context.run_id:
            raise ValueError("sandbox run identity missing")
        self._sandbox_active_run_id = context.run_id
        config = self.sandbox
        backend = DockerExecutionBackend(
            self.workspace,
            Path(tempfile.gettempdir()) / "deity-sandbox",
            excluded_paths=(
                self.db_path,
                self.db_path.with_name(self.db_path.name + "-wal"),
                self.db_path.with_name(self.db_path.name + "-shm"),
                *(self.db_path.with_suffix(f".v{version}.bak") for version in range(1, 5)),
            ),
            excluded_patterns=config.exclude,
            max_workspace_files=config.max_workspace_files,
            max_workspace_bytes=config.max_workspace_bytes,
            max_artifact_files=config.max_artifact_files,
            max_artifact_bytes=config.max_artifact_bytes,
            cleanup_timeout_seconds=config.cleanup_timeout_seconds,
        )
        budget = ResourceBudget(
            config.timeout_seconds,
            config.cpu_count,
            config.memory_mb * 1024 * 1024,
            config.pids_limit,
            config.max_output_chars,
            config.tmpfs_mb * 1024 * 1024,
        )
        network_approved = config.network == "none" or (
            self.approve is not None
            and self.approve("sandbox_network", {"network": config.network})
        )
        if not network_approved:
            return ToolResult(call.call_id, "denied", "sandbox_network_approval_required")
        request = ExecutionRequest(
            str(uuid4()),
            context.run_id,
            call.call_id,
            tuple(call.arguments["argv"]),
            ".",
            SandboxSpec(
                BackendKind.DOCKER,
                config.image,
                budget,
                network=config.network,
                network_approved=network_approved,
                env_names=config.env_names,
                artifact_policy=config.artifact_policy,
            ),
        )
        reviewer = None
        if config.artifact_policy == "review":
            snapshot = await GitWorkspaceAdapter(self.workspace, ignored=(self.db_path,)).capture()
            await repo.create_workspace_snapshot(snapshot, run_id=context.run_id)
            dirty = {
                item.path
                for item in snapshot.files
                if snapshot.git_head is not None and item.status not in {"!", "clean", "absent"}
            }
            submodules = {
                item.path for item in snapshot.files if item.status.startswith("submodule")
            }
            allow_dirty = set(
                await repo.plan_build_allowlist(context.run_id) if context.plan_id else ()
            )
            plan_targets = None
            if context.plan_id:
                plan = await repo.get_plan(context.plan_id)
                plan_targets = {path for step in plan.draft.steps for path in step.files}

            async def review(handle: SandboxHandle, manifest: ArtifactManifest) -> ArtifactManifest:
                if handle.staging_root is None:
                    raise ValueError("staging not available for review")
                baseline = {
                    entry.path: (entry.before_sha256, 0)
                    for entry in manifest.entries
                    if entry.before_sha256 is not None
                }
                applier = ArtifactApplier(self.workspace, Path(handle.staging_root), baseline)
                change_set_id = await repo.ensure_change_set(context.run_id, snapshot.snapshot_id)
                statuses: list[str] = []
                for ordinal, entry in enumerate(manifest.entries, 1):
                    await repo.prepare_artifact_apply(
                        context.run_id, manifest.execution_id, ordinal
                    )
                    if (
                        (plan_targets is not None and entry.path not in plan_targets)
                        or (entry.path in dirty and entry.path not in allow_dirty)
                        or (entry.operation == "delete" and entry.path in dirty)
                        or any(
                            entry.path == path or entry.path.startswith(f"{path}/")
                            for path in submodules
                        )
                        or (
                            guard is not None
                            and guard.require_read_before_write
                            and entry.operation != "create"
                            and guard.preimages.get((context.run_id, entry.path))
                            != entry.before_sha256
                        )
                        or not self._authorize("apply_patch", {"path": entry.path})
                    ):
                        await repo.complete_artifact_apply(
                            context.run_id, manifest.execution_id, ordinal, "denied"
                        )
                        statuses.append("denied")
                        continue
                    try:
                        change = applier.apply(entry, call.call_id)
                    except ValueError:
                        await repo.complete_artifact_apply(
                            context.run_id, manifest.execution_id, ordinal, "denied"
                        )
                        statuses.append("denied")
                    except BaseException:
                        await repo.complete_artifact_apply(
                            context.run_id, manifest.execution_id, ordinal, "unknown"
                        )
                        raise
                    else:
                        await repo.complete_artifact_apply(
                            context.run_id,
                            manifest.execution_id,
                            ordinal,
                            "applied",
                            change_set_id,
                            change,
                        )
                        statuses.append("applied")
                final_status: Literal["applied", "rejected", "partial"] = (
                    "applied"
                    if all(status == "applied" for status in statuses)
                    else "rejected"
                    if all(status == "denied" for status in statuses)
                    else "partial"
                )
                await repo.finalize_artifact_manifest(
                    context.run_id, manifest.execution_id, final_status
                )
                return replace(manifest, status=final_status)

            reviewer = review
        try:
            result, manifest = await SandboxService(backend, repo, reviewer=reviewer).run(request)
        except SandboxUnavailable as exc:
            return ToolResult(call.call_id, "denied", str(exc))
        if result.status == ExecutionStatus.UNKNOWN:
            raise RuntimeError("sandbox result unknown")
        if manifest.status == "partial":
            raise RuntimeError("sandbox artifact apply partial")
        if manifest.status in {"rejected", "unknown"}:
            return ToolResult(
                call.call_id, "error", f"artifact {manifest.status}", result.exit_code
            )
        status: Literal["ok", "error", "timeout"] = (
            "ok"
            if result.status == ExecutionStatus.SUCCEEDED
            else "timeout"
            if result.status == ExecutionStatus.TIMED_OUT
            else "error"
        )
        return ToolResult(call.call_id, status, result.output, result.exit_code, result.truncated)

    async def _mcp_dispatcher(
        self,
        local: LocalToolDispatcher,
        server_name: str | None = None,
    ) -> McpCompositeDispatcher | LocalToolDispatcher:
        if not self.mcp_enabled:
            return local
        if server_name is not None and server_name not in {s.name for s in self.mcp_servers}:
            raise ValueError(f"MCP server 未配置：{server_name}")
        configs: list[McpServerConfig] = []
        for server in self.mcp_servers:
            if server_name is not None and server.name != server_name:
                continue
            cwd = (self.workspace / server.cwd).resolve(strict=True)
            if not cwd.is_relative_to(self.workspace):
                raise ValueError("MCP server working directory escapes workspace")
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
        return McpCompositeDispatcher(local, manager, tuple(discovered))

    def mcp_list(self) -> list[str]:
        """Return configured server state without starting processes."""
        state = "enabled" if self.mcp_enabled else "disabled"
        return [f"{server.name} [{state}] not_started" for server in self.mcp_servers]

    async def mcp_test(self, server_name: str | None = None) -> list[str]:
        """Connect and validate schemas without business calls."""
        dispatcher = await self._mcp_dispatcher(LocalToolDispatcher(), server_name)
        if isinstance(dispatcher, McpCompositeDispatcher):
            return sorted(dispatcher.tools)
        return []

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

    def _code_search(self) -> LexicalCodeSearch:
        cache_path: Path | None = None
        if self.code_intelligence.cache_enabled:
            cache_root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
            workspace_id = hashlib.sha256(str(self.workspace).encode()).hexdigest()[:24]
            cache_path = cache_root / "deity/code-index" / f"{workspace_id}.json"
        return LexicalCodeSearch(
            self.workspace,
            cache_path=cache_path,
            max_files=self.code_intelligence.max_files,
            max_file_bytes=self.code_intelligence.max_file_bytes,
        )

    async def _compose_dispatcher(
        self,
        repo: SQLiteSessionRepository,
        mode: ExecutionMode,
        *,
        baseline: Any | None = None,
        allow_dirty_paths: tuple[str, ...] = (),
        change_set_id: str | None = None,
        plan_targets: tuple[str, ...] | None = None,
        approved_plan_writes: bool = False,
    ) -> tuple[ToolDispatcher, GitGuardedDispatcher | None]:
        def authorize(name: str, args: dict[str, Any]) -> bool:
            if approved_plan_writes and name in {"write_file", "apply_patch", "delete_file"}:
                return True
            return self._authorize(name, args)

        guard_ref: list[GitGuardedDispatcher | None] = [None]
        dispatcher: ToolDispatcher = await self._mcp_dispatcher(
            LocalToolDispatcher(
                approve=authorize,
                sandbox_shell=(
                    lambda call, context: self._sandbox_shell(repo, call, context, guard_ref[0])
                )
                if self.sandbox.enabled and self.sandbox.backend == "docker"
                else None,
            )
        )
        if self.skill_catalog is not None:
            dispatcher = SkillDispatcher(dispatcher, self.skill_catalog)
        if self.coding.code_intelligence_enabled:
            dispatcher = CodeIntelligenceDispatcher(dispatcher, self._code_search(), repo)
        if mode == ExecutionMode.PLAN:
            return (
                PlanningDispatcher(
                    dispatcher,
                    repo,
                    GitWorkspaceAdapter(self.workspace, ignored=(self.db_path,)),
                ),
                None,
            )
        if self.coding.git_guard_enabled:
            if baseline is None:
                baseline = await GitWorkspaceAdapter(
                    self.workspace, ignored=(self.db_path,)
                ).capture()
                await repo.create_workspace_snapshot(baseline)
            guarded = GitGuardedDispatcher(
                dispatcher,
                repo,
                baseline,
                allow_dirty_paths=allow_dirty_paths,
                require_read_before_write=self.git_guard.require_read_before_write,
                change_set_id=change_set_id,
                plan_targets=plan_targets,
            )
            guard_ref[0] = guarded
            return guarded, guarded
        return dispatcher, None

    async def run(
        self,
        task: str,
        *,
        model_name: str,
        session_id: str | None = None,
        execution_mode: ExecutionMode = ExecutionMode.BUILD,
    ) -> AgentResult:
        """Execute and persist a task within the selected workspace."""
        if self.model is None:
            raise ValueError("model is required to run")
        if execution_mode == ExecutionMode.PLAN and not self.coding.plan_build_enabled:
            raise ValueError("Plan/Build is disabled")
        task = normalize_unicode(task)
        repo = await SQLiteSessionRepository.open(self.db_path)
        try:
            dispatcher, guarded = await self._compose_dispatcher(repo, execution_mode)
            if self.multi_agent.enabled and execution_mode == ExecutionMode.BUILD:
                dispatcher = DelegationDispatcher(
                    dispatcher, self._delegate, self.multi_agent.max_children
                )
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
                execution_mode=execution_mode,
                policy_fingerprint=self.coding_fingerprint,
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
            if execution_mode == ExecutionMode.PLAN:
                initial_messages.insert(
                    1,
                    {
                        "role": "system",
                        "content": (
                            "当前运行是只读 Plan。先按需读取工作区，完成分析后必须调用 "
                            "submit_plan 提交结构化计划；最终文字回答不能代替计划提交。"
                            "步骤编号从 1 连续递增，字段严格使用 ordinal/title/description/"
                            "files/verification。工具返回 invalid_plan 时，修正参数并重新提交。"
                        ),
                    },
                )
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
            try:
                result = await loop.run(request, initial_messages=initial_messages)
            except BaseException:
                if guarded is not None and guarded.run_id is not None:
                    await repo.finalize_change_set(guarded.run_id, "unknown")
                elif (
                    self.sandbox.enabled
                    and self.sandbox.artifact_policy == "review"
                    and self._sandbox_active_run_id is not None
                ):
                    await repo.finalize_change_set(self._sandbox_active_run_id, "unknown")
                raise
            if guarded is not None and guarded.change_set_id is not None:
                diff = await GitWorkspaceAdapter(self.workspace, ignored=(self.db_path,)).diff()
                await repo.record_evidence(result.run_id, "git_diff", diff)
                await repo.finalize_change_set(
                    result.run_id,
                    "completed" if result.status == RunStatus.COMPLETED else "unknown",
                )
            elif self.sandbox.enabled and self.sandbox.artifact_policy == "review":
                await repo.finalize_change_set(
                    result.run_id,
                    "completed" if result.status == RunStatus.COMPLETED else "unknown",
                )
            await repo.append_message(active_session, result.run_id, "user", task)
            await repo.append_message(
                active_session, result.run_id, "assistant", result.final_text or ""
            )
            return result
        finally:
            self._active_loop = None
            self._sandbox_active_run_id = None
            await repo.close()

    async def run_build(self, build_run_id: str, plan_id: str) -> AgentResult:
        """Execute a pre-created Build Run without accepting requirement changes."""
        if self.model is None:
            raise ValueError("model is required to run")
        if not self.coding.plan_build_enabled or not self.coding.git_guard_enabled:
            raise ValueError("Plan/Build with Git Guard is required")
        repo = await SQLiteSessionRepository.open(self.db_path)
        try:
            plan = await repo.get_plan(plan_id)
            info = await repo.run_info(build_run_id)
            change_set = await repo.change_set_for_run(build_run_id)
            baseline = await repo.get_workspace_snapshot(change_set.baseline_snapshot_id)
            allow_dirty = await repo.plan_build_allowlist(build_run_id)
            targets = tuple(sorted({path for step in plan.draft.steps for path in step.files}))
            current = await GitWorkspaceAdapter(self.workspace, ignored=(self.db_path,)).capture(
                targets
            )
            if current.digest != baseline.digest:
                raise ValueError("workspace drift before build execution")
            dispatcher, _guarded = await self._compose_dispatcher(
                repo,
                ExecutionMode.BUILD,
                baseline=baseline,
                allow_dirty_paths=allow_dirty,
                change_set_id=change_set.change_set_id,
                plan_targets=targets,
                approved_plan_writes=True,
            )
            if self.multi_agent.enabled:
                dispatcher = DelegationDispatcher(
                    dispatcher, self._delegate, self.multi_agent.max_children
                )
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
                provider_name=self.provider_id or info["model_provider"],
                extension_fingerprint=self.extension_fingerprint,
                execution_mode=ExecutionMode.BUILD,
                plan_id=plan_id,
                policy_fingerprint=self.coding_fingerprint,
            )
            context = ContextBuilder(system_instructions=HARNESS_SYSTEM_INSTRUCTIONS)
            messages = context.initial(info["prompt"])
            messages[-1:-1] = [
                {
                    "role": "user",
                    "content": (
                        "已批准且不可改变的结构化计划：\n"
                        + json.dumps(asdict(plan.draft), ensure_ascii=False)
                    ),
                }
            ]
            loop = AgentLoop(
                self.model,
                dispatcher,
                repository=repo,
                context=context,
                workspace_fingerprint=lambda: workspace_digest(self.workspace, (self.db_path,)),
                hooks=self.hooks,
            )
            self._active_loop = loop
            try:
                result = await loop.run_created(request, build_run_id, messages)
            except BaseException:
                await repo.finalize_change_set(build_run_id, "unknown")
                raise
            diff = await GitWorkspaceAdapter(self.workspace, ignored=(self.db_path,)).diff()
            await repo.record_evidence(build_run_id, "git_diff", diff)
            await repo.finalize_change_set(
                build_run_id, "completed" if result.status == RunStatus.COMPLETED else "unknown"
            )
            await repo.append_message(
                info["session_id"], build_run_id, "assistant", result.final_text or ""
            )
            return result
        finally:
            self._active_loop = None
            self._sandbox_active_run_id = None
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
            elif self.sandbox.enabled or await repo.has_sandbox_execution(run_id):
                decision, reason = "manual_review", "sandbox_resume_requires_reconciliation"
            elif await repo.has_delegations(run_id):
                decision, reason = "manual_review", "delegation_requires_reconciliation"
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
                LocalToolDispatcher(approve=self._authorize)
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
            policy_fingerprint=self.coding_fingerprint,
        )
