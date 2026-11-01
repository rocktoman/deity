import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deity.interfaces.cli.main import app


def test_sandbox_cli_doctor_and_run(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("Docker CLI unavailable")
        pytest.skip("Docker unavailable")
    probe = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            os.environ.get("DEITY_SANDBOX_TEST_IMAGE", "alpine:3.22"),
            "--format",
            "{{.Id}}",
        ],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        if os.environ.get("DEITY_REQUIRE_DOCKER") == "1":
            pytest.fail("local fixture image unavailable")
        pytest.skip("local fixture image unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        '[sandbox]\nenabled=true\nbackend="docker"\nrequired=true\n'
        f'image="{probe.stdout.decode().strip()}"\n',
        encoding="utf-8",
    )
    cli = CliRunner()
    doctor = cli.invoke(app, ["sandbox", "doctor", "--workspace", str(workspace)])
    assert doctor.exit_code == 0
    configured_doctor = cli.invoke(
        app,
        [
            "sandbox",
            "doctor",
            "--workspace",
            str(workspace),
            "--config",
            str(config),
        ],
    )
    assert configured_doctor.exit_code == 0, configured_doctor.output
    assert "image_local=True" in configured_doctor.output
    result = cli.invoke(
        app,
        [
            "sandbox",
            "run",
            "--config",
            str(config),
            "--workspace",
            str(workspace),
            "--db",
            str(tmp_path / "deity.db"),
            "--",
            "cat",
            "hello.txt",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "status=succeeded" in result.output
