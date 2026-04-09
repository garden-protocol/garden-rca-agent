"""
Base class for chain specialist agents.
Each specialist knows the architecture of a specific chain's executor/watcher/relayer,
reads the relevant source code, and synthesizes root cause from logs + on-chain data.
"""
import anthropic
from abc import ABC, abstractmethod
from pathlib import Path

from models.alert import Alert
from tools.repo import build_repo_tool_definitions, execute_repo_tool


MODEL = "claude-opus-4-6"
KNOWLEDGE_DIR = Path(__file__).parent.parent.parent / "knowledge"

from config import settings as _settings
client = anthropic.Anthropic(api_key=_settings.anthropic_api_key)


class BaseSpecialist(ABC):

    @property
    @abstractmethod
    def chain(self) -> str:
        """Chain name: bitcoin, evm, solana"""
        ...

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """Chain-specific system prompt loaded from prompts/ directory."""
        ...

    def _load_knowledge(self) -> str:
        """Load the pre-generated knowledge doc for this chain."""
        path = KNOWLEDGE_DIR / f"{self.chain}.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return f"[No knowledge doc found for {self.chain}. Run POST /study/{self.chain} first.]"

    def _build_system(self) -> list[dict]:
        """Build system prompt blocks with prompt caching on the knowledge doc."""
        knowledge = self._load_knowledge()

        # System prompt is a list of content blocks when using cache_control
        return [
            {
                "type": "text",
                "text": self.system_prompt,
            },
            {
                "type": "text",
                "text": f"\n\n## Chain Knowledge Base\n\n{knowledge}",
                "cache_control": {"type": "ephemeral"},
            },
        ]

    def analyze(
        self,
        alert: Alert,
        log_summary: str,
        onchain_findings: dict | None = None,
    ) -> dict:
        """
        Analyze the alert using log data and optional on-chain findings.
        Uses repo tools to read source code as needed.

        Args:
            alert: Incoming alert
            log_summary: Markdown report from the Log Intelligence Agent
            onchain_findings: Optional findings from On-Chain Query Agent

        Returns:
            dict with 'root_cause', 'affected_components', 'suggested_actions',
                       'severity', 'confidence', 'raw_analysis'
        """
        alert_block = (
            f"**Order ID:** {alert.order_id}\n"
            f"**Alert type:** {alert.alert_type}\n"
            f"**Service:** {alert.service}\n"
            f"**Network:** {alert.network}\n"
            f"**Message:** {alert.message}\n"
            f"**Timestamp:** {alert.timestamp.isoformat()}\n"
        )
        if alert.deadline:
            alert_block += f"**Deadline:** {alert.deadline.isoformat()}\n"
        if alert.metadata:
            alert_block += f"**Metadata:** {alert.metadata}\n"

        onchain_block = ""
        if onchain_findings:
            onchain_block = (
                f"\n\n## On-Chain Findings\n\n"
                f"{onchain_findings.get('findings', '[none]')}\n"
            )

        user_message = (
            f"## Alert\n\n{alert_block}\n\n"
            f"## Log Intelligence Report\n\n{log_summary}"
            f"{onchain_block}\n\n"
            f"Using the knowledge base, source code tools, and the above evidence, "
            f"perform a full root cause analysis. Read relevant source files to understand "
            f"exact code paths involved. Identify:\n"
            f"1. Root cause (be specific — name files and line numbers where possible)\n"
            f"2. Affected components\n"
            f"3. Suggested actions to resolve and prevent recurrence\n"
            f"4. Severity: critical / high / medium / low\n"
            f"5. Confidence in your diagnosis: high / medium / low\n\n"
            f"End your response with a JSON block containing these fields:\n"
            f"```json\n"
            f'{{"root_cause": "...", "affected_components": [...], '
            f'"suggested_actions": [...], "severity": "...", "confidence": "..."}}\n'
            f"```"
        )

        messages = [{"role": "user", "content": user_message}]
        chain = self.chain  # capture for closure in tool execution
        total_input = total_output = total_cache_read = total_cache_write = 0

        def _accumulate(resp) -> None:
            nonlocal total_input, total_output, total_cache_read, total_cache_write
            u = resp.usage
            total_input       += u.input_tokens
            total_output      += u.output_tokens
            total_cache_read  += getattr(u, "cache_read_input_tokens", 0) or 0
            total_cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0

        # Only enable repo tools if at least one repo path exists on disk.
        # In prod without mounted repos, skip directly to a single knowledge-doc-based call.
        from config import settings as _cfg
        import os as _os
        repos_available = any(
            _os.path.isdir(p) for p in _cfg.repo_paths(chain).values()
        )

        if repos_available:
            tool_defs = build_repo_tool_definitions(chain)

            # Agentic loop with repo tools — capped to prevent runaway cost
            for _turn in range(25):
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=8192,
                    system=self._build_system(),
                    tools=tool_defs,
                    messages=messages,
                )
                _accumulate(response)

                if response.stop_reason == "end_turn":
                    break

                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
                if not tool_use_blocks:
                    break

                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for tool in tool_use_blocks:
                    result = execute_repo_tool(chain, tool.name, tool.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool.id,
                        "content": result,
                    })

                messages.append({"role": "user", "content": tool_results})

            # If the loop hit the turn cap with no text in the last response (still mid-tool-use),
            # satisfy pending tool_use blocks then force a written summary.
            if not any(b.type == "text" for b in response.content):
                pending_tool_uses = [b for b in response.content if b.type == "tool_use"]
                messages.append({"role": "assistant", "content": response.content})
                stub_results = [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool.id,
                        "content": "Tool call limit reached — no result available.",
                    }
                    for tool in pending_tool_uses
                ]
                stub_results.append({
                    "type": "text",
                    "text": (
                        "You have used the maximum number of tool calls. "
                        "Based on everything gathered so far, write your complete root cause analysis now."
                    ),
                })
                messages.append({"role": "user", "content": stub_results})
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=8192,
                    system=self._build_system(),
                    messages=messages,
                )
                _accumulate(response)
        else:
            # No repos on disk — analyse directly from knowledge docs
            response = client.messages.create(
                model=MODEL,
                max_tokens=8192,
                system=self._build_system(),
                messages=messages,
            )
            _accumulate(response)

        raw_analysis = next(
            (b.text for b in response.content if b.type == "text"),
            "[Specialist returned no analysis]",
        )

        # Parse the trailing JSON block
        structured = _extract_json_block(raw_analysis)

        return {
            "root_cause": structured.get("root_cause", raw_analysis[:500]),
            "affected_components": structured.get("affected_components", []),
            "suggested_actions": structured.get("suggested_actions", []),
            "severity": structured.get("severity", "medium"),
            "confidence": structured.get("confidence", "low"),
            "raw_analysis": raw_analysis,
            "usage": {
                "model": MODEL,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "cache_read_tokens": total_cache_read,
                "cache_write_tokens": total_cache_write,
            },
        }


def _extract_json_block(text: str) -> dict:
    """Extract the last ```json ... ``` block from a text response."""
    import json
    import re
    matches = re.findall(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if not matches:
        return {}
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return {}
