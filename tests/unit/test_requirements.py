"""ADR-020 B5：项目约束层——只能收紧、冲突报错、解析器接入。"""

from pathlib import Path

import pytest

from deity.application.config import Config, ConfigResolver, PermissionConfig
from deity.application.requirements import (
    Requirements,
    apply_requirements,
    load_requirements,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_file_means_no_requirements(tmp_path: Path) -> None:
    assert load_requirements(tmp_path / "requirements.toml") is None


def test_permission_only_tightens() -> None:
    config = Config(permission=PermissionConfig(shell="auto", workspace_write="deny"))
    tightened = apply_requirements(config, Requirements(shell="ask", workspace_write="auto"))
    assert tightened.permission.shell == "ask"  # auto -> ask 更严
    assert tightened.permission.workspace_write == "deny"  # 已更严，不被放宽


def test_run_caps_take_minimum_and_resume_only_disables() -> None:
    config = Config()
    tightened = apply_requirements(
        config, Requirements(max_turns=5, deadline_seconds=10_000, resume_enabled=False)
    )
    assert tightened.run.max_turns == 5
    assert tightened.run.deadline_seconds == config.run.deadline_seconds  # 更松的约束不生效
    assert tightened.run.resume_enabled is False


def test_multi_agent_only_disables_or_shrinks() -> None:
    from deity.application.config import MultiAgentConfig

    config = Config(multi_agent=MultiAgentConfig(enabled=True, max_children=8, max_depth=3))
    tightened = apply_requirements(config, Requirements(max_children=2, multi_agent_enabled=False))
    assert tightened.multi_agent.enabled is False
    assert tightened.multi_agent.max_children == 2
    assert tightened.multi_agent.max_depth == 3


def test_sandbox_required_upgrades_local_backend_via_constraint() -> None:
    config = Config()
    tightened = apply_requirements(
        config, Requirements(sandbox_required=True, sandbox_backend="bwrap")
    )
    assert tightened.sandbox.enabled is True
    assert tightened.sandbox.required is True
    assert tightened.sandbox.backend == "bwrap"


def test_sandbox_required_without_isolated_backend_fails_closed() -> None:
    with pytest.raises(ValueError):
        apply_requirements(Config(), Requirements(sandbox_required=True))


def test_conflicting_backend_constraint_rejected() -> None:
    from deity.application.config import SandboxConfig

    config = Config(
        sandbox=SandboxConfig(enabled=True, backend="docker", image="sha256:" + "a" * 64)
    )
    with pytest.raises(ValueError):
        apply_requirements(config, Requirements(sandbox_backend="bwrap"))


def test_network_constraint_forces_none() -> None:
    from deity.application.config import SandboxConfig

    config = Config(
        sandbox=SandboxConfig(
            enabled=True,
            backend="docker",
            image="sha256:" + "a" * 64,
            network="isolated",
            allowed_networks=("isolated",),
        )
    )
    tightened = apply_requirements(config, Requirements(sandbox_network="none"))
    assert tightened.sandbox.network == "none"


def test_invalid_requirements_file_rejected_without_leaking(tmp_path: Path) -> None:
    path = _write(tmp_path / "requirements.toml", '[run]\nmax_turns = "secret-marker"\n')
    with pytest.raises(ValueError) as error:
        load_requirements(path)
    assert "secret-marker" not in str(error.value)
    path = _write(tmp_path / "requirements.toml", '[sandbox]\nnetwork = "host"\n')
    with pytest.raises(ValueError):
        load_requirements(path)


def test_resolver_applies_requirements_after_project_layer(tmp_path: Path) -> None:
    project = _write(
        tmp_path / ".deity" / "config.toml",
        '[permission]\nshell = "auto"\n[run]\nmax_turns = 20\n',
    )
    _write(
        tmp_path / ".deity" / "requirements.toml",
        '[permission]\nshell = "deny"\n[run]\nmax_turns = 7\n',
    )
    config, sources = ConfigResolver.resolve({}, project, tmp_path / "absent.toml")
    assert config.permission.shell == "deny"
    assert config.run.max_turns == 7
    assert sources["requirements"] == "project"
    assert sources["permission"] == "project"
