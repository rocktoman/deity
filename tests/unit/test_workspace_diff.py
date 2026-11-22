"""Codex 移植：工作区 diff 助手（workspace_diff）的单元测试。"""

import subprocess
from pathlib import Path

import pytest

from deity.interfaces.cli.workspace_diff import git_branch, untracked_files, workspace_diff


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(cwd),
        },
    )


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_git_branch_in_repo(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    branch = git_branch(repo)
    assert branch in {"master", "main"}


def test_git_branch_outside_repo_is_none(tmp_path: Path) -> None:
    assert git_branch(tmp_path) is None


@pytest.mark.asyncio
async def test_workspace_diff_reports_modified_and_untracked(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    (repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "new.txt").write_text("fresh\n", encoding="utf-8")
    result = await workspace_diff(repo)
    assert result["is_git"] is True
    assert "two" in result["diff"]
    assert result["untracked"] == ["new.txt"]


@pytest.mark.asyncio
async def test_workspace_diff_clean_repo_is_empty(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    result = await workspace_diff(repo)
    assert result["diff"] == ""
    assert result["untracked"] == []


@pytest.mark.asyncio
async def test_workspace_diff_non_git_lists_files_as_untracked(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    result = await workspace_diff(tmp_path)
    assert result["is_git"] is False
    assert "x.py" in result["untracked"]


def test_untracked_files_respects_limit(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    for i in range(5):
        (repo / f"f{i}.txt").write_text("x", encoding="utf-8")
    assert untracked_files(repo, limit=3) == ["f0.txt", "f1.txt", "f2.txt"]
