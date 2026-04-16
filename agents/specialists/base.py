"""
Base class for chain specialist agents.
Each specialist knows the architecture of a specific chain's executor/watcher/relayer,
reads the relevant source code, and synthesizes root cause from logs + on-chain data.
"""
from abc import ABC, abstractmethod
from pathlib import Path

from models.alert import Alert
from tools.repo import build_repo_tool_definitions, execute_repo_tool
from tools.gitea import (
    is_configured as gitea_configured,
    build_gitea_tool_definitions,
    execute_gitea_tool,
)
from providers import get_provider


KNOWLEDGE_DIR = Path(__file__).parent.parent.parent / "knowledge"

from config import settings as _settings


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

    def _load_solver_knowledge(self) -> str:
        """Load cross-chain solver ecosystem knowledge (engine, comms, aggregator, daemon)."""
        path = KNOWLEDGE_DIR / "solver.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def _build_system(self) -> list[dict]:
        """Build system prompt blocks with prompt caching on the knowledge doc."""
        knowledge = self._load_knowledge()
        solver_knowledge = self._load_solver_knowledge()

        blocks = [
            {
                "type": "text",
                "text": self.system_prompt,
            },
        ]

        # Solver ecosystem knowledge is shared across all chain specialists
        if solver_knowledge:
            blocks.append({
                "type": "text",
                "text": f"\n\n## Solver Ecosystem Knowledge Base\n\n{solver_knowledge}",
            })

        # Chain-specific knowledge gets the cache breakpoint (largest, most reused block)
        blocks.append({
            "type": "text",
            "text": f"\n\n## Chain Knowledge Base\n\n{knowledge}",
            "cache_control": {"type": "ephemeral"},
        })

        return blocks

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
            dict with 'root_cause', 'affected_components', 'investigation_summary',
                       'remediation_actions', 'severity', 'confidence', 'raw_analysis'
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
            f"---\n\n"
            f"## Your Role: You Are the Investigator\n\n"
            f"You have full access to source code (via tools), log analysis (above), "
            f"on-chain findings (above), and deep knowledge of the codebase (in your system prompt). "
            f"YOUR job is to investigate, trace code paths, and explain what happened. "
            f"**Never tell the operator to inspect code, check logs, or verify on-chain state — "
            f"that is YOUR job and you have already done it (or can do it now with your tools).**\n\n"
            f"## Investigation Protocol\n\n"
            f"1. **TRACE the code path**: Using the knowledge base and source code tools, identify "
            f"what code executes for this order's state and alert type. For '{alert.alert_type}' alerts, "
            f"start from the relevant entry point in the knowledge base.\n"
            f"2. **CORRELATE evidence**: Match log patterns and on-chain state to specific code paths. "
            f"What condition in the code explains the observed behavior?\n"
            f"3. **IDENTIFY the failure point**: Name the exact function, condition, or external "
            f"dependency that failed. Cite file and function names from the knowledge base.\n"
            f"4. **DETERMINE root cause**: Explain WHY it failed (stale cache, insufficient gas, "
            f"missed event, deadline race, nonce desync, RPC failure, etc.)\n"
            f"5. **PRESCRIBE remediation**: Only actions that require human/operator intervention.\n\n"
            f"## Writing Style\n\n"
            f"Write like you're explaining the incident to a fellow engineer over chat — "
            f"plain, direct, concrete. Rules:\n"
            f"- Use plain English. No invented or fancy-sounding terms.\n"
            f"- If you're about to write a phrase like 'time-period refraction', "
            f"'dual-refund on expiry', 'temporal desynchronization', etc. — STOP. "
            f"Those are not real Garden concepts. Describe what actually happened instead.\n"
            f"- Approved vocabulary: 'timelock expired', 'refund', 'instant refund', "
            f"'source/destination initiate', 'source/destination redeem', 'HTLC', "
            f"'solver', 'relayer', 'executor', 'watcher'. If a concept doesn't fit one of these, "
            f"describe it literally (e.g. 'the solver never submitted the init tx' rather than "
            f"coining a term).\n"
            f"- Describe events in the order they happened. \"User initiated X at t0. "
            f"Solver did not initiate on destination. At t0+timelock, source auto-refunded.\"\n"
            f"- When both sides refund, call it a 'dual refund' (both sides refunded after "
            f"their respective timelocks expired) — not 'refraction' or any other invented term.\n"
            f"- If a sentence could confuse a core Garden engineer, rewrite it simpler.\n\n"
            f"## Output Format\n\n"
            f"Write a concise analysis following the protocol above, then end with:\n\n"
            f"```json\n"
            f'{{\n'
            f'  "root_cause": "1-2 sentences in plain English: what failed and why (cite code references; no invented jargon)",\n'
            f'  "affected_components": ["service/file:function or module"],\n'
            f'  "investigation_summary": "What you checked → what you found. 3-5 bullet points.",\n'
            f'  "remediation_actions": ["Only human-actionable steps: restart X, fund Y, update Z"],\n'
            f'  "severity": "critical|high|medium|low",\n'
            f'  "confidence": "high|medium|low"\n'
            f'}}\n'
            f"```\n\n"
            f"## Remediation Actions Rules\n\n"
            f"Valid remediation actions (things ONLY a human operator can do):\n"
            f"- Restart a service to clear in-memory cache / reset state\n"
            f"- Fund a wallet with native token for gas\n"
            f"- Update or rotate RPC endpoints\n"
            f"- Manually trigger a redeem/refund via CLI or API\n"
            f"- Escalate to the contract/protocol team\n"
            f"- Scale infra (increase connections, add replicas)\n"
            f"- Check external dependency status (RPC provider, mempool congestion)\n"
            f"- Rotate API keys or unlock keystores\n"
            f"- Wait for network conditions to improve (with specific what to wait for)\n\n"
            f"NEVER include these as remediation (you should do them yourself):\n"
            f"- 'Inspect/examine/review code in X' — you have the code\n"
            f"- 'Check logs for Y' — the log agent already queried logs\n"
            f"- 'Verify on-chain state of Z' — the on-chain agent already checked\n"
            f"- 'Add debug logging' — not incident remediation\n"
            f"- 'Optimize database queries' — not incident response\n"
            f"- 'Monitor X' — be specific about what to do, not what to watch\n\n"
            f"remediation_actions must have 2-5 items. "
            f"root_cause must be 1-2 sentences max."
        )

        provider = get_provider()
        model = _settings.get_specialist_model()
        messages = [{"role": "user", "content": user_message}]
        chain = self.chain  # capture for closure in tool execution
        total_input = total_output = total_cache_read = total_cache_write = 0

        def _accumulate(resp) -> None:
            nonlocal total_input, total_output, total_cache_read, total_cache_write
            u = resp.usage
            total_input       += u.input_tokens
            total_output      += u.output_tokens
            total_cache_read  += u.cache_read_tokens
            total_cache_write += u.cache_creation_tokens

        # Determine tool source: local filesystem > Gitea API > knowledge-only
        from config import settings as _cfg
        import os as _os
        repos_on_disk = any(
            _os.path.isdir(p) for p in _cfg.repo_paths(chain).values()
        )

        if repos_on_disk:
            tool_defs = build_repo_tool_definitions(chain)
            tool_executor = lambda name, inp: execute_repo_tool(chain, name, inp)
            max_turns = 25
        elif gitea_configured():
            tool_defs = build_gitea_tool_definitions(chain)
            tool_executor = lambda name, inp: execute_gitea_tool(chain, name, inp)
            max_turns = 15  # fewer turns for API-based tools (slower per call)
        else:
            tool_defs = None
            tool_executor = None
            max_turns = 0

        if tool_defs:
            # Agentic loop with code tools (filesystem or Gitea)
            for _turn in range(max_turns):
                response = provider.create_message(
                    model=model,
                    max_tokens=8192,
                    system=self._build_system(),
                    tools=tool_defs,
                    messages=messages,
                )
                _accumulate(response)

                if response.stop_reason == "end_turn":
                    break

                if not response.tool_calls:
                    break

                messages.append(provider.build_assistant_message(response))

                tool_results = []
                for tc in response.tool_calls:
                    result = tool_executor(tc.name, tc.input)
                    tool_results.append((tc.id, result))

                tr_msg = provider.build_tool_results_message(tool_results)
                if isinstance(tr_msg, list):
                    messages.extend(tr_msg)
                else:
                    messages.append(tr_msg)

            # If the loop hit the turn cap with no text in the last response (still mid-tool-use),
            # satisfy pending tool_use blocks then force a written summary.
            if not response.text and response.tool_calls:
                messages.append(provider.build_assistant_message(response))

                stub_results = [
                    (tc.id, "Tool call limit reached — no result available.")
                    for tc in response.tool_calls
                ]
                tr_msg = provider.build_tool_results_message(stub_results)
                if isinstance(tr_msg, list):
                    messages.extend(tr_msg)
                else:
                    messages.append(tr_msg)

                messages.append({
                    "role": "user",
                    "content": (
                        "You have used the maximum number of tool calls. "
                        "Based on everything gathered so far, write your complete root cause analysis now."
                    ),
                })
                response = provider.create_message(
                    model=model,
                    max_tokens=8192,
                    system=self._build_system(),
                    messages=messages,
                )
                _accumulate(response)
        else:
            # No code tools available — analyse from knowledge docs only
            response = provider.create_message(
                model=model,
                max_tokens=8192,
                system=self._build_system(),
                messages=messages,
            )
            _accumulate(response)

        raw_analysis = response.text or "[Specialist returned no analysis]"

        # Parse the trailing JSON block
        structured = _extract_json_block(raw_analysis)

        # Coerce fields that must be strings — some providers return lists for bullet points
        root_cause = structured.get("root_cause", raw_analysis[:500])
        if isinstance(root_cause, list):
            root_cause = " ".join(root_cause)
        investigation_summary = structured.get("investigation_summary", "")
        if isinstance(investigation_summary, list):
            investigation_summary = "\n".join(f"- {item}" for item in investigation_summary)

        return {
            "root_cause": root_cause,
            "affected_components": structured.get("affected_components", []),
            "investigation_summary": investigation_summary,
            "remediation_actions": structured.get("remediation_actions", []),
            "severity": structured.get("severity", "medium"),
            "confidence": structured.get("confidence", "low"),
            "raw_analysis": raw_analysis,
            "usage": {
                "model": model,
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
