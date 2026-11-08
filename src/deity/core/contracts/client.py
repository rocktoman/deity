"""统一客户端协议：CLI 与 TUI 共用的事件、审批与错误契约。

Core 层只定义数据与端口，不依赖 CLI、SQLite 或任何厂商 SDK。
"""

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

__all__ = (
    "ApprovalPort",
    "ApprovalRequest",
    "ClientError",
    "ClientErrorCode",
    "ClientEvent",
    "EventSubscription",
    "RunHandle",
    "approval_args_summary",
    "approval_id_for",
)

ClientErrorCode = Literal[
    "run_not_found",
    "approval_not_found",
    "already_decided",
    "subscription_closed",
    "slow_consumer",
]

ApprovalDecision = Literal["approved", "denied"]
ApprovalStatus = Literal["pending", "approved", "denied"]


def approval_args_summary(args: dict[str, Any], *, max_chars: int = 200) -> dict[str, Any]:
    """把工具参数压缩为审批摘要：不含文件内容全文，超长值截断。"""
    summary: dict[str, Any] = {}
    for key, value in args.items():
        if key in {"content", "file_content", "text", "old_text", "new_text"}:
            continue
        if isinstance(value, str) and len(value) > max_chars:
            summary[key] = value[: max_chars - 12] + "…(truncated)"
        elif isinstance(value, (list, tuple)) and len(value) > 20:
            summary[key] = list(value[:20]) + ["…(truncated)"]
        else:
            summary[key] = value
    return summary


def approval_id_for(tool_name: str, args: dict[str, Any]) -> str:
    """由工具名与参数摘要派生确定性审批 ID；同一调用重复请求得到同一 ID。"""
    encoded = json.dumps(
        {"tool": tool_name, "args": approval_args_summary(args)},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    ).encode()
    return "appr-" + hashlib.sha256(encoded).hexdigest()[:16]


@dataclass(frozen=True)
class ClientEvent:
    """一条可重放的客户端事件；seq 在同一 Run 内单调递增。"""

    seq: int
    run_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为可存储/可传输的字典。"""
        return {
            "seq": self.seq,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "kind": self.kind,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RunHandle:
    """一次 Run 的轻量句柄；不持有执行所有权。"""

    run_id: str
    session_id: str
    status: str


@dataclass
class ApprovalRequest:
    """一次待决审批；同一 approval_id 的重复决定只生效一次。"""

    approval_id: str
    run_id: str
    call_id: str
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    status: ApprovalStatus = "pending"
    decided_at: str | None = None

    def decide(self, decision: ApprovalDecision) -> "ApprovalRequest":
        """首次决定生效并记录时间；重复决定由调用方按首次结果处理。"""
        self.status = decision
        self.decided_at = datetime.now(UTC).isoformat()
        return self


class ClientError(Exception):
    """客户端协议错误，带稳定错误码。"""

    def __init__(self, code: ClientErrorCode, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class EventSubscription(Protocol):
    """事件订阅：异步迭代器，用完或断开时 close()。"""

    @property
    def closed(self) -> bool: ...

    def __aiter__(self) -> AsyncIterator[ClientEvent]: ...

    async def close(self) -> None: ...


class ApprovalPort(Protocol):
    """可等待的审批端口；RunService 经它向 CLI/TUI 请求审批。"""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalRequest: ...
