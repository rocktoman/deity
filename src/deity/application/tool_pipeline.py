"""工具管线组装（V1.1-M2 自 RunService 抽出）：按能力顺序叠加分发器。

组装顺序（与抽取前 RunService._compose_dispatcher 完全一致）：
LocalToolDispatcher（可选 sandbox shell 路由）→ MCP 组合 → Skill →
代码智能 → Plan 只读包装 / Git 守卫包装 → TurnDiff（最外层，
ADR-018 A4）。RunService 只保留单步能力
的实现（_authorize、_mcp_dispatcher、_sandbox_shell、_code_search），
组装顺序、计划模式的已批写放行与守卫回填（guard_ref）归本模块。
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from deity.adapters.code_intelligence import LexicalCodeSearch
from deity.adapters.git import GitWorkspaceAdapter
from deity.adapters.skills import SkillCatalog, SkillDispatcher
from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools import LocalToolDispatcher
from deity.adapters.tools.code_intelligence import CodeIntelligenceDispatcher
from deity.adapters.tools.git_guard import GitGuardedDispatcher
from deity.adapters.tools.planning import PlanningDispatcher
from deity.adapters.tools.turn_diff import TurnDiffDispatcher
from deity.core.contracts import (
    AgentRequest,
    ExecutionMode,
    ToolCall,
    ToolDispatcher,
    ToolResult,
)

SandboxShell = Callable[
    [SQLiteSessionRepository, ToolCall, AgentRequest, GitGuardedDispatcher | None],
    Awaitable[ToolResult],
]


class ToolPipeline:
    """把基础工具分发器按配置叠加成一次 Run 的最终分发器。"""

    def __init__(
        self,
        *,
        workspace: Path,
        db_path: Path,
        authorize: Callable[[str, dict[str, Any]], bool],
        mcp_wrap: Callable[[LocalToolDispatcher], Awaitable[ToolDispatcher]],
        sandbox_shell: SandboxShell | None,
        skill_catalog: SkillCatalog | None,
        code_intelligence_enabled: bool,
        code_search: Callable[[], LexicalCodeSearch],
        git_guard_enabled: bool,
        require_read_before_write: bool,
    ) -> None:
        self._workspace = workspace
        self._db_path = db_path
        self._authorize = authorize
        self._mcp_wrap = mcp_wrap
        self._sandbox_shell = sandbox_shell
        self._skill_catalog = skill_catalog
        self._code_intelligence_enabled = code_intelligence_enabled
        self._code_search = code_search
        self._git_guard_enabled = git_guard_enabled
        self._require_read_before_write = require_read_before_write

    async def compose(
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
        """按能力顺序组装分发器；返回（分发器, Git 守卫或 None）。"""

        def authorize(name: str, args: dict[str, Any]) -> bool:
            if approved_plan_writes and name in {"write_file", "apply_patch", "delete_file"}:
                return True
            return self._authorize(name, args)

        guard_ref: list[GitGuardedDispatcher | None] = [None]
        sandbox_shell = self._sandbox_shell
        routed_shell: Callable[[ToolCall, AgentRequest], Awaitable[ToolResult]] | None = None
        if sandbox_shell is not None:
            routed_shell = lambda call, context: sandbox_shell(repo, call, context, guard_ref[0])
        local = LocalToolDispatcher(approve=authorize, sandbox_shell=routed_shell)
        dispatcher: ToolDispatcher = await self._mcp_wrap(local)
        if self._skill_catalog is not None:
            dispatcher = SkillDispatcher(dispatcher, self._skill_catalog)
        if self._code_intelligence_enabled:
            dispatcher = CodeIntelligenceDispatcher(dispatcher, self._code_search(), repo)
        if mode == ExecutionMode.PLAN:
            return (
                TurnDiffDispatcher(
                    PlanningDispatcher(
                        dispatcher,
                        repo,
                        GitWorkspaceAdapter(self._workspace, ignored=(self._db_path,)),
                    )
                ),
                None,
            )
        if self._git_guard_enabled:
            if baseline is None:
                baseline = await GitWorkspaceAdapter(
                    self._workspace, ignored=(self._db_path,)
                ).capture()
                await repo.create_workspace_snapshot(baseline)
            guarded = GitGuardedDispatcher(
                dispatcher,
                repo,
                baseline,
                allow_dirty_paths=allow_dirty_paths,
                require_read_before_write=self._require_read_before_write,
                change_set_id=change_set_id,
                plan_targets=plan_targets,
            )
            guard_ref[0] = guarded
            return TurnDiffDispatcher(guarded), guarded
        return TurnDiffDispatcher(dispatcher), None


__all__ = ["ToolPipeline"]
