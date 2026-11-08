"""V1.0 S4：短 soak（1 分钟），断言无泄漏信号。

完整 10 分钟 soak 由 `python -m evals.runners.soak --minutes 10` 跑，
报告见 evals/reports/soak.json。
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_short_soak_has_no_leak_signal(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.runners.soak",
            "--minutes",
            "1",
            "--reports-dir",
            str(reports),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout + completed.stderr)[-2000:]
    report = json.loads((reports / "soak.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["leak"] is False
    assert report["orphan_processes"] == 0
    assert report["iterations"] > 0
    assert report["event_rows"] > 0
    assert report["rss_growth_mb"] <= report["threshold_mb"]
