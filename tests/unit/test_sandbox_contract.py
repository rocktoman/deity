from pathlib import Path

import pytest

from deity.application.config import load_config
from deity.core.contracts.execution import BackendKind, ExecutionStatus, ResourceBudget, SandboxSpec


def test_sandbox_defaults_and_required_policy(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")
    assert not config.sandbox.enabled
    assert config.sandbox.backend == "local"
    assert config.sandbox_fingerprint() == ""

    path = tmp_path / "config.toml"
    path.write_text(
        '[sandbox]\nenabled=true\nbackend="docker"\nrequired=true\n'
        'image="sha256:' + "a" * 64 + '"\n',
        encoding="utf-8",
    )
    enabled = load_config(path)
    assert enabled.sandbox_fingerprint()
    assert enabled.sandbox.required


@pytest.mark.parametrize(
    "body",
    [
        '[sandbox]\nenabled=true\nbackend="docker"\nimage="alpine:latest"\n',
        '[sandbox]\nenabled=true\nbackend="local"\nrequired=true\n',
        '[sandbox]\nenabled=true\nbackend="docker"\nunknown=1\n',
        '[sandbox]\nenabled=true\nbackend="docker"\nimage="sha256:'
        + "a" * 64
        + '"\nnetwork="host"\n',
        '[sandbox]\nallowed_networks=["host"]\n',
        '[sandbox]\nexclude=["../outside"]\n',
    ],
)
def test_sandbox_invalid_config_fails_closed(tmp_path: Path, body: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_execution_contract_values() -> None:
    spec = SandboxSpec(
        BackendKind.DOCKER,
        "sha256:" + "a" * 64,
        ResourceBudget(30, 1.0, 1024 * 1024, 32, 1000, 1024 * 1024),
    )
    assert spec.network == "none"
    assert ExecutionStatus.UNKNOWN.value == "unknown"
