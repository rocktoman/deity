"""S3 运行器端到端：示例实验跑完并出报告，列出全部变体与 case。"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_experiment_example_reports_both_variants(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.runners.experiment",
            "--config",
            "evals/experiments/compaction-ab.yaml",
            "--only",
            "B01",
            "--reports-dir",
            str(reports),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads((reports / "exp-compaction-ab.json").read_text())
    assert payload["experiment"] == "compaction-ab"
    assert payload["repetitions"] == 3
    assert payload["case_ids"] == ["B01"]
    assert {item["name"] for item in payload["variants"]} == {"off", "on"}
    patches = {item["name"]: item["harness_patch"] for item in payload["variants"]}
    assert patches["off"] == {"compaction_enabled": False}
    assert patches["on"] == {"compaction_enabled": True}
    for item in payload["variants"]:
        assert item["total"] == 3
        assert "mean_ms" in item and "median_ms" in item and "p95_ms" in item
    assert (reports / "exp-compaction-ab.md").is_file()
