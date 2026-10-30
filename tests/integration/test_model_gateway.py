from collections.abc import AsyncIterator
from types import SimpleNamespace as NS

import pytest

from deity.adapters.model.anthropic import AnthropicProvider
from deity.adapters.model.openai_compatible import OpenAICompatibleProvider
from deity.core.contracts import (
    AgentRequest,
    ModelEvent,
    ModelRequest,
    RunStatus,
    ToolCall,
    ToolResult,
)
from deity.core.runtime import AgentLoop


async def raw(*events):
    for event in events:
        yield event


class Tool:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        self.calls.append(call)
        return ToolResult(call.call_id, "ok", "read")


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
async def test_adapter_drives_core_with_local_stream(protocol: str) -> None:
    provider = (
        OpenAICompatibleProvider(api_key="test", model="test")
        if protocol == "openai"
        else AnthropicProvider(api_key="test", model="test")
    )
    turns = 0

    async def stream(request: ModelRequest) -> AsyncIterator[ModelEvent]:
        nonlocal turns
        turns += 1
        if protocol == "openai":
            if turns == 1:
                chunks = raw(
                    NS(
                        choices=[
                            NS(
                                delta=NS(
                                    content=None,
                                    tool_calls=[
                                        NS(
                                            index=0,
                                            id="c1",
                                            function=NS(name="read_file", arguments="{}"),
                                        )
                                    ],
                                ),
                                finish_reason="tool_calls",
                            )
                        ],
                        usage=None,
                    )
                )
            else:
                chunks = raw(
                    NS(
                        choices=[
                            NS(delta=NS(content="done", tool_calls=None), finish_reason="stop")
                        ],
                        usage=None,
                    )
                )
        elif turns == 1:
            chunks = raw(
                NS(
                    type="content_block_start",
                    content_block=NS(type="tool_use", id="c1", name="read_file"),
                ),
                NS(
                    type="content_block_delta", delta=NS(type="input_json_delta", partial_json="{}")
                ),
                NS(type="message_stop"),
            )
        else:
            chunks = raw(
                NS(type="content_block_delta", delta=NS(type="text_delta", text="done")),
                NS(type="message_stop"),
            )
        async for event in provider.parse(chunks):
            yield event

    provider.stream = stream  # type: ignore[method-assign]
    tool = Tool()
    result = await AgentLoop(provider, tool).run(AgentRequest("s", "t", "/fixture", "read", "test"))
    assert result.status == RunStatus.COMPLETED
    assert len(tool.calls) == 1
    assert tool.calls[0].call_id == "c1"
