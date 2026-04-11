"""
Orchestrator Agent.
Receives the alert (or order ID), routes to the correct chain specialist
(with log + on-chain support), and synthesizes the final RCA report.

Two entry points:
  investigate(order_id)  — smart pipeline: fetch order → classify state →
                           deterministic early-return checks → LLM pipeline
  run(alert)             — legacy alert-based pipeline (used by /rca endpoint)
"""
import logging
import time
from datetime import datetime, timezone

from models.alert import Alert
from models.report import RCAReport
from models.investigate import SwapState, InvestigateResponse, AgentTokenUsage, AICost
from models.pricing import compute_cost
import agents.log_agent as log_agent
from agents.specialists.bitcoin import BitcoinSpecialist
from agents.specialists.evm import EVMSpecialist
from agents.specialists.solana import SolanaSpecialist
from agents.onchain.bitcoin import BitcoinOnChainAgent
from agents.onchain.evm import EVMOnChainAgent
from agents.onchain.solana import SolanaOnChainAgent
from tools.orders_api import (
    fetch_order,
    parse_order_id,
    classify_state,
    normalize_chain,
    fetch_order_created_at,
    fetch_fiat_prices,
)
from tools.liquidity import check_solver_liquidity, get_solver_address
from config import settings


logger = logging.getLogger("rca-agent.orchestrator")

_SPECIALISTS = {
    "bitcoin": BitcoinSpecialist(),
    "evm": EVMSpecialist(),
    "solana": SolanaSpecialist(),
}

_ONCHAIN_AGENTS = {
    "bitcoin": BitcoinOnChainAgent(),
    "evm": EVMOnChainAgent(),
    "solana": SolanaOnChainAgent(),
}


# ── Refund context for LLM pipeline ──────────────────────────────────────────

def _refund_context(
    src: "SwapData",
    dst: "SwapData",
    co: "CreateOrder",
) -> str:
    """
    Build concise context about a refunded order for the LLM pipeline.
    Describes which side(s) were refunded and observable state.
    """
    parts: list[str] = []
    if src.is_refunded:
        parts.append(f"Source refunded (tx: {src.refund_tx_hash}).")
    if dst.is_refunded:
        parts.append(f"Destination refunded (tx: {dst.refund_tx_hash}).")

    if src.is_refunded and not dst.is_initiated:
        parts.append("Solver never initiated on destination.")
        if co.additional_data.is_blacklisted:
            parts.append("Order was blacklisted.")
    elif src.is_refunded and dst.is_initiated and not dst.is_redeemed:
        parts.append("Destination was initiated but never redeemed.")
    elif src.is_refunded and dst.is_redeemed:
        parts.append("Destination was redeemed but source was still refunded.")

    return " ".join(parts)


# ── Investigation pipeline ────────────────────────────────────────────────────

