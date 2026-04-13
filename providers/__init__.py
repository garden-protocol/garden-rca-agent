"""
Provider factory — returns the configured LLM provider instance.
"""
from .base import LLMProvider, LLMResponse, ToolCall, TokenUsage

_provider_instance: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """Return a singleton LLM provider based on config.settings."""
    global _provider_instance
    if _provider_instance is not None:
        return _provider_instance

    from config import settings

    name = settings.llm_provider.lower()

    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider
        _provider_instance = AnthropicProvider(api_key=settings.anthropic_api_key)
    elif name == "openai":
        from .openai_provider import OpenAIProvider
        _provider_instance = OpenAIProvider(api_key=settings.openai_api_key)
    else:
        raise ValueError(
            f"Unknown LLM provider '{name}'. Supported: anthropic, openai"
        )

    return _provider_instance


__all__ = [
    "get_provider",
    "LLMProvider",
    "LLMResponse",
    "ToolCall",
    "TokenUsage",
]
