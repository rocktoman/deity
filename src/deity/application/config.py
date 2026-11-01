"""Load and validate local Deity configuration without exposing secrets."""

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class PermissionConfig:
    shell: Literal["ask", "auto", "deny"] = "ask"
    workspace_write: Literal["ask", "auto", "deny"] = "ask"


@dataclass(frozen=True)
class RunConfig:
    max_turns: int = 30
    max_tool_calls: int = 100
    deadline_seconds: int = 900
    max_output_tokens: int | None = None
    compaction_enabled: bool = False
    resume_enabled: bool = True
    max_input_tokens_total: int | None = None
    max_output_tokens_total: int | None = None


@dataclass(frozen=True)
class CodingConfig:
    version: int = 1
    plan_build_enabled: bool = False
    code_intelligence_enabled: bool = False
    git_guard_enabled: bool = False
    verification_enabled: bool = False
    plan_ttl_seconds: int = 86_400


@dataclass(frozen=True)
class CodeIntelligenceConfig:
    backend: Literal["lexical"] = "lexical"
    cache_enabled: bool = True
    max_files: int = 20_000
    max_file_bytes: int = 2_000_000


@dataclass(frozen=True)
class GitGuardConfig:
    protect_initial_dirty: bool = True
    require_read_before_write: bool = True


@dataclass(frozen=True)
class VerificationConfig:
    permission: Literal["ask", "auto", "deny"] = "ask"
    timeout_seconds: int = 300
    max_output_chars: int = 16_000
    repeat: int = 1
    commands: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class SandboxConfig:
    version: int = 1
    enabled: bool = False
    backend: Literal["local", "docker"] = "local"
    required: bool = False
    image: str = ""
    workspace_mode: Literal["copy"] = "copy"
    read_only_rootfs: bool = True
    non_root: bool = True
    network: str = "none"
    allowed_networks: tuple[str, ...] = ()
    env_names: tuple[str, ...] = ()
    artifact_policy: Literal["discard", "review"] = "discard"
    cpu_count: float = 1.0
    memory_mb: int = 1024
    pids_limit: int = 128
    tmpfs_mb: int = 256
    timeout_seconds: int = 300
    cleanup_timeout_seconds: int = 15
    max_output_chars: int = 16000
    max_workspace_files: int = 50_000
    max_workspace_bytes: int = 1_073_741_824
    max_artifact_files: int = 200
    max_artifact_bytes: int = 52_428_800
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class MultiAgentConfig:
    version: int = 1
    enabled: bool = False
    max_depth: int = 1
    max_children: int = 4
    max_concurrency: int = 2
    max_result_chars: int = 4000


