import subprocess
import sys
from pathlib import Path


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "deity.interfaces.cli.main", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_help_and_version() -> None:
    assert run_cli("--help").returncode == 0
    version = run_cli("--version")
    assert version.returncode == 0
    assert "0.1.0" in version.stdout


def test_run_requires_model_config(tmp_path: Path) -> None:
    result = run_cli("run", "test", "--config", str(tmp_path / "missing.toml"))
    assert result.returncode != 0
    assert "模型未配置" in result.stdout + result.stderr
