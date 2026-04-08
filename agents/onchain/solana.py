"""Solana on-chain query agent using the Solana JSON-RPC API."""
import httpx
import json
from config import settings
from agents.onchain.base import BaseOnChainAgent


def _rpc(method: str, params: list, timeout: int = 15) -> dict:
    """Make a Solana JSON-RPC call."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        resp = httpx.post(settings.solana_rpc_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            return {"error": data["error"]}
        return data.get("result", {})
    except Exception as e:
        return {"error": str(e)}


class SolanaOnChainAgent(BaseOnChainAgent):

    @property
    def chain(self) -> str:
        return "solana"

    @property
    def system_prompt(self) -> str:
        return """\
You are a Solana on-chain query agent for the Garden bridge system.

Tool usage guide:
- BALANCE check (BALANCE_INSUFFICIENT/BALANCE_OK): use get_account on the wallet address. \
'lamports' is the balance (1 SOL = 1_000_000_000 lamports). \
Start response with BALANCE_INSUFFICIENT or BALANCE_OK.
- HTLC redeemed check (HTLC_REDEEMED/HTLC_PENDING): use get_account on the HTLC PDA address. \
If the account is null/doesn't exist → HTLC settled (HTLC_REDEEMED). \
If the account exists with data → HTLC still active (HTLC_PENDING). \
Also check get_recent_txs for redeem/refund signatures.
- Transaction status: use get_transaction by signature. Check 'err' field — null = success, non-null = failed.
- Blockhash expiry: use get_recent_blockhash and compare lastValidBlockHeight to current slot from get_slot. \
Blockhash is valid for ~150 slots (~60 seconds).
- Recent activity: use get_recent_txs with limit=20 to reconstruct HTLC lifecycle.

Always start with the required keyword (BALANCE_INSUFFICIENT, BALANCE_OK, HTLC_REDEEMED, HTLC_PENDING) when asked.\
"""

    @property
    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "get_account",
                "description": (
                    "Get Solana account info for an address. Returns lamports (balance) and owner program. "
                    "USE FOR BALANCE: 'lamports' field is the SOL balance (1 SOL = 1e9 lamports). "
                    "USE FOR HTLC: if result is null or 'value' is null, the account doesn't exist — "
                    "HTLC has been settled/closed. If account exists with data, HTLC is still active."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "Solana public key (base58)"},
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_transaction",
                "description": (
                    "Get a Solana transaction by signature. "
                    "Check 'err' field: null = success, non-null = failed/reverted. "
                    "USE THIS to verify initiation, redemption, or refund transactions."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "signature": {"type": "string", "description": "Transaction signature (base58)"},
                    },
                    "required": ["signature"],
                },
            },
            {
                "name": "get_recent_txs",
                "description": (
                    "Get recent transaction signatures for a Solana address. "
                    "Set limit=20 for HTLC lifecycle reconstruction. "
                    "Each entry has 'err' field — non-null means the tx failed. "
                    "USE THIS to find redeem/refund signatures on HTLC PDA."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "Solana public key"},
                        "limit": {
                            "type": "integer",
                            "description": "Max signatures to return (default 10, use 20 for HTLC reconstruction)",
                            "default": 10,
                        },
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_slot",
                "description": (
                    "Get current Solana slot. "
                    "USE THIS to check blockhash expiry: blockhash valid for ~150 slots (~60s). "
                    "Compare against lastValidBlockHeight from get_recent_blockhash."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_recent_blockhash",
                "description": (
                    "Get the latest finalized blockhash and its lastValidBlockHeight. "
                    "USE THIS to diagnose blockhash expiry: a submitted tx is invalid if current slot > lastValidBlockHeight. "
                    "The valid window is ~150 slots (~60 seconds) from blockhash creation."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "get_account":
            result = _rpc("getAccountInfo", [tool_input["address"], {"encoding": "base64"}])
            return json.dumps(result, default=str)

        elif tool_name == "get_transaction":
            result = _rpc("getTransaction", [
                tool_input["signature"],
                {"encoding": "json", "maxSupportedTransactionVersion": 0},
            ])
            if not result:
                return "Transaction not found (may not be confirmed yet)"
            slim = {
                "slot":        result.get("slot"),
                "blockTime":   result.get("blockTime"),
                "err":         result.get("meta", {}).get("err"),
                "fee":         result.get("meta", {}).get("fee"),
                "logMessages": (result.get("meta", {}).get("logMessages") or [])[:10],
            }
            return json.dumps(slim, default=str)

        elif tool_name == "get_recent_txs":
            result = _rpc("getSignaturesForAddress", [
                tool_input["address"],
                {"limit": tool_input.get("limit", 10)},
            ])
            if isinstance(result, list):
                lines = [
                    f"{r.get('signature', '?')} — slot {r.get('slot')} — err={r.get('err')}"
                    for r in result
                ]
                return "\n".join(lines) if lines else "No recent transactions"
            return json.dumps(result, default=str)

        elif tool_name == "get_slot":
            slot = _rpc("getSlot", [])
            return f"Current slot: {slot}"

        elif tool_name == "get_recent_blockhash":
            result = _rpc("getLatestBlockhash", [{"commitment": "finalized"}])
            if isinstance(result, dict) and "error" in result:
                return f"[RPC error: {result['error']}]"
            value = result.get("value", result)
            blockhash = value.get("blockhash", "N/A")
            last_valid = value.get("lastValidBlockHeight", "N/A")
            slot = _rpc("getSlot", [])
            return (
                f"Latest finalized blockhash: {blockhash}, "
                f"lastValidBlockHeight: {last_valid}, "
                f"current slot: {slot}."
            )

        return f"[Unknown tool: {tool_name}]"
