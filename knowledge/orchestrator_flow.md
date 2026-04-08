# Orchestrator Investigation Flow

This document describes the order-state-aware investigation pipeline (`POST /investigate`).
It is used as a knowledge reference for the orchestrator and committed to git so the
full team can understand the triage logic.

---

## Overview

When an order ID (or Garden Finance URL) is submitted for investigation, the orchestrator:

1. Fetches the full order from the API
2. Classifies the swap into one of three stuck states
3. Runs cheap deterministic checks (no LLM) — returns an early diagnosis if possible
4. Escalates to the full LLM pipeline (log agent + on-chain agent + specialist) only
   when the deterministic checks don't resolve the issue

---

## API Fields Used

| Field path | Meaning |
|---|---|
| `result.create_order.create_id` | Canonical order ID |
| `result.create_order.source_chain` | Source chain (API name) |
| `result.create_order.destination_chain` | Destination chain (API name) |
| `result.create_order.source_amount` | Expected source amount (string int) |
| `result.create_order.destination_amount` | Expected destination amount (string int) |
| `result.create_order.solver_id` | Solver wallet address |
| `result.create_order.additional_data.is_blacklisted` | Blacklist flag |
| `result.create_order.additional_data.deadline` | Solver deadline (UNIX timestamp) |
| `result.source_swap.initiate_tx_hash` | Source init tx (empty = not initiated) |
| `result.source_swap.redeem_tx_hash` | Source redeem tx |
| `result.source_swap.refund_tx_hash` | Source refund tx |
| `result.source_swap.filled_amount` | Amount user actually sent (string int) |
| `result.source_swap.amount` | Expected source amount (string int) |
| `result.source_swap.initiate_timestamp` | When source was initiated (ISO datetime) |
| `result.source_swap.secret_hash` | HTLC secret hash |
| `result.destination_swap.initiate_tx_hash` | Dest init tx (empty = not initiated) |
| `result.destination_swap.redeem_tx_hash` | Dest redeem tx |
| `result.destination_swap.refund_tx_hash` | Dest refund tx |
| `result.destination_swap.htlc_address` | HTLC contract/program address |

---

## Chain Name Mapping

The API returns chain names that differ from internal names used by specialists
and on-chain agents.

| API chain name | Internal chain name |
|---|---|
| `bitcoin` | `bitcoin` |
| `ethereum` | `evm` |
| `arbitrum` | `evm` |
| `optimism` | `evm` |
| `base` | `evm` |
| `polygon` | `evm` |
| `avalanche` | `evm` |
| `solana` | `solana` |
| `spark` | `spark` |

---

## State Classification Logic

```
if source_swap.initiate_tx_hash == "":
    → No User Init (hard early return before state assignment)

elif destination_swap.initiate_tx_hash == ""
     AND destination_swap.redeem_tx_hash == ""
     AND destination_swap.refund_tx_hash == "":
    → DestInitPending

elif destination_swap.initiate_tx_hash != ""
     AND destination_swap.redeem_tx_hash == ""
     AND destination_swap.refund_tx_hash == "":
    → UserRedeemPending

elif destination_swap.redeem_tx_hash != ""
     AND source_swap.redeem_tx_hash == ""
     AND source_swap.refund_tx_hash == "":
    → SolverRedeemPending

else:
    → Unknown (completed, double-refunded, or unexpected state)
```

---

## Early Return Checks

### Pre-state check
| Condition | Early return message |
|---|---|
| `source_swap.initiate_tx_hash == ""` | `"No User Init found for this order."` |

### DestInitPending — checked in this order

| # | Condition | Early return message |
|---|---|---|
| 1 | `additional_data.is_blacklisted == true` | `"Order is blacklisted; solver will not initiate on destination."` |
| 2 | `abs(filled_amount - amount) / amount * 100 > filled_amount_tolerance_pct` | `"Filled amount ({filled}) differs from expected ({amount}) by {pct}% — exceeds tolerance."` |
| 3 | Solver liquidity via `liquidity_url` < `destination_amount` (filtered by `solver_id`) | `"Please fund {shortage} more of {asset} on {dest_chain} for solver {solver_id}"` |
| 4 | `initiate_timestamp > additional_data.deadline` (UNIX) | `"Source initiate timestamp is past the solver deadline; solver will not initiate."` |

