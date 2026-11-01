from pathlib import Path

import pytest

from deity.application.config import load_config


def test_v05_features_default_off_and_do_not_change_empty_config(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")
    assert not config.coding.plan_build_enabled
    assert not config.coding.code_intelligence_enabled
    assert not config.coding.git_guard_enabled
    assert not config.coding.verification_enabled
    assert config.verification.permission == "ask"


def test_v05_config_is_strict_and_fingerprinted(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[coding]
version = 1
plan_build_enabled = true
code_intelligence_enabled = true
git_guard_enabled = true
verification_enabled = true
plan_ttl_seconds = 3600

[code_intelligence]
backend = "lexical"
cache_enabled = false
max_files = 500
max_file_bytes = 4096

[git_guard]
protect_initial_dirty = true
require_read_before_write = true

[verification]
permission = "auto"
timeout_seconds = 30
max_output_chars = 2000
repeat = 2
commands = [["python", "-m", "compileall", "src"]]
""".strip(),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.coding.plan_ttl_seconds == 3600
    assert config.code_intelligence.max_files == 500
    assert config.verification.repeat == 2
    assert config.verification.commands == (("python", "-m", "compileall", "src"),)
    assert config.coding_fingerprint()

    path.write_text("[coding]\nversion=1\nunknown=true\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize(
    "body",
    [
        "[coding]\nversion=2\n",
        "[coding]\nplan_ttl_seconds=1\n",
        "[code_intelligence]\nbackend='lsp'\n",
        "[code_intelligence]\nmax_files=0\n",
        "[git_guard]\nprotect_initial_dirty='yes'\n",
        "[verification]\npermission='allow'\n",
        "[verification]\nrepeat=4\n",
        "[verification]\ncommands=[['bash','-c','pytest']]\n",
    ],
)
def test_v05_invalid_config_fails_closed(tmp_path: Path, body: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)
