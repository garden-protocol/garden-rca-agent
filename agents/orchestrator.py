"""
Orchestrator Agent.
Receives the alert, routes to the correct chain specialist (with log + on-chain support),
and synthesizes the final RCA report.
"""
import time
import anthropic
from datetime import datetime, timezone

from models.alert import Alert
from models.report import RCAReport
import agents.log_agent as log_agent
from agents.specialists.bitcoin import BitcoinSpecialist
from agents.specialists.evm import EVMSpecialist
from agents.specialists.solana import SolanaSpecialist
from agents.specialists.spark import SparkSpecialist
from agents.onchain.bitcoin import BitcoinOnChainAgent
from agents.onchain.evm import EVMOnChainAgent
from agents.onchain.solana import SolanaOnChainAgent
from agents.onchain.spark import SparkOnChainAgent


_SPECIALISTS = {
    "bitcoin": BitcoinSpecialist(),
    "evm": EVMSpecialist(),
    "solana": SolanaSpecialist(),
    "spark": SparkSpecialist(),
}

_ONCHAIN_AGENTS = {
    "bitcoin": BitcoinOnChainAgent(),
    "evm": EVMOnChainAgent(),
    "solana": SolanaOnChainAgent(),
    "spark": SparkOnChainAgent(),
}


def run(alert: Alert) -> RCAReport:
    """
    Full RCA pipeline:
      1. Log Intelligence Agent queries Loki
      2. On-Chain Agent queries chain state (if needed — specialist decides)
      3. Chain Specialist performs root cause analysis
      4. Orchestrator assembles the final RCAReport

    Failures in any step are non-fatal — the pipeline degrades gracefully.
    """
    started_at = time.monotonic()

    # ── Step 1: Log Intelligence ──────────────────────────────────────────────
    log_result = {"summary": "[Log agent not run]", "raw_lines": []}
    try:
        log_result = log_agent.run(alert)
    except Exception as exc:
        log_result["summary"] = f"[Log agent failed: {exc}]"

    # ── Step 2: On-Chain Query ────────────────────────────────────────────────
    onchain_result: dict | None = None
    onchain_agent = _ONCHAIN_AGENTS.get(alert.chain)
    if onchain_agent:
        try:
            question = _onchain_question(alert)
            context = log_result["summary"][:1500]  # give logs as context
            onchain_result = onchain_agent.query(question, context)
        except Exception as exc:
            onchain_result = {
                "findings": f"[On-chain agent failed: {exc}]",
                "tool_calls": [],
            }

    # ── Step 3: Chain Specialist Analysis ────────────────────────────────────
    specialist = _SPECIALISTS.get(alert.chain)
    if not specialist:
        raise ValueError(f"No specialist for chain: {alert.chain!r}")

    specialist_result = {
        "root_cause": "[Specialist not run]",
        "affected_components": [],
        "suggested_actions": [],
        "severity": "medium",
        "confidence": "low",
        "raw_analysis": "",
    }
    try:
        specialist_result = specialist.analyze(
            alert=alert,
            log_summary=log_result["summary"],
            onchain_findings=onchain_result,
        )
    except Exception as exc:
        specialist_result["root_cause"] = f"[Specialist failed: {exc}]"
        specialist_result["raw_analysis"] = (
            f"## Log Summary\n\n{log_result['summary']}\n\n"
            f"## Specialist Error\n\n{exc}"
        )

    # ── Step 4: Assemble Report ───────────────────────────────────────────────
    duration = time.monotonic() - started_at

    log_evidence = _extract_evidence_lines(log_result["raw_lines"])

    return RCAReport(
        order_id=alert.order_id,
        chain=alert.chain,
        service=alert.service,
        network=alert.network,
        root_cause=specialist_result["root_cause"],
        affected_components=specialist_result["affected_components"],
        log_evidence=log_evidence,
        onchain_evidence=_serialize_onchain(onchain_result),
        suggested_actions=specialist_result["suggested_actions"],
        severity=specialist_result["severity"],
        confidence=specialist_result["confidence"],
        raw_analysis=specialist_result["raw_analysis"],
        generated_at=datetime.now(timezone.utc),
        duration_seconds=round(duration, 2),
    )


def _onchain_question(alert: Alert) -> str:
    """Generate a chain-appropriate on-chain question from the alert."""
    base = f"Order {alert.order_id} triggered a '{alert.alert_type}' alert on {alert.network}."
    if alert.alert_type in ("missed_init", "deadline_approaching"):
        return (
            f"{base} "
            f"Check whether the initiation transaction for this order exists on-chain. "
            f"If a transaction hash or address is available in the metadata, check its status. "
            f"Also check current network fee/congestion conditions. "
            f"Metadata: {alert.metadata}"
        )
    return (
        f"{base} "
        f"Investigate the current on-chain state relevant to this alert. "
        f"Metadata: {alert.metadata}"
    )


def _extract_evidence_lines(raw_lines: list[str]) -> list[str]:
    """Return the most relevant log lines (errors/warnings first, capped at 20)."""
    priority = [l for l in raw_lines if any(k in l.lower() for k in ("error", "err", "fail", "panic", "fatal"))]
    warnings = [l for l in raw_lines if any(k in l.lower() for k in ("warn", "timeout", "retry"))]
    rest = [l for l in raw_lines if l not in priority and l not in warnings]
    combined = priority + warnings + rest
    return combined[:20]


def _serialize_onchain(result: dict | None) -> dict | None:
    if result is None:
        return None
    return {
        "findings": result.get("findings", ""),
        "tool_calls_count": len(result.get("tool_calls", [])),
    }
