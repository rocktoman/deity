"""S3 A/B 实验：多变量校验失败、重复次数不足报错、报告含全部变体与 case。"""

import pytest

from deity.application.experiment import load_experiment_config, run_experiment
from deity.core.contracts.eval import (
    EvalCase,
    ExperimentConfig,
    MetricRecord,
    VariantConfig,
)

CASES = [
    EvalCase(
        id="B01",
        title="x",
        language="python",
        repo_fixture="python-mini-repo",
        task="x",
        test_command=("python", "-m", "pytest"),
        assertions=("tests/unit/test_core.py",),
    ),
    EvalCase(
        id="B02",
        title="y",
        language="python",
        repo_fixture="python-mini-repo",
        task="y",
        test_command=("python", "-m", "pytest"),
        assertions=("tests/unit/test_core.py",),
    ),
]


def test_variant_with_two_changed_keys_is_rejected() -> None:
    with pytest.raises(ValueError, match="只能改一项"):
        VariantConfig(name="bad", harness_patch={"compaction_enabled": True, "max_turns": 5})


def test_variant_with_zero_changed_keys_is_rejected() -> None:
    with pytest.raises(ValueError, match="只能改一项"):
        VariantConfig(name="empty", harness_patch={})


def test_experiment_needs_at_least_two_repetitions() -> None:
    with pytest.raises(ValueError, match="至少重复 2 次"):
        ExperimentConfig(
            name="x",
            dataset_version="v1",
            repetitions=1,
            variants=(
                VariantConfig(name="a", harness_patch={"compaction_enabled": False}),
                VariantConfig(name="b", harness_patch={"compaction_enabled": True}),
            ),
        )


def test_experiment_needs_at_least_two_variants() -> None:
    with pytest.raises(ValueError, match="至少需要 2 个变体"):
        ExperimentConfig(
            name="x",
            dataset_version="v1",
            repetitions=2,
            variants=(VariantConfig(name="a", harness_patch={"compaction_enabled": False}),),
        )


def _config() -> ExperimentConfig:
    return ExperimentConfig(
        name="compaction-ab",
        dataset_version="v1",
        repetitions=2,
        variants=(
            VariantConfig(name="off", harness_patch={"compaction_enabled": False}),
            VariantConfig(name="on", harness_patch={"compaction_enabled": True}),
        ),
    )


def test_report_lists_all_variants_and_all_cases() -> None:
    seen: list[tuple[str, str, int]] = []

    def execute(variant: VariantConfig, case: EvalCase, rep: int) -> MetricRecord:
        seen.append((variant.name, case.id, rep))
        return MetricRecord(case_id=case.id, passed=True, duration_ms=100 + rep)

    report = run_experiment(_config(), CASES, execute)
    assert report.experiment_name == "compaction-ab"
    assert report.repetitions == 2
    assert tuple(report.iter_cases()) == ("B01", "B02")
    assert {item.variant_name for item in report.variants} == {"off", "on"}
    for item in report.variants:
        assert item.total == 4  # 2 case × 2 次重复
        assert item.passed == 4
    # 每变体 × 每 case × 每次重复都跑了，没有选择性省略。
    assert len(seen) == 8


def test_statistics_use_stdlib_and_count_failures() -> None:
    durations = {"off": [100, 200, 300, 400], "on": [100, 100, 100, 100]}

    def execute(variant: VariantConfig, case: EvalCase, rep: int) -> MetricRecord:
        value = durations[variant.name].pop(0)
        failed = variant.name == "off" and value == 400
        return MetricRecord(
            case_id=case.id,
            passed=not failed,
            duration_ms=value,
            failure_category="test_failure" if failed else "none",
        )

    report = run_experiment(_config(), CASES, execute)
    off = next(item for item in report.variants if item.variant_name == "off")
    assert off.mean_ms == 250.0
    assert off.median_ms == 250.0
    assert off.p95_ms == 400.0
    assert off.passed == 3
    assert off.failure_categories == {"test_failure": 1}
    on = next(item for item in report.variants if item.variant_name == "on")
    assert on.mean_ms == 100.0
    assert on.p95_ms == 100.0


def test_load_experiment_config_rejects_multi_variable_patch(tmp_path) -> None:
    import json

    path = tmp_path / "bad.yaml"
    path.write_text(
        json.dumps(
            {
                "name": "bad",
                "dataset_version": "v1",
                "repetitions": 2,
                "variants": [
                    {"name": "a", "harness_patch": {"x": 1, "y": 2}},
                    {"name": "b", "harness_patch": {"x": 1}},
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="只能改一项"):
        load_experiment_config(path)
