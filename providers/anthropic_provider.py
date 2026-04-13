"""
Anthropic provider — wraps the Anthropic SDK behind the LLMProvider interface.
Supports prompt caching (cache_control) and extended thinking (thinking/output_config).
"""
import anthropic
from .base import LLMProvider, LLMResponse, ToolCall, TokenUsage


class AnthropicProvider(LLMProvider):

    def __init__(self, api_key: str):
        self._client = anthropic.Anthropic(api_key=api_key)

    def create_message(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str | list[dict],
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        call_kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            call_kwargs["tools"] = tools

        # Pass through Anthropic-specific params (thinking, output_config, etc.)
        for key in ("thinking", "output_config"):
            if key in kwargs:
                call_kwargs[key] = kwargs[key]

        resp = self._client.messages.create(**call_kwargs)

        # Parse response blocks into normalized form
        text = None
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                text = block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))

        stop = "tool_use" if tool_calls and resp.stop_reason != "end_turn" else "end_turn"

        u = resp.usage
        usage = TokenUsage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=usage,
            raw_content=resp.content,
        )

    def build_assistant_message(self, response: LLMResponse) -> dict:
        return {"role": "assistant", "content": response.raw_content}

    def build_tool_results_message(
        self, tool_results: list[tuple[str, str]]
    ) -> dict:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": result,
                }
                for call_id, result in tool_results
            ],
        }
