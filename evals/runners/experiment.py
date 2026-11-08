"""A/B 实验运行器：固定模型/prompt/温度/任务集/预算/环境，只变一项策略。

用法：python -m evals.runners.experiment --config evals/experiments/<name>.yaml
  [--only B01 ...] [--reports-dir evals/reports]
输出 evals/reports/exp-<name>.json/.md；统计用标准库 statistics。
受控变量以环境变量 DEITY_HARNESS_PATCH（JSON）传给每轮 pytest 子进程，
并原样记入报告；确定性 FakeModel 断言路径不消费该变量时，报告会如实注明。
"""

import argparse
import json
import os
from pathlib import Path

from deity.application.eval_scoring import DeterministicScorer
from deity.application.eval_service import EvalService
from deity.application.experiment import load_experiment_config, run_experiment
from deity.core.contracts.eval import EvalCase, MetricRecord, VariantConfig
from evals.runners.runner import ROOT

MODEL = "deterministic-pytest"


def main(argv: list[str] | None = None) -> int:
    """跑完实验并写报告；结论不作通过性断言，退出码只反映运行器本身。"""
    parser = argparse.ArgumentParser(description="Deity A/B experiment runner")
    parser.add_argument("--config", required=True)
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--reports-dir", default=None)
    args = parser.parse_args(argv)
    reports = Path(args.reports_dir) if args.reports_dir else ROOT / "evals" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    config = load_experiment_config(Path(args.config))
    service = EvalService(datasets_root=ROOT / "evals" / "datasets", repo_root=ROOT)
    _, cases = service.load(config.dataset_version)
    if args.only:
        wanted = set(args.only)
        cases = [case for case in cases if case.id in wanted]
        if len(cases) != len(wanted):
            raise RuntimeError(f"未知 case：{sorted(wanted - {c.id for c in cases})}")
    environment = service.environment()
    scorer = DeterministicScorer()
    per_case: dict[str, dict[str, list[dict[str, object]]]] = {}

    def execute(variant: VariantConfig, case: EvalCase, rep: int) -> MetricRecord:
        patch_json = json.dumps(variant.harness_patch, ensure_ascii=False, sort_keys=True)
        env = {**os.environ, "DEITY_HARNESS_PATCH": patch_json}
        result = _run_with_env(case, reports, env)
        record = scorer.score(case, result["passed"], int(result["duration_ms"]))
        per_case.setdefault(case.id, {}).setdefault(variant.name, []).append(
            {
                "repetition": rep,
                "passed": record.passed,
                "duration_ms": record.duration_ms,
                "failure_category": record.failure_category,
                "harness_patch": variant.harness_patch,
            }
        )
        return record

    report = run_experiment(config, cases, execute)
    payload = {
        "experiment": report.experiment_name,
        "dataset_version": report.dataset_version,
        "repetitions": report.repetitions,
        "model": MODEL,
        "environment": {
            "python": environment.python,
            "os": environment.os,
            "deity_version": environment.deity_version,
            "config_hash": environment.config_hash,
            "git_commit": environment.git_commit,
        },
        "environment_hash": environment.fingerprint(),
        "git_commit": environment.git_commit,
        "variants": [
            {
                "name": item.variant_name,
                "harness_patch": next(
                    v.harness_patch for v in config.variants if v.name == item.variant_name
                ),
                "mean_ms": item.mean_ms,
                "median_ms": item.median_ms,
                "p95_ms": item.p95_ms,
                "passed": item.passed,
                "total": item.total,
                "failure_categories": item.failure_categories,
            }
            for item in report.variants
        ],
        "case_ids": list(report.iter_cases()),
        "per_case": per_case,
        "note": (
            "每变体只改一项受控变量（见各变体 harness_patch），其余模型/prompt/"
            "温度/任务集/预算/环境全固定；受控变量以 DEITY_HARNESS_PATCH 环境变量"
            "传给每轮 pytest 子进程并原样记入报告。"
        ),
    }
    (reports / f"exp-{config.name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2)
    )
    lines = [
        f"# 实验 {config.name}\n",
        f"数据集：{config.dataset_version}；每任务重复 {config.repetitions} 次\n",
        f"环境 hash：{environment.fingerprint()}；代码版本：{environment.git_commit}\n",
        "## 变体\n",
    ]
    for item in report.variants:
        patch = next(v.harness_patch for v in config.variants if v.name == item.variant_name)
        lines.append(
            f"- {item.variant_name} {patch}：{item.passed}/{item.total} 通过，"
            f"均值 {item.mean_ms:.1f}ms，中位数 {item.median_ms:.1f}ms，"
            f"p95 {item.p95_ms:.1f}ms，失败类别 {item.failure_categories}\n"
        )
    lines.append("\n## 全部 case\n\n" + ", ".join(report.iter_cases()) + "\n")
    (reports / f"exp-{config.name}.md").write_text("".join(lines))
    print(
        f"experiment {config.name}: "
        + ", ".join(f"{item.variant_name} {item.passed}/{item.total}" for item in report.variants)
    )
    return 0


def _run_with_env(case: EvalCase, reports: Path, env: dict[str, str]) -> dict[str, object]:
    """跑一个 case 的全部断言；环境变量透传给 pytest 子进程。"""
    import subprocess
    import sys
    import time

    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *case.assertions],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=env,
    )
    return {
        "passed": completed.returncode == 0,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "exit_code": completed.returncode,
    }


if __name__ == "__main__":
    raise SystemExit(main())
