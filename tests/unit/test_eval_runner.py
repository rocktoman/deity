from pathlib import Path

from evals.runners.runner import run_case


def test_runner_reports_failed_assertion(tmp_path: Path) -> None:
    report = run_case("BAD", "tests/e2e/missing_test.py", tmp_path)
    assert report["passed"] is False
    assert report["verification"] == "FAIL"
    assert (tmp_path / "BAD.json").exists()
