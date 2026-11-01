"""Isolated V0.7 experiment; writable children are not a product feature."""

import subprocess
from pathlib import Path


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check, timeout=10
    )


def test_same_file_worktrees_conflict_without_touching_user_workspace(tmp_path: Path) -> None:
    repo = tmp_path / "fixture"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Deity Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "shared.txt").write_text("base\n")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-m", "fixture")
    first = tmp_path / "agent-one"
    second = tmp_path / "agent-two"
    _git(repo, "worktree", "add", "-b", "agent-one", str(first))
    _git(repo, "worktree", "add", "-b", "agent-two", str(second))
    (first / "shared.txt").write_text("one\n")
    (second / "shared.txt").write_text("two\n")
    _git(first, "add", "shared.txt")
    _git(first, "commit", "-m", "one")
    _git(second, "add", "shared.txt")
    _git(second, "commit", "-m", "two")
    assert (repo / "shared.txt").read_text() == "base\n"
    first_diff = _git(repo, "diff", "main..agent-one", "--", "shared.txt").stdout
    second_diff = _git(repo, "diff", "main..agent-two", "--", "shared.txt").stdout
    assert "+one" in first_diff and "+two" in second_diff
    _git(repo, "merge", "--no-ff", "agent-one")
    conflict = _git(repo, "merge", "--no-ff", "agent-two", check=False)
    assert conflict.returncode != 0
    assert "CONFLICT" in conflict.stdout
    assert "<<<<<<<" in (repo / "shared.txt").read_text()
