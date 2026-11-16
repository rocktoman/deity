"""Run 的 JSONL 导出（ADR-020 A7）：SQLite 主存储不变，JSONL 只作导出格式。

结构参照 openai/codex rollout 的「SessionMeta 首行 + 逐条条目」
（Apache-2.0，`codex-rs/rollout/`），字段为 Deity 自有定义，声明
见 `docs/06-legal/third-party-notices.md`。首行 session_meta 给出
run/session/工作区/模型/状态，其后每条已持久化事件一行，末行
summary 汇总计数。导出全程只读，不执行任何工具；事件 payload
沿用存储层已脱敏的内容，本模块不再接触原始参数。
"""

import json
from typing import Any, Protocol


class _ExportRepository(Protocol):
    async def run_info(self, run_id: str) -> dict[str, Any]: ...
    async def events(self, run_id: str) -> list[dict[str, Any]]: ...
    async def evidence(self, run_id: str) -> list[dict[str, str]]: ...
    async def delegation_tree(self, root_run_id: str) -> list[dict[str, Any]]: ...


async def export_run_jsonl(repo: _ExportRepository, run_id: str) -> str:
    """把一个 run 的已提交事实导出为 JSONL 文本（未知 run 抛 KeyError）。"""
    info = await repo.run_info(run_id)
    events = await repo.events(run_id)
    evidence = await repo.evidence(run_id)
    delegations = await repo.delegation_tree(run_id)
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "run_id": run_id,
                "session_id": info["session_id"],
                "task_id": info["task_id"],
                "workspace": info["workspace_root"],
                "status": info["status"],
                "model_provider": info["model_provider"],
                "model_name": info["model_name"],
                "prompt": info["prompt"],
            },
            ensure_ascii=False,
        )
    ]
    for event in events:
        lines.append(
            json.dumps(
                {
                    "type": "event",
                    "seq": event["sequence"],
                    "event_type": event["type"],
                    "payload": event["payload"],
                },
                ensure_ascii=False,
            )
        )
    lines.append(
        json.dumps(
            {
                "type": "summary",
                "run_id": run_id,
                "events": len(events),
                "evidence": len(evidence),
                "delegations": len(delegations),
            },
            ensure_ascii=False,
        )
    )
    return "\n".join(lines) + "\n"


__all__ = ["export_run_jsonl"]
