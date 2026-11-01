import subprocess
import sys
from pathlib import Path


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "deity.interfaces.cli.main", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_v05_help_code_and_verification_plan(tmp_path: Path) -> None:
    help_result = _cli("--help")
    assert help_result.returncode == 0
    for command in ("plan", "build", "plans", "code", "verify"):
        assert command in help_result.stdout

    config = tmp_path / "config.toml"
    config.write_text(
        "[coding]\ncode_intelligence_enabled=true\nverification_enabled=true\n"
        "[code_intelligence]\ncache_enabled=false\n"
        "[verification]\npermission='deny'\n",
        encoding="utf-8",
    )
    (tmp_path / "feature.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    symbol = _cli(
        "code",
        "symbol",
        "target",
        "--workspace",
        str(tmp_path),
        "--config",
        str(config),
    )
    assert symbol.returncode == 0 and '"line": 1' in symbol.stdout

    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths=['tests']\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("version = 1", encoding="utf-8")
    verification = _cli(
        "verify",
        "--show-plan",
        "--workspace",
        str(tmp_path),
        "--config",
        str(config),
        "--db",
        str(tmp_path / "deity.db"),
    )
    assert verification.returncode == 0
    assert "pytest" in verification.stdout
