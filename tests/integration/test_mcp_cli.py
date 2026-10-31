import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from deity.interfaces.cli.main import app


def test_mcp_cli_list_and_test(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "fixtures/mcp_echo_server.py"
    config = tmp_path / "config.toml"
    config.write_text(
        "[extensions]\nmcp_enabled = true\n[mcp.servers.one]\n"
        f"command = {json.dumps(sys.executable)}\nargs = [{json.dumps(str(script))}]\n"
    )
    runner = CliRunner()
    listed = runner.invoke(
        app, ["mcp", "list", "--config", str(config), "--workspace", str(tmp_path)]
    )
    assert listed.exit_code == 0 and "one" in listed.output
    tested = runner.invoke(
        app,
        [
            "mcp",
            "test",
            "--config",
            str(config),
            "--workspace",
            str(tmp_path),
            "--db",
            str(tmp_path / "deity.db"),
        ],
        input="y\n",
    )
    assert tested.exit_code == 0, tested.output
    assert "mcp:one:echo" in tested.output
