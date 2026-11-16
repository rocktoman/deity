"""项目约束层（ADR-020 B5）：`.deity/requirements.toml` 只能收紧，不能放宽。

分层参照 openai/codex 的 requirements 约束层思路（Apache-2.0，
`codex-rs/config/`），字段与收紧语义为 Deity 自有定义，声明见
`docs/06-legal/third-party-notices.md`。约束在常规分层（默认 <
用户 < 项目 < 命令行）合并完成后应用：管理员写下的上限对所有
层生效，任何层都不能把约束放宽。文件不存在即无约束；文件存在
但字段/类型非法时整体报错（与配置无效同一口径，不回显内容）。
"""

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from deity.application.config import Config

_PERMISSION_STRICTNESS = {"auto": 0, "ask": 1, "deny": 2}


@dataclass(frozen=True)
class Requirements:
    """一份已校验的约束：None 表示该项不约束。"""

    shell: Literal["ask", "auto", "deny"] | None = None
    workspace_write: Literal["ask", "auto", "deny"] | None = None
    sandbox_required: bool | None = None
    sandbox_network: str | None = None
    sandbox_backend: str | None = None
    max_turns: int | None = None
    max_tool_calls: int | None = None
    deadline_seconds: int | None = None
    resume_enabled: bool | None = None
    multi_agent_enabled: bool | None = None
    max_children: int | None = None
    max_depth: int | None = None


def load_requirements(path: Path) -> Requirements | None:
    """读约束文件；不存在返回 None，非法抛 ValueError（不泄露内容）。"""
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) - {
            "permission",
            "sandbox",
            "run",
            "multi_agent",
        }:
            raise ValueError
        permission = data.get("permission", {})
        sandbox = data.get("sandbox", {})
        run = data.get("run", {})
        multi_agent = data.get("multi_agent", {})
        if not all(isinstance(item, dict) for item in (permission, sandbox, run, multi_agent)):
            raise ValueError
        if set(permission) - {"shell", "workspace_write"}:
            raise ValueError
        if set(sandbox) - {"required", "network", "backend"}:
            raise ValueError
        if set(run) - {"max_turns", "max_tool_calls", "deadline_seconds", "resume_enabled"}:
            raise ValueError
        if set(multi_agent) - {"enabled", "max_children", "max_depth"}:
            raise ValueError
        for value in (permission.get("shell"), permission.get("workspace_write")):
            if value is not None and value not in _PERMISSION_STRICTNESS:
                raise ValueError
        if sandbox.get("required") is not None and type(sandbox["required"]) is not bool:
            raise ValueError
        if sandbox.get("network") is not None and sandbox["network"] != "none":
            raise ValueError
        if sandbox.get("backend") is not None and sandbox["backend"] not in {"docker", "bwrap"}:
            raise ValueError
        for value in (run.get("max_turns"), run.get("max_tool_calls"), run.get("deadline_seconds")):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError
        if run.get("resume_enabled") is not None and type(run["resume_enabled"]) is not bool:
            raise ValueError
        if multi_agent.get("enabled") is not None and type(multi_agent["enabled"]) is not bool:
            raise ValueError
        for value in (multi_agent.get("max_children"), multi_agent.get("max_depth")):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError
        return Requirements(
            shell=permission.get("shell"),
            workspace_write=permission.get("workspace_write"),
            sandbox_required=sandbox.get("required"),
            sandbox_network=sandbox.get("network"),
            sandbox_backend=sandbox.get("backend"),
            max_turns=run.get("max_turns"),
            max_tool_calls=run.get("max_tool_calls"),
            deadline_seconds=run.get("deadline_seconds"),
            resume_enabled=run.get("resume_enabled"),
            multi_agent_enabled=multi_agent.get("enabled"),
            max_children=multi_agent.get("max_children"),
            max_depth=multi_agent.get("max_depth"),
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, TypeError, ValueError):
        raise ValueError("配置无效：请检查 TOML 字段和权限设置") from None


def _stricter(current: str, cap: str | None) -> str:
    if cap is None:
        return current
    return current if _PERMISSION_STRICTNESS[current] >= _PERMISSION_STRICTNESS[cap] else cap


def apply_requirements(config: Config, requirements: Requirements) -> Config:
    """把约束应用到有效配置：每项取更严者；冲突的后端约束报错。"""
    permission = replace(
        config.permission,
        shell=_stricter(config.permission.shell, requirements.shell),  # type: ignore[arg-type]
        workspace_write=_stricter(  # type: ignore[arg-type]
            config.permission.workspace_write, requirements.workspace_write
        ),
    )
    sandbox = config.sandbox
    if requirements.sandbox_backend is not None:
        if sandbox.backend == "local":
            sandbox = replace(sandbox, backend=requirements.sandbox_backend)  # type: ignore[arg-type]
        elif sandbox.backend != requirements.sandbox_backend:
            raise ValueError("配置无效：请检查 TOML 字段和权限设置")
    if requirements.sandbox_network == "none":
        sandbox = replace(sandbox, network="none")
    if requirements.sandbox_required:
        if sandbox.backend == "local":
            raise ValueError("配置无效：请检查 TOML 字段和权限设置")
        sandbox = replace(sandbox, enabled=True, required=True)
    run = replace(
        config.run,
        max_turns=min(config.run.max_turns, requirements.max_turns or config.run.max_turns),
        max_tool_calls=min(
            config.run.max_tool_calls, requirements.max_tool_calls or config.run.max_tool_calls
        ),
        deadline_seconds=min(
            config.run.deadline_seconds,
            requirements.deadline_seconds or config.run.deadline_seconds,
        ),
        resume_enabled=config.run.resume_enabled and requirements.resume_enabled is not False,
    )
    multi_agent = replace(
        config.multi_agent,
        enabled=config.multi_agent.enabled and requirements.multi_agent_enabled is not False,
        max_children=min(
            config.multi_agent.max_children,
            requirements.max_children or config.multi_agent.max_children,
        ),
        max_depth=min(
            config.multi_agent.max_depth, requirements.max_depth or config.multi_agent.max_depth
        ),
    )
    return replace(config, permission=permission, sandbox=sandbox, run=run, multi_agent=multi_agent)


__all__ = ["Requirements", "apply_requirements", "load_requirements"]
