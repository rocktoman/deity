"""Anthropic messages stream adapter."""

from collections.abc import AsyncIterator
from typing import Any, cast

from anthropic import APIConnectionError, APIStatusError, APITimeoutError, AsyncAnthropic

from deity.core.contracts import ModelCapabilities, ModelEvent, ModelRequest
from deity.core.runtime import ModelFailure


class AnthropicProvider:
    """Normalize Anthropic streaming messages."""

    def __init__(self, *, api_key: str, model: str) -> None:
        self.model = model
        self.client = AsyncAnthropic(api_key=api_key, max_retries=0)

    def capabilities(self) -> ModelCapabilities:
        """Declare the supported baseline features."""
        return ModelCapabilities(True, True, False)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Request a stream and normalize raw events."""
        messages, system = self._messages(request.messages)
        kwargs: dict[str, Any] = {
            "model": request.model or self.model,
            "max_tokens": request.max_output_tokens or 4096,
            "messages": messages,
            "stream": True,
        }
        if system:
            kwargs["system"] = system
        if request.tools:
            kwargs["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool["parameters"],
                }
                for tool in request.tools
            ]
        try:
            raw = await self.client.messages.create(**kwargs)
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
    def _messages(messages: tuple[dict[str, Any], ...]) -> tuple[list[dict[str, Any]], str]:
        result: list[dict[str, Any]] = []
        system: list[str] = []
        for message in messages:
            role = message["role"]
            if role == "system":
                system.append(str(message["content"]))
            elif role == "tool":
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message["call_id"],
                                "content": message["content"],
                            }
                        ],
                    }
                )
            elif role == "assistant" and message.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if message.get("content"):
                    blocks.append({"type": "text", "text": message["content"]})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call["call_id"],
                        "name": call["name"],
                        "input": call["arguments"],
                    }
                    for call in message["tool_calls"]
                )
                result.append({"role": "assistant", "content": blocks})
            else:
                result.append({"role": role, "content": message["content"]})
        return result, "\n".join(system)

    async def parse(self, raw: AsyncIterator[Any]) -> AsyncIterator[ModelEvent]:
        """Map raw content blocks and preserve tool input fragments."""
        calls: dict[int, str] = {}
        last_index: int | None = None
        finish_reason: str | None = None
        async for event in raw:
            kind = event.type
            if kind == "content_block_start" and event.content_block.type == "tool_use":
                index = getattr(event, "index", len(calls))
                calls[index] = event.content_block.id
                last_index = index
                yield ModelEvent(
                    "tool_call_started",
                    call_id=event.content_block.id,
                    name=event.content_block.name,
                )
            elif kind == "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta":
                    yield ModelEvent("text_delta", text=delta.text)
                elif delta.type == "input_json_delta":
                    index = getattr(event, "index", last_index)
                    if index not in calls:
                        yield ModelEvent("error", text="unknown_tool_call")
                        return
                    yield ModelEvent(
                        "tool_call_arguments_delta", call_id=calls[index], text=delta.partial_json
                    )
            elif kind == "message_start":
                usage = getattr(event.message, "usage", None)
                if usage:
                    yield ModelEvent("usage", input_tokens=getattr(usage, "input_tokens", None))
            elif kind == "message_delta":
                finish_reason = getattr(event.delta, "stop_reason", None)
                usage = getattr(event, "usage", None)
                if usage:
                    yield ModelEvent("usage", output_tokens=getattr(usage, "output_tokens", None))
            elif kind == "message_stop":
                yield ModelEvent("message_completed", finish_reason=finish_reason)
