from pathlib import Path

from typer.testing import CliRunner

from deity.interfaces.cli.main import app


def test_skills_cli_lists_and_inspects_metadata(tmp_path: Path) -> None:
    directory = tmp_path / ".deity/skills/demo"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo metadata\n---\nsecret body text"
    )
    runner = CliRunner()
    listed = runner.invoke(app, ["skills", "list", "--workspace", str(tmp_path)])
    inspected = runner.invoke(app, ["skills", "inspect", "demo", "--workspace", str(tmp_path)])
    assert listed.exit_code == 0 and "demo" in listed.output
    assert inspected.exit_code == 0 and "Demo metadata" in inspected.output
    assert "secret body text" not in listed.output + inspected.output
