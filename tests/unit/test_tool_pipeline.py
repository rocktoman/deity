"""V1.1-M2：ToolPipeline 组装顺序（自 RunService 抽出后的行为等价）。"""

from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools import LocalToolDispatcher
from deity.adapters.tools.planning import PlanningDispatcher
from deity.adapters.tools.turn_diff import TurnDiffDispatcher
from deity.application.tool_pipeline import ToolPipeline
from deity.core.contracts import ExecutionMode, ToolDispatcher


def _pipeline(tmp_path: Path, **overrides: object) -> ToolPipeline:
    kwargs: dict[str, object] = {
        "workspace": tmp_path,
        "db_path": tmp_path / "deity.db",
        "authorize": lambda name, args: True,
        "mcp_wrap": lambda local: _identity(local),
        "sandbox_shell": None,
        "skill_catalog": None,
        "code_intelligence_enabled": False,
        "code_search": lambda: None,  # type: ignore[arg-type]
        "git_guard_enabled": False,
        "require_read_before_write": True,
    }
    kwargs.update(overrides)
    return ToolPipeline(**kwargs)  # type: ignore[arg-type]


async def _identity(local: LocalToolDispatcher) -> ToolDispatcher:
    return local


@pytest.mark.asyncio
async def test_compose_build_returns_local_dispatcher(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        dispatcher, guarded = await _pipeline(tmp_path).compose(repo, ExecutionMode.BUILD)
    finally:
        await repo.close()
    # ADR-018 A4：最外层恒为 TurnDiffDispatcher，内层仍是本地分发器。
    assert isinstance(dispatcher, TurnDiffDispatcher)
    assert isinstance(dispatcher.inner, LocalToolDispatcher)
    assert guarded is None


@pytest.mark.asyncio
async def test_compose_plan_wraps_planning_dispatcher(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        dispatcher, guarded = await _pipeline(tmp_path).compose(repo, ExecutionMode.PLAN)
    finally:
        await repo.close()
    assert isinstance(dispatcher, TurnDiffDispatcher)
    assert isinstance(dispatcher.inner, PlanningDispatcher)
    assert guarded is None


@pytest.mark.asyncio
async def test_approved_plan_writes_bypass_authorize(tmp_path: Path) -> None:
    calls: list[str] = []

    def authorize(name: str, args: dict) -> bool:  # type: ignore[type-arg]
        calls.append(name)
        return False

    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        dispatcher, _ = await _pipeline(tmp_path, authorize=authorize).compose(
            repo, ExecutionMode.BUILD, approved_plan_writes=True
        )
    finally:
        await repo.close()
    assert isinstance(dispatcher, TurnDiffDispatcher)
    local = dispatcher.inner
    assert isinstance(local, LocalToolDispatcher)
    assert local.approve is not None
    assert local.approve("write_file", {}) is True
    assert local.approve("shell", {}) is False
    assert calls == ["shell"]