def investigate(raw_order_id: str) -> InvestigateResponse:
    """
    Order-state-aware investigation pipeline.

    Steps:
      1. Parse order_id (strip URL prefix if needed)
      2. Fetch full order from Garden Finance API
      3. Classify swap state (DestInitPending / UserRedeemPending / SolverRedeemPending)
      4. Run cheap, deterministic early-return checks (no LLM) — uses on-chain agents
         for RPC queries when needed
      5. If no early return → build Alert + run the full LLM pipeline (run())
    """
    started_at = time.monotonic()
    order_id = parse_order_id(raw_order_id)
    logger.info("Investigating order %s", order_id)

    # ── Fetch order ───────────────────────────────────────────────────────────
    order = fetch_order(order_id)
    result = order.result
    co = result.create_order
    src = result.source_swap
    dst = result.destination_swap

    src_chain_api = src.chain          # API name, e.g. "bitcoin"
    dst_chain_api = dst.chain          # API name, e.g. "ethereum"
    src_chain = normalize_chain(src_chain_api)   # internal name, e.g. "bitcoin"
    dst_chain = normalize_chain(dst_chain_api)   # internal name, e.g. "evm"

    # ── Classify state ────────────────────────────────────────────────────────
    state = classify_state(order)
    logger.info("Order %s classified as: %s", order_id, state.value)

    def _early(reason: str) -> InvestigateResponse:
        return InvestigateResponse(
            order_id=order_id,
            state=state,
            source_chain=src_chain,
            destination_chain=dst_chain,
            early_return=True,
            reason=reason,
            generated_at=datetime.now(timezone.utc),
            duration_seconds=round(time.monotonic() - started_at, 2),
        )

    unsupported_chains = [c for c in (src_chain, dst_chain) if c not in _SPECIALISTS]
    if unsupported_chains:
        unsupported_csv = ", ".join(unsupported_chains)
        supported_csv = ", ".join(sorted(_SPECIALISTS.keys()))
        return _early(
            f"Unsupported chain(s) for investigation: {unsupported_csv}. "
            f"Supported chains: {supported_csv}"
        )

    # ── Refund check (before any state-specific logic) ─────────────────────
    if src.is_refunded or dst.is_refunded:
        state = SwapState.REFUNDED
        refund_context = _refund_context(src, dst, co)
        # Compute order lifetime from created_at to deadline (not to now)
        lifetime_str = ""
        deadline_unix = co.additional_data.deadline
        if deadline_unix:
            created_ts = result.created_at
            if created_ts.tzinfo is None:
                created_ts = created_ts.replace(tzinfo=timezone.utc)
            lifetime_secs = deadline_unix - int(created_ts.timestamp())
            if lifetime_secs > 0:
                lifetime_mins = lifetime_secs / 60
                lifetime_str = (
                    f"Order lifetime (created_at to deadline): {lifetime_mins:.0f} minutes. "
                )
        alert = _build_alert_from_order(order_id, order, state, src_chain, dst_chain)
        alert.message = (
            f"Order {order_id} has been refunded. {lifetime_str}{refund_context} "
            f"Investigate logs and on-chain data to determine the root cause."
        )
        rca_report, ai_cost = run(alert)
        return InvestigateResponse(
            order_id=order_id,
            state=state,
            source_chain=src_chain,
            destination_chain=dst_chain,
            early_return=False,
            rca_report=rca_report,
            ai_cost=ai_cost,
            generated_at=datetime.now(timezone.utc),
            duration_seconds=round(time.monotonic() - started_at, 2),
        )

    # ── No user init (pre-state check) ───────────────────────────────────────
    if not src.is_initiated:
        return InvestigateResponse(
            order_id=order_id,
            state=SwapState.USER_NOT_INITED,
            source_chain=src_chain,
            destination_chain=dst_chain,
            early_return=True,
            reason="Source initiate transaction not found; user has not initiated the swap yet.",
            generated_at=datetime.now(timezone.utc),
            duration_seconds=round(time.monotonic() - started_at, 2),
        )

    # ═════════════════════════════════════════════════════════════════════════
    # DestInitPending early-return checks
    # ═════════════════════════════════════════════════════════════════════════
    if state == SwapState.DEST_INIT_PENDING:

        # 0. Confirmation pending — init tx detected but not yet sufficiently confirmed.
        #    Solver will not initiate on destination until required_confirmations are met.
        if (
            src.initiate_tx_hash
            and src.required_confirmations > 0
            and src.current_confirmations < src.required_confirmations
        ):
            return _early(
                f"Source initiate transaction detected ({src.initiate_tx_hash}) but only "
                f"{src.current_confirmations}/{src.required_confirmations} confirmations reached. "
                f"Solver is waiting for full confirmation before initiating on destination — "
                f"this is expected behaviour, not a stuck order."
            )

        # 1. Blacklist check
        if co.additional_data.is_blacklisted:
            return _early("Order is blacklisted; solver will not initiate on destination.")

        # 2. Filled amount tolerance check — only meaningful once init is confirmed on-chain.
        # If initiate_block_number is "0" the tx hasn't been mined yet; skip the check.
        amount = src.amount_int
        filled = src.filled_amount_int
        if amount > 0 and src.initiate_block_number not in ("0", ""):
            deviation_pct = abs(filled - amount) / amount * 100
            if deviation_pct > settings.filled_amount_tolerance_pct:
                return _early(
                    f"Filled amount ({filled}) differs from expected ({amount}) by "
                    f"{deviation_pct:.1f}% — exceeds tolerance of "
                    f"{settings.filled_amount_tolerance_pct}%; solver threshold not met."
                )

        # 3. Price fluctuation check — compare stored quote prices against current market prices.
        # If either input or output token has moved beyond the threshold since quote time,
        # the solver would have rejected initiating on destination.
        ad = co.additional_data
        if ad.input_token_price is not None and ad.output_token_price is not None:
            try:
                fiat = fetch_fiat_prices()
                current_input = fiat.get(f"{co.source_chain}:{co.source_asset}")
                current_output = fiat.get(f"{co.destination_chain}:{co.destination_asset}")
                if current_input and current_output:
                    input_dev = abs(current_input - ad.input_token_price) / ad.input_token_price * 100
                    output_dev = abs(current_output - ad.output_token_price) / ad.output_token_price * 100
                    if input_dev > settings.price_deviation_tolerance_pct or output_dev > settings.price_deviation_tolerance_pct:
                        return _early(
                            f"Price fluctuation detected: input token '{co.source_asset}' moved "
                            f"{input_dev:.1f}%, output token '{co.destination_asset}' moved "
                            f"{output_dev:.1f}% from quote price (threshold: "
                            f"{settings.price_deviation_tolerance_pct}%); solver likely rejected "
                            f"the swap due to unfavourable price movement."
                        )
            except Exception as exc:
                logger.warning("Price fluctuation check failed: %s", exc)

        # 4. Solver liquidity check
        if settings.liquidity_url:
            has_liquidity, shortage_msg = check_solver_liquidity(
                solver_id=co.solver_id,
                dest_chain=dst_chain_api,
                asset=dst.asset,
                required_amount=co.destination_amount,
            )
            if not has_liquidity:
                return _early(shortage_msg)

        # 5. Deadline check — initiate_timestamp vs solver deadline
        deadline_unix = co.additional_data.deadline
        if deadline_unix and src.initiate_timestamp:
            initiate_ts = src.initiate_timestamp
            if initiate_ts.tzinfo is None:
                initiate_ts = initiate_ts.replace(tzinfo=timezone.utc)
            if int(initiate_ts.timestamp()) > deadline_unix:
                return _early(
                    "Source initiate timestamp is past the solver deadline; "
                    "solver will not initiate on destination."
                )

    # ═════════════════════════════════════════════════════════════════════════
    # UserRedeemPending early-return checks
    # ═════════════════════════════════════════════════════════════════════════
    elif state == SwapState.USER_REDEEM_PENDING:

        onchain_agent = _ONCHAIN_AGENTS.get(dst_chain)
        if onchain_agent:

            # 1. Relayer native balance check
            relayer_addr = settings.relayer_address(dst_chain)
            if relayer_addr:
                min_bal = settings.min_gas_balance(dst_chain)
                balance_question = (
                    f"Check the native token balance of address {relayer_addr}. "
                    f"The minimum required balance is {min_bal} (in the chain's base unit — "
                    f"wei for EVM, lamports for Solana, satoshis for Bitcoin). "
                    f"If the balance is below this threshold, start your response with "
                    f"'BALANCE_INSUFFICIENT' and include the actual balance. "
                    f"Otherwise start your response with 'BALANCE_OK' and include the balance."
                )
                try:
                    balance_result = onchain_agent.query(balance_question)
                    findings = balance_result.get("findings", "")
                    logger.info("UserRedeemPending balance check findings: %s", findings[:200])
                    if "BALANCE_INSUFFICIENT" in findings.upper():
                        return _early(
                            f"Please fund relayer {relayer_addr} with native token on "
                            f"{dst_chain_api}. {findings}"
                        )
                except Exception as exc:
                    logger.warning("Relayer balance check failed: %s", exc)

            # 2. HTLC already-redeemed check
            htlc_question = (
                f"Check whether the HTLC at address {dst.htlc_address} with "
                f"secret_hash {src.secret_hash} has already been redeemed on-chain. "
                f"Look up the contract or account state directly. "
                f"If it has already been redeemed, start your response with 'HTLC_REDEEMED'. "
                f"Otherwise start your response with 'HTLC_PENDING'."
            )
            try:
                htlc_result = onchain_agent.query(htlc_question)
                findings = htlc_result.get("findings", "")
                logger.info("UserRedeemPending HTLC check findings: %s", findings[:200])
                if "HTLC_REDEEMED" in findings.upper():
                    return _early(
                        "Watcher failed to update state. Destination redeem already happened "
                        "on-chain; please check RPC/watcher."
                    )
            except Exception as exc:
                logger.warning("HTLC redeem check failed: %s", exc)

    # ═════════════════════════════════════════════════════════════════════════
    # SolverRedeemPending early-return checks
    # ═════════════════════════════════════════════════════════════════════════
    elif state == SwapState.SOLVER_REDEEM_PENDING:

        onchain_agent = _ONCHAIN_AGENTS.get(src_chain)
        if onchain_agent:

            # 1. Executor native balance / gas check
            # Executor address is resolved from the liquidity API (per-solver, per-chain).
            executor_addr = get_solver_address(co.solver_id, src_chain_api)
            if executor_addr:
                min_bal = settings.min_gas_balance(src_chain)
                balance_question = (
                    f"Check the native token balance of executor address {executor_addr}. "
                    f"The minimum required balance is {min_bal} (base unit). "
                    f"If the balance is below this threshold, start your response with "
                    f"'BALANCE_INSUFFICIENT' and include the actual balance. "
                    f"Otherwise start your response with 'BALANCE_OK'."
                )
                try:
                    balance_result = onchain_agent.query(balance_question)
                    findings = balance_result.get("findings", "")
                    logger.info("SolverRedeemPending balance check findings: %s", findings[:200])
                    if "BALANCE_INSUFFICIENT" in findings.upper():
                        return _early(
                            f"Please fund executor {executor_addr} with native token on "
                            f"{src_chain_api}. {findings}"
                        )
                except Exception as exc:
                    logger.warning("Executor balance check failed: %s", exc)

            # 2. Source HTLC already-redeemed check
            htlc_question = (
                f"Check whether the HTLC at address {src.htlc_address} with "
                f"secret_hash {src.secret_hash} has already been redeemed on-chain. "
                f"If it has already been redeemed, start your response with 'HTLC_REDEEMED'. "
                f"Otherwise start your response with 'HTLC_PENDING'."
            )
            try:
                htlc_result = onchain_agent.query(htlc_question)
                findings = htlc_result.get("findings", "")
                logger.info("SolverRedeemPending source HTLC check findings: %s", findings[:200])
                if "HTLC_REDEEMED" in findings.upper():
                    return _early(
                        "Please check watcher RPCs; source redeem already happened on-chain "
                        "but watcher has not updated the order state."
                    )
            except Exception as exc:
                logger.warning("Source HTLC redeem check failed: %s", exc)

    # ── Unknown state ─────────────────────────────────────────────────────────
    elif state == SwapState.UNKNOWN:
        both_redeemed = src.is_redeemed and dst.is_redeemed
        if both_redeemed:
            return _early("Order has already completed successfully (both sides redeemed).")
        return _early(
            "Unable to classify order into a known stuck state. "
            "Manual investigation required."
        )

    # ═════════════════════════════════════════════════════════════════════════
    # No early return — escalate to full LLM pipeline
    # ═════════════════════════════════════════════════════════════════════════
    alert = _build_alert_from_order(order_id, order, state, src_chain, dst_chain)
    rca_report, ai_cost = run(alert)

    return InvestigateResponse(
        order_id=order_id,
        state=state,
        source_chain=src_chain,
        destination_chain=dst_chain,
        early_return=False,
        rca_report=rca_report,
        ai_cost=ai_cost,
        generated_at=datetime.now(timezone.utc),
        duration_seconds=round(time.monotonic() - started_at, 2),
    )