If none of the above fire → escalate to LLM pipeline (DestInitPending specialist focus: executor on destination chain).

### UserRedeemPending

On-chain checks are delegated to the **destination chain on-chain agent** (`_ONCHAIN_AGENTS[dst_chain]`).

| # | Condition | Early return message |
|---|---|---|
| 1 | Native balance of `RELAYER_ADDRESS_{DST_CHAIN}` < `min_gas_balance(dst_chain)` | `"Please fund relayer {address} with native token on {dest_chain}."` |
| 2 | HTLC at `destination_swap.htlc_address` is already redeemed on-chain | `"Watcher failed to update state. Destination redeem already happened on-chain."` |

The on-chain agent is asked to prefix its response with `BALANCE_INSUFFICIENT` / `BALANCE_OK`
or `HTLC_REDEEMED` / `HTLC_PENDING` so the orchestrator can parse deterministically.

If none fire → escalate to LLM pipeline (UserRedeemPending focus: relayer on destination chain).

### SolverRedeemPending

On-chain checks are delegated to the **source chain on-chain agent** (`_ONCHAIN_AGENTS[src_chain]`).

| # | Condition | Early return message |
|---|---|---|
| 1 | Native balance of `EXECUTOR_ADDRESS_{SRC_CHAIN}` < `min_gas_balance(src_chain)` | `"Please fund executor {address} with native token on {source_chain}."` |
| 2 | HTLC at `source_swap.htlc_address` is already redeemed on-chain | `"Please check watcher RPCs; source redeem already happened on-chain."` |

If none fire → escalate to LLM pipeline (SolverRedeemPending focus: executor on source chain).

---

## Full LLM Pipeline (fallback)

When early returns don't catch the issue, the orchestrator builds an `Alert` from order data
and calls `run(alert)` which runs:

1. **Log Intelligence Agent** (Haiku) — queries Loki for logs around the order's created_at
2. **On-Chain Agent** (Haiku) — queries live chain state for RCA context
3. **Chain Specialist** (Opus, with prompt caching) — performs root cause analysis with source code access
4. Assembles and returns a full `RCAReport`

### Alert construction from order state

| Stuck state | chain | service | alert_type |
|---|---|---|---|
| DestInitPending | destination chain | executor | missed_init |
| UserRedeemPending | destination chain | relayer | stuck_order |
| SolverRedeemPending | source chain | executor | stuck_order |

---

## Config Fields (config.py / .env)

| Field | Default | Description |
|---|---|---|
| `filled_amount_tolerance_pct` | `5.0` | Max % deviation in filled vs expected amount |
| `liquidity_url` | `""` | URL returning all solvers' available balances |
| `relayer_address_bitcoin` | `""` | Bitcoin relayer wallet address |
| `relayer_address_evm` | `""` | EVM relayer wallet address |
| `relayer_address_solana` | `""` | Solana relayer wallet address |
| `relayer_address_spark` | `""` | Spark relayer wallet address |
| `executor_address_bitcoin` | `""` | Bitcoin executor wallet address |
| `executor_address_evm` | `""` | EVM executor wallet address |
| `executor_address_solana` | `""` | Solana executor wallet address |
| `executor_address_spark` | `""` | Spark executor wallet address |
| `min_evm_gas_balance` | `10_000_000_000_000_000` | 0.01 ETH in wei |
| `min_solana_gas_balance` | `10_000_000` | 0.01 SOL in lamports |
| `min_bitcoin_gas_balance` | `10_000` | 10k satoshis |
| `min_spark_gas_balance` | `10_000_000_000_000_000` | 0.01 SPARK in wei |

---

## Token Optimization

| Stage | LLM tokens used |
|---|---|
| State classification | **0** — pure Python |
| DestInitPending checks 1–4 | **0** — HTTP/config checks |
| UserRedeemPending/SolverRedeemPending check 1 (balance) | **low** — Haiku on-chain agent, single targeted question |
| UserRedeemPending/SolverRedeemPending check 2 (HTLC) | **low** — Haiku on-chain agent, single targeted question |
| Full LLM pipeline (log + specialist) | **high** — only when all early returns pass |

The goal is to resolve the majority of stuck orders with zero or very low LLM token cost.
