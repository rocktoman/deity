from pathlib import Path

import pytest

from deity.application.config import load_config


def test_extensions_are_disabled_by_default(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")
    assert not config.extensions.mcp_enabled
    assert not config.extensions.skills_enabled
    assert not config.extensions.hooks_enabled


def test_unknown_extension_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[extensions]\nversion = 99\n")
    with pytest.raises(ValueError):
        load_config(path)


def test_mcp_server_config_is_strict(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[extensions]\nmcp_enabled = true\n[mcp.servers.demo]\ncommand = "python"\n'
        'args = ["-m", "example"]\nenv_names = ["DEMO_TOKEN"]\n'
    )
    config = load_config(path)
    assert config.mcp_servers[0].name == "demo"
    assert config.mcp_servers[0].env_names == ("DEMO_TOKEN",)
    path.write_text('[mcp.servers.demo]\ncommand = "python"\nextra = "unsafe"\n')
    with pytest.raises(ValueError):
        load_config(path)


def test_hook_registration_requires_explicit_fields(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[extensions]\nhooks_enabled = true\n[[hooks.registered]]\n"
        'name = "guard"\npoint = "BeforeTool"\nkind = "policy"\n'
        'callable = "deity.examples.v0_4_hook:deny_delete"\npriority = 0\n'
    )
    config = load_config(path)
    assert config.hook_specs[0].callable_path == "deity.examples.v0_4_hook:deny_delete"
