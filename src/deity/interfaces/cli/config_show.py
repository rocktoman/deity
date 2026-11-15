"""deity config show（V1.1-M4）：分层展示有效配置与每段来源。

层级与 ConfigResolver 一致：默认 < 用户 < 项目 < 命令行。展示只读：
模型段只显示密钥所在的环境变量名与该变量是否已设置（与 doctor 同一
口径，只查存在性，绝不读值、绝不打印值）。
"""

import os
from dataclasses import asdict, is_dataclass
from typing import Any

from deity.application.config import Config

_LAYER_LABELS = {
    "default": "默认",
    "user": "用户",
    "project": "项目",
    "cli": "命令行",
}

_SECTION_ORDER = (
    "model",
    "run",
    "permission",
    "extensions",
    "coding",
    "code_intelligence",
    "git_guard",
    "verification",
    "sandbox",
    "multi_agent",
    "tui",
    "doctor",
    "chat",
)


def _format_value(value: Any) -> str:
    """单个配置值的展示形式：布尔/数字/字符串直出，序列计数摘要。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        return f"[{len(value)} 项]"
    return str(value)


def _section_lines(name: str, value: Any, source: str) -> list[str]:
    label = _LAYER_LABELS.get(source, source)
    lines = [f"[{name}]（来源：{label}）"]
    if value is None:
        lines.append("  （未配置）")
        return lines
    if is_dataclass(value) and not isinstance(value, type):
        for key, item in asdict(value).items():
            lines.append(f"  {key} = {_format_value(item)}")
        return lines
    lines.append(f"  {_format_value(value)}")
    return lines


def format_config_show(config: Config, sources: dict[str, str]) -> str:
    """把有效配置渲染为分层文本：每段标注来源层，附密钥变量存在性。"""
    lines: list[str] = []
    for section in _SECTION_ORDER:
        lines.extend(
            _section_lines(section, getattr(config, section), sources.get(section, "default"))
        )
    lines.append(
        f"[mcp_servers]（来源：{_LAYER_LABELS.get(sources.get('mcp_servers', 'default'), 'default')}）"
    )
    if config.mcp_servers:
        for server in config.mcp_servers:
            lines.append(f"  {server.name} = {server.command}")
    else:
        lines.append("  （无）")
    lines.append(
        f"[hook_specs]（来源：{_LAYER_LABELS.get(sources.get('hook_specs', 'default'), 'default')}）"
    )
    if config.hook_specs:
        for hook in config.hook_specs:
            lines.append(f"  {hook.name} = {hook.point}/{hook.kind}")
    else:
        lines.append("  （无）")
    if config.model is not None:
        env_name = config.model.api_key_env
        present = "已设置" if os.environ.get(env_name) else "未设置"
        lines.append(f"密钥环境变量 {env_name}：{present}（只查存在性，不读值）")
    return "\n".join(lines)


__all__ = ["format_config_show"]
