from types import SimpleNamespace as NS

import httpx2
import pytest
from anthropic import APIStatusError as AnthropicStatusError
from openai import APIStatusError as OpenAIStatusError

from deity.adapters.model.anthropic import AnthropicProvider
from deity.adapters.model.openai_compatible import OpenAICompatibleProvider
from deity.core.contracts import ModelRequest
from deity.core.runtime import ModelFailure


async def chunks(*items):
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_openai_fragmented_tool_call() -> None:
    provider = OpenAICompatibleProvider(api_key="test", model="test-model")
    first = NS(
        choices=[
            NS(
                delta=NS(
                    content=None,
                    tool_calls=[
                        NS(index=0, id="c1", function=NS(name="read_file", arguments='{"path":'))
                    ],
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )
    second = NS(
        choices=[
            NS(
                delta=NS(
                    content=None,
                    tool_calls=[NS(index=0, id=None, function=NS(name=None, arguments='"a.py"}'))],
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )
    end = NS(
        choices=[NS(delta=NS(content=None, tool_calls=None), finish_reason="tool_calls")],
        usage=None,
    )
    events = [event async for event in provider.parse(chunks(first, second, end))]
    assert [(event.kind, event.call_id) for event in events if event.call_id] == [
        ("tool_call_started", "c1"),
        ("tool_call_arguments_delta", "c1"),
        ("tool_call_arguments_delta", "c1"),
    ]
    assert events[-1].kind == "message_completed"


@pytest.mark.asyncio
async def test_openai_usage_after_finish_is_before_completion() -> None:
    provider = OpenAICompatibleProvider(api_key="test", model="test-model")
    finish = NS(
        choices=[NS(delta=NS(content="ok", tool_calls=None), finish_reason="stop")], usage=None
    )
    usage = NS(choices=[], usage=NS(prompt_tokens=3, completion_tokens=2))
    events = [event async for event in provider.parse(chunks(finish, usage))]
    assert [event.kind for event in events] == ["text_delta", "usage", "message_completed"]


@pytest.mark.asyncio
async def test_anthropic_fragmented_tool_call() -> None:
    provider = AnthropicProvider(api_key="test", model="test-model")
    events = [
        event
        async for event in provider.parse(
            chunks(
                NS(
                    type="content_block_start",
                    content_block=NS(type="tool_use", id="c1", name="read_file"),
                ),
                NS(
                    type="content_block_delta",
                    delta=NS(type="input_json_delta", partial_json='{"path":"a.py"}'),
                ),
                NS(type="message_delta", delta=NS(stop_reason="tool_use"), usage=None),
                NS(type="message_stop"),
            )
        )
    ]
    assert [event.kind for event in events] == [
        "tool_call_started",
        "tool_call_arguments_delta",
        "message_completed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
async def test_sdk_stream_is_closed(protocol: str) -> None:
    class Raw:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self):
            if protocol == "openai":
                return chunks(
                    NS(
                        choices=[NS(delta=NS(content="ok", tool_calls=None), finish_reason="stop")],
                        usage=None,
                    )
                )
            return chunks(
                NS(type="content_block_delta", delta=NS(type="text_delta", text="ok")),
                NS(type="message_stop"),
            )

        async def close(self) -> None:
            self.closed = True

    raw_stream = Raw()

    async def create(**kwargs):
        return raw_stream

    provider = (
        OpenAICompatibleProvider(api_key="test", model="test")
        if protocol == "openai"
        else AnthropicProvider(api_key="test", model="test")
    )
    if protocol == "openai":
        provider.client = NS(chat=NS(completions=NS(create=create)))
    else:
        provider.client = NS(messages=NS(create=create))
    events = [
        event
        async for event in provider.stream(
            ModelRequest("test", ({"role": "user", "content": "hi"},))
        )
    ]
    assert events[-1].kind == "message_completed"
    assert raw_stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (429, True), (503, True)])
async def test_sdk_status_is_classified(protocol: str, status: int, retryable: bool) -> None:
    error_type = OpenAIStatusError if protocol == "openai" else AnthropicStatusError
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://example.test"))

    async def create(**kwargs):
        raise error_type("sample", response=response, body=None)

    provider = (
        OpenAICompatibleProvider(api_key="test", model="test")
        if protocol == "openai"
        else AnthropicProvider(api_key="test", model="test")
    )
    if protocol == "openai":
        provider.client = NS(chat=NS(completions=NS(create=create)))
    else:
        provider.client = NS(messages=NS(create=create))
    with pytest.raises(ModelFailure) as error:
        [
            event
            async for event in provider.stream(
                ModelRequest("test", ({"role": "user", "content": "hi"},))
            )
        ]
    assert error.value.retryable is retryable
    assert "sample" not in str(error.value)
