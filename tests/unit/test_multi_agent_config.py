from pathlib import Path

import pytest

from deity.application.config import load_config


def test_multi_agent_default_off_and_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert not load_config(path).multi_agent.enabled
    assert load_config(path).multi_agent_fingerprint() == ""
    path.write_text("[multi_agent]\nenabled=true\nmax_concurrency=2\n")
    config = load_config(path)
    assert config.multi_agent.enabled
    assert config.multi_agent_fingerprint()


@pytest.mark.parametrize(
    "body",
    [
        "[multi_agent]\nunknown=1\n",
        "[multi_agent]\nversion=2\n",
        "[multi_agent]\nenabled='yes'\n",
        "[multi_agent]\nmax_depth=0\n",
        "[multi_agent]\nmax_children=1\nmax_concurrency=2\n",
    ],
)
def test_multi_agent_config_rejects_invalid_values(tmp_path: Path, body: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(body)
    with pytest.raises(ValueError):
        load_config(path)
