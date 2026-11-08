"""评估平台契约：数据集、运行器/评分、A/B 实验、回放证据。

Core 层只定义数据与端口，不依赖 CLI、SQLite 或任何厂商 SDK；
统计只用标准库，不引入 scipy/numpy。
"""

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

__all__ = (
    "ComparisonReport",
    "DatasetVersion",
    "EvalCase",
    "EvalEnvironment",
    "EvalRunner",
    "EvidenceBundle",
    "ExperimentConfig",
    "FailureCategory",
    "FixtureManifest",
    "Language",
    "MetricRecord",
    "Scorer",
    "VariantConfig",
    "VariantStats",
)

Language = Literal["python", "java", "typescript"]
FailureCategory = Literal["none", "test_failure", "timeout", "safety_violation", "error"]


@dataclass(frozen=True)
class EvalCase:
    """一个固定评估用例：固定 fixture、任务、可执行断言。"""

    id: str
    title: str
    language: Language
    repo_fixture: str
    task: str
    test_command: tuple[str, ...]
    policy: str = "ask"
    budget_seconds: int = 60
    expected_changes: tuple[str, ...] = ()
    assertions: tuple[str, ...] = ()
    reuses: tuple[str, ...] = ()
    assertion_note: str = ""
    dataset_version: str = "v1"

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("EvalCase.id 不能为空")
        if self.language not in ("python", "java", "typescript"):
            raise ValueError(f"不支持的 language：{self.language}")
        if not self.test_command:
            raise ValueError(f"{self.id} 的 test_command 不能为空")
        if self.budget_seconds <= 0:
            raise ValueError(f"{self.id} 的 budget_seconds 必须大于 0")
        if not self.assertions:
            raise ValueError(f"{self.id} 的 assertions 不能为空：断言必须是可执行断言")


@dataclass(frozen=True)
class FixtureManifest:
    """一个 fixture 仓库的清单：来源路径与内容 hash。"""

    name: str
    language: Language
    source_path: str
    sha256: str
    environment_hash: str = ""


@dataclass(frozen=True)
class EvalEnvironment:
    """一次评估的环境快照；字段只含版本类信息，不含密钥。"""

    python: str
    os: str
    deity_version: str
    config_hash: str
    git_commit: str

    def fingerprint(self) -> str:
        """环境 hash：Python 版本 + OS + 代码版本 + 配置 hash，不含密钥。"""
        encoded = json.dumps(
            {
                "python": self.python,
                "os": self.os,
                "deity_version": self.deity_version,
                "config_hash": self.config_hash,
                "git_commit": self.git_commit,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DatasetVersion:
    """数据集版本：版本号 + 固定 case 清单。"""

    version: str
    cases: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetricRecord:
    """一个 case 的评分指标；只用可执行断言，不用模型自评。"""

    case_id: str
    passed: bool
    duration_ms: int
    tool_calls: int | None = None
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    failure_category: FailureCategory = "none"


@dataclass(frozen=True)
class EvidenceBundle:
    """一个 case 的证据包：报告文件路径与事件摘要。"""

    report_json: str
    report_md: str
    events: tuple[str, ...] = ()


class EvalRunner(Protocol):
    """在给定工作区跑一个 case，返回可执行断言的评分指标。"""

    def run(self, case: EvalCase, workspace: str) -> MetricRecord: ...


class Scorer(Protocol):
    """把一次执行的通过/失败与耗时折叠为 MetricRecord。"""

    def score(
        self,
        case: EvalCase,
        passed: bool,
        duration_ms: int,
        failure_category: FailureCategory = "none",
    ) -> MetricRecord: ...


@dataclass(frozen=True)
class VariantConfig:
    """一个实验变体：只允许改一项受控变量，多于一项校验失败。"""

    name: str
    harness_patch: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("VariantConfig.name 不能为空")
        if len(self.harness_patch) != 1:
            raise ValueError(
                f"变体 {self.name} 只能改一项受控变量，实际 {len(self.harness_patch)} 项"
            )


@dataclass(frozen=True)
class ExperimentConfig:
    """一次 A/B 实验：固定模型/prompt/温度/任务集/预算/环境，只变一项策略。"""

    name: str
    dataset_version: str
    repetitions: int = 3
    variants: tuple[VariantConfig, ...] = ()

    def __post_init__(self) -> None:
        if self.repetitions < 2:
            raise ValueError(f"实验 {self.name} 每任务至少重复 2 次，实际 {self.repetitions}")
        if len(self.variants) < 2:
            raise ValueError(f"实验 {self.name} 至少需要 2 个变体")


@dataclass(frozen=True)
class VariantStats:
    """一个变体在全部 case 上的统计：禁止选择性汇报，全部 case 都计入。"""

    variant_name: str
    mean_ms: float
    median_ms: float
    p95_ms: float
    passed: int
    total: int
    failure_categories: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ComparisonReport:
    """A/B 对比报告：列出全部变体与全部 case。"""

    experiment_name: str
    dataset_version: str
    repetitions: int
    case_ids: tuple[str, ...] = ()
    variants: tuple[VariantStats, ...] = ()

    def iter_cases(self) -> Iterator[str]:
        """全部 case ID；报告不得只挑通过的 case。"""
        yield from self.case_ids
