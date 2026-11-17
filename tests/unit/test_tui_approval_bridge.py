"""审批桥单元测试（ADR-022）：控制器在工作线程阻塞，界面回填选择。

桥不复制审批语义——这里验证的是同一 ApprovalController 经桥接后，
卡片呈现信息（工具/命令/模式/永久选项）与五种决策的折叠结果与 CLI
一致，且 abort 能让阻塞中的审批按拒绝收口。
"""

import threading
from pathlib import Path

from deity.application.approval_rules import ApprovalRules
from deity.interfaces.tui.approval_bridge import PendingPrompt, TuiApprovalBridge


def _bridge(tmp_path: Path, *, with_rules: bool = False) -> TuiApprovalBridge:
    rules = ApprovalRules(tmp_path / "rules.toml") if with_rules else None
    return TuiApprovalBridge(workspace=tmp_path, rules=rules)


def _decide_async(
    bridge: TuiApprovalBridge, name: str, args: dict
) -> tuple[threading.Thread, dict]:
    """在工作线程里跑一次 decide（与工具分发器调用审批的线程模型一致）。"""
    outcome: dict[str, bool] = {}

    def run() -> None:
        outcome["result"] = bridge.controller.decide(name, args)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def _wait_pending(bridge: TuiApprovalBridge) -> PendingPrompt:
    for _ in range(200):
        pending = bridge.pending
        if pending is not None:
            return pending
        threading.Event().wait(0.01)
    raise AssertionError("审批未到达桥")


def test_bridge_allow_once(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    thread, outcome = _decide_async(bridge, "write_file", {"path": "a.txt", "content": "hi"})
    pending = _wait_pending(bridge)
    assert pending.kind == "approval"
    assert "write_file" in pending.text
    assert "a.txt" in pending.text
    assert pending.allow_permanent is False  # 未传规则层时不出现永久选项
    assert bridge.submit("1") is True
    thread.join(timeout=5)
    assert outcome["result"] is True


def test_bridge_deny_by_default_choice(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    thread, outcome = _decide_async(bridge, "write_file", {"path": "a.txt", "content": "hi"})
    _wait_pending(bridge)
    assert bridge.submit("3") is True
    thread.join(timeout=5)
    assert outcome["result"] is False


def test_bridge_session_allow_skips_second_card(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    thread, outcome = _decide_async(bridge, "write_file", {"path": "a.txt", "content": "hi"})
    _wait_pending(bridge)
    assert bridge.submit("2") is True
    thread.join(timeout=5)
    assert outcome["result"] is True
    # 同一工具第二次调用：会话允许表命中，decide 直接返回 True 不再出卡。
    assert bridge.controller.decide("write_file", {"path": "b.txt", "content": "yo"}) is True
    assert bridge.pending is None


def test_bridge_shell_pattern_allow(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    args = {"command": "pytest -q", "argv": ["pytest", "-q"]}
    thread, outcome = _decide_async(bridge, "shell", args)
    pending = _wait_pending(bridge)
    assert pending.pattern == "pytest *"
    assert "pytest -q" in pending.text
    assert bridge.submit("2") is True
    thread.join(timeout=5)
    assert outcome["result"] is True
    # 同模式命令直接放行，不同命令仍需审批（阻塞→用 abort 收口验证它确实出卡）。
    assert bridge.controller.decide("shell", {"command": "pytest tests/unit"}) is True
    thread2, outcome2 = _decide_async(bridge, "shell", {"command": "ls", "argv": ["ls"]})
    _wait_pending(bridge)
    bridge.abort()
    thread2.join(timeout=5)
    assert outcome2["result"] is False


def test_bridge_allow_always_writes_rule(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, with_rules=True)
    thread, outcome = _decide_async(bridge, "write_file", {"path": "a.txt", "content": "hi"})
    pending = _wait_pending(bridge)
    assert pending.allow_permanent is True
    assert bridge.submit("4") is True
    thread.join(timeout=5)
    assert outcome["result"] is True
    saved = ApprovalRules.load(tmp_path / "rules.toml")
    assert any(rule.tool == "write_file" and rule.effect == "allow" for rule in saved.rules)


def test_bridge_abort_denies_blocked_approval(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    thread, outcome = _decide_async(bridge, "write_file", {"path": "a.txt", "content": "hi"})
    _wait_pending(bridge)
    bridge.abort()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome["result"] is False


def test_bridge_confirm_for_auto_mode(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    outcome: dict[str, bool] = {}

    def run() -> None:
        outcome["result"] = bridge.request_auto()

    thread = threading.Thread(target=run)
    thread.start()
    pending = _wait_pending(bridge)
    assert pending.kind == "confirm"
    assert bridge.submit("yes") is True
    thread.join(timeout=5)
    assert outcome["result"] is True
    assert bridge.controller.mode == "auto"


def test_bridge_submit_without_pending_is_noop(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    assert bridge.submit("1") is False
