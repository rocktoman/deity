"""ADR-019 A5：compact 结构化摘要——素材渲染、摘要嵌入、结转与回退。"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from deity.application.config import load_config
from deity.core.context.reliable import (
    SUMMARY_KIND,
    ConservativeEstimator,
    ContextBudget,
    ContextCompactor,
    ContextOverflow,
    build_summary_prompt,
)
from deity.core.contracts import (
    AgentRequest,
    ModelEvent,
    ModelRequest,
    RunStatus,
    ToolCall,
    ToolResult,
)
from deity.core.runtime import AgentLoop, ModelFailure

BUDGET = ContextBudget(3000, 300, 300)


def _history(turns: int = 8) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "完成既定任务"},
    ]
    for i in range(turns):
        messages.append(
            {
                "role": "assistant",
                "content": f"第{i}轮说明 " + "长" * 60,
                "tool_calls": [{"name": "read_file", "arguments": {"path": f"f{i}.txt"}}],
            }
        )
        messages.append(
            {
                "role": "tool",
                "call_id": f"c{i}",
                "status": "ok",
                "content": "内容" * 75,
            }
        )
    return messages


def _kinds(messages: Any) -> list[str]:
    kinds = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and content.startswith("{"):
            try:
                kinds.append(json.loads(content).get("kind", ""))
            except json.JSONDecodeError:
                pass
    return kinds


def test_summary_source_none_when_under_limit() -> None:
    compactor = ContextCompactor(ConservativeEstimator())
    small = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert compactor.summary_source(small, BUDGET) is None


def test_summary_source_renders_old_turns_only() -> None:
    compactor = ContextCompactor(ConservativeEstimator())
    source = compactor.summary_source(_history(), BUDGET)
    assert source is not None
    assert "第0轮说明" in source and "第3轮说明" in source
    assert "第7轮说明" not in source  # 最近 4 轮不折叠，不进素材
    assert "[调用 read_file]" in source


def test_compact_embeds_summary_after_ledger() -> None:
    compactor = ContextCompactor(ConservativeEstimator())
    snapshot = compactor.compact(_history(), BUDGET, summary="## 目标\n完成既定任务")
    kinds = _kinds(snapshot.messages)
    assert kinds == ["compaction_ledger_v1", SUMMARY_KIND]
    summary_message = next(
        message
        for message in snapshot.messages
        if isinstance(message.get("content"), str) and SUMMARY_KIND in message["content"]
    )
    payload = json.loads(summary_message["content"])
    assert payload["summary"] == "## 目标\n完成既定任务"


def test_compact_without_summary_has_ledger_only() -> None:
    compactor = ContextCompactor(ConservativeEstimator())
    snapshot = compactor.compact(_history(), BUDGET)
    assert _kinds(snapshot.messages) == ["compaction_ledger_v1"]


def test_summary_carried_forward_when_not_regenerated() -> None:
    compactor = ContextCompactor(ConservativeEstimator())
    first = compactor.compact(_history(), BUDGET, summary="上一代摘要")
    extended = list(first.messages) + _history(6)[2:]
    second = compactor.compact(extended, BUDGET)
    payload = next(
        json.loads(message["content"])
        for message in second.messages
        if isinstance(message.get("content"), str) and SUMMARY_KIND in message["content"]
    )
    assert payload["summary"] == "上一代摘要"


def test_summary_is_sanitized() -> None:
    compactor = ContextCompactor(ConservativeEstimator())
    snapshot = compactor.compact(_history(), BUDGET, summary="api_key=sk-123 记下来")
    payload = next(
        json.loads(message["content"])
        for message in snapshot.messages
        if isinstance(message.get("content"), str) and SUMMARY_KIND in message["content"]
    )
    assert "sk-123" not in payload["summary"]


def test_huge_summary_overflows_but_plain_compact_fits() -> None:
    compactor = ContextCompactor(ConservativeEstimator())
    with pytest.raises(ContextOverflow):
        compactor.compact(_history(), BUDGET, summary="摘" * 100_000)
    assert compactor.compact(_history(), BUDGET).estimated_tokens <= BUDGET.input_limit


def test_summary_prompt_has_sections() -> None:
    prompt = build_summary_prompt("素材")
    for section in ("## 目标", "## 进展", "## 关键决定", "## 未解决", "## 下一步"):
        assert section in prompt
    assert prompt.endswith("素材")


def test_config_compact_summary_flag(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[run]\ncompact_summary = true\n", encoding="utf-8")
    assert load_config(config_file).run.compact_summary is True
    config_file.write_text("[run]\n", encoding="utf-8")
    assert load_config(config_file).run.compact_summary is False


class _NoopTools:
    def definitions(self) -> tuple[dict[str, Any], ...]:
        return ({"name": "read_file", "description": "x", "parameters": {}},)

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        return ToolResult(call.call_id, "ok", "")


class _SummaryAwareModel:
    """摘要调用（提示含标记）返回摘要文本；普通轮次直接给最终答复。"""

    def __init__(self, *, fail_summary: bool = False) -> None:
        self.requests: list[ModelRequest] = []
        self.fail_summary = fail_summary

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        prompt = request.messages[0].get("content", "") if request.messages else ""
        if "上下文压缩摘要" in str(prompt):
            if self.fail_summary:
                raise ModelFailure("summary unavailable", retryable=False)
            yield ModelEvent("text_delta", text="## 目标\n完成既定任务\n## 进展\n已过半")
            yield ModelEvent("message_completed")
            return
        yield ModelEvent("text_delta", text="完成")
        yield ModelEvent("message_completed")


def _request(**overrides: Any) -> AgentRequest:
    values: dict[str, Any] = {
        "session_id": "s",
        "task_id": "t",
        "workspace": "/fixture",
        "user_text": "继续",
        "model": "scripted",
        "context_window_tokens": 3000,
        "compaction_enabled": True,
        "compact_summary": True,
    }
    values.update(overrides)
    return AgentRequest(**values)


@pytest.mark.asyncio
async def test_loop_compacts_with_summary_and_continues() -> None:
    model = _SummaryAwareModel()
    result = await AgentLoop(model, _NoopTools()).run(_request(), initial_messages=_history())
    assert result.status == RunStatus.COMPLETED
    assert len(model.requests) == 2  # 先摘要、后正式轮次
    kinds = _kinds(model.requests[1].messages)
    assert kinds == ["compaction_ledger_v1", SUMMARY_KIND]


@pytest.mark.asyncio
async def test_loop_falls_back_to_ledger_when_summary_fails() -> None:
    model = _SummaryAwareModel(fail_summary=True)
    result = await AgentLoop(model, _NoopTools()).run(_request(), initial_messages=_history())
    assert result.status == RunStatus.COMPLETED
    assert _kinds(model.requests[1].messages) == ["compaction_ledger_v1"]


@pytest.mark.asyncio
async def test_loop_without_flag_never_calls_summarizer() -> None:
    model = _SummaryAwareModel()
    result = await AgentLoop(model, _NoopTools()).run(
        _request(compact_summary=False), initial_messages=_history()
    )
    assert result.status == RunStatus.COMPLETED
    assert len(model.requests) == 1
    assert _kinds(model.requests[0].messages) == ["compaction_ledger_v1"]
