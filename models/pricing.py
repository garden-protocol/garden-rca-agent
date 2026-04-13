"""
LLM model pricing — cost per million tokens (USD).
Covers Anthropic and OpenAI models used by the RCA pipeline.
"""

# Cost per 1 million tokens, in USD
_PRICING: dict[str, dict[str, float]] = {
    # ── Anthropic ──
    "claude-haiku-4-5-20251001": {
        "input":       0.80,
        "output":      4.00,
        "cache_read":  0.08,
        "cache_write": 1.00,
    },
    "claude-sonnet-4-6": {
        "input":       3.00,
        "output":      15.00,
        "cache_read":  0.30,
        "cache_write": 3.75,
    },
    "claude-opus-4-6": {
        "input":       15.00,
        "output":      75.00,
        "cache_read":   1.50,
        "cache_write": 18.75,
    },
    # ── OpenAI ──
    "gpt-4o": {
        "input":       2.50,
        "output":      10.00,
        "cache_read":  0.0,
        "cache_write": 0.0,
    },
    "gpt-4o-mini": {
        "input":       0.15,
        "output":      0.60,
        "cache_read":  0.0,
        "cache_write": 0.0,
    },
    "o4-mini": {
        "input":       1.10,
        "output":      4.40,
        "cache_read":  0.0,
        "cache_write": 0.0,
    },
}

_PER_TOKEN = 1_000_000


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Return the USD cost for the given token counts and model."""
    p = _PRICING.get(model)
    if p is None:
        return 0.0
    return (
        input_tokens       * p["input"]       / _PER_TOKEN
        + output_tokens    * p["output"]      / _PER_TOKEN
        + cache_read_tokens  * p["cache_read"]  / _PER_TOKEN
        + cache_write_tokens * p["cache_write"] / _PER_TOKEN
    )
