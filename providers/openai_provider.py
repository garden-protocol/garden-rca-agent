"""
OpenAI provider — wraps the OpenAI SDK behind the LLMProvider interface.
Converts tool definitions from Anthropic format (input_schema) to OpenAI format (parameters).
"""
import json
from openai import OpenAI
from .base import LLMProvider, LLMResponse, ToolCall, TokenUsage


class OpenAIProvider(LLMProvider):

    def __init__(self, api_key: str):
        self._client = OpenAI(api_key=api_key)

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        """Convert Anthropic-format tool definitions to OpenAI function-calling format."""
        converted = []
        for tool in tools:
            converted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", tool.get("parameters", {})),
                },
            })
        return converted

    def _system_to_string(self, system: str | list[dict]) -> str:
        """Flatten Anthropic system blocks (with cache_control) into a plain string."""
        if isinstance(system, str):
            return system
        return "\n\n".join(block.get("text", "") for block in system)

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
        # kwargs like thinking, output_config are silently ignored for OpenAI

        # Convert messages: Anthropic tool_result format → OpenAI tool role format
        oai_messages = self._convert_messages(system, messages)

        call_kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": oai_messages,
        }
        if tools:
            call_kwargs["tools"] = self._convert_tools(tools)

        resp = self._client.chat.completions.create(**call_kwargs)

        choice = resp.choices[0]
        msg = choice.message

        text = msg.content
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    input=json.loads(tc.function.arguments),
                ))

        stop = "tool_use" if tool_calls else "end_turn"

        u = resp.usage
        usage = TokenUsage(
            input_tokens=u.prompt_tokens if u else 0,
            output_tokens=u.completion_tokens if u else 0,
        )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=usage,
            raw_content=msg,
        )

    def _convert_messages(
        self, system: str | list[dict], messages: list[dict]
    ) -> list[dict]:
        """
        Convert Anthropic-style messages to OpenAI format.

        Key differences:
        - System prompt is a message with role="system" (not a separate param)
        - Tool results use role="tool" with tool_call_id (not user messages with type=tool_result)
        - Assistant messages with tool_use blocks become assistant messages with tool_calls
        """
        oai = [{"role": "system", "content": self._system_to_string(system)}]

        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if role == "user":
                # Could be a plain string, or a list of blocks (tool_result or text)
                if isinstance(content, str):
                    oai.append({"role": "user", "content": content})
                elif isinstance(content, list):
                    # Separate tool_result blocks from text blocks
                    tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                    text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]

                    for tr in tool_results:
                        oai.append({
                            "role": "tool",
                            "tool_call_id": tr["tool_use_id"],
                            "content": tr.get("content", ""),
                        })
                    for tb in text_blocks:
                        oai.append({"role": "user", "content": tb.get("text", "")})
                else:
                    oai.append({"role": "user", "content": str(content)})

            elif role == "assistant":
                # content is the raw Anthropic response.content (list of blocks)
                # We need to convert to OpenAI format
                if hasattr(content, "__iter__") and not isinstance(content, str):
                    text_parts = []
                    tool_calls = []
                    for block in content:
                        if hasattr(block, "type"):
                            # Anthropic SDK objects
                            if block.type == "text":
                                text_parts.append(block.text)
                            elif block.type == "tool_use":
                                tool_calls.append({
                                    "id": block.id,
                                    "type": "function",
                                    "function": {
                                        "name": block.name,
                                        "arguments": json.dumps(block.input),
                                    },
                                })
                            elif block.type == "thinking":
                                pass  # skip thinking blocks
                        elif isinstance(block, dict):
                            if block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                            elif block.get("type") == "tool_use":
                                tool_calls.append({
                                    "id": block["id"],
                                    "type": "function",
                                    "function": {
                                        "name": block["name"],
                                        "arguments": json.dumps(block.get("input", {})),
                                    },
                                })

                    entry: dict = {"role": "assistant"}
                    entry["content"] = "\n".join(text_parts) if text_parts else ""
                    if tool_calls:
                        entry["tool_calls"] = tool_calls
                    oai.append(entry)
                else:
                    oai.append({"role": "assistant", "content": str(content) if content else ""})

        return oai

    def build_assistant_message(self, response: LLMResponse) -> dict:
        """Build OpenAI-style assistant message from normalized response."""
        entry: dict = {"role": "assistant", "content": response.text or ""}
        if response.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.input),
                    },
                }
                for tc in response.tool_calls
            ]
        return entry

    def build_tool_results_message(
        self, tool_results: list[tuple[str, str]]
    ) -> dict | list[dict]:
        """
        Build OpenAI-style tool result messages.
        OpenAI expects one message per tool result (unlike Anthropic which batches them).
        Returns a list — the caller should extend messages rather than append.
        """
        return [
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": result,
            }
            for call_id, result in tool_results
        ]
