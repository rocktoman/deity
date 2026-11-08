"""S1 隔离集成：两案先后/并行跑，互不污染；每案独立临时 DB。"""

from pathlib import Path

from deity.application.eval_service import EvalService

DATASETS = Path(__file__).parents[2] / "evals" / "datasets"
REPO = Path(__file__).parents[2]


def test_two_cases_run_in_isolated_copies() -> None:
    service = EvalService(datasets_root=DATASETS, repo_root=REPO)
    _, cases = service.load("v1")
    by_lang = {case.language: case for case in cases}
    py_case = by_lang["python"]
    java_case = by_lang["java"]
    isolated_py = service.prepare_case(py_case)
    isolated_java = service.prepare_case(java_case)
    try:
        # 各自的隔离副本都有 fixture 文件，且互不相同目录。
        assert list(isolated_py.workdir.iterdir())
        assert list(isolated_java.workdir.iterdir())
        assert isolated_py.workdir != isolated_java.workdir
        # 在 Python 副本里改文件，Java 副本与原始 fixture 都不受影响。
        target = next(isolated_py.workdir.iterdir())
        target.write_text("mutated by eval")
        assert (REPO / "tests" / "fixtures" / py_case.repo_fixture / target.name).read_text(
            encoding="utf-8"
        ) != "mutated by eval"
        # 每案独立临时 DB，路径在各自的隔离目录之外、互不相同。
        assert isolated_py.db_path.parent != isolated_java.db_path.parent
        assert isolated_py.db_path.name == "deity.db"
        assert isolated_java.db_path.name == "deity.db"
    finally:
        service.cleanup(isolated_py)
        service.cleanup(isolated_java)
