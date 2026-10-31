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
    assert "sha256:" in listed.output and "sha256:" in inspected.output
    assert "secret body text" not in listed.output + inspected.output


def test_skills_cli_reports_parse_error_with_source(tmp_path: Path) -> None:
    root = tmp_path / ".deity/skills"
    good = root / "good"
    bad = root / "bad"
    good.mkdir(parents=True)
    bad.mkdir()
    (good / "SKILL.md").write_text("---\nname: good\ndescription: Good\n---\nbody")
    (bad / "SKILL.md").write_text("malformed")
    listed = CliRunner().invoke(app, ["skills", "list", "--workspace", str(tmp_path)])
    assert listed.exit_code == 2
    assert "good" in listed.output and "bad" in listed.output
    assert "project" in listed.output and "frontmatter" in listed.output
