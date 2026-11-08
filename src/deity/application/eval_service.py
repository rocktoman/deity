"""评估服务：加载校验数据集、隔离复制 fixture、记录环境 hash。

每个 case 跑在独立复制目录、独立临时 DB；跑完清理，失败时保留路径写进报告。
环境 hash 只含版本类信息，不含密钥。
"""

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from deity import __version__
from deity.core.contracts.eval import DatasetVersion, EvalCase, EvalEnvironment, Language

KNOWN_FIXTURES = {
    "python-mini-repo": "python",
    "java-mini-repo": "java",
    "typescript-mini-repo": "typescript",
}


def fixture_sha256(path: Path) -> str:
    """fixture 目录的内容 hash：按相对路径排序，拼路径与文件内容。"""
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            digest.update(str(item.relative_to(path)).encode())
            digest.update(b"\0")
            digest.update(item.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _git_commit(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    commit = completed.stdout.strip()
    return commit if completed.returncode == 0 and commit else "unknown"


@dataclass
class IsolatedCase:
    """一个 case 的隔离工作区：独立复制目录 + 独立临时 DB。"""

    case: EvalCase
    workdir: Path
    db_path: Path
    fixture_sha256: str


class EvalService:
    """加载 evals/datasets/<version>，校验 schema，做隔离准备。"""

    def __init__(self, datasets_root: Path, repo_root: Path) -> None:
        self._datasets_root = datasets_root
        self._repo_root = repo_root

    def load(self, version: str = "v1") -> tuple[DatasetVersion, list[EvalCase]]:
        """加载并校验一个数据集版本；缺 fixture、重复 ID、坏 schema 都报错。"""
        dataset_dir = self._datasets_root / version
        manifest_path = dataset_dir / "manifest.yaml"
        if not manifest_path.is_file():
            raise ValueError(f"数据集 {version} 缺少 manifest：{manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        fixtures = {
            str(item["name"]): item
            for item in manifest.get("fixtures", [])
            if isinstance(item, dict)
        }
        case_ids = [str(item) for item in manifest.get("cases", [])]
        cases: list[EvalCase] = []
        seen: set[str] = set()
        for case_id in case_ids:
            case_path = dataset_dir / f"{case_id}.yaml"
            if not case_path.is_file():
                raise ValueError(f"数据集 {version} 缺少 case 文件：{case_path}")
            spec = json.loads(case_path.read_text(encoding="utf-8"))
            case = EvalCase(
                id=str(spec["id"]),
                title=str(spec.get("title", "")),
                language=cast("Language", str(spec["language"])),
                repo_fixture=str(spec["repo_fixture"]),
                task=str(spec.get("task", "")),
                test_command=tuple(str(item) for item in spec.get("test_command", [])),
                policy=str(spec.get("policy", "ask")),
                budget_seconds=int(spec.get("budget_seconds", 60)),
                expected_changes=tuple(str(item) for item in spec.get("expected_changes", [])),
                assertions=tuple(str(item) for item in spec.get("assertions", [])),
                reuses=tuple(str(item) for item in spec.get("reuses", [])),
                assertion_note=str(spec.get("assertion_note", "")),
                dataset_version=str(spec.get("dataset_version", version)),
            )
            if case.id in seen:
                raise ValueError(f"数据集 {version} 有重复 case ID：{case.id}")
            seen.add(case.id)
            fixture = fixtures.get(case.repo_fixture)
            if fixture is None:
                raise ValueError(f"{case.id} 的 repo_fixture 未知：{case.repo_fixture}")
            fixture_path = self._repo_root / str(fixture["source_path"])
            if not fixture_path.is_dir():
                raise ValueError(f"{case.id} 的 fixture 不存在：{fixture_path}")
            actual = fixture_sha256(fixture_path)
            if actual != str(fixture["sha256"]):
                raise ValueError(f"{case.id} 的 fixture 已漂移：{fixture_path} sha256 不符")
            cases.append(case)
        return DatasetVersion(version=version, cases=tuple(case_ids)), cases

    def environment(self, config_text: str = "") -> EvalEnvironment:
        """当前环境快照；config_hash 是配置文本的 hash，不含密钥原文。"""
        return EvalEnvironment(
            python=sys.version.split()[0],
            os=f"{platform.system().lower()}-{platform.machine()}",
            deity_version=__version__,
            config_hash=hashlib.sha256(config_text.encode()).hexdigest(),
            git_commit=_git_commit(self._repo_root),
        )

    def prepare_case(self, case: EvalCase) -> IsolatedCase:
        """把 fixture 复制到隔离临时目录；每案独立临时 DB。"""
        fixture_path = self._repo_root / "tests" / "fixtures" / case.repo_fixture
        if not fixture_path.is_dir():
            raise ValueError(f"{case.id} 的 fixture 不存在：{fixture_path}")
        digest = fixture_sha256(fixture_path)
        tmpdir = Path(tempfile.mkdtemp(prefix=f"deity-eval-{case.id}-"))
        workdir = tmpdir / "repo"
        shutil.copytree(fixture_path, workdir)
        return IsolatedCase(
            case=case,
            workdir=workdir,
            db_path=tmpdir / "deity.db",
            fixture_sha256=digest,
        )

    def cleanup(self, isolated: IsolatedCase) -> None:
        """清理隔离临时目录（失败时保留路径由调用方写进报告）。"""
        shutil.rmtree(isolated.db_path.parent, ignore_errors=True)


__all__ = ["KNOWN_FIXTURES", "EvalService", "IsolatedCase", "fixture_sha256"]
