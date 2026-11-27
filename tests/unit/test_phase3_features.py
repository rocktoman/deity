"""Phase 3 能力测试（ADR-026）：RepoMapBuilder、RoutedModelProvider 与 /map /route /ps /kill。"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from deity.adapters.code_intelligence.repo_map import RepoMapBuilder
from deity.adapters.execution.session_process import ShellSessionManager
from deity.adapters.model.router import ModelRoute, RoutedModelProvider
from deity.application.config import PermissionConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelCapabilities, ModelEvent, ModelRequest
from deity.core.runtime import ModelFailure
from deity.interfaces.cli.chat_approval import make_approve
from deity.interfaces.tui.approval_bridge import TuiApprovalBridge
from deity.interfaces.tui.interactive import ChatInput, InteractiveApp


class _ScriptedProvider:
    def __init__(self, events: list[ModelEvent], *, fail_after: int | None = None) -> None:
        self._events = events
        self._fail_after = fail_after
        self.seen_models: list[str] = []

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(True, True, True)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.seen_models.append(request.model)
        if self._fail_after == 0:
            raise ModelFailure("model_rate_limited", retryable=True)
        for idx, ev in enumerate(self._events, start=1):
            yield ev
            if self._fail_after == idx:
                raise ModelFailure("model_connection", retryable=True)


def test_repo_map_builder_extracts_python_ast_and_ts_symbols(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "service.py").write_text(
        "class Engine:\n    def run(self, x: int) -> None:\n        pass\n\n"
        "async def helper(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    (tmp_path / "web.ts").write_text(
        "export interface Config { id: string }\nexport function boot() {}\n",
        encoding="utf-8",
    )
    result = RepoMapBuilder(tmp_path).build()
    assert result.file_count == 2
    assert result.symbol_count >= 4
    assert not result.truncated
    assert "class Engine" in result.text
    assert "def run(self, x)" in result.text
    assert "async def helper(a, b)" in result.text
    assert "interface Config" in result.text
    assert "function boot" in result.text

    capped = RepoMapBuilder(tmp_path, max_chars=40).build()
    assert capped.truncated is True


@pytest.mark.asyncio
async def test_routed_model_provider_fallbacks_safely_and_blocks_post_semantic_fallback() -> None:
    failing_primary = _ScriptedProvider([], fail_after=0)
    backup = _ScriptedProvider(
        [ModelEvent("text_delta", text="来自备用模型"), ModelEvent("message_completed")]
    )
    fast = _ScriptedProvider(
        [ModelEvent("text_delta", text="轻量回答"), ModelEvent("message_completed")]
    )
    router = RoutedModelProvider(
        ModelRoute("primary", failing_primary, "pro-model"),
        fallbacks=(ModelRoute("backup", backup, "backup-model"),),
        fast_route=ModelRoute("fast", fast, "flash-model"),
    )

    # 无工具请求优先走 fast_route。
    fast_req = ModelRequest("default", ({"role": "user", "content": "hi"},), ())
    fast_events = [ev async for ev in router.stream(fast_req)]
    assert any(ev.text == "轻量回答" for ev in fast_events)
    assert router.last_route_name == "fast"
    assert fast.seen_models == ["flash-model"]

    # 带工具请求走 primary，失败且未产生语义输出时安全降级到 backup。
    tool_req = ModelRequest(
        "default",
        ({"role": "user", "content": "edit"},),
        ({"name": "read_file", "parameters": {}},),
    )
    fallback_events = [ev async for ev in router.stream(tool_req)]
    assert any(ev.text == "来自备用模型" for ev in fallback_events)
    assert router.last_route_name == "backup"
    assert router.fallback_history == [("primary", "backup", "model_rate_limited")]
    assert "已触发降级次数：1" in router.status_summary()

    # 一旦已产生语义输出再报错，绝不切换重放。
    partial_primary = _ScriptedProvider([ModelEvent("text_delta", text="半截")], fail_after=1)
    strict_router = RoutedModelProvider(
        ModelRoute("primary", partial_primary, "pro-model"),
        fallbacks=(ModelRoute("backup", backup, "backup-model"),),
    )
    with pytest.raises(ModelFailure):
        _ = [ev async for ev in strict_router.stream(tool_req)]
    assert strict_router.fallback_history == []


@pytest.mark.asyncio
async def test_shell_session_manager_list_sessions(tmp_path: Path) -> None:
    mgr = ShellSessionManager()
    await mgr.start("bg1", ("sleep", "10"), tmp_path)
    try:
        listed = mgr.list_sessions()
        assert len(listed) == 1
        assert listed[0]["name"] == "bg1"
        assert listed[0]["running"] is True
    finally:
        await mgr.close_all()


@pytest.mark.asyncio
async def test_tui_map_route_ps_and_kill_commands(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    (tmp_path / "demo.py").write_text("def greet(name):\n    return name\n", encoding="utf-8")

    primary = _ScriptedProvider(
        [ModelEvent("text_delta", text="ok"), ModelEvent("message_completed")]
    )
    router = RoutedModelProvider(ModelRoute("primary", primary, "main-model"))
    bridge = TuiApprovalBridge(workspace=tmp_path)
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=router,
        approve=make_approve(bridge.controller),
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
    )
    app = InteractiveApp(service, bridge=bridge, model_name="main-model", workspace=tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#input", ChatInput)
        box.focus()

        for cmd in ("/map", "/route", "/ps", "/kill missing"):
            box.text = cmd
            await pilot.press("enter")
            await pilot.pause()

        conv = "\n".join(
            str(w.render())
            for w in app.query_one("#conversation").walk_children(with_self=False)
            if callable(getattr(w, "render", None))
        )
        assert "def greet(name)" in conv
        assert "主路由：primary" in conv
        assert "当前没有运行中的后台 Shell 会话" in conv
        assert "未找到后台 Shell 会话：missing" in conv
        await pilot.press("ctrl+q")
