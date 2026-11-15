"""ADR-019 A3：持久审批规则——匹配、落盘、amend 全流程与授权顺序。"""

import io
from pathlib import Path

from rich.console import Console

from deity.application.approval_rules import (
    ApprovalRule,
    ApprovalRules,
    command_pattern_for,
)
from deity.application.config import PermissionConfig
from deity.application.run_service import RunService
from deity.interfaces.cli.chat_approval import ApprovalController


def test_command_pattern_derivation() -> None:
    assert command_pattern_for({"argv": ["pytest", "-q", "tests/"]}) == "pytest *"
    assert command_pattern_for({"argv": ["ls"]}) == "ls"
    assert command_pattern_for({"command": "git status"}) == "git *"
    assert command_pattern_for({"argv": []}) is None


def test_decide_allow_deny_and_miss(tmp_path: Path) -> None:
    rules = ApprovalRules(
        tmp_path / "rules.toml",
        (
            ApprovalRule("shell", "pytest *", "allow"),
            ApprovalRule("shell", "rm *", "deny"),
            ApprovalRule("write_file", None, "allow"),
        ),
    )
    assert rules.decide("shell", {"argv": ["pytest", "-q"]}) is True
    assert rules.decide("shell", {"argv": ["rm", "-rf", "x"]}) is False
    assert rules.decide("shell", {"argv": ["make"]}) is None
    assert rules.decide("write_file", {"path": "a.txt"}) is True
    assert rules.decide("delete_file", {"path": "a.txt"}) is None


def test_deny_beats_allow_when_both_match(tmp_path: Path) -> None:
    rules = ApprovalRules(
        tmp_path / "rules.toml",
        (
            ApprovalRule("shell", None, "allow"),
            ApprovalRule("shell", "sudo *", "deny"),
        ),
    )
    assert rules.decide("shell", {"argv": ["sudo", "ls"]}) is False
    assert rules.decide("shell", {"argv": ["ls"]}) is True


def test_toml_round_trip_and_remove(tmp_path: Path) -> None:
    path = tmp_path / "rules.toml"
    rules = ApprovalRules.load(path)
    assert rules.rules == () and rules.load_error is None
    assert rules.add(ApprovalRule("shell", "pytest *", "allow")) is True
    assert rules.add(ApprovalRule("shell", "pytest *", "allow")) is False
    assert rules.add(ApprovalRule("write_file", None, "deny")) is True
    loaded = ApprovalRules.load(path)
    assert loaded.rules == rules.rules
    removed = loaded.remove(0)
    assert removed.pattern == "pytest *"
    assert ApprovalRules.load(path).rules == loaded.rules


def test_malformed_file_loads_empty_with_error(tmp_path: Path) -> None:
    path = tmp_path / "rules.toml"
    path.write_text("[[rule]]\ntool = 3\n", encoding="utf-8")
    rules = ApprovalRules.load(path)
    assert rules.rules == ()
    assert rules.load_error is not None
    assert rules.decide("shell", {"argv": ["pytest"]}) is None


def _service(tmp_path: Path, rules: ApprovalRules, approve_result: bool) -> RunService:
    return RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        approve=lambda name, args: approve_result,
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
        rules=rules,
    )


def test_authorize_consults_rules_before_callback(tmp_path: Path) -> None:
    rules = ApprovalRules(
        tmp_path / "rules.toml",
        (
            ApprovalRule("shell", "pytest *", "allow"),
            ApprovalRule("shell", "curl *", "deny"),
        ),
    )
    service = _service(tmp_path, rules, approve_result=False)
    # 规则放行：回调（会拒绝）不被咨询。
    assert service._authorize("shell", {"argv": ["pytest", "-q"]}) is True
    # 未命中规则：落到回调。
    assert service._authorize("shell", {"argv": ["make"]}) is False
    # 规则拒绝：即使回调会放行也拒绝。
    service2 = _service(tmp_path, rules, approve_result=True)
    assert service2._authorize("shell", {"argv": ["curl", "http://x"]}) is False


def test_config_deny_policy_beats_allow_rule(tmp_path: Path) -> None:
    rules = ApprovalRules(tmp_path / "rules.toml", (ApprovalRule("shell", None, "allow"),))
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        approve=lambda name, args: True,
        permission=PermissionConfig(shell="deny"),
        rules=rules,
    )
    assert service._authorize("shell", {"argv": ["ls"]}) is False


def test_amend_flow_card_saves_rule_and_next_session_auto_allows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rules.toml"
    rules = ApprovalRules.load(path)
    console = Console(file=io.StringIO(), width=100)
    controller = ApprovalController(
        workspace=tmp_path, console=console, input_fn=lambda prompt: "4", rules=rules
    )
    assert controller.decide("shell", {"argv": ["pytest", "-q", "tests/"]}) is True
    # 规则已落盘：新进程视角（重新加载）能看到同一条模式规则。
    reloaded = ApprovalRules.load(path)
    assert reloaded.rules == (ApprovalRule("shell", "pytest *", "allow"),)
    # 下一个会话：服务层规则先行表态，审批回调（会拒绝）根本不会被问到。
    service = _service(tmp_path, reloaded, approve_result=False)
    assert service._authorize("shell", {"argv": ["pytest", "tests/unit"]}) is True


def test_card_without_rules_treats_option_4_as_deny(tmp_path: Path) -> None:
    console = Console(file=io.StringIO(), width=100)
    controller = ApprovalController(
        workspace=tmp_path, console=console, input_fn=lambda prompt: "4"
    )
    assert controller.decide("shell", {"argv": ["pytest"]}) is False
