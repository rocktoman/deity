"""S1 数据集与隔离：manifest 校验、环境 hash、case 间隔离。"""

import json
from pathlib import Path

import pytest

from deity.application.eval_service import EvalService, fixture_sha256
from deity.core.contracts.eval import EvalCase, EvalEnvironment

DATASETS = Path(__file__).parents[2] / "evals" / "datasets"
REPO = Path(__file__).parents[2]


def _service() -> EvalService:
    return EvalService(datasets_root=DATASETS, repo_root=REPO)


def test_manifest_has_twenty_cases_with_language_split() -> None:
    version, cases = _service().load("v1")
    assert version.version == "v1"
    assert len(cases) == 20
    assert [case.id for case in cases] == [f"B{i:02d}" for i in range(1, 21)]
    assert len({case.id for case in cases}) == 20
    counts = {"python": 0, "java": 0, "typescript": 0}
    for case in cases:
        counts[case.language] += 1
    assert counts == {"python": 8, "java": 6, "typescript": 6}
    assert tuple(version.cases) == tuple(case.id for case in cases)


def test_every_case_has_executable_assertions_and_mapping_note() -> None:
    _, cases = _service().load("v1")
    for case in cases:
        assert case.assertions, case.id
        assert case.test_command, case.id
        assert case.budget_seconds > 0
        assert case.repo_fixture in {
            "python-mini-repo",
            "java-mini-repo",
            "typescript-mini-repo",
        }
        if case.reuses:
            assert case.assertion_note, f"{case.id} 复用 selector 须写明映射关系"


def test_manifest_fixture_sha256_matches_disk() -> None:
    service = _service()
    version, cases = service.load("v1")
    assert version.version == "v1"
    manifest = json.loads((DATASETS / "v1" / "manifest.yaml").read_text())
    for item in manifest["fixtures"]:
        actual = fixture_sha256(REPO / item["source_path"])
        assert actual == item["sha256"], item["name"]
    assert {case.repo_fixture for case in cases} <= {item["name"] for item in manifest["fixtures"]}


def test_missing_fixture_is_rejected(tmp_path: Path) -> None:
    service = EvalService(datasets_root=tmp_path, repo_root=REPO)
    (tmp_path / "v1").mkdir()
    (tmp_path / "v1" / "manifest.yaml").write_text(
        json.dumps({"version": "v1", "cases": ["B01"], "fixtures": []})
    )
    (tmp_path / "v1" / "B01.yaml").write_text(
        json.dumps(
            {
                "id": "B01",
                "title": "缺 fixture",
                "language": "python",
                "repo_fixture": "no-such-repo",
                "task": "x",
                "test_command": ["python", "-m", "pytest", "-q", "tests/unit/test_core.py"],
                "assertions": ["tests/unit/test_core.py"],
                "dataset_version": "v1",
            }
        )
    )
    with pytest.raises(ValueError, match="repo_fixture"):
        service.load("v1")


def test_environment_hash_is_stable_and_contains_no_secret() -> None:
    env = EvalEnvironment(
        python="3.12.3",
        os="linux",
        deity_version="0.1.0",
        config_hash="abc",
        git_commit="deadbeef",
    )
    assert env.fingerprint() == env.fingerprint()
    assert "sk-fake-secret-value" not in env.fingerprint()
    other = EvalEnvironment(
        python="3.12.3",
        os="linux",
        deity_version="0.1.0",
        config_hash="xyz",
        git_commit="deadbeef",
    )
    assert other.fingerprint() != env.fingerprint()


def test_eval_case_rejects_empty_assertions() -> None:
    with pytest.raises(ValueError, match="assertions"):
        EvalCase(
            id="B99",
            title="x",
            language="python",
            repo_fixture="python-mini-repo",
            task="x",
            test_command=("python", "-m", "pytest"),
            assertions=(),
        )


def test_prepare_case_gives_each_case_its_own_dir_and_db(tmp_path: Path) -> None:
    service = _service()
    _, cases = service.load("v1")
    first, second = cases[0], cases[1]
    isolated_a = service.prepare_case(first)
    isolated_b = service.prepare_case(second)
    try:
        assert isolated_a.workdir != isolated_b.workdir
        assert isolated_a.db_path != isolated_b.db_path
        assert isolated_a.workdir.is_dir() and isolated_b.workdir.is_dir()
        # A 的隔离目录写文件，B 的副本看不到：case 间无泄漏。
        (isolated_a.workdir / "leak.txt").write_text("only-a")
        assert not (isolated_b.workdir / "leak.txt").exists()
        assert isolated_a.fixture_sha256
        assert isolated_a.fixture_sha256 == fixture_sha256(
            REPO / "tests" / "fixtures" / first.repo_fixture
        )
    finally:
        service.cleanup(isolated_a)
        service.cleanup(isolated_b)
    assert not isolated_a.workdir.exists()
    assert not isolated_b.workdir.exists()
