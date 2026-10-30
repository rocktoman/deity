"""OpenAI-compatible chat completion stream adapter."""

import json
from collections.abc import AsyncIterator
from typing import Any, cast

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from deity.core.contracts import ModelCapabilities, ModelEvent, ModelRequest
from deity.core.runtime import ModelFailure


class OpenAICompatibleProvider:
    """Normalize OpenAI-compatible streaming chat completions."""

    def __init__(self, *, api_key: str, model: str, base_url: str | None = None) -> None:
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)

    def capabilities(self) -> ModelCapabilities:
        """Declare the supported baseline features."""
        return ModelCapabilities(True, True, False)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Request a stream and normalize every received chunk."""
        messages = self._messages(request.messages)
        kwargs: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": messages,
            "stream": True,
            "parallel_tool_calls": False,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            kwargs["tools"] = [{"type": "function", "function": tool} for tool in request.tools]
        if request.max_output_tokens is not None:
            kwargs["max_tokens"] = request.max_output_tokens
        try:
            raw = await self.client.chat.completions.create(**kwargs)
            try:
                async for event in self.parse(cast(AsyncIterator[Any], raw)):
                    yield event
            finally:
                await raw.close()
        except APIStatusError as exc:
            status = exc.status_code
            code = (
                "model_auth"
                if status in (401, 403)
                else "model_rate_limited"
                if status == 429
                else "model_http"
            )
            header = exc.response.headers.get("retry-after")
            try:
                retry_after = max(0.0, float(header)) if header is not None else None
            except ValueError:
                retry_after = None
            raise ModelFailure(
                code,
                retryable=status == 429 or status in {500, 502, 503, 504},
                retry_after=retry_after,
            ) from None
        except (APIConnectionError, APITimeoutError):
            raise ModelFailure("model_connection", retryable=True) from None

    @staticmethod
    def _messages(messages: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            if message["role"] == "tool":
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": message["call_id"],
                        "content": message["content"],
                    }
                )
            elif message["role"] == "assistant" and message.get("tool_calls"):
                result.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or None,
                        "tool_calls": [
                            {
                                "id": call["call_id"],
                                "type": "function",
                                "function": {
                                    "name": call["name"],
                                    "arguments": json.dumps(call["arguments"]),
                                },
                            }
                            for call in message["tool_calls"]
                        ],
                    }
                )
            else:
                result.append({"role": message["role"], "content": message["content"]})
        return result

    async def parse(self, raw: AsyncIterator[Any]) -> AsyncIterator[ModelEvent]:
        """Map SDK chunks without executing tools or accepting partial arguments."""
        calls: dict[int, str] = {}
        finish_reason: str | None = None
        async for chunk in raw:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                yield ModelEvent(
                    "usage",
                    input_tokens=getattr(usage, "prompt_tokens", None),
                    output_tokens=getattr(usage, "completion_tokens", None),
                )
            for choice in getattr(chunk, "choices", []):
                delta = choice.delta
                if getattr(delta, "content", None):
                    yield ModelEvent("text_delta", text=delta.content)
                for call in getattr(delta, "tool_calls", None) or []:
                    if call.index not in calls:
                        if not call.id or not call.function.name:
                            yield ModelEvent("error", text="invalid_tool_call")
                            return
                        calls[call.index] = call.id
                        yield ModelEvent(
                            "tool_call_started", call_id=call.id, name=call.function.name
                        )
                    fragment = getattr(call.function, "arguments", None)
                    if fragment:
                        yield ModelEvent(
                            "tool_call_arguments_delta", call_id=calls[call.index], text=fragment
                        )
                if choice.finish_reason is not None:
                    finish_reason = choice.finish_reason
        if finish_reason is not None:
            yield ModelEvent("message_completed", finish_reason=finish_reason)
