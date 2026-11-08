"""确定性评分：只用可执行断言的通过/失败与耗时，不用模型自评。"""

import statistics
from typing import Any

from deity.core.contracts.eval import (
    EvalCase,
    FailureCategory,
    MetricRecord,
)

__all__ = ["DeterministicScorer", "summarize_metrics"]


class DeterministicScorer:
    """把一次执行的通过/失败折叠为 MetricRecord；超时按预算判定。"""

    def score(
        self,
        case: EvalCase,
        passed: bool,
        duration_ms: int,
        failure_category: FailureCategory = "none",
    ) -> MetricRecord:
        """通过为 none；失败默认按耗时判 test_failure/timeout；显式类别优先。"""
        if passed:
            category: FailureCategory = "none"
        elif failure_category != "none":
            category = failure_category
        elif duration_ms > case.budget_seconds * 1000:
            category = "timeout"
        else:
            category = "test_failure"
        return MetricRecord(
            case_id=case.id,
            passed=passed,
            duration_ms=duration_ms,
            failure_category=category,
        )


def summarize_metrics(records: list[MetricRecord]) -> dict[str, Any]:
    """聚合指标；空结果不 crash，均值用标准库 statistics。"""
    total = len(records)
    passed = sum(1 for item in records if item.passed)
    categories: dict[str, int] = {}
    for item in records:
        if item.failure_category != "none":
            categories[item.failure_category] = categories.get(item.failure_category, 0) + 1
    durations = [item.duration_ms for item in records]
    return {
        "total": total,
        "passed": passed,
        "pass_rate": (passed / total) if total else 0.0,
        "failure_categories": categories,
        "mean_duration_ms": statistics.mean(durations) if durations else 0.0,
        "median_duration_ms": statistics.median(durations) if durations else 0.0,
    }


def metric_to_dict(record: MetricRecord) -> dict[str, Any]:
    """MetricRecord 的可序列化形式，用于汇总报告。"""
    return {
        "case_id": record.case_id,
        "passed": record.passed,
        "duration_ms": record.duration_ms,
        "tool_calls": record.tool_calls,
        "turns": record.turns,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "failure_category": record.failure_category,
    }
