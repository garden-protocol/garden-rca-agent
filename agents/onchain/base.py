"""
Base class for on-chain query agents.
Each chain inherits from this and implements chain-specific RPC tools.
"""
import anthropic
from abc import ABC, abstractmethod


MODEL = "claude-haiku-4-5-20251001"

from config import settings as _settings
client = anthropic.Anthropic(api_key=_settings.anthropic_api_key)


class BaseOnChainAgent(ABC):
    """
    Abstract base for on-chain query agents.
    Subclasses define chain-specific tool definitions and execution logic.
    """

    @property
    @abstractmethod
    def chain(self) -> str:
        """Chain name: bitcoin, evm, solana"""
        ...

    @property
    def system_prompt(self) -> str:
        """Chain-specific system prompt. Override in subclasses for targeted guidance."""
        return (
            f"You are an on-chain query agent for the {self.chain} network. "
            f"You have access to RPC tools to query live blockchain state. "
            f"Be precise and factual. Report exactly what you find — do not speculate. "
            f"If a query fails, report the error honestly."
        )

    @property
    @abstractmethod
    def tool_definitions(self) -> list[dict]:
        """List of Claude tool definitions for this chain's RPC calls."""
        ...

    @abstractmethod
    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool call and return result as string."""
        ...

    def query(self, question: str, context: str = "") -> dict:
        """
        Ask a question about on-chain state.
        Claude will use the chain's RPC tools to find the answer.

        Args:
            question: What to check on-chain (e.g. "Is tx 0xabc in the mempool?")
            context: Optional context from the alert/logs to help Claude

        Returns:
            dict with 'findings' (str) and 'tool_calls' (list)
        """
        system = self.system_prompt

        user_content = question
        if context:
            user_content = f"Context from logs/alert:\n{context}\n\nQuestion: {question}"

        messages = [{"role": "user", "content": user_content}]
        tool_calls_made = []

        # Agentic loop — capped to prevent runaway cost
        for _turn in range(5):
            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                system=system,
                tools=self.tool_definitions,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                break

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_use_blocks:
                break

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for tool in tool_use_blocks:
                result = self.execute_tool(tool.name, tool.input)
                tool_calls_made.append({"tool": tool.name, "input": tool.input, "result": result})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

        # Extract final text
        findings = next(
            (b.text for b in response.content if b.type == "text"),
            "[No findings returned]",
        )

        return {"findings": findings, "tool_calls": tool_calls_made}