def _build_alert_from_order(
    order_id: str,
    order: "OrderApiResponse",
    state: SwapState,
    src_chain: str,
    dst_chain: str,
) -> Alert:
    """
    Construct an Alert from full order data so the existing run() pipeline can handle it.
    The chain/service/network fields are inferred from the order and stuck state.
    """
    result = order.result
    co = result.create_order
    src = result.source_swap

    # Decide which chain's service is responsible based on stuck state
    if state == SwapState.REFUNDED:
        # For refunds: investigate the destination chain executor (solver side)
        # since the most common refund cause is solver failing to initiate.
        dst = result.destination_swap
        chain = dst_chain if not dst.is_initiated else src_chain
        service = "executor"
        alert_type = "refunded"
    elif state == SwapState.DEST_INIT_PENDING:
        chain = dst_chain
        service = "executor"
        alert_type = "missed_init"
    elif state == SwapState.USER_REDEEM_PENDING:
        chain = dst_chain
        service = "relayer"
        alert_type = "stuck_order"
    else:  # SolverRedeemPending
        chain = src_chain
        service = "executor"
        alert_type = "stuck_order"

    # Build a rich order context summary for the specialist
    ad = co.additional_data
    deadline_unix = ad.deadline
    created_ts = result.created_at
    if created_ts.tzinfo is None:
        created_ts = created_ts.replace(tzinfo=timezone.utc)

    # Compute time-related context
    time_context = ""
    if deadline_unix:
        now_unix = int(datetime.now(timezone.utc).timestamp())
        remaining_secs = deadline_unix - now_unix
        if remaining_secs > 0:
            time_context = f"Deadline in {remaining_secs // 60}m {remaining_secs % 60}s."
        else:
            time_context = f"Deadline EXPIRED {abs(remaining_secs) // 60}m ago."

    order_context = (
        f"## Order State Summary\n\n"
        f"- **Route**: {co.source_chain} ({src.asset}) → {co.destination_chain} ({dst.asset})\n"
        f"- **Source amount**: {co.source_amount} | **Destination amount**: {co.destination_amount}\n"
        f"- **Solver ID**: {co.solver_id}\n"
        f"- **Created**: {created_ts.isoformat()}\n"
        f"- **Source initiated**: {'YES' if src.is_initiated else 'NO'}"
    )
    if src.is_initiated:
        order_context += f" (tx: {src.initiate_tx_hash})"
        if src.required_confirmations > 0:
            order_context += f" [{src.current_confirmations}/{src.required_confirmations} confirmations]"
    order_context += (
        f"\n- **Destination initiated**: {'YES' if dst.is_initiated else 'NO'}"
    )
    if dst.is_initiated:
        order_context += f" (tx: {dst.initiate_tx_hash})"
    order_context += (
        f"\n- **Source redeemed**: {'YES' if src.is_redeemed else 'NO'}"
        f" | **Destination redeemed**: {'YES' if dst.is_redeemed else 'NO'}"
        f"\n- **Source refunded**: {'YES' if src.is_refunded else 'NO'}"
        f" | **Destination refunded**: {'YES' if dst.is_refunded else 'NO'}"
        f"\n- **Timelock**: source={src.timelock}s, destination={dst.timelock}s"
    )
    if deadline_unix:
        order_context += f"\n- **Deadline**: {datetime.fromtimestamp(deadline_unix, tz=timezone.utc).isoformat()} ({time_context})"
    if ad.is_blacklisted:
        order_context += f"\n- **BLACKLISTED**: Yes"
    if src.filled_amount:
        order_context += f"\n- **Filled amount**: {src.filled_amount} (expected: {src.amount})"
    order_context += "\n"

    message = (
        f"Order {order_id} is stuck in state {state.value}.\n\n"
        f"{order_context}"
    )

    return Alert(
        order_id=order_id,
        alert_type=alert_type,
        chain=chain,  # type: ignore[arg-type]
        service=service,  # type: ignore[arg-type]
        network="mainnet",
        message=message,
        timestamp=datetime.now(timezone.utc),
        deadline=datetime.fromtimestamp(deadline_unix, tz=timezone.utc) if deadline_unix else None,
        metadata={
            "order_created_at": result.created_at.isoformat(),
            "source_chain": co.source_chain,
            "destination_chain": co.destination_chain,
            "solver_id": co.solver_id,
            "source_amount": co.source_amount,
            "destination_amount": co.destination_amount,
            "src_initiate_tx_hash": src.initiate_tx_hash,
            "secret_hash": src.secret_hash,
            "stuck_state": state.value,
            "deadline": deadline_unix,
            "source_timelock": src.timelock,
            "destination_timelock": dst.timelock,
        },
    )


