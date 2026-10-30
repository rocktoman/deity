import subprocess
import sys


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


def test_run_is_not_implemented() -> None:
    result = run_cli("run", "test")
    assert result.returncode != 0
    assert "模型未配置" in result.stdout + result.stderr
