"""S2 评分：指标聚合、失败分类、空结果不 crash。"""

from deity.application.eval_scoring import DeterministicScorer, summarize_metrics
from deity.core.contracts.eval import EvalCase, MetricRecord

CASE = EvalCase(
    id="B01",
    title="x",
    language="python",
    repo_fixture="python-mini-repo",
    task="x",
    test_command=("python", "-m", "pytest"),
    budget_seconds=60,
    assertions=("tests/unit/test_core.py",),
)


def test_scorer_pass_is_none_category() -> None:
    record = DeterministicScorer().score(CASE, passed=True, duration_ms=120)
    assert record.case_id == "B01"
    assert record.passed is True
    assert record.failure_category == "none"
    assert record.duration_ms == 120


def test_scorer_failure_within_budget_is_test_failure() -> None:
    record = DeterministicScorer().score(CASE, passed=False, duration_ms=500)
    assert record.failure_category == "test_failure"


def test_scorer_failure_over_budget_is_timeout() -> None:
    record = DeterministicScorer().score(CASE, passed=False, duration_ms=61_000)
    assert record.failure_category == "timeout"


def test_scorer_explicit_category_is_kept() -> None:
    record = DeterministicScorer().score(
        CASE, passed=False, duration_ms=10, failure_category="safety_violation"
    )
    assert record.failure_category == "safety_violation"


def test_summarize_empty_does_not_crash() -> None:
    summary = summarize_metrics([])
    assert summary["total"] == 0
    assert summary["passed"] == 0
    assert summary["pass_rate"] == 0.0


def test_summarize_aggregates_and_counts_categories() -> None:
    records = [
        MetricRecord(case_id="B01", passed=True, duration_ms=100),
        MetricRecord(case_id="B02", passed=False, duration_ms=200, failure_category="test_failure"),
        MetricRecord(case_id="B03", passed=False, duration_ms=300, failure_category="timeout"),
    ]
    summary = summarize_metrics(records)
    assert summary["total"] == 3
    assert summary["passed"] == 1
    assert summary["pass_rate"] == 1 / 3
    assert summary["failure_categories"] == {"test_failure": 1, "timeout": 1}
    assert summary["mean_duration_ms"] == 200.0
