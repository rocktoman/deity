"""A/B 实验执行：固定一切，只变一项受控变量；统计只用标准库。"""

import json
import math
import statistics
from collections.abc import Callable
from pathlib import Path

from deity.core.contracts.eval import (
    ComparisonReport,
    EvalCase,
    ExperimentConfig,
    MetricRecord,
    VariantConfig,
    VariantStats,
)

__all__ = ["load_experiment_config", "run_experiment"]


def load_experiment_config(path: Path) -> ExperimentConfig:
    """从 YAML（JSON 形式，与既有 cases 惯例一致）加载实验配置并校验。"""
    spec = json.loads(path.read_text(encoding="utf-8"))
    return ExperimentConfig(
        name=str(spec["name"]),
        dataset_version=str(spec.get("dataset_version", "v1")),
        repetitions=int(spec.get("repetitions", 3)),
        variants=tuple(
            VariantConfig(
                name=str(item["name"]),
                harness_patch=dict(item.get("harness_patch", {})),
            )
            for item in spec.get("variants", [])
        ),
    )


def _p95(sorted_values: list[float]) -> float:
    """第 95 百分位：标准库 statistics 排序后的最近秩法。"""
    if not sorted_values:
        return 0.0
    rank = math.ceil(0.95 * len(sorted_values))
    return float(sorted_values[max(0, min(rank, len(sorted_values)) - 1)])


def run_experiment(
    config: ExperimentConfig,
    cases: list[EvalCase],
    execute: Callable[[VariantConfig, EvalCase, int], MetricRecord],
) -> ComparisonReport:
    """每变体 × 每 case × 每次重复都执行；报告列出全部变体与全部 case。"""
    variants: list[VariantStats] = []
    for variant in config.variants:
        durations: list[float] = []
        passed = 0
        total = 0
        categories: dict[str, int] = {}
        for case in cases:
            for rep in range(config.repetitions):
                record = execute(variant, case, rep)
                durations.append(float(record.duration_ms))
                total += 1
                if record.passed:
                    passed += 1
                elif record.failure_category != "none":
                    categories[record.failure_category] = (
                        categories.get(record.failure_category, 0) + 1
                    )
        ordered = sorted(durations)
        variants.append(
            VariantStats(
                variant_name=variant.name,
                mean_ms=statistics.mean(durations) if durations else 0.0,
                median_ms=statistics.median(durations) if durations else 0.0,
                p95_ms=_p95(ordered),
                passed=passed,
                total=total,
                failure_categories=categories,
            )
        )
    return ComparisonReport(
        experiment_name=config.name,
        dataset_version=config.dataset_version,
        repetitions=config.repetitions,
        case_ids=tuple(case.id for case in cases),
        variants=tuple(variants),
    )
