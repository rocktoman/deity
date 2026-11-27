"""Phase 4 能力测试（ADR-027）：SeatbeltExecutionBackend 与 TUI /timeline /replay /eval-save。"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deity.adapters.execution.seatbelt import SeatbeltExecutionBackend
from deity.application.config import PermissionConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent
from deity.core.contracts.execution import (
    BackendKind,
    ExecutionRequest,
    ResourceBudget,
    SandboxPolicy,
    SandboxSpec,
)
from deity.interfaces.cli.chat_approval import make_approve
from deity.interfaces.tui.approval_bridge import TuiApprovalBridge
from deity.interfaces.tui.interactive import ChatInput, InteractiveApp


class _SimpleModel:
    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        yield ModelEvent("text_delta", text="执行完成")
        yield ModelEvent("message_completed")


def _make_request(
    *,
    policy: SandboxPolicy = SandboxPolicy.WORKSPACE_WRITE,
    network: str = "none",
    argv: tuple[str, ...] = ("echo", "hi"),
) -> ExecutionRequest:
    budget = ResourceBudget(
        timeout_seconds=10.0,
        cpu_count=None,
        memory_bytes=None,
        pids_limit=None,
        output_chars=16_000,
        tmpfs_bytes=None,
    )
    spec = SandboxSpec(
        backend=BackendKind.SEATBELT,
        image=None,
        network=network,
        network_approved=False,
        budget=budget,
        policy=policy,
    )
    return ExecutionRequest(
        execution_id="exec-1",
        run_id="run-1",
        call_id="call-1",
        argv=argv,
        cwd=".",
        spec=spec,
    )


def test_seatbelt_profile_and_argv_enforce_isolation(tmp_path: Path) -> None:
    stage = (tmp_path / "stage").resolve()
    stage.mkdir()

    rw_profile = SeatbeltExecutionBackend.build_profile(stage, SandboxPolicy.WORKSPACE_WRITE)
    assert "(deny default)" in rw_profile
    assert "(deny network*)" in rw_profile
    assert f'(subpath "{stage}")' in rw_profile

    ro_profile = SeatbeltExecutionBackend.build_profile(stage, SandboxPolicy.READ_ONLY)
    assert "(deny network*)" in ro_profile
    assert str(stage) not in ro_profile

    argv = SeatbeltExecutionBackend.create_argv(_make_request(), stage)
    assert argv[0] == "sandbox-exec"
    assert argv[1] == "-p"
    assert argv[-2:] == ("echo", "hi")

    with pytest.raises(ValueError, match="full-access"):
        SeatbeltExecutionBackend.create_argv(_make_request(policy=SandboxPolicy.FULL_ACCESS), stage)
    with pytest.raises(ValueError, match="network=none"):
        SeatbeltExecutionBackend.create_argv(_make_request(network="bridge"), stage)


@pytest.mark.asyncio
async def test_seatbelt_capabilities_and_preflight(tmp_path: Path) -> None:
    stage_root = tmp_path / "staging"
    stage_root.mkdir()
    backend = SeatbeltExecutionBackend(tmp_path, stage_root)
    caps = await backend.capabilities()
    assert caps.kind == BackendKind.SEATBELT
    assert caps.cpu_limit is False
    assert caps.memory_limit is False
    assert caps.pids_limit is False

    bad_req = _make_request(policy=SandboxPolicy.FULL_ACCESS)
    assert await backend.preflight(bad_req) is False


async def _wait_until(predicate: Any, timeout: float = 10.0) -> None:
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return
        await asyncio.sleep(0.05)
        elapsed += 0.05
    raise AssertionError("等待超时")


@pytest.mark.asyncio
async def test_tui_timeline_replay_and_eval_save_commands(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    bridge = TuiApprovalBridge(workspace=tmp_path)
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=_SimpleModel(),  # type: ignore[arg-type]
        approve=make_approve(bridge.controller),
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
    )
    app = InteractiveApp(service, bridge=bridge, model_name="fake", workspace=tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one("#input", ChatInput)
        box.focus()

        box.text = "跑一轮"
        await pilot.press("enter")
        await _wait_until(lambda: not app._run_active and app.last_run_id is not None)
        await pilot.pause()

        for cmd in ("/timeline", "/replay", "/eval-save U01-smoke"):
            box.text = cmd
            await pilot.press("enter")
            await pilot.pause()

        conv = "\n".join(
            str(w.render())
            for w in app.query_one("#conversation").walk_children(with_self=False)
            if callable(getattr(w, "render", None))
        )
        assert "证据时间线" in conv
        assert "只读回放" in conv
        assert "已沉淀评估用例模板" in conv

        saved = tmp_path / "evals" / "cases" / "U01-smoke.yaml"
        assert saved.is_file()
        yaml_body = saved.read_text(encoding="utf-8")
        assert "id: U01-smoke" in yaml_body
        assert f"source_run_id: {app.last_run_id}" in yaml_body

        # 同名用例拒绝覆盖。
        box.text = "/eval-save U01-smoke"
        await pilot.press("enter")
        await pilot.pause()
        conv2 = "\n".join(
            str(w.render())
            for w in app.query_one("#conversation").walk_children(with_self=False)
            if callable(getattr(w, "render", None))
        )
        assert "拒绝覆盖" in conv2
        await pilot.press("ctrl+q")