# ── Legacy alert-based pipeline ───────────────────────────────────────────────

def run(alert: Alert) -> tuple[RCAReport, AICost]:
    """
    Full RCA pipeline:
      1. Enrich alert with order API data (created_at)
      2. Log Intelligence Agent queries Loki
      3. On-Chain Agent queries chain state
      4. Chain Specialist performs root cause analysis
      5. Orchestrator assembles the final RCAReport + AICost

    Failures in any step are non-fatal — the pipeline degrades gracefully.
    """
    started_at = time.monotonic()

    alert = _enrich_with_order_created_at(alert)

    # ── Step 1: Log Intelligence ──────────────────────────────────────────────
    log_result = {"summary": "[Log agent not run]", "raw_lines": [], "usage": None}
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
            context = log_result["summary"][:1500]
            onchain_result = onchain_agent.query(question, context)
        except Exception as exc:
            onchain_result = {
                "findings": f"[On-chain agent failed: {exc}]",
                "tool_calls": [],
                "usage": None,
            }

    # ── Step 3: Chain Specialist Analysis ────────────────────────────────────
    specialist = _SPECIALISTS.get(alert.chain)
    if not specialist:
        raise ValueError(f"No specialist for chain: {alert.chain!r}")

    specialist_result = {
        "root_cause": "[Specialist not run]",
        "affected_components": [],
        "investigation_summary": "",
        "remediation_actions": [],
        "severity": "medium",
        "confidence": "low",
        "raw_analysis": "",
        "usage": None,
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

    # Build key log evidence from LLM-curated evidence (preferred) or fallback
    from models.report import LogEvidence
    key_log_evidence = []
    for ev in log_result.get("key_evidence", []):
        if isinstance(ev, dict) and ev.get("line"):
            key_log_evidence.append(LogEvidence(
                line=ev["line"],
                significance=ev.get("significance", ""),
                source=ev.get("source", ""),
            ))
    # Cap at 10 evidence items
    key_log_evidence = key_log_evidence[:10]

    report = RCAReport(
        order_id=alert.order_id,
        chain=alert.chain,
        service=alert.service,
        network=alert.network,
        root_cause=specialist_result["root_cause"],
        affected_components=specialist_result["affected_components"],
        investigation_summary=specialist_result.get("investigation_summary", ""),
        key_log_evidence=key_log_evidence,
        onchain_evidence=_serialize_onchain(onchain_result),
        remediation_actions=specialist_result.get("remediation_actions", []),
        severity=specialist_result["severity"],
        confidence=specialist_result["confidence"],
        raw_analysis=specialist_result["raw_analysis"],
        generated_at=datetime.now(timezone.utc),
        duration_seconds=round(duration, 2),
    )

    log_usage      = _build_agent_usage(log_result.get("usage"))
    onchain_usage  = _build_agent_usage((onchain_result or {}).get("usage"))
    specialist_usage = _build_agent_usage(specialist_result.get("usage"))
    total = sum(
        u.cost_usd for u in (log_usage, onchain_usage, specialist_usage) if u
    )
    ai_cost = AICost(
        log_agent=log_usage,
        onchain_agent=onchain_usage,
        specialist=specialist_usage,
        total_cost_usd=round(total, 6),
    )

    return report, ai_cost


def _enrich_with_order_created_at(alert: Alert) -> Alert:
    """Enrich alert.metadata with order_created_at from the order API. Non-fatal on failure."""
    metadata = dict(alert.metadata or {})
    if metadata.get("order_created_at"):
        return alert
    try:
        created_at, source_path = fetch_order_created_at(alert.order_id)
    except Exception as exc:
        logger.warning("Order API lookup failed for order %s: %s", alert.order_id, exc)
        return alert
    metadata["order_created_at"] = created_at.isoformat()
    metadata["order_created_at_source"] = source_path
    logger.info(
        "Order %s created_at resolved via API: %s (source=%s)",
        alert.order_id,
        metadata["order_created_at"],
        source_path,
    )
    return alert.model_copy(update={"metadata": metadata})


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



# _extract_evidence_lines removed — log evidence is now LLM-curated by the log agent


def _build_agent_usage(raw: dict | None) -> AgentTokenUsage | None:
    """Convert a raw usage dict returned by an agent into an AgentTokenUsage model."""
    if not raw:
        return None
    model = raw.get("model", "unknown")
    inp   = raw.get("input_tokens", 0)
    out   = raw.get("output_tokens", 0)
    cr    = raw.get("cache_read_tokens", 0)
    cw    = raw.get("cache_write_tokens", 0)
    return AgentTokenUsage(
        model=model,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
        cost_usd=round(compute_cost(model, inp, out, cr, cw), 6),
    )


def _serialize_onchain(result: dict | None) -> dict | None:
    if result is None:
        return None
    return {
        "findings": result.get("findings", ""),
        "tool_calls_count": len(result.get("tool_calls", [])),
    }
