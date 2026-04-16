"""
Tron on-chain query agent using raw JSON-RPC via httpx.

Tron's full-node exposes an eth_*-compatible JSON-RPC endpoint, so we
use the same method names as EVM (eth_getBalance, eth_call, eth_getLogs,
etc.) but call them over plain HTTP with httpx — web3.py is NOT used
because Tron is not fully web3-compatible.

Address note: Tron addresses are displayed in Base58 format (T...) but
the JSON-RPC layer expects hex addresses prefixed with 0x41.  Callers
should pass whichever format they have; this agent does NOT auto-convert
(conversion requires the `base58` library which may not be installed).

The HTLC contract is GardenHTLCv3 (TRC20).
  - getOrder selector: 0x9c3f1e90
  - Returns a 192-byte (6 × 32) struct:
      (initiator, redeemer, initiatedAt, timelock, amount, fulfilledAt)
"""
import json

import httpx

from config import settings
from agents.onchain.base import BaseOnChainAgent
from tools.garden_api import lookup_asset

# ── JSON-RPC helper ──────────────────────────────────────────────────────────

_RPC_ID = 0


def _rpc(method: str, params: list | None = None) -> dict:
    """
    Send a JSON-RPC 2.0 request to the configured Tron RPC endpoint.
    Returns the 'result' field or raises on error.
    """
    global _RPC_ID
    _RPC_ID += 1
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or [],
        "id": _RPC_ID,
    }
    resp = httpx.post(settings.tron_rpc_url, json=payload, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        err = body["error"]
        raise RuntimeError(f"RPC error {err.get('code')}: {err.get('message')}")
    return body.get("result")


# ── Selectors / ABI constants ────────────────────────────────────────────────

# GardenHTLCv3.getOrder(bytes32) → 0x9c3f1e90
_GET_ORDER_SELECTOR = "0x9c3f1e90"

# ERC20.balanceOf(address) → 0x70a08231
_BALANCE_OF_SELECTOR = "0x70a08231"


# ── Agent ────────────────────────────────────────────────────────────────────


class TronOnChainAgent(BaseOnChainAgent):

    @property
    def chain(self) -> str:
        return "tron"

    @property
    def system_prompt(self) -> str:
        return """\
You are a Tron on-chain query agent for the Garden bridge system.
The RPC endpoint speaks eth_*-compatible JSON-RPC.

Tron address formats:
  - Base58 (user-facing): starts with T, e.g. TJYs…
  - Hex (RPC-level): starts with 0x41, e.g. 0x41abc…
  When passing addresses to tools, use whichever format you have — the \
tools accept both but the RPC returns hex.

Native currency: TRX.  1 TRX = 1,000,000 SUN.

HTLC contract: GardenHTLCv3 (TRC20).
  getOrder(bytes32) returns a 192-byte struct:
    slot 0 – initiator  (address)
    slot 1 – redeemer   (address)
    slot 2 – initiatedAt (uint256)
    slot 3 – timelock    (uint256)
    slot 4 – amount      (uint256)
    slot 5 – fulfilledAt (uint256)

Tool usage guide:
- BALANCE check (native TRX)          → get_native_balance
  Start response with BALANCE_INSUFFICIENT or BALANCE_OK.
- BALANCE check (TRC20 tokens)        → get_trc20_balance with asset_id
  Start response with BALANCE_INSUFFICIENT or BALANCE_OK.
- HTLC redeemed check                 → get_htlc_order_state
  fulfilledAt != 0 → HTLC_REDEEMED.  Else → HTLC_PENDING.
- Transaction status                  → get_transaction, then get_transaction_receipt
  receipt status=1 success, status=0 reverted.
- Event search                        → get_logs
- Current block height                → get_block_number

Always start with the required keyword when asked for one.\
"""

    @property
    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "get_native_balance",
                "description": (
                    "Get the native TRX balance of a Tron address via eth_getBalance. "
                    "Returns balance in SUN (1 TRX = 1,000,000 SUN). "
                    "USE THIS for native-asset balance checks (BALANCE_INSUFFICIENT / BALANCE_OK). "
                    "For TRC20 tokens use get_trc20_balance."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {
                            "type": "string",
                            "description": "Tron address (Base58 T... or hex 0x41...)",
                        },
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_trc20_balance",
                "description": (
                    "Get the TRC20 token balance of a Tron address for a specific Garden asset. "
                    "Resolves token contract address and decimals from the Garden Finance API. "
                    "USE THIS for TRC20 balance checks. asset_id example: 'tron:usdt'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "wallet_address": {
                            "type": "string",
                            "description": "Tron address to check (Base58 or hex)",
                        },
                        "asset_id": {
                            "type": "string",
                            "description": "Garden asset ID, e.g. 'tron:usdt'",
                        },
                    },
                    "required": ["wallet_address", "asset_id"],
                },
            },
            {
                "name": "get_transaction",
                "description": (
                    "Get a Tron transaction by hash via eth_getTransactionByHash. "
                    "blockNumber=null means pending/dropped. "
                    "USE THIS to check if an initiation tx was sent and mined."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tx_hash": {
                            "type": "string",
                            "description": "Transaction hash (0x...)",
                        },
                    },
                    "required": ["tx_hash"],
                },
            },
            {
                "name": "get_transaction_receipt",
                "description": (
                    "Get the receipt of a mined Tron transaction. "
                    "status=1 success, status=0 reverted. "
                    "USE THIS to check success/revert after confirming tx is mined."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tx_hash": {
                            "type": "string",
                            "description": "Transaction hash (0x...)",
                        },
                    },
                    "required": ["tx_hash"],
                },
            },
            {
                "name": "get_htlc_order_state",
                "description": (
                    "Read GardenHTLCv3 order state from the on-chain contract via eth_call. "
                    "Uses selector 0x9c3f1e90 (getOrder). "
                    "fulfilledAt != 0 → HTLC_REDEEMED. "
                    "fulfilledAt == 0 and initiatedAt != 0 → HTLC_PENDING. "
                    "initiatedAt == 0 → not initiated."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "contract_address": {
                            "type": "string",
                            "description": "HTLC contract address (Base58 or hex)",
                        },
                        "order_id": {
                            "type": "string",
                            "description": "Order ID as hex bytes32 (0x...)",
                        },
                    },
                    "required": ["contract_address", "order_id"],
                },
            },
            {
                "name": "get_logs",
                "description": (
                    "Fetch event logs from a Tron contract via eth_getLogs. "
                    "USE THIS to search for HTLC Redeemed/Initiated events."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "contract_address": {
                            "type": "string",
                            "description": "Contract address (Base58 or hex)",
                        },
                        "from_block": {
                            "type": "integer",
                            "description": "Start block (inclusive)",
                        },
                        "to_block": {
                            "type": "integer",
                            "description": "End block. Omit for latest.",
                        },
                        "topics": {
                            "type": "array",
                            "description": "Topic filters (hex strings). topics[0] is event signature hash.",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["contract_address", "from_block"],
                },
            },
            {
                "name": "get_block_number",
                "description": "Get the current Tron block number via eth_blockNumber.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    # ── Tool execution ───────────────────────────────────────────────────────

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if not settings.tron_rpc_url:
            return "[TRON_RPC_URL not configured]"

        try:
            if tool_name == "get_native_balance":
                address = tool_input["address"]
                result = _rpc("eth_getBalance", [address, "latest"])
                balance_sun = int(result, 16)
                balance_trx = balance_sun / 1_000_000
                return (
                    f"Balance: {balance_sun} SUN ({balance_trx:.6f} TRX) "
                    f"for address {address}"
                )

            elif tool_name == "get_trc20_balance":
                asset = lookup_asset(tool_input["asset_id"])
                if asset is None:
                    return f"[Asset not found] '{tool_input['asset_id']}' not in Garden chain registry."
                if asset["is_native"] or not asset["token_address"]:
                    # Native TRX — fall back to eth_getBalance
                    result = _rpc("eth_getBalance", [tool_input["wallet_address"], "latest"])
                    balance_sun = int(result, 16)
                    balance_trx = balance_sun / 1_000_000
                    return (
                        f"Native asset (TRX). Balance: {balance_sun} SUN ({balance_trx:.6f} TRX)"
                    )
                # TRC20 balanceOf(address) — ABI-encode: selector + address padded to 32 bytes
                wallet = tool_input["wallet_address"]
                # Strip 0x prefix if present, then zero-pad to 64 hex chars (32 bytes)
                addr_hex = wallet.lower().removeprefix("0x").zfill(64)
                call_data = _BALANCE_OF_SELECTOR + addr_hex

                result = _rpc("eth_call", [
                    {"to": asset["token_address"], "data": call_data},
                    "latest",
                ])
                raw_balance = int(result, 16) if result and result != "0x" else 0
                decimals = asset["decimals"]
                human = raw_balance / (10 ** decimals)
                return (
                    f"Token: {tool_input['asset_id']} ({asset['token_address']}), "
                    f"decimals: {decimals}. "
                    f"Balance: {raw_balance} raw ({human:.{decimals}f} tokens)"
                )

            elif tool_name == "get_transaction":
                tx = _rpc("eth_getTransactionByHash", [tool_input["tx_hash"]])
                if tx is None:
                    return "Transaction not found (may be pending, dropped, or invalid hash)."
                slim = {
                    "hash":        tx.get("hash"),
                    "from":        tx.get("from"),
                    "to":          tx.get("to"),
                    "nonce":       tx.get("nonce"),
                    "value":       tx.get("value"),
                    "gas":         tx.get("gas"),
                    "gasPrice":    tx.get("gasPrice"),
                    "blockNumber": tx.get("blockNumber"),
                }
                return json.dumps(slim, default=str)

            elif tool_name == "get_transaction_receipt":
                receipt = _rpc("eth_getTransactionReceipt", [tool_input["tx_hash"]])
                if receipt is None:
                    return "Transaction not yet mined (receipt is null)."
                slim = {
                    "status":            receipt.get("status"),
                    "blockNumber":       receipt.get("blockNumber"),
                    "gasUsed":           receipt.get("gasUsed"),
                    "effectiveGasPrice": receipt.get("effectiveGasPrice"),
                    "logs_count":        len(receipt.get("logs", [])),
                }
                return json.dumps(slim, default=str)

            elif tool_name == "get_htlc_order_state":
                order_id = tool_input["order_id"]
                # Ensure order_id is a 64-char hex string (32 bytes) without 0x prefix
                order_hex = order_id.removeprefix("0x").zfill(64)
                call_data = _GET_ORDER_SELECTOR + order_hex

                result = _rpc("eth_call", [
                    {"to": tool_input["contract_address"], "data": call_data},
                    "latest",
                ])

                # Response is 192 bytes (384 hex chars + "0x" prefix):
                #   slot 0: initiator   (address, right-padded in 32 bytes)
                #   slot 1: redeemer    (address)
                #   slot 2: initiatedAt (uint256)
                #   slot 3: timelock    (uint256)
                #   slot 4: amount      (uint256)
                #   slot 5: fulfilledAt (uint256)
                raw = result.removeprefix("0x") if result else ""
                if len(raw) < 384:
                    return f"[Unexpected response length: got {len(raw)} hex chars, expected 384]"

                # Each slot is 64 hex characters (32 bytes)
                initiator    = "0x" + raw[0:64][-40:]    # last 20 bytes of slot 0
                redeemer     = "0x" + raw[64:128][-40:]  # last 20 bytes of slot 1
                initiated_at = int(raw[128:192], 16)
                timelock     = int(raw[192:256], 16)
                amount       = int(raw[256:320], 16)
                fulfilled_at = int(raw[320:384], 16)

                if initiated_at == 0:
                    state = "not_initiated"
                elif fulfilled_at != 0:
                    state = "redeemed"
                else:
                    state = "pending"

                return (
                    f"HTLC order state on tron: {state}. "
                    f"initiator={initiator}, redeemer={redeemer}, "
                    f"initiatedAt={initiated_at}, fulfilledAt={fulfilled_at}, "
                    f"amount={amount}, timelock={timelock}."
                )

            elif tool_name == "get_logs":
                filter_params: dict = {
                    "address":   tool_input["contract_address"],
                    "fromBlock": hex(tool_input["from_block"]),
                    "toBlock":   hex(tool_input["to_block"]) if tool_input.get("to_block") else "latest",
                }
                if tool_input.get("topics"):
                    filter_params["topics"] = tool_input["topics"]
                logs = _rpc("eth_getLogs", [filter_params])
                if not logs:
                    return "No logs found for the given filter on tron."
                slim = [
                    {
                        "blockNumber":     log.get("blockNumber"),
                        "transactionHash": log.get("transactionHash"),
                        "topics":          log.get("topics", []),
                        "data":            (log.get("data") or "")[:66],  # first 32 bytes + 0x
                    }
                    for log in logs[:20]
                ]
                return json.dumps(slim, default=str)

            elif tool_name == "get_block_number":
                result = _rpc("eth_blockNumber")
                block_num = int(result, 16)
                return f"Current Tron block number: {block_num} (hex: {result})"

        except Exception as e:
            return f"[RPC error on tron: {type(e).__name__}: {e}]"

        return f"[Unknown tool: {tool_name}]"
