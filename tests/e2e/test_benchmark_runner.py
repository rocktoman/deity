"""S2 运行器端到端：benchmark 跑 2 个 B 案，报告字段齐全。"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_benchmark_two_cases_write_complete_reports(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.runners.benchmark",
            "--dataset",
            "v1",
            "--only",
            "B01",
            "B02",
            "--reports-dir",
            str(reports),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "2/2" in completed.stdout
    for case_id in ("B01", "B02"):
        report = json.loads((reports / f"{case_id}.json").read_text())
        assert report["case_id"] == case_id
        assert report["passed"] is True
        assert report["environment"]["python"]
        assert report["environment"]["git_commit"]
        assert report["model"] == "deterministic-pytest"
        assert report["config_hash"]
        assert report["dataset_version"] == "v1"
        assert "duration_ms" in report
        assert (reports / f"{case_id}.md").is_file()
    summary = json.loads((reports / "benchmark-v1.json").read_text())
    assert summary["dataset_version"] == "v1"
    assert summary["passed"] == 2 and summary["total"] == 2
    assert summary["model"] == "deterministic-pytest"
    assert summary["git_commit"]
    assert summary["environment_hash"]
    assert len(summary["cases"]) == 2
    assert all(item["passed"] for item in summary["cases"])
