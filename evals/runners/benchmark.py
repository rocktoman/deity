"""确定性离线 benchmark：FakeModel/pytest 断言跑 B01–B20。

用法：python -m evals.runners.benchmark [--dataset v1] [--only B01 ...]
  [--reports-dir evals/reports]
输出 evals/reports/B*.json/.md 与汇总 benchmark-v1.json，含环境、模型、
配置 hash、代码版本与每案指标；评分只用可执行断言，不用模型自评。
"""

import argparse
import json
from pathlib import Path

from deity.application.eval_scoring import (
    DeterministicScorer,
    metric_to_dict,
    summarize_metrics,
)
from deity.application.eval_service import EvalService
from evals.runners.runner import ROOT, run_case

MODEL = "deterministic-pytest"


def main(argv: list[str] | None = None) -> int:
    """跑所选 B 案并写报告；任一失败即返回非零退出码。"""
    parser = argparse.ArgumentParser(description="Deity benchmark runner")
    parser.add_argument("--dataset", default="v1")
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--reports-dir", default=None)
    args = parser.parse_args(argv)
    reports = Path(args.reports_dir) if args.reports_dir else ROOT / "evals" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    service = EvalService(datasets_root=ROOT / "evals" / "datasets", repo_root=ROOT)
    version, cases = service.load(args.dataset)
    if args.only:
        wanted = set(args.only)
        cases = [case for case in cases if case.id in wanted]
        if len(cases) != len(wanted):
            missing = sorted(wanted - {case.id for case in cases})
            raise RuntimeError(f"未知 case：{missing}")
    environment = service.environment()
    env_dict = {
        "python": environment.python,
        "os": environment.os,
        "deity_version": environment.deity_version,
        "config_hash": environment.config_hash,
        "git_commit": environment.git_commit,
    }
    scorer = DeterministicScorer()
    records = []
    for case in cases:
        isolated = service.prepare_case(case)
        try:
            result = run_case(case.id, list(case.assertions), reports)
            record = scorer.score(case, result["passed"], int(result["duration_ms"]))
            records.append(record)
            enriched = {
                **result,
                "dataset_version": version.version,
                "environment": env_dict,
                "environment_hash": environment.fingerprint(),
                "model": MODEL,
                "config_hash": environment.config_hash,
                "git_commit": environment.git_commit,
                "code_version": environment.git_commit,
                "metric": metric_to_dict(record),
                "isolated_workdir": str(isolated.workdir),
                "isolated_db": str(isolated.db_path),
            }
            (reports / f"{case.id}.json").write_text(
                json.dumps(enriched, ensure_ascii=False, indent=2)
            )
            (reports / f"{case.id}.md").write_text(
                f"# {case.id} {case.title}\n\n"
                f"验证：{'PASS' if record.passed else 'FAIL'}\n\n"
                f"语言：{case.language}；fixture：{case.repo_fixture}\n\n"
                f"环境 hash：{environment.fingerprint()}\n\n"
                f"模型：{MODEL}；代码版本：{environment.git_commit}\n\n"
                f"断言：`{'`, `'.join(case.assertions)}`\n\n"
                f"耗时：{record.duration_ms}ms；失败类别：{record.failure_category}\n"
            )
        finally:
            service.cleanup(isolated)
    summary = {
        "dataset_version": version.version,
        "environment": env_dict,
        "environment_hash": environment.fingerprint(),
        "model": MODEL,
        "config_hash": environment.config_hash,
        "git_commit": environment.git_commit,
        "code_version": environment.git_commit,
        **summarize_metrics(records),
        "cases": [metric_to_dict(item) for item in records],
    }
    (reports / f"benchmark-{version.version}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    print(f"{summary['passed']}/{summary['total']} benchmark cases passed")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
