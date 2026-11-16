"""ADR-021：可写委派契约、信任分级、patch 回收构建与结果收敛。"""

import json
from pathlib import Path

import pytest

from deity.adapters.execution.workspace import collect_manifest, file_hashes
from deity.adapters.tools.patch import plan_patch
from deity.adapters.tools.writable_child import WritableChildDispatcher
from deity.application.delegation_patch import build_patch
from deity.core.contracts import AgentRequest, ToolCall
from deity.core.contracts.delegation import DelegationBudget, DelegationRequest, SubAgentResult
from deity.core.delegation import ResultReducer, grade_delegation

BUDGET = DelegationBudget(2, 4, 30)


def _contract(tools: tuple[str, ...], mode: str = "read_only", budget: DelegationBudget = BUDGET):
    return DelegationRequest("d1", "parent", "goal", tools, budget, mode=mode)  # type: ignore[arg-type]


def test_writable_contract_accepts_write_tools() -> None:
    contract = _contract(("read_file", "write_file", "apply_patch"), mode="writable")
    assert contract.mode == "writable"


@pytest.mark.parametrize("tools", [("shell",), ("git_status",), ("write_file", "shell")])
def test_writable_contract_rejects_shell_and_git(tools: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        _contract(tools, mode="writable")


def test_read_only_contract_still_rejects_write_tools() -> None:
    with pytest.raises(ValueError):
        _contract(("write_file",), mode="read_only")


def test_grade_delegation_levels() -> None:
    assert grade_delegation((_contract(("read_file",)),)) == "standard"
    assert grade_delegation((_contract(("write_file",), mode="writable"),)) == "elevated"
    assert grade_delegation((_contract(("write_file", "apply_patch"), mode="writable"),)) == "high"
    big = DelegationBudget(16, 4, 30)
    assert grade_delegation((_contract(("write_file",), mode="writable", budget=big),)) == "high"


def _stage_pair(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "ws"
    stage = tmp_path / "stage"
    workspace.mkdir()
    stage.mkdir()
    return workspace, stage


def test_build_patch_create_modify_delete_round_trip(tmp_path: Path) -> None:
    workspace, stage = _stage_pair(tmp_path)
    (workspace / "keep.txt").write_text("old line\nsame\n", encoding="utf-8")
    (workspace / "gone.txt").write_text("bye\n", encoding="utf-8")
    (stage / "keep.txt").write_text("new line\nsame\n", encoding="utf-8")
    (stage / "born.txt").write_text("fresh\ncontent\n", encoding="utf-8")
    # 基线 = 子运行开始时的暂存区快照（与当时的 workspace 一致）
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "keep.txt").write_text("old line\nsame\n", encoding="utf-8")
    (baseline_dir / "gone.txt").write_text("bye\n", encoding="utf-8")
    before = file_hashes(baseline_dir)
    after = file_hashes(stage)
    manifest = collect_manifest("child", before, after)
    harvested = build_patch(workspace, stage, manifest.entries, max_chars=10000)
    assert harvested.patch is not None
    assert set(harvested.files) == {"keep.txt", "gone.txt", "born.txt"}
    assert harvested.skipped == ()

    def read(path: str) -> str | None:
        target = workspace / path
        return target.read_text(encoding="utf-8") if target.exists() else None

    changes = {change.path: change for change in plan_patch(harvested.patch, read)}
    assert changes["born.txt"].new_content == "fresh\ncontent\n"
    assert changes["keep.txt"].new_content == "new line\nsame\n"
    assert changes["gone.txt"].kind == "delete"


def test_build_patch_skips_binary_and_unterminated(tmp_path: Path) -> None:
    workspace, stage = _stage_pair(tmp_path)
    (stage / "bin.dat").write_bytes(b"\x00\x01\x02")
    (stage / "noeol.txt").write_text("no trailing newline", encoding="utf-8")
    before = file_hashes(workspace)
    after = file_hashes(stage)
    manifest = collect_manifest("child", before, after)
    harvested = build_patch(workspace, stage, manifest.entries, max_chars=10000)
    assert harvested.patch is None
    assert set(harvested.skipped) == {"bin.dat", "noeol.txt"}


def test_build_patch_oversize_returns_no_partial_patch(tmp_path: Path) -> None:
    workspace, stage = _stage_pair(tmp_path)
    (stage / "big.txt").write_text("x\n" * 500, encoding="utf-8")
    manifest = collect_manifest("child", file_hashes(workspace), file_hashes(stage))
    harvested = build_patch(workspace, stage, manifest.entries, max_chars=100)
    assert harvested.patch is None
    assert harvested.reason == "patch_exceeds_max_chars"
    assert harvested.skipped == ("big.txt",)


def test_reducer_drops_patch_first_when_over_limit() -> None:
    result = SubAgentResult(
        "d",
        "child",
        "completed",
        "done",
        (),
        patch="*** Begin Patch\n" + "x" * 500,
        patch_files=("a.txt",),
    )
    encoded = ResultReducer.reduce((result,), max_chars=400, trust_grade="elevated")
    payload = json.loads(encoded)
    assert payload["trust_grade"] == "elevated"
    assert payload["results"][0]["patch"] is None
    assert "patch omitted" in payload["results"][0]["summary"]


@pytest.mark.asyncio
async def test_writable_child_dispatcher_filters_and_writes(tmp_path: Path) -> None:
    dispatcher = WritableChildDispatcher(("read_file", "write_file"))
    names = {definition["name"] for definition in dispatcher.definitions()}
    assert names == {"read_file", "write_file"}
    context = AgentRequest("s", "t", str(tmp_path), "goal", "fake")
    denied = await dispatcher.dispatch(ToolCall("c1", "shell", {"argv": ["ls"]}), context)
    assert denied.status == "denied"
    written = await dispatcher.dispatch(
        ToolCall("c2", "write_file", {"path": "out.txt", "content": "hi\n"}), context
    )
    assert written.status == "ok"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hi\n"


def test_writable_child_dispatcher_rejects_bad_allowlist() -> None:
    with pytest.raises(ValueError):
        WritableChildDispatcher(("shell",))
