"""持久审批规则（ADR-019 A3）：审批决定沉淀为磁盘上的规则资产。

规则文件是 TOML，默认位于 `~/.config/deity/rules.toml`（与
config.toml 同目录），可用环境变量 `DEITY_RULES_PATH` 覆盖。每条
规则是 `[[rule]] tool / pattern / effect`：shell 规则的 pattern 是
与会话允许（V1.1-M4）同语义的命令模式（fnmatch，如 `pytest *`），
其余工具为工具级规则（pattern 为 None）。判定：任一匹配的 deny
规则拒绝，否则任一匹配的 allow 规则放行，否则不表态（None），交
回既有审批路径。规则不能推翻配置层 deny 策略（由调用方保证顺序）。

文件损坏时按空规则加载并保留 `load_error`，不阻断运行；保存为
临时文件 + 原子替换。本模块同时承载 shell 命令文本提取与模式派
生（原在 interfaces/cli/chat_approval.py，下沉到应用层以便复用，
CLI 改为再导出）。
"""

import fnmatch
import json
import os
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


def shell_command_text(args: dict[str, Any]) -> str:
    """从 shell 工具参数取命令全文：command 字符串优先，其次 argv 拼接。"""
    command = args.get("command")
    if isinstance(command, str):
        return command
    argv = args.get("argv")
    if isinstance(argv, list):
        return " ".join(str(item) for item in argv)
    return ""


def command_pattern_for(args: dict[str, Any]) -> str | None:
    """由一条 shell 命令派生允许模式：首 token + ` *`（单 token 用其本身）。

    例：`pytest -q tests/` -> `pytest *`；`ls` -> `ls`。
    无法解析出 token 时返回 None，调用方回退到工具级规则。
    """
    text = shell_command_text(args).strip()
    if not text:
        return None
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    if not tokens:
        return None
    if len(tokens) == 1:
        return tokens[0]
    return f"{tokens[0]} *"


@dataclass(frozen=True)
class ApprovalRule:
    """一条持久规则：工具 + 可选命令模式 + 效果。"""

    tool: str
    pattern: str | None
    effect: Literal["allow", "deny"]


def default_rules_path() -> Path:
    """规则文件默认位置：`DEITY_RULES_PATH` 优先，其次 ~/.config/deity/。"""
    override = os.environ.get("DEITY_RULES_PATH")
    if override:
        return Path(override)
    return Path.home() / ".config" / "deity" / "rules.toml"


def _rule_matches(rule: ApprovalRule, name: str, args: dict[str, Any]) -> bool:
    if rule.tool != name:
        return False
    if rule.pattern is None:
        return True
    if name != "shell":
        return False
    text = shell_command_text(args).strip()
    return bool(text) and fnmatch.fnmatchcase(text, rule.pattern)


class ApprovalRules:
    """一组已加载的持久规则 + 其文件位置；变更即落盘。"""

    def __init__(
        self,
        path: Path,
        rules: tuple[ApprovalRule, ...] = (),
        load_error: str | None = None,
    ) -> None:
        self.path = path
        self._rules: list[ApprovalRule] = list(rules)
        self.load_error = load_error

    @classmethod
    def load(cls, path: Path | None = None) -> "ApprovalRules":
        """从磁盘加载；文件不存在视为空规则，内容损坏记录 load_error。"""
        target = path or default_rules_path()
        if not target.is_file():
            return cls(target)
        try:
            with target.open("rb") as handle:
                data = tomllib.load(handle)
            entries = data.get("rule", [])
            if not isinstance(entries, list):
                raise TypeError("rule 必须是数组表")
            rules: list[ApprovalRule] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    raise TypeError("rule 条目必须是表")
                tool = entry.get("tool")
                pattern = entry.get("pattern")
                effect = entry.get("effect")
                if (
                    not isinstance(tool, str)
                    or not tool
                    or effect not in ("allow", "deny")
                    or (pattern is not None and not isinstance(pattern, str))
                ):
                    raise ValueError("rule 条目字段非法")
                rules.append(ApprovalRule(tool, pattern, effect))
        except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
            return cls(target, (), f"{type(exc).__name__}: {exc}")
        return cls(target, tuple(rules))

    @property
    def rules(self) -> tuple[ApprovalRule, ...]:
        return tuple(self._rules)

    def decide(self, name: str, args: dict[str, Any]) -> bool | None:
        """规则表态：匹配 deny 优先，其次 allow，未命中返回 None。"""
        allowed = False
        for rule in self._rules:
            if not _rule_matches(rule, name, args):
                continue
            if rule.effect == "deny":
                return False
            allowed = True
        return True if allowed else None

    def add(self, rule: ApprovalRule) -> bool:
        """新增规则并落盘；已存在完全相同的规则时返回 False。"""
        if rule in self._rules:
            return False
        self._rules.append(rule)
        self.save()
        return True

    def remove(self, index: int) -> ApprovalRule:
        """按序号删除规则并落盘，返回被删条目。"""
        rule = self._rules.pop(index)
        self.save()
        return rule

    def save(self) -> None:
        """序列化为 TOML 并原子替换（临时文件 + os.replace）。"""
        lines = [
            "# Deity 持久审批规则（ADR-019）：由审批卡片的「永久允许」或 deity rules 维护。",
            "",
        ]
        for rule in self._rules:
            lines.append("[[rule]]")
            lines.append(f"tool = {json.dumps(rule.tool, ensure_ascii=False)}")
            if rule.pattern is not None:
                lines.append(f"pattern = {json.dumps(rule.pattern, ensure_ascii=False)}")
            lines.append(f"effect = {json.dumps(rule.effect)}")
            lines.append("")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(tmp, self.path)


__all__ = [
    "ApprovalRule",
    "ApprovalRules",
    "command_pattern_for",
    "default_rules_path",
    "shell_command_text",
]
