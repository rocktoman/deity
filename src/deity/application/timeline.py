"""ADR-021：证据时间线——把已落库的运行事实按序组织为只读视图。

来源全部是存储层既有事实：events（含 turn_diff 的文件级明细）、
evidence、delegations、verification report。本模块不写库、不重放
执行；未知事件类型按原名透传，旧读者可忽略。
"""

from dataclasses import dataclass
from typing import Any, Protocol


class _TimelineRepo(Protocol):
    async def run_info(self, run_id: str) -> dict[str, str]: ...

    async def events(self, run_id: str) -> list[dict[str, Any]]: ...

    async def evidence(self, run_id: str) -> list[dict[str, str]]: ...

    async def delegation_tree(self, root_run_id: str) -> list[dict[str, Any]]: ...

    async def verification_report(self, run_id: str) -> Any: ...


@dataclass(frozen=True)
class TimelineEntry:
    """时间线一条：序号、类别、标题与一行明细。"""

    seq: int
    kind: str
    title: str
    detail: str = ""


_EVENT_TITLES = {
    "RunCreated": "运行创建",
    "RunStatusChanged": "状态变更",
    "ModelTurnCompleted": "模型轮次",
    "ToolClaimed": "工具调用",
    "ToolCompleted": "工具完成",
    "ToolFailed": "工具失败",
    "DelegationPatchHarvested": "委派补丁回收",
    "VerificationRecorded": "证据记录",
    "RunForkedFrom": "分叉自父运行",
    "turn_diff": "文件变更",
}


def _event_detail(kind: str, payload: dict[str, Any]) -> str:
    if kind == "turn_diff":
        files = payload.get("files", [])
        names = ", ".join(
            f"{item.get('path', '?')}({item.get('status', '?')})"
            for item in files
            if isinstance(item, dict)
        )
        return names[:300]
    if kind == "DelegationPatchHarvested":
        files = ", ".join(str(item) for item in payload.get("files", []))
        return f"{files} · {payload.get('chars', 0)} 字符"[:300]
    if kind == "RunStatusChanged":
        return str(payload.get("status", payload.get("to", "")))[:120]
    if kind in ("ToolClaimed", "ToolCompleted", "ToolFailed"):
        return str(payload.get("name", payload.get("tool", "")))[:120]
    return ""


async def run_timeline(repo: _TimelineRepo, run_id: str) -> list[TimelineEntry]:
    """聚合一个 Run 的时间线；未知 run 抛 KeyError（来自 run_info）。"""
    await repo.run_info(run_id)
    entries: list[TimelineEntry] = []
    seq = 0
    for event in await repo.events(run_id):
        seq += 1
        kind = str(event.get("type", ""))
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        entries.append(
            TimelineEntry(
                seq,
                kind,
                _EVENT_TITLES.get(kind, kind),
                _event_detail(kind, payload),
            )
        )
    for item in await repo.evidence(run_id):
        seq += 1
        entries.append(
            TimelineEntry(
                seq,
                "evidence",
                f"证据·{item.get('kind', '')}",
                item.get("summary", "")[:300],
            )
        )
    for child in await repo.delegation_tree(run_id):
        seq += 1
        entries.append(
            TimelineEntry(
                seq,
                "delegation",
                "委派子运行",
                f"{child.get('child_run_id', '')[:8]} · {child.get('status', '')}",
            )
        )
    try:
        report = await repo.verification_report(run_id)
    except KeyError:
        report = None
    if report is not None:
        seq += 1
        status = getattr(report, "status", "")
        entries.append(
            TimelineEntry(
                seq, "verification", "验证报告", str(getattr(status, "value", status))[:120]
            )
        )
    return entries


def format_timeline(entries: list[TimelineEntry]) -> str:
    """渲染为纯文本行（CLI 与聊天共用）。"""
    lines = []
    for entry in entries:
        suffix = f" — {entry.detail}" if entry.detail else ""
        lines.append(f"{entry.seq:>3}. [{entry.kind}] {entry.title}{suffix}")
    return "\n".join(lines)
