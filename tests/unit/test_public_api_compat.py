"""V1.0 S1：公开 API 与事件协议兼容快照。

冻结清单见 docs/02-architecture/adr-015-v1.0-compat.md。
任何签名/字段变化都必须先改 ADR，否则本测试失败是预期行为。
"""

import inspect
from pathlib import Path

import pytest

import deity
from deity.application.client_service import ClientService
from deity.core.contracts import (
    COMPAT_VERSION,
    ClientEvent,
    EventPublisher,
    ModelProvider,
    SessionRepository,
    ToolDispatcher,
)
from deity.core import contracts

# 公开 API 快照：__all__ 即公开 API 清单，增删改先改 ADR。
EXPECTED_PUBLIC_API = (
    "AgentRequest",
    "AgentResult",
    "ApprovalPort",
    "ApprovalRequest",
    "ChangeSet",
    "ChangeSetRepository",
    "ClientError",
    "ClientEvent",
    "CodeSearchPort",
    "CodeSearchResult",
    "COMPAT_VERSION",
    "ComparisonReport",
    "DatasetVersion",
    "EvalCase",
    "EvalEnvironment",
    "EvalRunner",
    "EventPublisher",
    "EventSubscription",
    "EvidenceBundle",
    "ExecutionMode",
    "ExperimentConfig",
    "FileChange",
    "FinalReport",
    "FixtureManifest",
    "GitCheckpoint",
    "IndexCache",
    "MetricRecord",
    "ModelCapabilities",
    "ModelEvent",
    "ModelProvider",
    "ModelRequest",
    "PatchApplyResult",
    "PatchApplyStatus",
    "PlanApproval",
    "PlanArtifact",
    "PlanDraft",
    "PlanRepository",
    "PlanStep",
    "RecoveryDecision",
    "ReferenceQuery",
    "RunBudget",
    "RunHandle",
    "RunLease",
    "RunStatus",
    "Scorer",
    "SessionRepository",
    "SymbolLocation",
    "ToolCall",
    "ToolDispatcher",
    "ToolResult",
    "VariantConfig",
    "VariantStats",
    "VerificationCommand",
    "VerificationEvidence",
    "VerificationPlan",
    "VerificationRepository",
    "VerificationStatus",
    "Verifier",
    "WorkspaceFileState",
    "WorkspaceSnapshot",
    "WorkspaceSnapshotPort",
    "approval_args_summary",
    "approval_id_for",
)


def test_version_is_1_0_0() -> None:
    assert deity.__version__ == "1.0.0"
    assert COMPAT_VERSION == 1


def test_public_api_snapshot() -> None:
    assert tuple(sorted(contracts.__all__)) == tuple(sorted(EXPECTED_PUBLIC_API))
    for name in contracts.__all__:
        assert hasattr(contracts, name), f"__all__ 含未导出名称：{name}"


def test_protocol_method_signatures() -> None:
    assert list(inspect.signature(ModelProvider.capabilities).parameters) == ["self"]
    assert list(inspect.signature(ModelProvider.stream).parameters) == ["self", "request"]
    assert list(inspect.signature(ToolDispatcher.dispatch).parameters) == [
        "self",
        "call",
        "context",
    ]
    assert list(inspect.signature(EventPublisher.publish).parameters) == [
        "self",
        "run_id",
        "kind",
        "payload",
    ]
    session_params = inspect.signature(SessionRepository.create_run).parameters
    assert list(session_params) == ["self", "request", "run_id"]
    assert list(inspect.signature(SessionRepository.claim_tool).parameters) == [
        "self",
        "run_id",
        "call_id",
        "name",
        "args",
        "epoch",
        "approval",
    ]
    assert list(inspect.signature(SessionRepository.mark_tool_unknown).parameters) == [
        "self",
        "run_id",
        "call_id",
        "reason",
        "epoch",
    ]


def test_client_service_protocol_methods() -> None:
    for name in (
        "start_run",
        "subscribe_events",
        "approve",
        "cancel",
        "resume",
        "wait",
        "collect",
        "pending_approvals",
    ):
        assert callable(getattr(ClientService, name, None)), f"ClientService 缺方法：{name}"


def test_client_event_fields_frozen() -> None:
    fields = {field.name for field in ClientEvent.__dataclass_fields__.values()}
    assert fields == {"seq", "run_id", "kind", "payload", "schema_version", "created_at"}
    event = ClientEvent(seq=1, run_id="r1", kind="RunStatusChanged")
    assert event.schema_version == 1
    assert event.to_dict() == {
        "seq": 1,
        "run_id": "r1",
        "schema_version": 1,
        "kind": "RunStatusChanged",
        "payload": {},
        "created_at": "",
    }


def test_old_schema_v1_event_reads_compatibly() -> None:
    """手工构造的旧 schema_version=1 事件只读兼容：缺失字段按默认值补全。"""
    raw = {"seq": 7, "run_id": "r-old", "schema_version": 1, "kind": "ToolCompleted"}
    event = ClientEvent(**raw)  # type: ignore[arg-type]
    assert event.schema_version == 1
    assert event.payload == {}
    assert event.created_at == ""
    # 字段只增不减：未知字段不得静默吞掉后改变已有字段含义。
    with pytest.raises(TypeError):
        ClientEvent(seq=1, run_id="r", kind="X", removed_field="nope")  # type: ignore[call-arg]


def test_no_versioned_file_still_says_0_1_0() -> None:
    root = Path(__file__).parents[2]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "1.0.0"' in pyproject
    assert 'version = "0.1.0"' not in pyproject
