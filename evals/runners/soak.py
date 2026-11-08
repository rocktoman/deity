"""长时 soak：循环跑 FakeModel 短任务，采样内存、事件行数与孤儿进程。

用法：python -m evals.runners.soak [--minutes 10] [--threshold-mb 64]
  [--reports-dir evals/reports]
输出 evals/reports/soak.json；RSS 增长超阈值或发现孤儿进程即失败（退出码 1）。

RSS 采样：Linux 读 /proc/self/status 的 VmRSS（当前值，非 high-water）；
无 /proc 时回退到 resource.ru_maxrss。阈值首轮实测后固化，见
docs/04-evaluation/v1.0-release-gate.md。
"""

import argparse
import asyncio
import json
import os
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, RunStatus

ROOT = Path(__file__).parents[2]
DEFAULT_THRESHOLD_MB = 64.0


class FakeModel:
    """确定性短任务模型：第一轮读一个文件，第二轮给出一句结论。"""

    def __init__(self) -> None:
        self._turns = iter(
            [
                [
                    ModelEvent("tool_call_started", call_id="soak-c1", name="read_file"),
                    ModelEvent(
                        "tool_call_arguments_delta",
                        call_id="soak-c1",
                        text='{"path": "calc.py"}',
                    ),
                    ModelEvent("message_completed"),
                ],
                [ModelEvent("text_delta", text="ok"), ModelEvent("message_completed")],
            ]
        )

    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        for event in next(self._turns):
            yield event


def current_rss_mb() -> tuple[float, str]:
    """返回当前 RSS（MB）与采样方法。"""
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) / 1024.0, "proc_status"
    except OSError:
        pass
    ru_maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux: KB；macOS: bytes。
    if sys.platform == "darwin":
        return ru_maxrss / (1024.0 * 1024.0), "ru_maxrss"
    return ru_maxrss / 1024.0, "ru_maxrss"


def count_child_processes() -> int | None:
    """用 ps 检查本进程的子进程数；ps 不可用时返回 None。

    注意：ps 自身也是本进程的子进程，须按 PID 排除，否则恒为 1。
    """
    try:
        with subprocess.Popen(
            ["ps", "-o", "pid=", "--ppid", str(os.getpid())],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ) as proc:
            assert proc.stdout is not None
            output = proc.stdout.read()
            ps_pid = proc.pid
    except OSError:
        return None
    pids = {int(line.strip()) for line in output.splitlines() if line.strip().isdigit()}
    pids.discard(ps_pid)
    return len(pids)


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(len(ordered) * pct / 100.0)))
    return ordered[index]


async def _run_once(db_path: Path, workspace: Path) -> float:
    started = time.monotonic()
    service = RunService(db_path=db_path, workspace=workspace, model=FakeModel())  # type: ignore[arg-type]
    result = await asyncio.wait_for(
        service.run("读取 calc.py 并给出结论", model_name="fake"), timeout=30
    )
    assert result.status == RunStatus.COMPLETED, result.status
    return (time.monotonic() - started) * 1000.0


async def soak(minutes: float, threshold_mb: float) -> dict[str, object]:
    """循环跑短任务直到时限，返回采样与判定。"""
    deadline = time.monotonic() + minutes * 60.0
    started_at = time.monotonic()
    rss_start, method = current_rss_mb()
    samples: list[dict[str, float]] = [{"t_seconds": 0.0, "rss_mb": rss_start}]
    durations: list[float] = []
    iterations = 0
    last_sample = started_at
    with tempfile.TemporaryDirectory(prefix="deity-soak-") as tmp:
        tmp_path = Path(tmp)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        db_path = tmp_path / "deity.db"
        while time.monotonic() < deadline:
            durations.append(await _run_once(db_path, workspace))
            iterations += 1
            now = time.monotonic()
            if now - last_sample >= 30.0 or now >= deadline:
                rss, _ = current_rss_mb()
                samples.append({"t_seconds": now - started_at, "rss_mb": rss})
                last_sample = now
        with sqlite3.connect(db_path) as db:
            event_rows = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            run_rows = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    rss_end, _ = current_rss_mb()
    samples.append({"t_seconds": time.monotonic() - started_at, "rss_mb": rss_end})
    growth = rss_end - rss_start
    orphans = count_child_processes()
    return {
        "minutes": minutes,
        "threshold_mb": threshold_mb,
        "rss_method": method,
        "rss_start_mb": round(rss_start, 2),
        "rss_end_mb": round(rss_end, 2),
        "rss_growth_mb": round(growth, 2),
        "rss_max_mb": round(max(sample["rss_mb"] for sample in samples), 2),
        "samples": samples,
        "iterations": iterations,
        "runs": run_rows,
        "event_rows": event_rows,
        "duration_ms_mean": round(statistics.mean(durations), 2) if durations else 0.0,
        "duration_ms_p95": round(percentile(durations, 95), 2),
        "orphan_processes": orphans,
        "leak": growth > threshold_mb,
        "passed": growth <= threshold_mb and (orphans == 0 or orphans is None),
    }


def main(argv: list[str] | None = None) -> int:
    """解析参数、跑 soak、写报告；泄漏或孤儿进程即返回非零退出码。"""
    parser = argparse.ArgumentParser(description="Deity soak runner")
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--threshold-mb", type=float, default=DEFAULT_THRESHOLD_MB)
    parser.add_argument("--reports-dir", default=None)
    args = parser.parse_args(argv)
    reports = Path(args.reports_dir) if args.reports_dir else ROOT / "evals" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(soak(args.minutes, args.threshold_mb))
    (reports / "soak.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        f"soak {result['iterations']} iterations, "
        f"RSS {result['rss_start_mb']} -> {result['rss_end_mb']} MB "
        f"(growth {result['rss_growth_mb']} MB, threshold {result['threshold_mb']} MB), "
        f"events {result['event_rows']}, orphans {result['orphan_processes']}, "
        f"p95 {result['duration_ms_p95']}ms: {'PASS' if result['passed'] else 'FAIL'}"
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