@dataclass(frozen=True)
class Config:
    run: RunConfig = field(default_factory=RunConfig)
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    model: "ModelConfig | None" = None
    extensions: "ExtensionConfig" = field(default_factory=lambda: ExtensionConfig())
    mcp_servers: tuple["McpServerSettings", ...] = ()
    hook_specs: tuple["HookSpec", ...] = ()
    coding: CodingConfig = field(default_factory=CodingConfig)
    code_intelligence: CodeIntelligenceConfig = field(default_factory=CodeIntelligenceConfig)
    git_guard: GitGuardConfig = field(default_factory=GitGuardConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    multi_agent: MultiAgentConfig = field(default_factory=MultiAgentConfig)

    def multi_agent_fingerprint(self) -> str:
        """Hash delegation settings only when the feature is enabled."""
        if not self.multi_agent.enabled:
            return ""
        return hashlib.sha256(
            json.dumps(self.multi_agent.__dict__, sort_keys=True).encode()
        ).hexdigest()

    def sandbox_fingerprint(self) -> str:
        """Hash enabled sandbox policy without environment values."""
        if not self.sandbox.enabled:
            return ""
        return hashlib.sha256(
            json.dumps(self.sandbox.__dict__, sort_keys=True).encode()
        ).hexdigest()

    def coding_fingerprint(self) -> str:
        """Hash V0.5 recovery-relevant policy without paths or secrets."""
        payload = {
            "coding": self.coding.__dict__,
            "code_intelligence": self.code_intelligence.__dict__,
            "git_guard": self.git_guard.__dict__,
            "verification": self.verification.__dict__,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ExtensionConfig:
    version: int = 1
    mcp_enabled: bool = False
    skills_enabled: bool = False
    hooks_enabled: bool = False

    def fingerprint(self) -> str:
        """Hash recovery-relevant extension settings without secrets."""
        payload = {
            "version": self.version,
            "mcp_enabled": self.mcp_enabled,
            "skills_enabled": self.skills_enabled,
            "hooks_enabled": self.hooks_enabled,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class McpServerSettings:
    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str = "."
    env_names: tuple[str, ...] = ()
    timeout_seconds: float = 15.0


@dataclass(frozen=True)
class HookSpec:
    name: str
    point: Literal["BeforeModel", "BeforeTool", "AfterTool", "BeforeFinish"]
    kind: Literal["observer", "policy"]
    callable_path: str
    priority: int = 0


@dataclass(frozen=True)
class ModelConfig:
    provider: Literal["openai_compatible", "anthropic"]
    name: str
    api_key_env: str
    base_url: str | None = None
    context_window_tokens: int | None = None


def load_config(path: Path) -> Config:
    """Read a TOML config; reject invalid values with secret-free errors."""
    if not path.exists():
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) - {
            "run",
            "permission",
            "model",
            "extensions",
            "mcp",
            "hooks",
            "coding",
            "code_intelligence",
            "git_guard",
            "verification",
            "sandbox",
            "multi_agent",
        }:
            raise ValueError
        run = data.get("run", {})
        permission = data.get("permission", {})
        model = data.get("model", {})
        extensions = data.get("extensions", {})
        mcp = data.get("mcp", {})
        hooks = data.get("hooks", {})
        coding = data.get("coding", {})
        code_intelligence = data.get("code_intelligence", {})
        git_guard = data.get("git_guard", {})
        verification = data.get("verification", {})
        sandbox = data.get("sandbox", {})
        multi_agent = data.get("multi_agent", {})
        if not all(
            isinstance(item, dict)
            for item in (
                run,
                permission,
                model,
                extensions,
                mcp,
                hooks,
                coding,
                code_intelligence,
                git_guard,
                verification,
                sandbox,
                multi_agent,
            )
        ):
            raise ValueError
        if set(coding) - {
            "version",
            "plan_build_enabled",
            "code_intelligence_enabled",
            "git_guard_enabled",
            "verification_enabled",
            "plan_ttl_seconds",
        }:
            raise ValueError
        coding_config = CodingConfig(**coding)
        if (
            coding_config.version != 1
            or any(
                type(getattr(coding_config, key)) is not bool
                for key in (
                    "plan_build_enabled",
                    "code_intelligence_enabled",
                    "git_guard_enabled",
                    "verification_enabled",
                )
            )
            or type(coding_config.plan_ttl_seconds) is not int
            or not 300 <= coding_config.plan_ttl_seconds <= 604_800
        ):
            raise ValueError
        if set(code_intelligence) - {
            "backend",
            "cache_enabled",
            "max_files",
            "max_file_bytes",
        }:
            raise ValueError
        code_config = CodeIntelligenceConfig(**code_intelligence)
        if (
            code_config.backend != "lexical"
            or type(code_config.cache_enabled) is not bool
            or type(code_config.max_files) is not int
            or not 1 <= code_config.max_files <= 100_000
            or type(code_config.max_file_bytes) is not int
            or not 1_024 <= code_config.max_file_bytes <= 8 * 1024 * 1024
        ):
            raise ValueError
        if set(git_guard) - {"protect_initial_dirty", "require_read_before_write"}:
            raise ValueError
        git_config = GitGuardConfig(**git_guard)
        if any(type(value) is not bool for value in git_config.__dict__.values()):
            raise ValueError
        if set(verification) - {
            "permission",
            "timeout_seconds",
            "max_output_chars",
            "repeat",
            "commands",
        }:
            raise ValueError
        raw_commands = verification.get("commands", [])
        if (
            not isinstance(raw_commands, list)
            or len(raw_commands) > 16
            or any(
                not isinstance(command, list)
                or not 1 <= len(command) <= 32
                or any(
                    not isinstance(argument, str)
                    or not argument
                    or len(argument) > 1000
                    or any(char in argument for char in "\0\r\n")
                    for argument in command
                )
                for command in raw_commands
            )
        ):
            raise ValueError
        commands = tuple(tuple(argument for argument in command) for command in raw_commands)
        if any(command[0] in {"sh", "bash", "zsh", "cmd", "powershell"} for command in commands):
            raise ValueError
        verification_values = {
            key: value for key, value in verification.items() if key != "commands"
        }
        verification_config = VerificationConfig(**verification_values, commands=commands)
        if (
            verification_config.permission not in {"ask", "auto", "deny"}
            or type(verification_config.timeout_seconds) is not int
            or not 1 <= verification_config.timeout_seconds <= 1_800
            or type(verification_config.max_output_chars) is not int
            or not 1_000 <= verification_config.max_output_chars <= 1_000_000
            or type(verification_config.repeat) is not int
            or not 1 <= verification_config.repeat <= 3
        ):
            raise ValueError
        if set(sandbox) - set(SandboxConfig.__dataclass_fields__):
            raise ValueError
        sandbox_config = SandboxConfig(**sandbox)
        if (
            type(sandbox_config.version) is not int
            or sandbox_config.version != 1
            or type(sandbox_config.enabled) is not bool
            or type(sandbox_config.required) is not bool
            or sandbox_config.backend not in {"local", "docker"}
            or sandbox_config.workspace_mode != "copy"
            or type(sandbox_config.read_only_rootfs) is not bool
            or not sandbox_config.read_only_rootfs
            or type(sandbox_config.non_root) is not bool
            or not sandbox_config.non_root
            or sandbox_config.artifact_policy not in {"discard", "review"}
            or (sandbox_config.required and sandbox_config.backend != "docker")
            or (
                sandbox_config.enabled
                and sandbox_config.backend == "docker"
                and re.fullmatch(r"(?:[\w./-]+@)?sha256:[a-f0-9]{64}", sandbox_config.image) is None
            )
            or not isinstance(sandbox_config.allowed_networks, (list, tuple))
            or not isinstance(sandbox_config.env_names, (list, tuple))
            or not isinstance(sandbox_config.exclude, (list, tuple))
            or len(sandbox_config.exclude) > 100
            or any(
                not isinstance(pattern, str)
                or re.fullmatch(r"[A-Za-z0-9._/*-]{1,128}", pattern) is None
                or pattern.startswith("/")
                or ".." in Path(pattern).parts
                for pattern in sandbox_config.exclude
            )
            or any(
                re.fullmatch(r"[A-Z][A-Z0-9_]*", name) is None for name in sandbox_config.env_names
            )
            or any(
                re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", name) is None
                or name in {"host", "bridge", "default"}
                for name in sandbox_config.allowed_networks
            )
            or (
                sandbox_config.network != "none"
                and sandbox_config.network not in sandbox_config.allowed_networks
            )
            or type(sandbox_config.cpu_count) not in (float, int)
            or not 0.1 <= sandbox_config.cpu_count <= 16
            or any(
                type(value) is not int or value <= 0
                for value in (
                    sandbox_config.memory_mb,
                    sandbox_config.pids_limit,
                    sandbox_config.tmpfs_mb,
                    sandbox_config.timeout_seconds,
                    sandbox_config.cleanup_timeout_seconds,
                    sandbox_config.max_output_chars,
                    sandbox_config.max_workspace_files,
                    sandbox_config.max_workspace_bytes,
                    sandbox_config.max_artifact_files,
                    sandbox_config.max_artifact_bytes,
                )
            )
            or sandbox_config.cleanup_timeout_seconds > 60
            or sandbox_config.max_workspace_files > 100_000
            or sandbox_config.max_workspace_bytes > 4 * 1024**3
            or sandbox_config.max_artifact_files > 1_000
            or sandbox_config.max_artifact_bytes > 100 * 1024**2
        ):
            raise ValueError
        sandbox_config = SandboxConfig(
            **{
                **sandbox_config.__dict__,
                "allowed_networks": tuple(sandbox_config.allowed_networks),
                "env_names": tuple(sandbox_config.env_names),
                "exclude": tuple(sandbox_config.exclude),
            }
        )
        if set(multi_agent) - set(MultiAgentConfig.__dataclass_fields__):
            raise ValueError
        multi_agent_config = MultiAgentConfig(**multi_agent)
        if (
            type(multi_agent_config.version) is not int
            or multi_agent_config.version != 1
            or type(multi_agent_config.enabled) is not bool
            or any(
                type(value) is not int or value <= 0
                for value in (
                    multi_agent_config.max_depth,
                    multi_agent_config.max_children,
                    multi_agent_config.max_concurrency,
                    multi_agent_config.max_result_chars,
                )
            )
            or multi_agent_config.max_depth > 4
            or multi_agent_config.max_children > 32
            or multi_agent_config.max_concurrency > multi_agent_config.max_children
            or multi_agent_config.max_result_chars > 16000
        ):
            raise ValueError
        if set(hooks) - {"registered"} or not isinstance(hooks.get("registered", []), list):
            raise ValueError
        hook_specs: list[HookSpec] = []
        for item in hooks.get("registered", []):
            if not isinstance(item, dict) or set(item) != {
                "name",
                "point",
                "kind",
                "callable",
                "priority",
            }:
                raise ValueError
            if (
                not isinstance(item["name"], str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["name"]) is None
                or item["point"] not in {"BeforeModel", "BeforeTool", "AfterTool", "BeforeFinish"}
                or item["kind"] not in {"observer", "policy"}
                or (item["point"] == "AfterTool" and item["kind"] == "policy")
                or not isinstance(item["callable"], str)
                or re.fullmatch(r"[A-Za-z_][\w.]*:[A-Za-z_][\w]*", item["callable"]) is None
                or type(item["priority"]) is not int
                or abs(item["priority"]) > 1000
            ):
                raise ValueError
            hook_specs.append(
                HookSpec(
                    item["name"], item["point"], item["kind"], item["callable"], item["priority"]
                )
            )
        if set(mcp) - {"servers"} or not isinstance(mcp.get("servers", {}), dict):
            raise ValueError
        if len(mcp.get("servers", {})) > 8:
            raise ValueError
        servers: list[McpServerSettings] = []
        for name, item in mcp.get("servers", {}).items():
            if not isinstance(item, dict) or set(item) - {
                "command",
                "args",
                "cwd",
                "env_names",
                "timeout_seconds",
            }:
                raise ValueError
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None:
                raise ValueError
            command = item.get("command")
            args = item.get("args", [])
            cwd = item.get("cwd", ".")
            env_names = item.get("env_names", [])
            timeout = item.get("timeout_seconds", 15.0)
            if (
                not isinstance(command, str)
                or not command
                or len(command) > 4096
                or not isinstance(args, list)
                or len(args) > 32
                or not all(isinstance(x, str) for x in args)
                or any(len(x) > 4096 for x in args)
                or not isinstance(cwd, str)
                or not cwd
                or len(cwd) > 4096
                or not isinstance(env_names, list)
                or len(env_names) > 16
                or not all(
                    isinstance(x, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", x) for x in env_names
                )
                or type(timeout) not in (int, float)
                or timeout <= 0
                or timeout > 120
            ):
                raise ValueError
            servers.append(
                McpServerSettings(name, command, tuple(args), cwd, tuple(env_names), float(timeout))
            )
        if set(extensions) - {"version", "mcp_enabled", "skills_enabled", "hooks_enabled"}:
            raise ValueError
        if extensions.get("version", 1) != 1 or any(
            type(extensions.get(key, False)) is not bool
            for key in ("mcp_enabled", "skills_enabled", "hooks_enabled")
        ):
            raise ValueError
        if set(run) - {
            "max_turns",
            "max_tool_calls",
            "deadline_seconds",
            "max_output_tokens",
            "compaction_enabled",
            "resume_enabled",
            "max_input_tokens_total",
            "max_output_tokens_total",
        }:
            raise ValueError
        if set(permission) - {"shell", "workspace_write"}:
            raise ValueError
        if set(model) - {
            "provider",
            "name",
            "base_url",
            "api_key_env",
            "context_window_tokens",
        }:
            raise ValueError
        values = {
            key: run.get(key, getattr(RunConfig(), key))
            for key in ("max_turns", "max_tool_calls", "deadline_seconds")
        }
        if any(type(value) is not int or value <= 0 for value in values.values()):
            raise ValueError
        max_output_tokens = run.get("max_output_tokens")
        if max_output_tokens is not None and (
            type(max_output_tokens) is not int or max_output_tokens <= 0
        ):
            raise ValueError
        flags = {
            key: run.get(key, getattr(RunConfig(), key))
            for key in ("compaction_enabled", "resume_enabled")
        }
        if any(type(value) is not bool for value in flags.values()):
            raise ValueError
        token_limits = {
            key: run.get(key) for key in ("max_input_tokens_total", "max_output_tokens_total")
        }
        if any(
            value is not None and (type(value) is not int or value <= 0)
            for value in token_limits.values()
        ):
            raise ValueError
        policy = PermissionConfig(**permission)
        if policy.shell not in ("ask", "auto", "deny") or policy.workspace_write not in (
            "ask",
            "auto",
            "deny",
        ):
            raise ValueError
        model_config = ModelConfig(**model) if model else None
        if model_config is not None and (
            model_config.provider not in ("openai_compatible", "anthropic")
            or not model_config.name
            or re.fullmatch(r"[A-Z][A-Z0-9_]*", model_config.api_key_env) is None
            or (
                model_config.context_window_tokens is not None
                and (
                    type(model_config.context_window_tokens) is not int
                    or model_config.context_window_tokens <= 0
                )
            )
        ):
            raise ValueError
        if "compaction_enabled" not in run and model_config is not None:
            flags["compaction_enabled"] = model_config.context_window_tokens is not None
        if flags["compaction_enabled"] and (
            model_config is None or model_config.context_window_tokens is None
        ):
            raise ValueError
        return Config(
            run=RunConfig(**values, max_output_tokens=max_output_tokens, **flags, **token_limits),
            permission=policy,
            model=model_config,
            extensions=ExtensionConfig(**extensions),
            mcp_servers=tuple(servers),
            hook_specs=tuple(hook_specs),
            coding=coding_config,
            code_intelligence=code_config,
            git_guard=git_config,
            verification=verification_config,
            sandbox=sandbox_config,
            multi_agent=multi_agent_config,
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, TypeError, ValueError):
        raise ValueError("配置无效：请检查 TOML 字段和权限设置") from None
