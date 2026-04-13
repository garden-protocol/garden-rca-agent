"""
Base class for on-chain query agents.
Each chain inherits from this and implements chain-specific RPC tools.
"""
from abc import ABC, abstractmethod
from providers import get_provider

from config import settings as _settings


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

        provider = get_provider()
        model = _settings.get_fast_model()
        messages = [{"role": "user", "content": user_content}]
        tool_calls_made = []
        total_input = total_output = total_cache_read = total_cache_write = 0

        # Agentic loop — capped to prevent runaway cost
        for _turn in range(5):
            response = provider.create_message(
                model=model,
                max_tokens=4096,
                system=system,
                tools=self.tool_definitions,
                messages=messages,
            )

            u = response.usage
            total_input       += u.input_tokens
            total_output      += u.output_tokens
            total_cache_read  += u.cache_read_tokens
            total_cache_write += u.cache_creation_tokens

            if response.stop_reason == "end_turn":
                break

            if not response.tool_calls:
                break

            messages.append(provider.build_assistant_message(response))

            tool_results = []
            for tc in response.tool_calls:
                result = self.execute_tool(tc.name, tc.input)
                tool_calls_made.append({"tool": tc.name, "input": tc.input, "result": result})
                tool_results.append((tc.id, result))

            tr_msg = provider.build_tool_results_message(tool_results)
            if isinstance(tr_msg, list):
                messages.extend(tr_msg)
            else:
                messages.append(tr_msg)

        # Extract final text
        findings = response.text or "[No findings returned]"

        return {
            "findings": findings,
            "tool_calls": tool_calls_made,
            "usage": {
                "model": model,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "cache_read_tokens": total_cache_read,
                "cache_write_tokens": total_cache_write,
            },
        }
