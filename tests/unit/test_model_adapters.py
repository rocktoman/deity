from types import SimpleNamespace as NS

import httpx2
import pytest
from anthropic import APIConnectionError as AnthropicConnectionError
from anthropic import APIStatusError as AnthropicStatusError
from anthropic import APITimeoutError as AnthropicTimeoutError
from openai import APIConnectionError as OpenAIConnectionError
from openai import APIStatusError as OpenAIStatusError
from openai import APITimeoutError as OpenAITimeoutError

from deity.adapters.model.anthropic import AnthropicProvider
from deity.adapters.model.openai_compatible import OpenAICompatibleProvider
from deity.core.contracts import ModelRequest
from deity.core.runtime import ModelFailure


async def chunks(*items):
    for item in items:
        yield item


def test_anthropic_provider_survives_bracketed_ipv6_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NO_PROXY 含方括号 IPv6（如 [::1]）时构造不得崩溃。

    回归：anthropic 2.3.0 自带的 get_environment_proxies 是旧版 httpx 逻辑复刻，
    [::1] 会落入通配分支拼出 all://*[::1]，httpx2 解析报 Invalid port: ':1]'。
    """
    monkeypatch.setenv(
        "NO_PROXY", "localhost,127.0.0.1,::1,[::1],198.19.0.1,[fd8b:4f84:7d32:99::1]"
    )
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1,[::1]")
    provider = AnthropicProvider(api_key="<redacted>", model="test")
    assert provider.model == "test"


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

    seen_kwargs = {}

    async def create(**kwargs):
        seen_kwargs.update(kwargs)
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
    if protocol == "openai":
        assert "max_tokens" not in seen_kwargs
    else:
        assert seen_kwargs["max_tokens"] == 4096


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


def _provider_raising(protocol: str, error: Exception):
    """构造一个 create() 恒抛指定异常的 provider（密钥用可辨识假值）。"""

    async def create(**kwargs):
        raise error

    provider = (
        OpenAICompatibleProvider(api_key="sk-test-secret", model="test")
        if protocol == "openai"
        else AnthropicProvider(api_key="sk-test-secret", model="test")
    )
    if protocol == "openai":
        provider.client = NS(chat=NS(completions=NS(create=create)))
    else:
        provider.client = NS(messages=NS(create=create))
    return provider


async def _capture_failure(provider) -> ModelFailure:
    with pytest.raises(ModelFailure) as captured:
        [
            event
            async for event in provider.stream(
                ModelRequest("test", ({"role": "user", "content": "hi"},))
            )
        ]
    return captured.value


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
async def test_sdk_status_error_detail_has_status_and_server_message(protocol: str) -> None:
    error_type = OpenAIStatusError if protocol == "openai" else AnthropicStatusError
    response = httpx2.Response(404, request=httpx2.Request("POST", "https://example.test"))
    server_message = "The model `deepseek-v4-flash` does not exist"
    failure = await _capture_failure(
        _provider_raising(
            protocol,
            error_type(
                server_message,
                response=response,
                body={"error": {"message": server_message}},
            ),
        )
    )
    assert failure.code == "model_http"
    assert str(failure) == "model_http"
    assert failure.detail == f"HTTP 404: {server_message}"
    assert "sk-test-secret" not in failure.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
async def test_sdk_status_error_detail_is_sanitized(protocol: str) -> None:
    error_type = OpenAIStatusError if protocol == "openai" else AnthropicStatusError
    response = httpx2.Response(400, request=httpx2.Request("POST", "https://example.test"))
    failure = await _capture_failure(
        _provider_raising(
            protocol,
            error_type("bad\nrequest\r\n" + "x" * 500, response=response, body=None),
        )
    )
    assert failure.code == "model_http"
    assert failure.detail is not None
    assert "\n" not in failure.detail
    assert "\r" not in failure.detail
    assert failure.detail.startswith("HTTP 400: bad request ")
    assert len(failure.detail) <= len("HTTP 400: ") + 200
    assert "sk-test-secret" not in failure.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("kind", ["connection", "timeout"])
async def test_sdk_connection_error_detail_is_type_name(protocol: str, kind: str) -> None:
    request = httpx2.Request("POST", "https://example.test")
    if protocol == "openai":
        error = (
            OpenAIConnectionError(request=request)
            if kind == "connection"
            else OpenAITimeoutError(request=request)
        )
        expected = "APIConnectionError" if kind == "connection" else "APITimeoutError"
    else:
        error = (
            AnthropicConnectionError(request=request)
            if kind == "connection"
            else AnthropicTimeoutError(request=request)
        )
        expected = "APIConnectionError" if kind == "connection" else "APITimeoutError"
    failure = await _capture_failure(_provider_raising(protocol, error))
    assert failure.code == "model_connection"
    assert failure.detail == expected
    assert "sk-test-secret" not in failure.detail
