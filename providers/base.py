"""
Normalized interface for LLM providers.
All agents interact through this abstraction — never import a provider SDK directly.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """A normalized tool call extracted from an LLM response."""
    id: str
    name: str
    input: dict


@dataclass
class TokenUsage:
    """Token usage — superset of all providers. Unsupported fields stay 0."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class LLMResponse:
    """Normalized LLM response returned by every provider."""
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"     # "end_turn" or "tool_use"
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw_content: object = None        # provider-native content for message history


class LLMProvider(ABC):
    """Abstract interface that every provider must implement."""

    @abstractmethod
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
        """Send a chat completion request and return a normalized response."""
        ...

    @abstractmethod
    def build_assistant_message(self, response: LLMResponse) -> dict:
        """Build a messages-list entry for the assistant turn."""
        ...

    @abstractmethod
    def build_tool_results_message(
        self, tool_results: list[tuple[str, str]]
    ) -> dict:
        """
        Build a messages-list entry for tool results.
        Each tuple is (tool_call_id, result_content).
        """
        ...
